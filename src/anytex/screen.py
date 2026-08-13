"""Screen-aware LaTeX overlays for full-screen terminal applications."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import pyte

from .protocol import KittyGraphics, TerminalGeometry
from .render import LatexRenderer, RenderError


@dataclass(frozen=True)
class EquationRegion:
    math: str
    block: bool
    row: int
    column: int
    spans: tuple[tuple[int, int, int], ...]

    @property
    def rows(self) -> frozenset[int]:
        return frozenset(row for row, _column, _width in self.spans)

    @property
    def height(self) -> int:
        return max(self.rows) - min(self.rows) + 1


@dataclass(frozen=True)
class _Placement:
    region: EquationRegion
    image_id: int


_NORMALIZED_INLINE = re.compile(r"\((\\[A-Za-z]+(?:[^()]|\\[()])*)\)")
_EXPLICIT_INLINE = re.compile(r"\\\((.+?)\\\)")
_DOLLAR_INLINE = re.compile(r"(?<!\\)\$(?!\$)(\S(?:.*?\S)?)\$(?!\$)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def find_equations(lines: list[str], parse_dollars: bool = True) -> list[EquationRegion]:
    """Find equations in a reconstructed terminal grid."""
    regions: list[EquationRegion] = []
    claimed_rows: set[int] = set()
    openers = {"[": "]", r"\[": r"\]", "$$": "$$"}

    # Claim complete documents before their inner math delimiters. Claude often
    # emits these in fenced code blocks, including an occasional invalid
    # \document{article} shorthand. The renderer will discard the preamble.
    document_start = None
    document_end = None
    for index, line in enumerate(lines):
        token = line.strip()
        if document_start is None and (
            token.startswith(r"\documentclass")
            or token.startswith(r"\begin{document}")
            or token.startswith(r"\document{")
        ):
            document_start = index
        if document_start is not None and r"\end{document}" in token:
            document_end = index
            break
    if document_start is not None:
        if document_end is None:
            claimed_rows.update(range(document_start, len(lines)))
        else:
            start = document_start
            if start > 0 and lines[start - 1].strip().lower() in {"```latex", "```tex", "```"}:
                start -= 1
            end = document_end
            if end + 1 < len(lines) and lines[end + 1].strip() == "```":
                end += 1
            source = "\n".join(lines[start : end + 1]).strip()
            nonempty = [line for line in lines[start : end + 1] if line.strip()]
            column = min(_indent(line) for line in nonempty)
            spans = tuple(
                (source_row, column, max(1, len(lines[source_row].rstrip()) - column))
                for source_row in range(start, end + 1)
                if lines[source_row].rstrip()
            )
            regions.append(EquationRegion(source, True, start, column, spans))
            claimed_rows.update(range(start, end + 1))

    row = 0
    while row < len(lines):
        if row in claimed_rows:
            row += 1
            continue
        token = lines[row].strip()
        closer = openers.get(token)
        if closer is None:
            row += 1
            continue
        end = row + 1
        while end < len(lines) and lines[end].strip() != closer:
            end += 1
        if end >= len(lines):
            if any("\\" in line for line in lines[row + 1 :]):
                # A TUI often paints a display one row at a time. Do not
                # mistake parenthesized terms inside that unfinished display
                # for standalone inline equations.
                claimed_rows.update(range(row, len(lines)))
            row += 1
            continue
        inner_lines = [line.strip() for line in lines[row + 1 : end]]
        math = "\n".join(inner_lines).strip()
        if not math or "\\" not in math:
            row += 1
            continue
        nonempty = [line for line in lines[row : end + 1] if line.strip()]
        column = min(_indent(line) for line in nonempty)
        spans = tuple(
            (source_row, column, max(1, len(lines[source_row].rstrip()) - column))
            for source_row in range(row, end + 1)
            if lines[source_row].rstrip()
        )
        regions.append(EquationRegion(math, True, row, column, spans))
        claimed_rows.update(range(row, end + 1))
        row = end + 1

    patterns = [_EXPLICIT_INLINE, _NORMALIZED_INLINE]
    if parse_dollars:
        patterns.append(_DOLLAR_INLINE)
    for row, line in enumerate(lines):
        if row in claimed_rows:
            continue
        occupied: list[tuple[int, int]] = []
        for pattern in patterns:
            for match in pattern.finditer(line):
                start, end = match.span()
                if any(start < used_end and end > used_start for used_start, used_end in occupied):
                    continue
                math = match.group(1).strip()
                if not math or (pattern is _DOLLAR_INLINE and re.fullmatch(r"[\d.,]+", math)):
                    continue
                regions.append(
                    EquationRegion(math, False, row, start, ((row, start, end - start),))
                )
                occupied.append((start, end))
    return regions


class ScreenLatexOverlay:
    """Pass through VT output while placing images over reconstructed math."""

    _SYNC_START = b"\x1b[?2026h"
    _SYNC_END = b"\x1b[?2026l"

    def __init__(
        self,
        renderer: LatexRenderer,
        graphics: KittyGraphics,
        geometry: TerminalGeometry,
        *,
        parse_dollars: bool = True,
        on_error: Callable[[RenderError], None] | None = None,
    ):
        self.renderer = renderer
        self.graphics = graphics
        self.parse_dollars = parse_dollars
        self.on_error = on_error
        self.screen = pyte.Screen(geometry.columns, geometry.rows)
        self.stream = pyte.ByteStream(self.screen)
        self.screen.dirty.clear()
        self._placements: dict[EquationRegion, _Placement] = {}
        self._failed: set[EquationRegion] = set()
        self._pending = bytearray()
        self._saw_sync = False

    def resize(self, columns: int, rows: int) -> None:
        self.screen.resize(lines=rows, columns=columns)
        self.graphics.geometry = TerminalGeometry(
            columns,
            rows,
            self.graphics.geometry.cell_width,
            self.graphics.geometry.cell_height,
        )

    def feed(self, data: bytes) -> bytes:
        """Preserve synchronized-update boundaries while injecting overlays.

        A single PTY read can contain several complete Codex frames plus the
        beginning of another one. Each frame must be reconciled against the
        screen state at *its* boundary; batching the whole read makes overlays
        use future coordinates and is especially destructive during scrolling.
        """
        self._pending.extend(data)
        output = bytearray()
        while True:
            boundary = self._pending.find(self._SYNC_END)
            if boundary < 0:
                break
            end = boundary + len(self._SYNC_END)
            segment = bytes(self._pending[:end])
            del self._pending[:end]
            self.stream.feed(segment)
            if self._SYNC_START in segment:
                self._saw_sync = True
            commands = self._reconcile_commands()
            # Insert our repaint before Codex commits its synchronized frame.
            # Deletion, child scrolling, source clearing, and replacement image
            # placement therefore become visible as one atomic update.
            output.extend(segment[:-len(self._SYNC_END)])
            output.extend(self._preserve_cursor(commands))
            output.extend(self._SYNC_END)

        # Retain the longest possible split control-sequence prefix. Everything
        # before it is safe to feed and forward immediately.
        keep = max(len(self._SYNC_START), len(self._SYNC_END)) - 1
        if len(self._pending) > keep:
            count = len(self._pending) - keep
            segment = bytes(self._pending[:count])
            del self._pending[:count]
            self.stream.feed(segment)
            if self._SYNC_START in segment:
                self._saw_sync = True
            output.extend(segment)
            if not self._saw_sync and (b"\n" in segment or b"\r" in segment):
                output.extend(self._wrap_repaint(self._reconcile_commands()))
        return bytes(output)

    def finish(self) -> bytes:
        output = bytearray(self._pending)
        if self._pending:
            self.stream.feed(bytes(self._pending))
            self._pending.clear()
        output.extend(self._wrap_repaint(self._reconcile_commands()))
        return bytes(output)

    def _reconcile_commands(self) -> bytes:
        regions = set(find_equations(self.screen.display, self.parse_dollars))
        dirty = set(self.screen.dirty)
        commands = bytearray()
        retained: dict[EquationRegion, _Placement] = {}

        for region, placement in self._placements.items():
            if region in regions and region.rows.isdisjoint(dirty):
                retained[region] = placement
            else:
                commands.extend(self.graphics.delete(placement.image_id))

        for region in sorted(regions, key=lambda item: (item.row, item.column, item.block)):
            if region in retained or region in self._failed:
                continue
            try:
                image = self.renderer.render(region.math, region.block)
            except RenderError as exc:
                self._failed.add(region)
                if self.on_error is not None:
                    self.on_error(exc)
                continue

            commands.extend(self._clear(region))
            commands.extend(self._move(region.row, region.column))
            encoded, image_id, _columns, _rows = self.graphics.encode_at(
                image,
                block=region.block,
                row_limit=region.height if region.block else 1,
                column_limit=(
                    max(1, self.screen.columns - region.column)
                    if region.block
                    else max(width for _row, _column, width in region.spans)
                ),
            )
            commands.extend(encoded)
            retained[region] = _Placement(region, image_id)

        self._placements = retained
        self.screen.dirty.clear()
        return bytes(commands)

    @staticmethod
    def _preserve_cursor(commands: bytes) -> bytes:
        return b"\x1b7" + commands + b"\x1b8" if commands else b""

    def _wrap_repaint(self, commands: bytes) -> bytes:
        if not commands:
            return b""
        return self._SYNC_START + self._preserve_cursor(commands) + self._SYNC_END

    @staticmethod
    def _move(row: int, column: int) -> bytes:
        return f"\x1b[{row + 1};{column + 1}H".encode("ascii")

    def _clear(self, region: EquationRegion) -> bytes:
        result = bytearray(b"\x1b[0m")
        for row, column, width in region.spans:
            result.extend(self._move(row, column))
            result.extend(b" " * width)
        return bytes(result)
