"""Loopback-only dashboard with same-origin, token-protected refresh and updates."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .model import freshness

WEB = Path(__file__).with_name("web")


class Busy(Exception):
    pass


class State:
    def __init__(self, loader, source, updates=None):
        """updates: an object with run(site, kind, slug, restore_database), server_state() and
        set_nightly(site, enabled) (see transport.SshUpdates), or None for a read-only dashboard."""
        self.loader, self.source, self.updates = loader, source, updates
        self.data = None
        self.error = None
        self.history = []
        self.nightly = None  # None: not read yet or unreadable, shown as unknown
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.update_lock = threading.Lock()

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.data = self.loader()
            self.error = None
        except (ValueError, OSError, TypeError, KeyError):
            self.error = "Cannot read a valid snapshot. Check the status file or SSH connection."
        finally:
            self.lock.release()
        if self.updates:
            try:
                self.history, self.nightly = self.updates.server_state()
            except ValueError:
                pass

    def payload(self):
        return {"status": self.data, "error": self.error, "source": self.source,
                "freshness": freshness(self.data) if self.data else None,
                "refresh_token": self.token, "refreshing": self.lock.locked(),
                "capabilities": {"updates": bool(self.updates) and not (self.data or {}).get("demo")},
                "update_running": self.update_lock.locked(), "history": self.history[:20], "nightly": self.nightly}

    def update(self, request):
        """Run one update the current snapshot reports; the server checks again before acting."""
        if not self.payload()["capabilities"]["updates"] or not self.data:
            raise ValueError("Updates are not enabled for this dashboard, or no snapshot is loaded")
        if not isinstance(request, dict) or set(request) - {"site", "kind", "slug", "restore_database"}:
            raise ValueError("Invalid update request")
        site_id, kind, slug = request.get("site"), request.get("kind"), request.get("slug")
        restore_database = request.get("restore_database", True)
        if type(restore_database) is not bool:
            raise ValueError("Invalid rollback choice")
        site = next((s for s in self.data["sites"] if s["id"] == site_id and s["kind"] == "wordpress"), None)
        if kind == "app":
            app = next((s.get("app") for s in self.data["sites"] if s["id"] == site_id and s["kind"] == "web"), None) or {}
            offered = app.get("update") or {}
            if offered.get("sha") != slug or not restore_database:
                raise ValueError("The current snapshot offers no such commit")
            if offered.get("gate") != "passed":
                raise ValueError("The security gate has not passed on this commit")
            item = {"update_version": slug}
        elif kind == "core":
            item = (site or {}).get("core") if slug is None else None
        elif kind in ("plugin", "theme"):
            item = next((i for i in (site or {}).get(f"{kind}s", []) if i.get("name") == slug), None)
        else:
            item = None
        if not item or not item.get("update_version"):
            raise ValueError("The current snapshot reports no such update")
        if not self.update_lock.acquire(blocking=False):
            raise Busy("Another update is running")
        try:
            results = self.updates.run(site_id, kind, slug, restore_database)
        finally:
            self.update_lock.release()
        self.refresh()
        return results

    def set_nightly(self, request):
        """Switch nightly automatic updates for one WordPress site in the current snapshot."""
        if not self.payload()["capabilities"]["updates"] or not self.data:
            raise ValueError("Updates are not enabled for this dashboard, or no snapshot is loaded")
        if not isinstance(request, dict) or set(request) != {"site", "enabled"} or type(request["enabled"]) is not bool:
            raise ValueError("Invalid nightly request")
        if not any(s["id"] == request["site"] and s["kind"] == "wordpress" for s in self.data["sites"]):
            raise ValueError("Unknown WordPress site")
        setting = self.updates.set_nightly(request["site"], request["enabled"])
        if self.nightly is None:
            try:
                self.history, self.nightly = self.updates.server_state()
            except ValueError:
                pass
        if self.nightly is not None:
            self.nightly = {**self.nightly, request["site"]: setting}
        return setting


def make_server(state, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body, content_type="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403, {"error": "Invalid host"})
            route = urlsplit(self.path).path
            if route == "/api/status":
                return self.reply(200, state.payload())
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if route in assets:
                name, kind = assets[route]
                return self.reply(200, (WEB / name).read_bytes(), kind)
            return self.reply(404, {"error": "Not found"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.allowed_host() or self.headers.get("Origin") != origin or not secrets.compare_digest(self.headers.get("X-Site-Watch-Token", ""), state.token):
                return self.reply(403, {"error": "Invalid origin or token"})
            if self.headers.get("Transfer-Encoding"):
                return self.reply(400, {"error": "Chunked requests are not accepted"})
            if self.path in ("/api/update", "/api/nightly"):
                return self.update()
            if self.path != "/api/refresh":
                return self.reply(404, {"error": "Not found"})
            if self.headers.get("Content-Length", "0") != "0":
                return self.reply(400, {"error": "No request body accepted"})
            state.refresh()
            return self.reply(200, state.payload())

        def update(self):
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Content-Type") != "application/json" or not length.isdigit() or not 0 < int(length) <= 1024:
                return self.reply(400, {"error": "Send a small JSON body"})
            try:
                body = json.loads(self.rfile.read(int(length)))
                if self.path == "/api/nightly":
                    return self.reply(200, {"nightly_setting": state.set_nightly(body), **state.payload()})
                results = state.update(body)
            except Busy as exc:
                return self.reply(409, {"error": str(exc)})
            except ValueError as exc:
                return self.reply(400, {"error": str(exc), **state.payload()})
            return self.reply(200, {"results": results, **state.payload()})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
