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
        """updates: (run(site, kind, slug) -> results, history() -> log items), or None for read-only."""
        self.loader, self.source, self.updates = loader, source, updates
        self.data = None
        self.error = None
        self.history = []
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
                self.history = self.updates[1]()
            except ValueError:
                pass

    def payload(self):
        return {"status": self.data, "error": self.error, "source": self.source,
                "freshness": freshness(self.data) if self.data else None,
                "refresh_token": self.token, "refreshing": self.lock.locked(),
                "capabilities": {"updates": bool(self.updates) and not (self.data or {}).get("demo")},
                "update_running": self.update_lock.locked(), "history": self.history[:20]}

    def update(self, request):
        """Run one update the current snapshot reports; the server checks again before acting."""
        if not self.payload()["capabilities"]["updates"]:
            raise ValueError("Updates are not enabled for this dashboard")
        if not isinstance(request, dict) or set(request) - {"site", "kind", "slug"}:
            raise ValueError("Invalid update request")
        site_id, kind, slug = request.get("site"), request.get("kind"), request.get("slug")
        site = next((s for s in self.data["sites"] if s["id"] == site_id and s["kind"] == "wordpress"), None)
        if kind == "core":
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
            results = self.updates[0](site_id, kind, slug)
        finally:
            self.update_lock.release()
        self.refresh()
        return results


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
            if self.path == "/api/update":
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
                results = state.update(json.loads(self.rfile.read(int(length))))
            except Busy as exc:
                return self.reply(409, {"error": str(exc)})
            except ValueError as exc:
                return self.reply(400, {"error": str(exc), **state.payload()})
            return self.reply(200, {"results": results, **state.payload()})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
