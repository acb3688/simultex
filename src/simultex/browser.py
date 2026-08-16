"""Loopback-only browser companion for mirroring a terminal session."""

from __future__ import annotations

import base64
import html
import json
import secrets
import sys
import threading
from collections import deque
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Full, Queue
from urllib.parse import parse_qs, urlsplit


_ASSET_ROOT = Path(__file__).with_name("browser_assets")
_MAX_HISTORY_BYTES = 8 * 1024 * 1024
_CLIENT_QUEUE_SIZE = 512
_MAX_SESSION_IMAGE_BYTES = 64 * 1024 * 1024
_SESSION_IMAGE_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


@dataclass(frozen=True)
class _Event:
    name: str
    data: str
    size: int
    sequence: int = 0

    def encode(self) -> bytes:
        return (
            f"id: {self.sequence}\nevent: {self.name}\ndata: {self.data}\n\n"
        ).encode("utf-8")


class _EventBus:
    def __init__(self, history_limit: int = _MAX_HISTORY_BYTES):
        self._history_limit = history_limit
        self._history: deque[_Event] = deque()
        self._history_size = 0
        self._clients: set[Queue[_Event | None]] = set()
        self._lock = threading.Lock()
        self._next_sequence = 1

    def publish(self, event: _Event) -> None:
        with self._lock:
            event = replace(event, sequence=self._next_sequence)
            self._next_sequence += 1
            self._history.append(event)
            self._history_size += event.size
            while self._history and self._history_size > self._history_limit:
                removed = self._history.popleft()
                self._history_size -= removed.size
            stale: list[Queue[_Event | None]] = []
            for client in self._clients:
                try:
                    client.put_nowait(event)
                except Full:
                    stale.append(client)
            for client in stale:
                self._clients.discard(client)
                try:
                    client.get_nowait()
                    client.put_nowait(None)
                except (Empty, Full):
                    pass

    def subscribe(
        self, after_sequence: int = 0
    ) -> tuple[list[_Event], Queue[_Event | None]]:
        client: Queue[_Event | None] = Queue(maxsize=_CLIENT_QUEUE_SIZE)
        with self._lock:
            history = [event for event in self._history if event.sequence > after_sequence]
            self._clients.add(client)
        return history, client

    def unsubscribe(self, client: Queue[_Event | None]) -> None:
        with self._lock:
            self._clients.discard(client)

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.put_nowait(None)
            except Full:
                pass


class BrowserCompanion:
    """Serve a read-only rich transcript derived from raw PTY output."""

    def __init__(
        self,
        port: int = 0,
        *,
        parse_dollars: bool = True,
        content_root: Path | None = None,
    ):
        self.token = secrets.token_urlsafe(24)
        self.parse_dollars = parse_dollars
        self.content_root = (content_root or Path.cwd()).resolve()
        self._events = _EventBus()
        handler = self._handler_type()
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="simultex-browser",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def start(self) -> None:
        self._thread.start()

    def feed(self, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        self._events.publish(_Event("output", encoded, len(data)))

    def resize(self, columns: int, rows: int) -> None:
        payload = json.dumps({"columns": columns, "rows": rows}, separators=(",", ":"))
        self._events.publish(_Event("resize", payload, len(payload)))

    def api_event(self, payload: dict[str, object]) -> None:
        """Publish a normalized model event without changing PTY rendering."""
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._events.publish(_Event("api", encoded, len(encoded.encode("utf-8"))))

    def close(self) -> None:
        self._events.close()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> BrowserCompanion:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        companion = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "simultex-browser"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlsplit(self.path)
                if parsed.path == "/":
                    self._serve_index()
                    return
                if parsed.path == "/events":
                    self._serve_events(parsed.query)
                    return
                if parsed.path == "/session-image":
                    self._serve_session_image(parsed.query)
                    return
                self._serve_asset(parsed.path)

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _headers(self, content_type: str, length: int | None = None) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; connect-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                    "img-src 'self' data: blob: http: https:",
                )
                if length is not None:
                    self.send_header("Content-Length", str(length))

            def _serve_index(self) -> None:
                try:
                    template = (_ASSET_ROOT / "index.html").read_text(encoding="utf-8")
                except OSError:
                    self.send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "browser assets are missing; run npm install && npm run build in browser-ui",
                    )
                    return
                config = html.escape(
                    json.dumps({"parseDollars": companion.parse_dollars}),
                    quote=True,
                )
                body = template.replace("__SIMULTEX_CONFIG__", config).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._headers("text/html; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _serve_session_image(self, query: str) -> None:
                values = parse_qs(query)
                supplied = values.get("token", [""])[0]
                if not secrets.compare_digest(supplied, companion.token):
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                requested = values.get("path", [""])[0]
                if not requested:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                candidate = Path(requested)
                if not candidate.is_absolute():
                    candidate = companion.content_root / candidate
                try:
                    path = candidate.resolve(strict=True)
                    path.relative_to(companion.content_root)
                    size = path.stat().st_size
                except (OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = _SESSION_IMAGE_TYPES.get(path.suffix.lower())
                if not path.is_file() or content_type is None or size > _MAX_SESSION_IMAGE_BYTES:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = path.read_bytes()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self._headers(content_type, len(body))
                self.end_headers()
                self.wfile.write(body)

            def _serve_asset(self, request_path: str) -> None:
                name = request_path.removeprefix("/")
                if not name or Path(name).name != name:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                path = _ASSET_ROOT / name
                types = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".woff2": "font/woff2",
                    ".woff": "font/woff",
                    ".ttf": "font/ttf",
                }
                content_type = types.get(path.suffix)
                if content_type is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = path.read_bytes()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self._headers(content_type, len(body))
                self.end_headers()
                self.wfile.write(body)

            def _serve_events(self, query: str) -> None:
                supplied = parse_qs(query).get("token", [""])[0]
                if not secrets.compare_digest(supplied, companion.token):
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                origin = self.headers.get("Origin")
                expected = f"http://127.0.0.1:{companion.port}"
                if origin not in {None, expected}:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return

                try:
                    after_sequence = max(int(self.headers.get("Last-Event-ID", "0")), 0)
                except ValueError:
                    after_sequence = 0
                history, events = companion._events.subscribe(after_sequence)
                try:
                    self.send_response(HTTPStatus.OK)
                    self._headers("text/event-stream; charset=utf-8")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    for event in history:
                        self.wfile.write(event.encode())
                    self.wfile.flush()
                    while True:
                        try:
                            event = events.get(timeout=15)
                        except Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if event is None:
                            return
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    companion._events.unsubscribe(events)

        return Handler
