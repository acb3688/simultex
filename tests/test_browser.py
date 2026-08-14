from __future__ import annotations

import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from anytex.browser import BrowserCompanion, _Event, _EventBus
from anytex.cli import build_parser


class EventBusTests(unittest.TestCase):
    def test_new_subscriber_receives_history(self) -> None:
        bus = _EventBus(history_limit=100)
        bus.publish(_Event("output", "YWJj", 3))

        history, client = bus.subscribe()

        self.assertEqual([(event.name, event.data) for event in history], [("output", "YWJj")])
        bus.unsubscribe(client)

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


if __name__ == "__main__":
    unittest.main()
