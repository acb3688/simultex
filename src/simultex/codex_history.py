"""Recover exact Markdown from a locally saved Codex CLI session."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_VALUE_OPTIONS = {
    "-a",
    "--add-dir",
    "--ask-for-approval",
    "-C",
    "--cd",
    "-c",
    "--config",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}


def codex_resume_history_events(
    command: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    """Return browser API events for the session selected by ``codex resume``.

    Codex still owns resume behavior. This best-effort reader only recovers the
    exact Markdown that the TUI necessarily loses when it paints saved messages
    into a terminal. Any unknown command or storage shape returns no events.
    """

    selection = _resume_selection(command)
    if selection is None:
        return []
    session_id, use_last, include_all = selection
    environment = os.environ if environ is None else environ
    codex_home = Path(environment.get("CODEX_HOME", Path.home() / ".codex"))
    session_path = _find_session(
        codex_home,
        session_id=session_id,
        use_last=use_last,
        include_all=include_all,
        cwd=_effective_cwd(command, cwd or Path.cwd()),
    )
    if session_path is None:
        return []
    return _read_history_events(session_path)


def _resume_selection(command: Sequence[str]) -> tuple[str | None, bool, bool] | None:
    arguments = list(command)
    resume_index = _resume_subcommand_index(arguments)
    if resume_index is None:
        return None

    session_id = None
    use_last = False
    include_all = False
    arguments = arguments[resume_index + 1 :]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--last":
            use_last = True
        elif argument == "--all":
            include_all = True
        elif argument in _VALUE_OPTIONS:
            index += 1
        elif any(argument.startswith(f"{option}=") for option in _VALUE_OPTIONS):
            pass
        elif argument.startswith("-"):
            pass
        elif _SESSION_ID.fullmatch(argument):
            session_id = argument.lower()
            break
        else:
            # This may be a session name or the optional prompt. Resolving names
            # would require Codex's private SQLite index, so leave selection to
            # Codex instead of guessing and displaying the wrong conversation.
            break
        index += 1

    if session_id is None and not use_last:
        return None
    return session_id, use_last, include_all


def _resume_subcommand_index(command: list[str]) -> int | None:
    index = 1
    while index < len(command):
        argument = command[index]
        if argument in _VALUE_OPTIONS:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in _VALUE_OPTIONS):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return index if argument == "resume" else None
    return None


def _effective_cwd(command: Sequence[str], cwd: Path) -> Path:
    selected = cwd
    arguments = list(command[1:])
    for index, argument in enumerate(arguments):
        if argument in {"-C", "--cd"} and index + 1 < len(arguments):
            selected = Path(arguments[index + 1])
        elif argument.startswith("--cd="):
            selected = Path(argument.split("=", 1)[1])
    if not selected.is_absolute():
        selected = cwd / selected
    return selected.resolve()


def _session_roots(codex_home: Path) -> Iterable[Path]:
    for name in ("sessions", "archived_sessions"):
        root = codex_home / name
        if root.is_dir():
            yield root


def _find_session(
    codex_home: Path,
    *,
    session_id: str | None,
    use_last: bool,
    include_all: bool,
    cwd: Path,
) -> Path | None:
    if session_id is not None:
        suffix = f"-{session_id}.jsonl"
        for root in _session_roots(codex_home):
            for path in root.rglob(f"*{suffix}"):
                if path.is_file():
                    return path
        return None

    if not use_last:
        return None
    candidates: list[tuple[float, Path]] = []
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    for path in sessions.rglob("*.jsonl"):
        metadata = _session_metadata(path)
        if metadata is None or metadata.get("source") != "cli":
            continue
        if not include_all:
            saved_cwd = metadata.get("cwd")
            if not isinstance(saved_cwd, str):
                continue
            try:
                if Path(saved_cwd).resolve() != cwd:
                    continue
            except OSError:
                continue
        try:
            recency = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((recency, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _session_metadata(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as source:
            record = json.loads(source.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("type") != "session_meta" or not isinstance(
        record.get("payload"), dict
    ):
        return None
    return record["payload"]


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in {"text", "Text"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n\n".join(parts).strip()


def _read_history_events(path: Path) -> list[dict[str, Any]]:
    metadata = _session_metadata(path)
    if metadata is None or not isinstance(metadata.get("id"), str):
        return []
    history_id = f"codex-history-{metadata['id']}"
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
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                timestamp = record.get("timestamp")
                if payload.get("type") == "task_complete":
                    finish_turn(timestamp)
                    continue
                if payload.get("type") != "item_completed":
                    continue
                item = payload.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                markdown = _content_text(item.get("content"))
                if not markdown:
                    continue
                if item_type == "UserMessage":
                    finish_turn(timestamp)
                    turn_number += 1
                    active_turn = f"history-turn-{turn_number}"
                    events.append(
                        _history_event(
                            history_id,
                            "turn.started",
                            timestamp,
                            provider="openai",
                            turn_id=active_turn,
                            ordinal=turn_number,
                            user={"markdown": markdown},
                        )
                    )
                elif item_type == "AgentMessage" and active_turn is not None:
                    call_number += 1
                    call_id = f"history-call-{call_number}"
                    common = {
                        "provider": "openai",
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
                            output_index=0,
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
