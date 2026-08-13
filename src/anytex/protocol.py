"""Kitty graphics protocol encoding and terminal sizing."""

from __future__ import annotations

import base64
import fcntl
import os
import struct
import termios
from dataclasses import dataclass
from typing import Mapping

from .render import RenderedImage


def supports_kitty_graphics(env: Mapping[str, str]) -> bool:
    """Use conservative detection; false positives emit visible garbage."""
    term = env.get("TERM", "").lower()
    program = env.get("TERM_PROGRAM", "").lower()
    return bool(
        env.get("KITTY_WINDOW_ID")
        or env.get("GHOSTTY_RESOURCES_DIR")
        or "kitty" in term
        or program in {"kitty", "ghostty"}
    )


@dataclass(frozen=True)
class TerminalGeometry:
    columns: int = 80
    rows: int = 24
    cell_width: float = 9.0
    cell_height: float = 18.0

    @classmethod
    def from_fd(cls, fd: int) -> "TerminalGeometry":
        try:
            packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
            rows, columns, xpixel, ypixel = struct.unpack("HHHH", packed)
        except (OSError, ValueError):
            return cls()
        columns = columns or 80
        rows = rows or 24
        cell_width = xpixel / columns if xpixel else 9.0
        cell_height = ypixel / rows if ypixel else 18.0
        return cls(columns, rows, cell_width, cell_height)


class KittyGraphics:
    def __init__(
        self,
        geometry: TerminalGeometry,
        inline_rows: int = 1,
        max_rows: int = 12,
        chunk_size: int = 4096,
    ):
        self.geometry = geometry
        self.inline_rows = inline_rows
        self.max_rows = max_rows
        self.chunk_size = chunk_size
        self._image_id = 0

    def _next_image_id(self) -> int:
        self._image_id = self._image_id % 4_294_967_295 + 1
        return self._image_id

    def encode(self, image: RenderedImage, block: bool) -> bytes:
        rows = self._display_rows(image, block)
        columns = max(
            1,
            round((image.width / image.height) * rows * self.geometry.cell_height / self.geometry.cell_width),
        )
        max_columns = max(1, self.geometry.columns - 2)
        if columns > max_columns:
            rows = max(1, round(rows * max_columns / columns))
            columns = max_columns

        image_id = self._next_image_id()
        result = self._transmit(image, image_id, columns, rows, move_cursor=not block)
        if block:
            result += b"\r\n" * rows
        return result

    def encode_at(
        self,
        image: RenderedImage,
        *,
        block: bool,
        row_limit: int,
        column_limit: int,
    ) -> tuple[bytes, int, int, int]:
        """Encode a non-cursor-moving placement at the current cell."""
        rows = min(row_limit, self._display_rows(image, block))
        rows = max(1, rows)
        columns = max(
            1,
            round((image.width / image.height) * rows * self.geometry.cell_height / self.geometry.cell_width),
        )
        if columns > column_limit:
            columns = max(1, column_limit)
            rows = max(1, min(row_limit, round(
                columns * self.geometry.cell_width * image.height
                / (self.geometry.cell_height * image.width)
            )))
        image_id = self._next_image_id()
        return self._transmit(image, image_id, columns, rows, move_cursor=False), image_id, columns, rows

    def delete(self, image_id: int) -> bytes:
        sequence = f"\x1b_Ga=d,d=I,q=2,i={image_id};\x1b\\".encode("ascii")
        return self._wrap_tmux(sequence)

    def _transmit(
        self,
        image: RenderedImage,
        image_id: int,
        columns: int,
        rows: int,
        *,
        move_cursor: bool,
    ) -> bytes:
        payload = base64.b64encode(image.data)
        chunks = [payload[i : i + self.chunk_size] for i in range(0, len(payload), self.chunk_size)]
        result = bytearray()
        for index, chunk in enumerate(chunks):
            more = int(index < len(chunks) - 1)
            if index == 0:
                control = f"a=T,f=100,q=2,i={image_id},c={columns},r={rows},m={more}"
                if not move_cursor:
                    control += ",C=1"
            else:
                control = f"q=2,m={more}"
            sequence = b"\x1b_G" + control.encode("ascii") + b";" + chunk + b"\x1b\\"
            result.extend(self._wrap_tmux(sequence))
        return bytes(result)

    def _display_rows(self, image: RenderedImage, block: bool) -> int:
        if not block:
            return self.inline_rows
        native_rows = max(2, round(image.height / self.geometry.cell_height))
        return min(self.max_rows, native_rows)

    @staticmethod
    def _wrap_tmux(sequence: bytes) -> bytes:
        if "TMUX" not in os.environ:
            return sequence
        return b"\x1bPtmux;" + sequence.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
