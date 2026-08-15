from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from urllib.error import HTTPError
from urllib.request import urlopen

from anytex.browser import BrowserCompanion, _Event, _EventBus
from anytex.cli import _wait_for_browser_url, build_parser


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

        self.assertIn("AnyTeX Browser Companion", body)
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

    def test_cli_accepts_browser_options(self) -> None:
        args = build_parser().parse_args(
            ["--browser", "--browser-port", "8123", "--", "codex"]
        )

        self.assertTrue(args.browser)
        self.assertEqual(8123, args.browser_port)

    def test_claude_launch_waits_after_showing_a_copy_url_note(self) -> None:
        delays = []
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            _wait_for_browser_url("anthropic", seconds=10, sleeper=delays.append)

        self.assertEqual(delays, [10])
        self.assertIn("Claude starts in 10 seconds", stderr.getvalue())
        self.assertIn("copy or open the browser companion URL", stderr.getvalue())

    def test_codex_launch_has_no_browser_url_delay(self) -> None:
        delays = []

        _wait_for_browser_url("openai", seconds=10, sleeper=delays.append)

        self.assertEqual(delays, [])

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
