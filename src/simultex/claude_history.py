"""Recover exact Markdown from a locally saved Claude Code session."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_INTERRUPTED = {"[Request interrupted by user]", "[Request cancelled by user]"}


def claude_resume_history_events(
    command: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return browser API events for an explicit ``claude --resume`` session.

    Claude still owns resume behavior. This best-effort reader only recovers the
    exact Markdown that the TUI necessarily loses when it paints saved messages
    into a terminal. Picker and named-session forms return no events rather than
    guessing and displaying the wrong conversation.
    """

    session_id = _resume_session_id(command)
    if session_id is None:
        return []
    environment = os.environ if environ is None else environ
    claude_home = Path(
        environment.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
    )
    session_path = _find_session(claude_home, session_id)
    if session_path is None:
        return []
    return _read_history_events(session_path, session_id)


def _resume_session_id(command: Sequence[str]) -> str | None:
    arguments = list(command[1:])
    for index, argument in enumerate(arguments):
        value: str | None = None
        if argument in {"-r", "--resume"}:
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                value = arguments[index + 1]
        elif argument.startswith("--resume="):
            value = argument.split("=", 1)[1]
        if value is not None:
            return value.lower() if _SESSION_ID.fullmatch(value) else None
    return None


def _find_session(claude_home: Path, session_id: str) -> Path | None:
    projects = claude_home / "projects"
    if not projects.is_dir():
        return None
    for path in projects.glob(f"*/{session_id}.jsonl"):
        if path.is_file():
            return path
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n\n".join(parts).strip()


def _read_history_events(path: Path, session_id: str) -> list[dict[str, Any]]:
    history_id = f"claude-history-{session_id}"
    events: list[dict[str, Any]] = []
    turn_number = 0
    call_number = 0
    active_turn: str | None = None

    def finish_turn(timestamp: Any = None) -> None:
        nonlocal active_turn
        if active_turn is None:
            return
        events.append(
            _history_event(history_id, "turn.completed", timestamp, turn_id=active_turn)
        )
        active_turn = None

    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    not isinstance(record, dict)
                    or record.get("isSidechain") is True
                    or record.get("isMeta") is True
                ):
                    continue
                record_type = record.get("type")
                if record_type not in {"user", "assistant"}:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                markdown = _message_text(message.get("content"))
                if not markdown:
                    continue
                timestamp = record.get("timestamp")
                if record_type == "user":
                    if markdown in _INTERRUPTED:
                        finish_turn(timestamp)
                        continue
                    if isinstance(message.get("content"), list) and any(
                        isinstance(part, dict) and part.get("type") == "tool_result"
                        for part in message["content"]
                    ):
                        continue
                    finish_turn(timestamp)
                    turn_number += 1
                    active_turn = f"history-turn-{turn_number}"
                    events.append(
                        _history_event(
                            history_id,
                            "turn.started",
                            timestamp,
                            provider="anthropic",
                            turn_id=active_turn,
                            ordinal=turn_number,
                            user={"markdown": markdown},
                        )
                    )
                elif active_turn is not None:
                    call_number += 1
                    call_id = f"history-call-{call_number}"
                    common = {
                        "provider": "anthropic",
                        "turn_id": active_turn,
                        "call_id": call_id,
                    }
                    events.append(
                        _history_event(history_id, "call.started", timestamp, **common)
                    )
                    events.append(
                        _history_event(
                            history_id,
                            "assistant.part.done",
                            timestamp,
                            content_index=0,
                            markdown=markdown,
                            **common,
                        )
                    )
                    events.append(
                        _history_event(
                            history_id,
                            "call.completed",
                            timestamp,
                            status="completed",
                            **common,
                        )
                    )
    except (OSError, UnicodeDecodeError):
        return []
    finish_turn()
    return events


def _history_event(
    session_id: str,
    event_type: str,
    timestamp: Any,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "version": 1,
        "type": event_type,
        "session_id": session_id,
        "timestamp": timestamp,
        **payload,
    }
