from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from simultex.browser import BrowserCompanion, _Event, _EventBus
from simultex.cli import _show_browser_startup, build_parser


class EventBusTests(unittest.TestCase):
    def test_new_subscriber_receives_history(self) -> None:
        bus = _EventBus(history_limit=100)
        bus.publish(_Event("output", "YWJj", 3))

        history, client = bus.subscribe()

        self.assertEqual([(event.name, event.data) for event in history], [("output", "YWJj")])
        self.assertEqual([event.sequence for event in history], [1])
        bus.unsubscribe(client)

    def test_reconnecting_subscriber_only_receives_unseen_history(self) -> None:
        bus = _EventBus(history_limit=100)
        bus.publish(_Event("output", "YWFh", 3))
        bus.publish(_Event("output", "YmJi", 3))

        history, client = bus.subscribe(after_sequence=1)

        self.assertEqual([event.data for event in history], ["YmJi"])
        self.assertEqual([event.sequence for event in history], [2])
        bus.unsubscribe(client)

    def test_event_encoding_includes_sse_resume_id(self) -> None:
        event = _Event("output", "YWJj", 3, sequence=42)

        self.assertTrue(event.encode().startswith(b"id: 42\nevent: output\n"))

    def test_history_is_bounded(self) -> None:
        bus = _EventBus(history_limit=4)
        bus.publish(_Event("output", "YWFh", 3))
        bus.publish(_Event("output", "YmJi", 3))

        history, client = bus.subscribe()

        self.assertEqual([event.data for event in history], ["YmJi"])
        bus.unsubscribe(client)


class BrowserCompanionTests(unittest.TestCase):
    def test_serves_page_on_loopback_with_embedded_config(self) -> None:
        with BrowserCompanion(parse_dollars=False) as companion:
            with urlopen(companion.url, timeout=2) as response:
                body = response.read().decode("utf-8")

        self.assertIn("SimulTeX Browser Companion", body)
        self.assertIn("parseDollars", body)
        self.assertIn("false", body)
        self.assertIn('id="transcript"', body)
        self.assertIn('id="download-html"', body)
        self.assertNotIn("math-overlays", body)
        self.assertEqual("127.0.0.1", companion._server.server_address[0])

    def test_event_stream_rejects_wrong_token(self) -> None:
        with BrowserCompanion() as companion:
            url = f"http://127.0.0.1:{companion.port}/events?token=wrong"
            with self.assertRaises(HTTPError) as raised:
                urlopen(url, timeout=2)

        self.assertEqual(403, raised.exception.code)

    def test_serves_token_protected_images_from_the_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "plot.png"
            image.write_bytes(b"png-bytes")
            with BrowserCompanion(content_root=root) as companion:
                query = urlencode({"token": companion.token, "path": "plot.png"})
                url = f"http://127.0.0.1:{companion.port}/session-image?{query}"
                with urlopen(url, timeout=2) as response:
                    body = response.read()
                    content_type = response.headers["Content-Type"]

        self.assertEqual(body, b"png-bytes")
        self.assertEqual(content_type, "image/png")

    def test_session_images_cannot_escape_the_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "session"
            root.mkdir()
            (parent / "secret.png").write_bytes(b"secret")
            with BrowserCompanion(content_root=root) as companion:
                query = urlencode({"token": companion.token, "path": "../secret.png"})
                url = f"http://127.0.0.1:{companion.port}/session-image?{query}"
                with self.assertRaises(HTTPError) as raised:
                    urlopen(url, timeout=2)

        self.assertEqual(404, raised.exception.code)

    def test_page_allows_http_and_https_markdown_images(self) -> None:
        with BrowserCompanion() as companion:
            with urlopen(companion.url, timeout=2) as response:
                policy = response.headers["Content-Security-Policy"]

        self.assertIn("img-src 'self' data: blob: http: https:", policy)

    def test_cli_enables_browser_by_default_and_accepts_its_options(self) -> None:
        args = build_parser().parse_args(
            ["--browser-port", "8123", "--", "codex"]
        )

        self.assertTrue(args.browser)
        self.assertEqual(8123, args.browser_port)

    def test_browser_startup_shows_colored_logo_url_and_waits_for_enter(self) -> None:
        class TerminalBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        stdin = TerminalBuffer("\n")
        stderr = TerminalBuffer()

        _show_browser_startup(
            "http://127.0.0.1:8123/?token=secret",
            input_stream=stdin,
            output_stream=stderr,
        )

        output = stderr.getvalue()
        self.assertIn("\x1b[38;2;", output)
        self.assertIn("http://127.0.0.1:8123/?token=secret", output)
        self.assertIn("Copy or open the browser companion link", output)
        self.assertIn("press Enter to continue", output)
        self.assertEqual(stdin.tell(), 1)

    def test_browser_startup_does_not_block_or_color_noninteractive_output(self) -> None:
        stdin = io.StringIO("unread")
        stderr = io.StringIO()

        _show_browser_startup(
            "http://127.0.0.1:8123/",
            input_stream=stdin,
            output_stream=stderr,
        )

        self.assertEqual(stdin.tell(), 0)
        self.assertNotIn("\x1b[", stderr.getvalue())
        self.assertIn("stdin is not interactive", stderr.getvalue())

    def test_api_events_share_the_ordered_event_bus(self) -> None:
        with BrowserCompanion() as companion:
            companion.feed(b"before")
            companion.api_event(
                {"version": 1, "type": "turn.started", "turn_id": "turn-1"}
            )
            companion.feed(b"after")

            history, client = companion._events.subscribe()
            companion._events.unsubscribe(client)

        self.assertEqual([event.name for event in history], ["output", "api", "output"])
        self.assertEqual(json.loads(history[1].data)["turn_id"], "turn-1")
        self.assertEqual([event.sequence for event in history], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
