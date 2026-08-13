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

    row = 0
    while row < len(lines):
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
        self._control_tail = b""
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
        self.stream.feed(data)
        controls = self._control_tail + data
        if self._SYNC_START in controls:
            self._saw_sync = True
        refresh = self._SYNC_END in controls or (not self._saw_sync and (b"\n" in data or b"\r" in data))
        self._control_tail = controls[-16:]
        if not refresh:
            return data
        return data + self._refresh()

    def finish(self) -> bytes:
        return self._refresh()

    def _refresh(self) -> bytes:
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
        if not commands:
            return b""
        return self._SYNC_START + b"\x1b7" + bytes(commands) + b"\x1b8" + self._SYNC_END

    @staticmethod
    def _move(row: int, column: int) -> bytes:
        return f"\x1b[{row + 1};{column + 1}H".encode("ascii")

    def _clear(self, region: EquationRegion) -> bytes:
        result = bytearray(b"\x1b[0m")
        for row, column, width in region.spans:
            result.extend(self._move(row, column))
            result.extend(b" " * width)
        return bytes(result)
