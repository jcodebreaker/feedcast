"""`feedcast serve`: a tiny local listener that serves the reports, their images and the setup page,
and records the page's feedback buttons.

Bound to 127.0.0.1 only. Requests whose Host or Origin isn't 127.0.0.1:<port> are refused, so another website
open in the same browser can't write feedback (CSRF) or read reports through DNS rebinding. The setup page's
actions also need a random per-server token that only the setup page carries (X-Feedcast-Token).
"""
from __future__ import annotations

import json
import os
import hmac
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
import hashlib
import signal
from pathlib import Path

from . import config, db, media, setup

# Changes whenever the server code or its pages change, so `ensure_server` can replace a server
# left running from an older version instead of handing the browser its stale pages.
_SRC = Path(__file__).resolve().parent
BUILD = hashlib.sha1(b"".join(f.read_bytes() for f in sorted([*_SRC.glob("*.py"), *_SRC.glob("templates/*")]))).hexdigest()[:12]
PING = b"feedcast " + BUILD.encode()

PORT = 8765
IDLE_S = 2 * 60 * 60
VERDICTS = {"not_relevant", "more_like_this"}
REPORT_PATH = re.compile(r"^/reports/(\d{4}-\d{2}-\d{2}|index)\.html$")
MEDIA_PATH = re.compile(r"^/media/([0-9a-f]{40}\.(" + "|".join(sorted(media.EXTS)) + r"))$")
MEDIA_TYPES = {v: k for k, v in media.TYPES.items()}
MAX_BODY = 4096


class Handler(BaseHTTPRequestHandler):
    server_version = "feedcast"

    def log_message(self, *args):  # quiet: the page is the interface
        pass

    def _origin(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        self.server.last = time.monotonic()
        if self.headers.get("Host") != self._origin():
            return self._send(403, b"forbidden")
        path = self.path.split("?", 1)[0]
        if path == "/ping":
            return self._send(200, PING)
        if path == "/":
            return self._send(302, extra={"Location": "/reports/index.html"})
        if path == "/setup":
            return self._send(200, setup.render_page(self.server.token).encode(),
                              "text/html; charset=utf-8")
        if path == "/api/jobs":
            return self._json(200, setup.job_state())
        if m := MEDIA_PATH.match(path):
            f = media.media_dir() / m.group(1)
            if not f.is_file():
                return self._send(404, b"not found")
            return self._send(200, f.read_bytes(), MEDIA_TYPES[m.group(2)], {"Cache-Control": "max-age=86400"})
        m = REPORT_PATH.match(path)
        if not m:
            return self._send(404, b"not found")
        f = config.reports_dir() / f"{m.group(1)}.html"
        if not f.is_file():
            return self._send(404, b"not found")
        if m.group(1) != "index":
            conn = db.connect()
            with conn:
                conn.execute("UPDATE reports SET opened_at = COALESCE(opened_at, ?) WHERE date = ?",
                             (datetime.now(timezone.utc).isoformat(), m.group(1)))
            conn.close()
        self._send(200, f.read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        self.server.last = time.monotonic()
        if self.headers.get("Host") != self._origin() or self.headers.get("Origin") != f"http://{self._origin()}":
            return self._json(403, {"error": "forbidden"})
        api = self.path.startswith("/api/")
        if self.path != "/feedback" and not api:
            return self._json(404, {"error": "not found"})
        if api and not hmac.compare_digest(self.headers.get("X-Feedcast-Token") or "", self.server.token):
            return self._json(403, {"error": "forbidden"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "json only"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._json(413, {"error": "bad length"})
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "bad json"})
        if api:
            code, out = setup.action(self.path[len("/api/"):], body)
            return self._json(code, out)
        key = body.get("key") if isinstance(body, dict) else None
        verdict = body.get("verdict") if isinstance(body, dict) else None
        if verdict not in VERDICTS or not isinstance(key, str):
            return self._json(400, {"error": "bad verdict"})
        conn = db.connect()
        try:
            if not conn.execute("SELECT 1 FROM posts WHERE key = ?", (key,)).fetchone():
                return self._json(400, {"error": "unknown post"})
            with conn:
                conn.execute("INSERT INTO feedback (post_key, verdict, at) VALUES (?,?,?)",
                             (key, verdict, datetime.now(timezone.utc).isoformat()))
        finally:
            conn.close()
        self._json(200, {"ok": True})


def make_server(port: int = PORT) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True  # one thread per request: an idle browser connection can't block the rest
    srv.last = time.monotonic()
    srv.token = secrets.token_urlsafe(24)
    return srv


def serve(srv: HTTPServer, idle_s: float = IDLE_S) -> None:
    srv.timeout = 30
    try:
        while time.monotonic() - srv.last < idle_s:
            srv.handle_request()
    finally:
        srv.server_close()


def _ping(port: int) -> bytes | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=0.5) as r:
            return r.read()
    except OSError:
        return None


def server_up(port: int = PORT) -> bool:
    return _ping(port) == PING


def stop_stale(port: int = PORT) -> None:
    """A Feedcast server from an older version is on the port: stop it so a current one can start."""
    got = _ping(port)
    if not got or got == PING or not got.startswith(b"feedcast"):
        return
    try:
        pid = int(config.path("serve.pid").read_text())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return
    for _ in range(20):
        if _ping(port) is None:
            return
        time.sleep(0.1)


def start_detached(port: int = PORT) -> None:
    """Start `feedcast serve` in its own session so it outlives the report job."""
    log = config.path("logs", "serve.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        subprocess.Popen(
            [sys.executable, "-m", "feedcast.cli", "serve", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, env=os.environ.copy(),
        )


def ensure_server(port: int = PORT, up=None, start=None) -> bool:
    """Start `feedcast serve` if it isn't running; True once it answers."""
    up, start = up or server_up, start or start_detached
    if up(port):
        return True
    if up is server_up:
        stop_stale(port)
    start(port)
    for _ in range(15):
        if up(port):
            return True
        time.sleep(0.2)
    return False


def cmd_serve(args) -> int:
    stop_stale(args.port)
    if server_up(args.port):
        print(f"Already running at http://127.0.0.1:{args.port}/")
        return 0
    try:
        srv = make_server(args.port)
    except OSError as e:
        print(f"Can't listen on 127.0.0.1:{args.port}: {e.strerror or e}", file=sys.stderr)
        return 1
    pid = config.path("serve.pid")
    pid.parent.mkdir(parents=True, exist_ok=True)
    pid.write_text(str(os.getpid()))
    print(f"Feedcast at http://127.0.0.1:{args.port}/ (stops after 2 h idle)", flush=True)
    try:
        serve(srv)
    except KeyboardInterrupt:
        pass
    return 0


def register(sub) -> None:
    p = sub.add_parser("serve", help="Serve the reports and the setup page, and save the page's feedback buttons.")
    p.add_argument("--port", type=int, default=PORT)
    p.set_defaults(func=cmd_serve)
