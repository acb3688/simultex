from __future__ import annotations

import gzip
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, WebSocket
from websockets.sync.client import connect as websocket_connect

from simultex.model_proxy import (
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
        self.assertIn('openai_base_url="http://127.0.0.1:9000/token"', command)
        self.assertFalse(any(arg.startswith("model_provider=") for arg in command))
        self.assertFalse(any(arg.startswith("model_providers.") for arg in command))
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

    def test_anthropic_web_search_model_call_is_recognized_structurally(self) -> None:
        prompt = (
            "Perform a web search for the query: "
            "Jacobian conjecture disproved counterexample 2026"
        )
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "tool", "name": "web_search"},
        }

        self.assertTrue(_is_anthropic_auxiliary_request(prompt, payload))
        self.assertFalse(_is_anthropic_auxiliary_request(prompt))
        self.assertFalse(
            _is_anthropic_auxiliary_request(
                prompt,
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [{"name": "WebSearch"}],
                    "tool_choice": {"type": "auto"},
                },
            )
        )

    def test_anthropic_web_fetch_model_call_is_recognized_by_its_envelope(self) -> None:
        prompt = """Web page content:
---
An article copied from a web page.
---

Summarize the result.

Provide a concise response based only on the content above. In your response:
 - Enforce a strict 125-character maximum for quotes from any source document.
"""
        payload = {"messages": [{"role": "user", "content": prompt}]}

        self.assertTrue(_is_anthropic_auxiliary_request(prompt, payload))
        self.assertFalse(_is_anthropic_auxiliary_request(prompt))
        self.assertFalse(
            _is_anthropic_auxiliary_request(
                prompt,
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [{"name": "Bash"}],
                },
            )
        )
        self.assertFalse(
            _is_anthropic_auxiliary_request(
                "Web page content:\nPlease review this text I pasted.",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Web page content:\nPlease review this text I pasted.",
                        }
                    ]
                },
            )
        )

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

    def test_anthropic_skill_body_is_continuation_context(self) -> None:
        source = """Base directory for this skill: /tmp/bundled-skills/hash/dataviz

Internal instructions that should not become a user message.
"""

        self.assertEqual(
            _request_details(
                "anthropic",
                {"messages": [{"role": "user", "content": source}]},
            ),
            (None, True),
        )

    def test_anthropic_meta_text_after_skill_result_is_continuation_context(self) -> None:
        internal = """Approach this as the design lead at a small studio.

## Fundamentals for every artifact

Internal instructions that should not become a user message.
"""
        messages = [
            {"role": "user", "content": "Build a diagram"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-skill",
                        "name": "Skill",
                        "input": {"skill": "artifact-design"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-skill",
                        "content": "Launching skill: artifact-design",
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": internal}]},
        ]

        self.assertEqual(
            _request_details("anthropic", {"messages": messages}),
            (None, True),
        )

    def test_anthropic_skill_result_and_meta_text_can_share_a_message(self) -> None:
        messages = [
            {"role": "user", "content": "Build a diagram"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-skill",
                        "name": "Skill",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-skill",
                        "content": "Launching skill: artifact-design",
                    },
                    {
                        "type": "text",
                        "text": "Internal artifact design instructions",
                    },
                ],
            },
        ]

        self.assertEqual(
            _request_details("anthropic", {"messages": messages}),
            (None, True),
        )

    def test_anthropic_text_after_an_ordinary_tool_is_not_hidden(self) -> None:
        messages = [
            {"role": "user", "content": "Inspect it"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu-read", "name": "Read"}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-read",
                        "content": "done",
                    }
                ],
            },
            {"role": "user", "content": "Now explain the result"},
        ]

        self.assertEqual(
            _request_details("anthropic", {"messages": messages}),
            ("Now explain the result", False),
        )

    def test_anthropic_image_metadata_is_continuation_context(self) -> None:
        source = (
            "[Image: original 2210x1462, displayed at 2000x1323. "
            "Multiply coordinates by 1.10 to map to original image.]"
        )

        self.assertEqual(
            _request_details(
                "anthropic",
                {"messages": [{"role": "user", "content": source}]},
            ),
            (None, True),
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

    def test_websocket_is_forwarded_and_emits_api_events(self) -> None:
        upstream_app = FastAPI()
        received: dict[str, object] = {}

        @upstream_app.websocket("/{path:path}")
        async def upstream_socket(path: str, websocket: WebSocket):
            received["path"] = path
            received["query"] = websocket.url.query
            received["authorization"] = websocket.headers.get("authorization")
            await websocket.accept()
            received["warmup"] = json.loads(await websocket.receive_text())
            await websocket.send_text(json.dumps({
                "type": "response.completed",
                "response": {"id": "warmup", "status": "completed"},
            }))
            received["payload"] = json.loads(await websocket.receive_text())
            await websocket.send_text(json.dumps({
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "Exact \\TeX",
            }))
            await websocket.send_text(json.dumps({
                "type": "response.completed",
                "response": {"status": "completed"},
            }))
            await websocket.close()

        upstream_socket_fd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_socket_fd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream_socket_fd.bind(("127.0.0.1", 0))
        upstream_socket_fd.listen(16)
        port = int(upstream_socket_fd.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(
            upstream_app,
            log_level="critical",
            access_log=False,
            log_config=None,
        ))
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [upstream_socket_fd]},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)

        sink = EventSink()
        try:
            with ModelApiProxy(
                "openai", sink, upstream=f"http://127.0.0.1:{port}"
            ) as proxy:
                url = proxy.url.replace("http://", "ws://") + "/responses?beta=true"
                with websocket_connect(
                    url,
                    additional_headers={"Authorization": "Bearer secret-value"},
                ) as client:
                    client.send(json.dumps({
                        "type": "response.create",
                        "input": [],
                        "generate": False,
                        "stream": True,
                    }))
                    warmup = json.loads(client.recv())
                    client.send(json.dumps({
                        "type": "response.create",
                        "input": "render exact TeX",
                        "stream": True,
                    }))
                    first = json.loads(client.recv())
                    second = json.loads(client.recv())
        finally:
            server.should_exit = True
            thread.join(timeout=3)
            upstream_socket_fd.close()

        self.assertEqual(warmup["response"]["id"], "warmup")
        self.assertEqual(first["delta"], "Exact \\TeX")
        self.assertEqual(second["type"], "response.completed")
        self.assertEqual(received["path"], "responses")
        self.assertEqual(received["query"], "beta=true")
        self.assertEqual(received["authorization"], "Bearer secret-value")
        self.assertFalse(received["warmup"]["generate"])
        self.assertEqual(received["payload"]["type"], "response.create")
        self.assertEqual(
            sum(event["type"] == "turn.started" for event in sink.events),
            1,
        )
        self.assertEqual(
            [event.get("delta") for event in sink.events if event["type"] == "assistant.delta"],
            ["Exact \\TeX"],
        )
        response_event = next(
            event for event in sink.events if event["type"] == "call.response"
        )
        self.assertEqual(response_event["transport"], "websocket")


if __name__ == "__main__":
    unittest.main()
