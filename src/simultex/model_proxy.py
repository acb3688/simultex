"""Transparent model API proxy and provider-neutral transcript events."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_REQUEST_STRIP = _HOP_BY_HOP | {"host", "content-length"}
# httpx decodes upstream content encodings as it streams. The downstream body
# and the observer therefore both see the same decoded bytes, so the original
# representation metadata must not be forwarded.
_RESPONSE_STRIP = _HOP_BY_HOP | {"content-encoding", "content-length"}
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_MAX_NON_STREAM_RESPONSE = 16 * 1024 * 1024


class ApiEventSink(Protocol):
    def api_event(self, payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class CallContext:
    provider: str
    turn_id: str
    call_id: str


class TranscriptCoordinator:
    """Assign provider requests to turns without inspecting terminal text."""

    def __init__(self, sink: ApiEventSink):
        self.sink = sink
        self.session_id = f"session-{secrets.token_hex(8)}"
        self._lock = threading.Lock()
        self._turn_number = 0
        self._call_number = 0
        self._active_turn: str | None = None

    def begin_call(
        self,
        provider: str,
        user_markdown: str | None,
        continuation: bool,
    ) -> CallContext:
        with self._lock:
            if self._active_turn is None or (user_markdown is not None and not continuation):
                if self._active_turn is not None:
                    self._publish_locked("turn.completed", turn_id=self._active_turn)
                self._turn_number += 1
                self._active_turn = f"turn-{self._turn_number}"
                payload: dict[str, Any] = {
                    "provider": provider,
                    "ordinal": self._turn_number,
                }
                if user_markdown is not None:
                    payload["user"] = {"markdown": user_markdown}
                self._publish_locked("turn.started", turn_id=self._active_turn, **payload)

            assert self._active_turn is not None
            self._call_number += 1
            call_id = f"call-{self._call_number}"
            self._publish_locked(
                "call.started",
                provider=provider,
                turn_id=self._active_turn,
                call_id=call_id,
            )
            return CallContext(provider, self._active_turn, call_id)

    def publish(self, event_type: str, context: CallContext, **payload: Any) -> None:
        with self._lock:
            self._publish_locked(
                event_type,
                provider=context.provider,
                turn_id=context.turn_id,
                call_id=context.call_id,
                **payload,
            )

    def close(self) -> None:
        with self._lock:
            if self._active_turn is not None:
                self._publish_locked("turn.completed", turn_id=self._active_turn)
                self._active_turn = None

    def _publish_locked(self, event_type: str, **payload: Any) -> None:
        self.sink.api_event(
            {
                "version": 1,
                "type": event_type,
                "session_id": self.session_id,
                "timestamp": time.time(),
                **payload,
            }
        )


class _SseDecoder:
    """Incrementally decode SSE data fields across arbitrary byte boundaries."""

    def __init__(self, callback: Any):
        self.callback = callback
        self.buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while True:
            boundary = self._boundary()
            if boundary is None:
                return
            index, width = boundary
            block = bytes(self.buffer[:index])
            del self.buffer[: index + width]
            self._dispatch(block)

    def finish(self) -> None:
        if self.buffer:
            self._dispatch(bytes(self.buffer))
            self.buffer.clear()

    def _boundary(self) -> tuple[int, int] | None:
        matches = []
        for marker in (b"\r\n\r\n", b"\n\n", b"\r\r"):
            index = self.buffer.find(marker)
            if index >= 0:
                matches.append((index, len(marker)))
        return min(matches) if matches else None

    def _dispatch(self, block: bytes) -> None:
        values = []
        for line in block.splitlines():
            if not line.startswith(b"data:"):
                continue
            value = line[5:]
            if value.startswith(b" "):
                value = value[1:]
            values.append(value)
        if not values:
            return
        data = b"\n".join(values)
        if data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self.callback(payload)


@dataclass
class _ResponseObserver:
    coordinator: TranscriptCoordinator
    context: CallContext
    streaming: bool
    buffer: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        """Initialize provider-specific stream state in subclasses."""

    def feed(self, data: bytes) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        raise NotImplementedError

    def failed(self, detail: str) -> None:
        self.coordinator.publish("call.failed", self.context, error=detail)

    def emit(self, event_type: str, **payload: Any) -> None:
        self.coordinator.publish(event_type, self.context, **payload)


class _OpenAIObserver(_ResponseObserver):
    def __post_init__(self) -> None:
        self.parts: dict[tuple[int, int], str] = {}
        self.decoder = _SseDecoder(self._event)

    def feed(self, data: bytes) -> None:
        if self.streaming:
            self.decoder.feed(data)
        elif len(self.buffer) < _MAX_NON_STREAM_RESPONSE:
            self.buffer.extend(data[: _MAX_NON_STREAM_RESPONSE - len(self.buffer)])

    def finish(self) -> None:
        if self.streaming:
            self.decoder.finish()
            return
        try:
            payload = json.loads(self.buffer)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for output_index, item in enumerate(payload.get("output", [])):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content_index, part in enumerate(item.get("content", [])):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        self.emit(
                            "assistant.part.done",
                            output_index=output_index,
                            content_index=content_index,
                            markdown=text,
                        )
        self.emit("call.completed", status=payload.get("status", "completed"))

    def _event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        output_index = payload.get("output_index", 0)
        content_index = payload.get("content_index", 0)
        key = (output_index, content_index)
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                self.parts[key] = self.parts.get(key, "") + delta
                self.emit(
                    "assistant.delta",
                    output_index=output_index,
                    content_index=content_index,
                    delta=delta,
                )
        elif event_type == "response.output_text.done":
            text = payload.get("text")
            if not isinstance(text, str):
                text = self.parts.get(key, "")
            self.emit(
                "assistant.part.done",
                output_index=output_index,
                content_index=content_index,
                markdown=text,
            )
        elif event_type in {"response.completed", "response.incomplete"}:
            response = payload.get("response")
            status = response.get("status") if isinstance(response, dict) else None
            self.emit("call.completed", status=status or event_type.removeprefix("response."))
        elif event_type in {"response.failed", "error"}:
            self.failed(event_type)


class _AnthropicObserver(_ResponseObserver):
    def __post_init__(self) -> None:
        self.parts: dict[int, str] = {}
        self.stop_reason: str | None = None
        self.decoder = _SseDecoder(self._event)

    def feed(self, data: bytes) -> None:
        if self.streaming:
            self.decoder.feed(data)
        elif len(self.buffer) < _MAX_NON_STREAM_RESPONSE:
            self.buffer.extend(data[: _MAX_NON_STREAM_RESPONSE - len(self.buffer)])

    def finish(self) -> None:
        if self.streaming:
            self.decoder.finish()
            return
        try:
            payload = json.loads(self.buffer)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for index, part in enumerate(payload.get("content", [])):
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    self.emit("assistant.part.done", content_index=index, markdown=text)
        self.emit("call.completed", status=payload.get("stop_reason", "completed"))

    def _event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        index = payload.get("index", 0)
        if event_type == "content_block_start":
            block = payload.get("content_block")
            text = (
                block.get("text")
                if isinstance(block, dict) and block.get("type") == "text"
                else None
            )
            if isinstance(text, str) and text:
                self.parts[index] = text
                self.emit("assistant.delta", content_index=index, delta=text)
        elif event_type == "content_block_delta":
            delta = payload.get("delta")
            text = (
                delta.get("text")
                if isinstance(delta, dict) and delta.get("type") == "text_delta"
                else None
            )
            if isinstance(text, str):
                self.parts[index] = self.parts.get(index, "") + text
                self.emit("assistant.delta", content_index=index, delta=text)
        elif event_type == "content_block_stop" and index in self.parts:
            self.emit(
                "assistant.part.done",
                content_index=index,
                markdown=self.parts[index],
            )
        elif event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                self.stop_reason = delta["stop_reason"]
        elif event_type == "message_stop":
            self.emit("call.completed", status=self.stop_reason or "completed")
        elif event_type == "error":
            self.failed("error")


def detect_provider(command: Sequence[str]) -> str | None:
    if not command:
        return None
    name = Path(command[0]).name.lower().removesuffix(".exe")
    if name == "codex":
        return "openai"
    if name == "claude":
        return "anthropic"
    return None


def route_child(
    command: Sequence[str],
    provider: str,
    proxy_url: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    routed = list(command)
    child_env = dict(os.environ if environ is None else environ)
    if provider == "anthropic":
        child_env["ANTHROPIC_BASE_URL"] = proxy_url
    elif provider == "openai":
        routed[1:1] = [
            "-c",
            'model_provider="simultex"',
            "-c",
            'model_providers.simultex.name="SimulTeX OpenAI proxy"',
            "-c",
            f"model_providers.simultex.base_url={json.dumps(proxy_url)}",
            "-c",
            "model_providers.simultex.requires_openai_auth=true",
            "-c",
            "model_providers.simultex.supports_websockets=false",
        ]
    else:
        raise ValueError(f"unsupported provider: {provider}")
    return routed, child_env


def _request_details(provider: str, payload: dict[str, Any]) -> tuple[str | None, bool]:
    if provider == "openai":
        value = payload.get("input")
        if isinstance(value, str):
            return value, False
        if not isinstance(value, list):
            return None, True
        for item in reversed(value):
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}:
                return None, True
            if item.get("role") == "user":
                text = _content_text(item.get("content"), {"input_text", "text"})
                return (text, False) if text is not None else (None, True)
        return None, True

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, True
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for part in content
        ):
            current_text = _content_text(content, {"text"})
            if current_text is not None:
                cleaned = _strip_anthropic_system_messages(current_text)
                if cleaned:
                    if _is_anthropic_internal_context(
                        cleaned
                    ) or _follows_anthropic_skill_tool(messages, index):
                        return None, True
                    return cleaned, False
            for prior in reversed(messages[:index]):
                if not isinstance(prior, dict) or prior.get("role") != "user":
                    continue
                text = _content_text(prior.get("content"), {"text"})
                if text is not None:
                    cleaned = _strip_anthropic_system_messages(text)
                    if cleaned:
                        if _is_anthropic_internal_context(cleaned):
                            continue
                        return cleaned, True
            return None, True
        text = _content_text(content, {"text"})
        if text is None:
            return None, True
        cleaned = _strip_anthropic_system_messages(text)
        if _is_anthropic_internal_context(cleaned) or _follows_anthropic_skill_tool(
            messages, index
        ):
            return None, True
        return (cleaned, False) if cleaned else (None, True)
    return None, True


def _follows_anthropic_skill_tool(messages: list[Any], user_index: int) -> bool:
    """Recognize skill instructions that Claude Code records as meta user text."""

    result_ids: set[str] = set()
    first_result_index = user_index
    for index in range(user_index, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            break
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            tool_use_id = part.get("tool_use_id")
            if isinstance(tool_use_id, str):
                result_ids.add(tool_use_id)
                first_result_index = index
    if not result_ids:
        return False

    for message in reversed(messages[:first_result_index]):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            break
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        return any(
            isinstance(part, dict)
            and part.get("type") == "tool_use"
            and part.get("name") == "Skill"
            and part.get("id") in result_ids
            for part in content
        )
    return False


def _strip_anthropic_system_messages(user_markdown: str) -> str:
    return re.sub(
        r"<system-reminder(?:\s[^>]*)?>[\s\S]*?</system-reminder>\s*",
        "",
        user_markdown,
    ).strip()


def _is_anthropic_internal_context(user_markdown: str) -> bool:
    stripped = user_markdown.strip()
    return bool(
        re.match(r"Base directory for this skill: [^\n]+\n\n\S", stripped)
        or re.fullmatch(
            r"\[Image: original \d+x\d+, displayed at \d+x\d+\. "
            r"Multiply coordinates by \d+(?:\.\d+)? to map to original image\.\]",
            stripped,
        )
    )


def _is_anthropic_auxiliary_request(
    user_markdown: str | None, payload: dict[str, Any] | None = None
) -> bool:
    if user_markdown is None:
        return False
    stripped = user_markdown.strip()
    if stripped == "quota":
        return True
    if _is_anthropic_web_search_request(payload):
        return True
    if _is_anthropic_web_fetch_request(stripped, payload):
        return True
    if stripped.startswith(
        "[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]"
    ):
        return True
    return (
        stripped.startswith("<session>")
        and "</session>" in stripped
        and "Write the title in the predominant language of the session" in stripped
    )


def _is_anthropic_web_search_request(payload: dict[str, Any] | None) -> bool:
    """Recognize Claude Code's isolated model call that drives WebSearch."""

    if payload is None:
        return False
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get("role") != "user"
    ):
        return False
    tool_choice = payload.get("tool_choice")
    if not isinstance(tool_choice, dict) or tool_choice.get("name") != "web_search":
        return False
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and (
            tool.get("name") == "web_search"
            or (
                isinstance(tool.get("type"), str)
                and tool["type"].startswith("web_search_")
            )
        )
        for tool in tools
    )


def _is_anthropic_web_fetch_request(
    user_markdown: str, payload: dict[str, Any] | None
) -> bool:
    """Recognize Claude Code's isolated model call that summarizes WebFetch data."""

    if payload is None:
        return False
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get("role") != "user"
        or payload.get("tools") not in (None, [])
    ):
        return False
    if not user_markdown.startswith("Web page content:\n"):
        return False
    return (
        "\nProvide a concise response based only on the content above. In your response:"
        in user_markdown
        or user_markdown.endswith(
            "Provide a concise response based on the content above. Include relevant "
            "details, code examples, and documentation excerpts as needed."
        )
    )


def _content_text(content: Any, accepted_types: set[str]) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    values = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in accepted_types:
            continue
        text = part.get("text")
        if isinstance(text, str):
            values.append(text)
    return "\n".join(values) if values else None


class ModelApiProxy:
    """Run a loopback-only API proxy in a background ASGI server."""

    def __init__(
        self,
        provider: str,
        sink: ApiEventSink,
        *,
        upstream: str | None = None,
    ):
        if provider not in {"openai", "anthropic"}:
            raise ValueError(f"unsupported provider: {provider}")
        self.provider = provider
        self.upstream = upstream.rstrip("/") if upstream else None
        self.token = secrets.token_urlsafe(24)
        self.coordinator = TranscriptCoordinator(sink)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(128)
        except OSError:
            self._socket.close()
            raise
        self._port = int(self._socket.getsockname()[1])
        self.app = self._build_app()
        config = uvicorn.Config(
            self.app,
            log_level="critical",
            access_log=False,
            log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            name="simultex-model-proxy",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5
        while not self._server.started:
            if not self._thread.is_alive():
                raise OSError("model proxy stopped during startup")
            if time.monotonic() >= deadline:
                raise OSError("model proxy did not start")
            time.sleep(0.01)

    def close(self) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self._socket.close()
        self.coordinator.close()

    def __enter__(self) -> ModelApiProxy:
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            timeout = httpx.Timeout(connect=30, read=None, write=300, pool=30)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                app.state.client = client
                yield

        app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{token}/{path:path}", methods=list(_METHODS))
        async def forward(token: str, path: str, request: Request):
            if not secrets.compare_digest(token, self.token):
                return JSONResponse({"error": "not found"}, status_code=404)

            body = await request.body()
            context, request_streaming = self._begin_observed_call(path, body)
            target = self._target_url(path, request)
            headers = [
                (name, value)
                for name, value in request.headers.items()
                if name.lower() not in _REQUEST_STRIP
            ]
            upstream_request = request.app.state.client.build_request(
                request.method,
                target,
                headers=headers,
                content=body,
            )
            try:
                upstream_response = await request.app.state.client.send(
                    upstream_request,
                    stream=True,
                )
            except httpx.RequestError as exc:
                if context is not None:
                    self.coordinator.publish(
                        "call.failed", context, error=type(exc).__name__
                    )
                return JSONResponse(
                    {"error": {"type": "proxy_error", "message": "upstream request failed"}},
                    status_code=502,
                )

            content_type = upstream_response.headers.get("content-type", "")
            content_encoding = upstream_response.headers.get("content-encoding", "")
            streaming = request_streaming or "text/event-stream" in content_type.lower()
            if context is not None:
                self.coordinator.publish(
                    "call.response",
                    context,
                    upstream_status=upstream_response.status_code,
                    content_type=content_type,
                    content_encoding=content_encoding,
                    streaming=streaming,
                )
            observer = None
            if upstream_response.status_code < 400:
                observer = self._observer(context, streaming=streaming)
            elif context is not None:
                self.coordinator.publish(
                    "call.failed",
                    context,
                    upstream_status=upstream_response.status_code,
                )

            async def relay():
                try:
                    async for chunk in upstream_response.aiter_bytes():
                        if observer is not None:
                            observer.feed(chunk)
                        yield chunk
                    if observer is not None:
                        observer.finish()
                except asyncio.CancelledError:
                    if observer is not None:
                        observer.failed("cancelled")
                    raise
                except httpx.HTTPError as exc:
                    if observer is not None:
                        observer.failed(type(exc).__name__)
                    raise
                finally:
                    await upstream_response.aclose()

            response_headers = {
                name: value
                for name, value in upstream_response.headers.items()
                if name.lower() not in _RESPONSE_STRIP
            }
            return StreamingResponse(
                relay(),
                status_code=upstream_response.status_code,
                headers=response_headers,
                background=BackgroundTask(upstream_response.aclose),
            )

        return app

    def _begin_observed_call(
        self, path: str, body: bytes
    ) -> tuple[CallContext | None, bool]:
        normalized = "/" + path.strip("/")
        inference = (
            self.provider == "openai" and normalized.endswith("/responses")
        ) or (
            self.provider == "anthropic" and normalized.endswith("/v1/messages")
        )
        if not inference:
            return None, False
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        user_markdown, continuation = _request_details(self.provider, payload)
        if self.provider == "anthropic" and _is_anthropic_auxiliary_request(
            user_markdown, payload
        ):
            return None, payload.get("stream") is True
        context = self.coordinator.begin_call(self.provider, user_markdown, continuation)
        return context, payload.get("stream") is True

    def _observer(
        self, context: CallContext | None, *, streaming: bool
    ) -> _ResponseObserver | None:
        if context is None:
            return None
        if self.provider == "openai":
            return _OpenAIObserver(self.coordinator, context, streaming)
        return _AnthropicObserver(self.coordinator, context, streaming)

    def _target_url(self, path: str, request: Request) -> str:
        root = self.upstream or self._default_upstream(request)
        target = f"{root}/{path.lstrip('/')}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return target

    def _default_upstream(self, request: Request) -> str:
        if self.provider == "anthropic":
            return "https://api.anthropic.com"
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer sk-"):
            return "https://api.openai.com/v1"
        return "https://chatgpt.com/backend-api/codex"


def validate_upstream(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("upstream must be an absolute http or https URL")
    return value.rstrip("/")
