from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from simultex.codex_history import codex_resume_history_events


SESSION_ONE = "01a00930-5b8b-7722-b7e0-c94806474c93"
SESSION_TWO = "01a00931-1111-7222-8333-444444444444"


def write_session(
    codex_home: Path,
    session_id: str,
    cwd: Path,
    messages: list[tuple[str, str]],
    *,
    modified: float = 1.0,
    source: str = "cli",
) -> Path:
    directory = codex_home / "sessions" / "2026" / "08" / "15"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-15T23-00-00-{session_id}.jsonl"
    records = [
        {
            "timestamp": "2026-08-16T06:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(cwd),
                "source": source,
            },
        }
    ]
    for index, (role, markdown) in enumerate(messages, start=1):
        item_type = "UserMessage" if role == "user" else "AgentMessage"
        content_type = "text" if role == "user" else "Text"
        records.append(
            {
                "timestamp": f"2026-08-16T06:00:{index:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": item_type,
                        "content": [{"type": content_type, "text": markdown}],
                    },
                },
            }
        )
        if role == "assistant":
            records.append(
                {
                    "timestamp": f"2026-08-16T06:00:{index:02d}Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            )
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    os.utime(path, (modified, modified))
    return path


class CodexResumeHistoryTests(unittest.TestCase):
    def test_explicit_session_restores_exact_markdown(self) -> None:
        exact = """Here is math:

\\[x^2 + y^2 = z^2\\]

```python
print("hello")
```

```mermaid
flowchart LR
    A --> B
```"""
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            write_session(
                codex_home,
                SESSION_ONE,
                Path("/workspace"),
                [("user", "Render it"), ("assistant", exact)],
            )

            events = codex_resume_history_events(
                ["codex", "resume", SESSION_ONE],
                environ={"CODEX_HOME": str(codex_home)},
                cwd=Path("/workspace"),
            )

        self.assertEqual(
            [event["type"] for event in events],
            [
                "turn.started",
                "call.started",
                "assistant.part.done",
                "call.completed",
                "turn.completed",
            ],
        )
        self.assertEqual(events[0]["user"]["markdown"], "Render it")
        self.assertEqual(events[2]["markdown"], exact)
        self.assertTrue(events[0]["session_id"].endswith(SESSION_ONE))

    def test_last_uses_most_recent_cli_session_in_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            write_session(
                codex_home,
                SESSION_ONE,
                Path("/workspace"),
                [("user", "older"), ("assistant", "answer")],
                modified=10,
            )
            write_session(
                codex_home,
                SESSION_TWO,
                Path("/elsewhere"),
                [("user", "newer"), ("assistant", "answer")],
                modified=20,
            )

            scoped = codex_resume_history_events(
                ["codex", "resume", "--last"],
                environ={"CODEX_HOME": str(codex_home)},
                cwd=Path("/workspace"),
            )
            all_sessions = codex_resume_history_events(
                ["codex", "resume", "--last", "--all"],
                environ={"CODEX_HOME": str(codex_home)},
                cwd=Path("/workspace"),
            )

        self.assertEqual(scoped[0]["user"]["markdown"], "older")
        self.assertEqual(all_sessions[0]["user"]["markdown"], "newer")

    def test_last_honors_codex_working_directory_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            target = root / "target"
            target.mkdir()
            write_session(
                codex_home,
                SESSION_ONE,
                target,
                [("user", "target session"), ("assistant", "answer")],
            )

            events = codex_resume_history_events(
                ["codex", "resume", "--last", "-C", str(target)],
                environ={"CODEX_HOME": str(codex_home)},
                cwd=root,
            )

        self.assertEqual(events[0]["user"]["markdown"], "target session")

    def test_non_resume_and_picker_commands_do_not_guess_a_session(self) -> None:
        self.assertEqual(codex_resume_history_events(["codex"]), [])
        self.assertEqual(codex_resume_history_events(["codex", "resume"]), [])
        self.assertEqual(
            codex_resume_history_events(["codex", "-m", "resume", SESSION_ONE]),
            [],
        )
        self.assertEqual(
            codex_resume_history_events(["codex", "resume", "named-session"]),
            [],
        )

    def test_unknown_or_malformed_session_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            path = write_session(
                codex_home,
                SESSION_ONE,
                Path("/workspace"),
                [("user", "hello")],
            )
            path.write_text("not json\n", encoding="utf-8")

            events = codex_resume_history_events(
                ["codex", "resume", SESSION_ONE],
                environ={"CODEX_HOME": str(codex_home)},
                cwd=Path("/workspace"),
            )

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
