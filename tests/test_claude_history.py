from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from simultex.claude_history import claude_resume_history_events


SESSION_ID = "3898397c-a482-4310-b550-81e9df3f2f38"


def write_session(claude_home: Path, records: list[dict]) -> Path:
    directory = claude_home / "projects" / "-workspace"
    directory.mkdir(parents=True)
    path = directory / f"{SESSION_ID}.jsonl"
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    return path


def message(
    record_type: str,
    content: object,
    timestamp: str,
    *,
    sidechain: bool = False,
    meta: bool = False,
) -> dict:
    return {
        "type": record_type,
        "sessionId": SESSION_ID,
        "timestamp": timestamp,
        "isSidechain": sidechain,
        "isMeta": meta,
        "message": {"role": record_type, "content": content},
    }


class ClaudeResumeHistoryTests(unittest.TestCase):
    def test_explicit_session_restores_exact_markdown(self) -> None:
        exact = """Here is math:

\\[x^2 + y^2 = z^2\\]

```python
print("hello")
```"""
        records = [
            message("user", "Render it", "2026-08-17T01:00:00Z"),
            message(
                "assistant",
                [{"type": "thinking", "thinking": "internal"}],
                "2026-08-17T01:00:01Z",
            ),
            message(
                "assistant",
                [{"type": "text", "text": exact}],
                "2026-08-17T01:00:02Z",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            claude_home = Path(directory)
            write_session(claude_home, records)
            events = claude_resume_history_events(
                ["claude", "--resume", SESSION_ID],
                environ={"CLAUDE_CONFIG_DIR": str(claude_home)},
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
        self.assertEqual(events[0]["provider"], "anthropic")
        self.assertTrue(events[0]["session_id"].endswith(SESSION_ID))

    def test_tool_results_interruptions_and_sidechains_are_not_messages(self) -> None:
        records = [
            message("user", "Inspect it", "2026-08-17T01:00:00Z"),
            message(
                "assistant",
                [{"type": "tool_use", "id": "tool-1", "name": "Read"}],
                "2026-08-17T01:00:01Z",
            ),
            message(
                "user",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done"}],
                "2026-08-17T01:00:02Z",
            ),
            message(
                "assistant",
                [{"type": "text", "text": "The result"}],
                "2026-08-17T01:00:03Z",
            ),
            message(
                "assistant",
                [{"type": "text", "text": "sidechain text"}],
                "2026-08-17T01:00:04Z",
                sidechain=True,
            ),
            message(
                "user",
                [{"type": "text", "text": "internal skill instructions"}],
                "2026-08-17T01:00:04Z",
                meta=True,
            ),
            message(
                "user",
                [{"type": "text", "text": "[Request interrupted by user]"}],
                "2026-08-17T01:00:05Z",
            ),
            message(
                "assistant",
                [{"type": "text", "text": "No response requested."}],
                "2026-08-17T01:00:06Z",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            claude_home = Path(directory)
            write_session(claude_home, records)
            events = claude_resume_history_events(
                ["claude", f"--resume={SESSION_ID}"],
                environ={"CLAUDE_CONFIG_DIR": str(claude_home)},
            )

        self.assertEqual(
            [event["user"]["markdown"] for event in events if event["type"] == "turn.started"],
            ["Inspect it"],
        )
        self.assertEqual(
            [event["markdown"] for event in events if event["type"] == "assistant.part.done"],
            ["The result"],
        )

    def test_picker_named_and_non_resume_commands_do_not_guess(self) -> None:
        self.assertEqual(claude_resume_history_events(["claude"]), [])
        self.assertEqual(claude_resume_history_events(["claude", "--resume"]), [])
        self.assertEqual(
            claude_resume_history_events(["claude", "--resume", "named-session"]),
            [],
        )
        self.assertEqual(claude_resume_history_events(["claude", "--continue"]), [])

    def test_unknown_or_malformed_session_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claude_home = Path(directory)
            path = write_session(claude_home, [])
            path.write_text("not json\n", encoding="utf-8")
            events = claude_resume_history_events(
                ["claude", "-r", SESSION_ID],
                environ={"CLAUDE_CONFIG_DIR": str(claude_home)},
            )

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
