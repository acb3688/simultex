"""Incremental, chunk-boundary-safe recognition of common math delimiters."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


Replacement = Callable[[bytes, str, bool], bytes | None]

_ANSI = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[PX^_].*?\x1b\\)",
    re.DOTALL,
)


def _escaped(data: bytearray, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and data[index] == 0x5C:
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


@dataclass(frozen=True)
class _Candidate:
    start: int
    content_start: int | None
    closer: bytes
    block: bool
    kind: str = "fixed"


class LatexStreamParser:
    """Replace delimited LaTeX while preserving all unrelated bytes exactly."""

    def __init__(
        self,
        replacement: Replacement,
        parse_dollars: bool = True,
        max_candidate_bytes: int = 16_384,
    ):
        self.replacement = replacement
        self.parse_dollars = parse_dollars
        self.max_candidate_bytes = max_candidate_bytes
        self._buffer = bytearray()
        self._discard_newline = False

    def feed(self, data: bytes) -> bytes:
        self._buffer.extend(data)
        return self._drain(final=False)

    def finish(self) -> bytes:
        return self._drain(final=True)

    def _drain(self, final: bool) -> bytes:
        output = bytearray()
        while self._buffer:
            if self._discard_newline:
                if self._buffer.startswith(b"\r\n"):
                    del self._buffer[:2]
                    self._discard_newline = False
                    continue
                if self._buffer.startswith(b"\n"):
                    del self._buffer[:1]
                    self._discard_newline = False
                    continue
                if self._buffer == b"\r" and not final:
                    break
                self._discard_newline = False
            found = self._find_opener()
            if found is None:
                if final:
                    output.extend(self._buffer)
                    self._buffer.clear()
                else:
                    keep = 1 if self._buffer[-1:] in (b"\\", b"$", b"(") else 0
                    if len(self._buffer) > keep:
                        output.extend(self._buffer[:-keep] if keep else self._buffer)
                        del self._buffer[: len(self._buffer) - keep]
                break

            if found.start:
                output.extend(self._buffer[: found.start])
                del self._buffer[: found.start]
                found = _Candidate(
                    0,
                    None if found.content_start is None else found.content_start - found.start,
                    found.closer,
                    found.block,
                    found.kind,
                )
            if found.content_start is None:
                if final:
                    output.append(self._buffer[0])
                    del self._buffer[0]
                    continue
                break
            closed = self._find_closer(found)
            if closed is None:
                if final or len(self._buffer) > self.max_candidate_bytes:
                    output.append(self._buffer[0])
                    del self._buffer[0]
                    continue
                break

            end, content_end = closed
            original = bytes(self._buffer[:end])
            inner = bytes(self._buffer[found.content_start : content_end])
            del self._buffer[:end]
            clean = _ANSI.sub(b"", inner).replace(b"\r", b"")
            try:
                math = clean.decode("utf-8")
            except UnicodeDecodeError:
                output.extend(original)
                continue
            if not self._plausible(math, found):
                output.extend(original)
                continue
            rendered = self.replacement(original, math, found.block)
            output.extend(original if rendered is None else rendered)
            if rendered is not None and found.block:
                # The graphics encoder has already advanced past the rows used
                # by a display image. Avoid adding the source line break too.
                self._discard_newline = True
        return bytes(output)

    def _find_opener(self) -> _Candidate | None:
        candidates: list[_Candidate] = []
        for opener, closer, block in ((b"\\[", b"\\]", True), (b"\\(", b"\\)", False)):
            start = self._buffer.find(opener)
            while start >= 0 and _escaped(self._buffer, start):
                start = self._buffer.find(opener, start + 1)
            if start >= 0:
                candidates.append(_Candidate(start, start + len(opener), closer, block))

        loose = self._find_loose_display_opener()
        if loose is not None:
            candidates.append(loose)

        # Codex's terminal Markdown renderer sometimes consumes the backslashes
        # in \(...\), leaving forms such as (\mathbf{u}). Requiring a TeX command
        # immediately after '(' avoids interpreting ordinary prose as math.
        start = self._buffer.find(b"(\\")
        if start >= 0:
            candidates.append(_Candidate(start, start + 1, b")", False, "normalized-paren"))

        if self.parse_dollars:
            start = 0
            while True:
                start = self._buffer.find(b"$", start)
                if start < 0:
                    break
                if _escaped(self._buffer, start):
                    start += 1
                    continue
                if self._buffer[start : start + 2] == b"$$":
                    candidates.append(_Candidate(start, start + 2, b"$$", True, "dollar"))
                    break
                # A single-dollar opener may not be followed by whitespace.
                if start + 1 == len(self._buffer):
                    candidates.append(_Candidate(start, start + 1, b"$", False, "dollar"))
                    break
                next_character = chr(self._buffer[start + 1])
                # Treat $12.50 as currency, not as the start of an equation.
                # Purely numeric math remains available through \(...\).
                if not next_character.isspace() and not next_character.isdigit():
                    candidates.append(_Candidate(start, start + 1, b"$", False, "dollar"))
                    break
                start += 1
        return min(candidates, key=lambda item: item.start) if candidates else None

    def _find_loose_display_opener(self) -> _Candidate | None:
        start = 0
        while True:
            start = self._buffer.find(b"[", start)
            if start < 0:
                return None
            line_start = self._buffer.rfind(b"\n", 0, start) + 1
            prefix = _ANSI.sub(b"", bytes(self._buffer[line_start:start]))
            if prefix.strip(b" \t\r"):
                start += 1
                continue
            newline = self._buffer.find(b"\n", start + 1)
            if newline < 0:
                # Wait for the complete line. In particular, Codex often emits
                # the SGR reset after '[' in a later PTY read.
                return _Candidate(line_start, None, b"]", True, "normalized-display")
            suffix = _ANSI.sub(b"", bytes(self._buffer[start + 1 : newline]))
            if suffix.strip(b" \t\r") == b"":
                return _Candidate(line_start, newline + 1, b"]", True, "normalized-display")
            start += 1

    def _find_closer(self, candidate: _Candidate) -> tuple[int, int] | None:
        assert candidate.content_start is not None
        if candidate.kind == "normalized-display":
            return self._find_loose_display_closer(candidate.content_start)
        if candidate.kind == "normalized-paren":
            depth = 1
            for index in range(candidate.content_start, len(self._buffer)):
                if self._buffer[index] == 0x28 and not _escaped(self._buffer, index):
                    depth += 1
                elif self._buffer[index] == 0x29 and not _escaped(self._buffer, index):
                    depth -= 1
                    if depth == 0:
                        return index + 1, index
            return None

        offset = candidate.content_start
        while True:
            index = self._buffer.find(candidate.closer, offset)
            if index < 0:
                return None
            if _escaped(self._buffer, index):
                offset = index + 1
                continue
            if candidate.closer == b"$":
                if self._buffer[index : index + 2] == b"$$":
                    offset = index + 2
                    continue
                if index == candidate.content_start or chr(self._buffer[index - 1]).isspace():
                    offset = index + 1
                    continue
            return index + len(candidate.closer), index

    def _find_loose_display_closer(self, offset: int) -> tuple[int, int] | None:
        index = offset
        while True:
            index = self._buffer.find(b"]", index)
            if index < 0:
                return None
            line_start = self._buffer.rfind(b"\n", offset, index) + 1
            prefix = _ANSI.sub(b"", bytes(self._buffer[line_start:index]))
            if prefix.strip(b" \t\r"):
                index += 1
                continue
            newline = self._buffer.find(b"\n", index + 1)
            if newline < 0:
                suffix = bytes(self._buffer[index + 1 :])
                visible_suffix = _ANSI.sub(b"", suffix).strip(b" \t\r")
                if visible_suffix:
                    # An escape may be split across reads, so wait rather than
                    # flushing a delimiter that can still become valid.
                    if b"\x1b" in suffix:
                        return None
                    index += 1
                    continue
                end = len(self._buffer)
            else:
                suffix = _ANSI.sub(b"", bytes(self._buffer[index + 1 : newline]))
                if suffix.strip(b" \t\r"):
                    index += 1
                    continue
                end = newline
            if end < index + 1:
                index += 1
                continue
            content_end = line_start
            if content_end > offset and self._buffer[content_end - 1] == 0x0A:
                content_end -= 1
                if content_end > offset and self._buffer[content_end - 1] == 0x0D:
                    content_end -= 1
            return end, content_end

    @staticmethod
    def _plausible(math: str, candidate: _Candidate) -> bool:
        value = math.strip()
        if not value:
            return False
        if candidate.kind == "dollar" and candidate.closer == b"$" and re.fullmatch(r"[\d.,]+", value):
            return False
        if candidate.kind in {"normalized-display", "normalized-paren"} and "\\" not in value:
            return False
        return True
