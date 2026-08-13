"""Print screen frames containing TeX from a captured Codex PTY session."""

from __future__ import annotations

import argparse

import pyte


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    parser.add_argument("--columns", type=int, default=153)
    parser.add_argument("--rows", type=int, default=47)
    args = parser.parse_args()

    raw = open(args.capture, "rb").read()
    sync_end = b"\x1b[?2026l"
    parts = raw.split(sync_end)
    screen = pyte.Screen(args.columns, args.rows)
    stream = pyte.ByteStream(screen)
    hits: list[tuple[int, list[tuple[int, str]]]] = []
    for number, part in enumerate(parts):
        stream.feed(part + sync_end if number < len(parts) - 1 else part)
        text = "\n".join(screen.display)
        if r"\rho" in text or r"\nabla" in text:
            rows = [
                (index + 1, line.rstrip())
                for index, line in enumerate(screen.display)
                if "\\" in line or line.strip() in ("[", "]")
            ]
            hits.append((number, rows))

    print(f"frames: {len(parts)}; frames containing TeX: {len(hits)}")
    for number, rows in hits[-8:]:
        print(f"FRAME {number}")
        for row, line in rows:
            print(row, repr(line))


if __name__ == "__main__":
    main()
