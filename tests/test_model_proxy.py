from __future__ import annotations

import gzip
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from anytex.model_proxy import (
    ModelApiProxy,
    TranscriptCoordinator,
    _AnthropicObserver,
    _OpenAIObserver,
    _is_anthropic_auxiliary_request,
    _request_details,
    _strip_anthropic_system_messages,
    detect_provider,
    route_child,
    validate_upstream,
)


class EventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def api_event(self, payload) -> None:
        self.events.append(dict(payload))


class LaunchRoutingTests(unittest.TestCase):
    def test_detects_direct_codex_and_claude_commands(self) -> None:
        self.assertEqual(detect_provider(["/usr/local/bin/codex"]), "openai")
        self.assertEqual(detect_provider(["claude", "--resume"]), "anthropic")
        self.assertIsNone(detect_provider(["python", "tool.py"]))

    def test_claude_routing_is_scoped_to_child_environment(self) -> None:
        command, environ = route_child(
            ["claude", "--resume"],
            "anthropic",
            "http://127.0.0.1:9000/token",
            {"PATH": "/bin"},
        )

        self.assertEqual(command, ["claude", "--resume"])
        self.assertEqual(environ["PATH"], "/bin")
        self.assertEqual(
            environ["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9000/token"
        )

    def test_codex_routing_uses_one_run_config_overrides(self) -> None:
        command, environ = route_child(
            ["codex", "resume", "--last"],
            "openai",
            "http://127.0.0.1:9000/token",
            {"PATH": "/bin"},
        )

        self.assertEqual(command[0], "codex")
        self.assertIn('model_provider="anytex"', command)
        self.assertIn(
            'model_providers.anytex.base_url="http://127.0.0.1:9000/token"',
            command,
        )
        self.assertIn("model_providers.anytex.requires_openai_auth=true", command)
        self.assertIn("model_providers.anytex.supports_websockets=false", command)
        self.assertEqual(command[-2:], ["resume", "--last"])
        self.assertEqual(environ, {"PATH": "/bin"})

    def test_upstream_validation_requires_an_absolute_http_url(self) -> None:
        self.assertEqual(validate_upstream("https://example.test/v1/"), "https://example.test/v1")
        with self.assertRaises(ValueError):
            validate_upstream("file:///tmp/socket")


class RequestClassificationTests(unittest.TestCase):
    def test_openai_user_text_starts_a_turn(self) -> None:
        text, continuation = _request_details(
            "openai",
            {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Show $A^2$."}],
                    }
                ]
            },
        )
        self.assertEqual(text, "Show $A^2$.")
        self.assertFalse(continuation)

    def test_openai_function_output_continues_the_active_turn(self) -> None:
        self.assertEqual(
            _request_details(
                "openai",
                {"input": [{"type": "function_call_output", "output": "done"}]},
            ),
            (None, True),
        )

    def test_anthropic_tool_result_continues_the_active_turn(self) -> None:
        self.assertEqual(
            _request_details(
                "anthropic",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "content": "done"}],
                        }
                    ]
                },
            ),
            (None, True),
        )

    def test_anthropic_tool_result_recovers_the_original_user_message(self) -> None:
        self.assertEqual(
            _request_details(
                "anthropic",
                {
                    "messages": [
                        {"role": "user", "content": "Show a matrix"},
                        {"role": "assistant", "content": "Using a tool"},
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "content": "done"}],
                        },
                    ]
                },
            ),
            ("Show a matrix", True),
        )

    def test_anthropic_text_beside_an_interrupted_tool_starts_a_new_turn(self) -> None:
        self.assertEqual(
            _request_details(
                "anthropic",
                {
                    "messages": [
                        {"role": "user", "content": "Write this to a file"},
                        {"role": "assistant", "content": "Using a tool"},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "content": "The user interrupted this tool",
                                    "is_error": True,
                                },
                                {"type": "text", "text": "Just show it here instead"},
                            ],
                        },
                    ]
                },
            ),
            ("Just show it here instead", False),
        )

    def test_anthropic_system_context_beside_a_tool_remains_a_continuation(self) -> None:
        reminder = """<system-reminder>
Internal context
</system-reminder>"""

        self.assertEqual(
            _request_details(
                "anthropic",
                {
                    "messages": [
                        {"role": "user", "content": "Original prompt"},
                        {"role": "assistant", "content": "Using a tool"},
                        {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "content": "done"},
                                {"type": "text", "text": reminder},
                            ],
                        },
                    ]
                },
            ),
            ("Original prompt", True),
        )

    def test_anthropic_auxiliary_prompts_are_recognized(self) -> None:
        title_prompt = """<session>
User request
</session>

Write the title in the predominant language of the session"""

        self.assertTrue(_is_anthropic_auxiliary_request("quota"))
        self.assertTrue(_is_anthropic_auxiliary_request(title_prompt))
        self.assertTrue(
            _is_anthropic_auxiliary_request(
                "[SUGGESTION MODE: Suggest what the user might naturally type next "
                "into Claude Code.]\n\nReply with ONLY the suggestion."
            )
        )
        self.assertFalse(_is_anthropic_auxiliary_request("Explain session handling"))

    def test_anthropic_system_reminders_are_removed_from_user_text(self) -> None:
        source = """<system-reminder>
# currentDate
Today's date is 2026-08-15.
</system-reminder>

Yo"""

        self.assertEqual(_strip_anthropic_system_messages(source), "Yo")
        self.assertEqual(
            _request_details(
                "anthropic",
                {"messages": [{"role": "user", "content": source}]},
            ),
            ("Yo", False),
        )


class AdapterTests(unittest.TestCase):
    def test_openai_sse_is_chunk_safe_and_preserves_exact_tex(self) -> None:
        sink = EventSink()
        coordinator = TranscriptCoordinator(sink)
        context = coordinator.begin_call("openai", "matrix", False)
        observer = _OpenAIObserver(coordinator, context, True)
        frames = [
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "2&1\\\\\n",
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "0&3",
            },
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": "2&1\\\\\n0&3",
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
        stream = b"".join(
            b"data: " + json.dumps(frame).encode() + (b"\r\n\r\n" if index == 0 else b"\n\n")
            for index, frame in enumerate(frames)
        )

        for index in range(0, len(stream), 7):
            observer.feed(stream[index : index + 7])
        observer.finish()

        deltas = [event["delta"] for event in sink.events if event["type"] == "assistant.delta"]
        done = [event for event in sink.events if event["type"] == "assistant.part.done"]
        self.assertEqual(deltas, ["2&1\\\\\n", "0&3"])
        self.assertEqual(done[0]["markdown"], "2&1\\\\\n0&3")

    def test_anthropic_text_deltas_ignore_thinking_and_tool_json(self) -> None:
        sink = EventSink()
        coordinator = TranscriptCoordinator(sink)
        context = coordinator.begin_call("anthropic", "explain", False)
        observer = _AnthropicObserver(coordinator, context, True)
        frames = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Exact \\TeX"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "hidden"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        observer.feed(b"".join(
            f"event: item\ndata: {json.dumps(frame)}\n\n".encode() for frame in frames
        ))
        observer.finish()

        self.assertEqual(
            [event["delta"] for event in sink.events if event["type"] == "assistant.delta"],
            ["Exact \\TeX"],
        )
        self.assertEqual(
            [event["status"] for event in sink.events if event["type"] == "call.completed"],
            ["end_turn"],
        )


class _UpstreamHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_type = "text/event-stream"
    response_encoding: str | None = None
    response_chunks: list[bytes] = []
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", type(self).response_type)
        if type(self).response_encoding:
            self.send_header("Content-Encoding", type(self).response_encoding)
        self.send_header("X-Upstream-Test", "present")
        self.end_headers()
        for chunk in type(self).response_chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _UpstreamHandler.requests = []
        _UpstreamHandler.response_status = 200
        _UpstreamHandler.response_type = "text/event-stream"
        _UpstreamHandler.response_encoding = None
        _UpstreamHandler.response_chunks = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(timeout=2)

    @property
    def upstream_url(self) -> str:
        return f"http://127.0.0.1:{self.upstream.server_address[1]}"

    def test_stream_is_forwarded_exactly_and_emits_api_events(self) -> None:
        first = b'data: {"type":"response.output_text.delta","delta":"A\\\\\\\\B"}\n\n'
        second = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        _UpstreamHandler.response_chunks = [first[:11], first[11:], second]
        sink = EventSink()

        with ModelApiProxy("openai", sink, upstream=self.upstream_url) as proxy:
            request = Request(
                f"{proxy.url}/responses?beta=true",
                data=json.dumps({"input": "render a matrix", "stream": True}).encode(),
                headers={
                    "Authorization": "Bearer secret-value",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                body = response.read()
                upstream_header = response.headers["X-Upstream-Test"]

        self.assertEqual(body, first + second)
        self.assertEqual(upstream_header, "present")
        self.assertEqual(_UpstreamHandler.requests[0]["path"], "/responses?beta=true")
        self.assertEqual(
            _UpstreamHandler.requests[0]["authorization"], "Bearer secret-value"
        )
        self.assertEqual(
            [event["type"] for event in sink.events],
            [
                "turn.started",
                "call.started",
                "call.response",
                "assistant.delta",
                "call.completed",
                "turn.completed",
            ],
        )
        self.assertFalse(any("secret-value" in json.dumps(event) for event in sink.events))

    def test_compressed_stream_is_decoded_for_forwarding_and_observation(self) -> None:
        stream = (
            b'data: {"type":"response.output_text.delta","delta":"Exact \\\\TeX"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        )
        compressed = gzip.compress(stream)
        _UpstreamHandler.response_encoding = "gzip"
        _UpstreamHandler.response_chunks = [compressed[:13], compressed[13:]]
        sink = EventSink()

        with ModelApiProxy("openai", sink, upstream=self.upstream_url) as proxy:
            request = Request(
                f"{proxy.url}/responses",
                data=json.dumps({"input": "render exact TeX", "stream": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                body = response.read()
                content_encoding = response.headers.get("Content-Encoding")

        self.assertEqual(body, stream)
        self.assertIsNone(content_encoding)
        self.assertEqual(
            [
                event.get("delta")
                for event in sink.events
                if event["type"] == "assistant.delta"
            ],
            ["Exact \\TeX"],
        )
        self.assertIn("call.completed", [event["type"] for event in sink.events])

    def test_request_stream_flag_overrides_a_non_sse_content_type(self) -> None:
        stream = (
            b'data: {"type":"response.output_text.delta","delta":"raw Markdown"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        )
        _UpstreamHandler.response_type = "application/octet-stream"
        _UpstreamHandler.response_chunks = [stream]
        sink = EventSink()

        with ModelApiProxy("openai", sink, upstream=self.upstream_url) as proxy:
            request = Request(
                f"{proxy.url}/responses",
                data=json.dumps({"input": "stream this", "stream": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.read(), stream)

        response_event = next(
            event for event in sink.events if event["type"] == "call.response"
        )
        self.assertEqual(response_event["content_type"], "application/octet-stream")
        self.assertTrue(response_event["streaming"])
        self.assertEqual(
            [
                event.get("delta")
                for event in sink.events
                if event["type"] == "assistant.delta"
            ],
            ["raw Markdown"],
        )

    def test_upstream_error_status_and_body_are_not_wrapped(self) -> None:
        _UpstreamHandler.response_status = 429
        _UpstreamHandler.response_type = "application/json"
        _UpstreamHandler.response_chunks = [b'{"type":"rate_limit_error"}']
        sink = EventSink()

        with ModelApiProxy("anthropic", sink, upstream=self.upstream_url) as proxy:
            request = Request(
                f"{proxy.url}/v1/messages",
                data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            body = raised.exception.read()
            raised.exception.close()

        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(body, b'{"type":"rate_limit_error"}')
        self.assertIn(
            {"type": "call.failed", "upstream_status": 429},
            [
                {
                    "type": event["type"],
                    "upstream_status": event.get("upstream_status"),
                }
                for event in sink.events
                if event["type"] == "call.failed"
            ],
        )

    def test_secret_path_prefix_is_required(self) -> None:
        sink = EventSink()
        with ModelApiProxy("openai", sink, upstream=self.upstream_url) as proxy:
            request = Request(
                f"http://127.0.0.1:{proxy.port}/wrong/responses",
                data=b"{}",
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            raised.exception.close()
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(_UpstreamHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
