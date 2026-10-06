"""The dashboard's HTTP server: static files plus a small JSON API, on 127.0.0.1 only.

Requests addressed to any other host name are refused (that blocks DNS-rebinding pages),
and state-changing requests must be same-origin JSON.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from . import common as C
from .events import EVENT_TYPES
from .monitor import STATIC_FILES, Monitor

HOST = "127.0.0.1"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
MAX_BODY = 16_000


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already in use;
    # there the port is bound exclusively instead, so a taken port is reported as taken.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port: int, monitor: Monitor) -> None:
        self.monitor = monitor
        super().__init__((HOST, port), _Handler)

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"


def bind(monitor: Monitor, first_port: int, tries: int = C.PORT_TRIES) -> DashboardServer:
    """The configured port, or the next free one: another Cremind profile's monitor, or the
    standalone app, may already hold it."""
    last: OSError | None = None
    for port in range(first_port, first_port + tries):
        try:
            return DashboardServer(port, monitor)
        except OSError as e:
            last = e
    raise OSError(f"no free port in {first_port}-{first_port + tries - 1}: {last}")


def serve_in_background(server: DashboardServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="dashboard", daemon=True)
    thread.start()
    return thread


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: one request per connection, so a request refused before its body is read
    # can't leave that body behind on a kept-alive connection.
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's signature
        pass  # one line per poll would bury the monitor's own log

    # ------------------------------------------------------------ helpers

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        port = self.server.port
        return self.headers.get("Host", "") in (f"{HOST}:{port}", f"localhost:{port}")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid length") from None
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid JSON") from None

    # ------------------------------------------------------------ routes

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        path = unquote(urlsplit(self.path).path)
        monitor = self.server.monitor
        if path == "/api/state":
            return self._json(200, monitor.api_state())
        if path == "/api/health":
            return self._json(200, {"ok": True, "pid": os.getpid(), "port": self.server.port, "ready": monitor.ready})
        name = "index.html" if path == "/" else path.lstrip("/")
        if name not in STATIC_FILES:
            return self._json(404, {"error": "not found"})
        try:
            body = (C.DASHBOARD_DIR / name).read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        return self._send(200, body, MIME[os.path.splitext(name)[1]])

    def do_POST(self) -> None:  # noqa: N802
        self._change("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._change("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"error": "method not allowed"})

    def _change(self, method: str) -> None:
        """State-changing requests: same-origin JSON only."""
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self._json(403, {"error": "forbidden origin"})
        if not str(self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "expected JSON"})
        try:
            body = self._read_body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not isinstance(body, dict):
            return self._json(400, {"error": "expected a JSON object"})

        path = unquote(urlsplit(self.path).path)
        monitor = self.server.monitor
        if method == "POST" and path == "/api/settings":
            return self._json(200, {"ok": True, "settings": monitor.apply_settings(body)})
        if method == "POST" and path == "/api/test-alert":
            event_type = body.get("type") or "limit_warning"
            if event_type not in EVENT_TYPES:
                return self._json(400, {"error": f"unknown event type; one of {', '.join(EVENT_TYPES)}"})
            try:
                written = monitor.test_alert(event_type)
            except OSError as e:
                return self._json(500, {"ok": False, "error": str(e)})
            return self._json(200, {"ok": True, "type": event_type, "file": written.name})
        m = re.fullmatch(r"/api/profiles/([\w-]{1,32})/open", path)
        if method == "POST" and m:
            opened = monitor.open_profile(m.group(1))
            if opened is None:
                return self._json(404, {"error": "unknown profile"})
            return self._json(200 if opened else 501, {"ok": opened})
        m = re.fullmatch(r"/api/accounts/([\w-]{1,64})", path)
        if m:
            account_id = m.group(1)
            if method == "DELETE":
                ok = monitor.forget_account(account_id)
            elif isinstance(body.get("label"), str):
                ok = monitor.set_label(account_id, body["label"])
            else:
                return self._json(400, {"error": "expected a label"})
            return self._json(200 if ok else 404, {"ok": True} if ok else {"error": "unknown account"})
        return self._json(404, {"error": "not found"})
