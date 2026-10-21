"""Loopback-only dashboard with same-origin, token-protected refresh."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .model import freshness

WEB = Path(__file__).with_name("web")


class State:
    def __init__(self, loader, source):
        self.loader, self.source = loader, source
        self.data = None
        self.error = None
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()

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

    def payload(self):
        return {"status": self.data, "error": self.error, "source": self.source,
                "freshness": freshness(self.data) if self.data else None,
                "refresh_token": self.token, "refreshing": self.lock.locked(),
                "capabilities": {"updates": False}}


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
            if self.path != "/api/refresh":
                return self.reply(404, {"error": "Not found"})
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                return self.reply(400, {"error": "No request body accepted"})
            state.refresh()
            return self.reply(200, state.payload())

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
