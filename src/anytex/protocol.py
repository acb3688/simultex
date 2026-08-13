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


# Kitty's official row/column diacritic table. These codepoints encode cell
# coordinates on U+10EEEE image placeholders. Keep the order unchanged.
_DIACRITIC_CODEPOINTS = """
0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357 035B 0363 0364 0365
0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484 0485 0486 0487 0592 0593 0594 0595 0597
0598 0599 059C 059D 059E 059F 05A0 05A1 05A8 05A9 05AB 05AC 05AF 05C4 0610 0611 0612 0613 0614 0615
0616 0617 0657 0658 0659 065A 065B 065D 065E 06D6 06D7 06D8 06D9 06DA 06DB 06DC 06DF 06E0 06E1 06E2
06E4 06E7 06E8 06EB 06EC 0730 0732 0733 0735 0736 073A 073D 073F 0740 0741 0743 0745 0747 0749 074A
07EB 07EC 07ED 07EE 07EF 07F0 07F1 07F3 0816 0817 0818 0819 081B 081C 081D 081E 081F 0820 0821 0822
0823 0825 0826 0827 0829 082A 082B 082C 082D 0951 0953 0954 0F82 0F83 0F86 0F87 135D 135E 135F 17DD
193A 1A17 1A75 1A76 1A77 1A78 1A79 1A7A 1A7B 1A7C 1B6B 1B6D 1B6E 1B6F 1B70 1B71 1B72 1B73 1CD0 1CD1
1CD2 1CDA 1CDB 1CE0 1DC0 1DC1 1DC3 1DC4 1DC5 1DC6 1DC7 1DC8 1DC9 1DCB 1DCC 1DD1 1DD2 1DD3 1DD4 1DD5
1DD6 1DD7 1DD8 1DD9 1DDA 1DDB 1DDC 1DDD 1DDE 1DDF 1DE0 1DE1 1DE2 1DE3 1DE4 1DE5 1DE6 1DFE 20D0 20D1
20D4 20D5 20D6 20D7 20DB 20DC 20E1 20E7 20E9 20F0 2CEF 2CF0 2CF1 2DE0 2DE1 2DE2 2DE3 2DE4 2DE5 2DE6
2DE7 2DE8 2DE9 2DEA 2DEB 2DEC 2DED 2DEE 2DEF 2DF0 2DF1 2DF2 2DF3 2DF4 2DF5 2DF6 2DF7 2DF8 2DF9 2DFA
2DFB 2DFC 2DFD 2DFE 2DFF A66F A67C A67D A6F0 A6F1 A8E0 A8E1 A8E2 A8E3 A8E4 A8E5 A8E6 A8E7 A8E8 A8E9
A8EA A8EB A8EC A8ED A8EE A8EF A8F0 A8F1 AAB0 AAB2 AAB3 AAB7 AAB8 AABE AABF AAC1 FE20 FE21 FE22 FE23
FE24 FE25 FE26 10A0F 10A38 1D185 1D186 1D187 1D188 1D189 1D1AA 1D1AB 1D1AC 1D1AD 1D242 1D243 1D244
"""
_DIACRITICS = tuple(chr(int(value, 16)) for value in _DIACRITIC_CODEPOINTS.split())
_PLACEHOLDER = "\U0010eeee"


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
        # The placeholder protocol carries the low 24 bits in foreground
        # color. Staying within that range avoids a third ID diacritic.
        self._image_id = self._image_id % 16_777_215 + 1
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

    def encode_virtual(
        self,
        image: RenderedImage,
        *,
        block: bool,
        row_limit: int,
        column_limit: int,
    ) -> tuple[bytes, int, int, int]:
        """Transmit an image as a scrollback-safe virtual placement."""
        rows = max(1, min(row_limit, self._display_rows(image, block)))
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
        if rows >= len(_DIACRITICS) or columns >= len(_DIACRITICS):
            raise ValueError("image placement exceeds Kitty's coordinate table")
        image_id = self._next_image_id()
        encoded = self._transmit(
            image, image_id, columns, rows, move_cursor=False, virtual=True
        )
        return encoded, image_id, columns, rows

    @staticmethod
    def placeholder_row(image_id: int, row: int, columns: int) -> bytes:
        """Return one row of explicit, text-cell-attached image placeholders."""
        red = (image_id >> 16) & 0xFF
        green = (image_id >> 8) & 0xFF
        blue = image_id & 0xFF
        cells = "".join(
            _PLACEHOLDER + _DIACRITICS[row] + _DIACRITICS[column]
            for column in range(columns)
        )
        return (
            f"\x1b[38;2;{red};{green};{blue}m".encode("ascii")
            + cells.encode("utf-8")
            + b"\x1b[39m"
        )

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
        virtual: bool = False,
    ) -> bytes:
        payload = base64.b64encode(image.data)
        chunks = [payload[i : i + self.chunk_size] for i in range(0, len(payload), self.chunk_size)]
        result = bytearray()
        for index, chunk in enumerate(chunks):
            more = int(index < len(chunks) - 1)
            if index == 0:
                control = f"a=T,f=100,q=2,i={image_id},c={columns},r={rows},m={more}"
                if virtual:
                    control += ",U=1"
                elif not move_cursor:
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
