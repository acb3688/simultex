"""Replay a raw PTY capture through the production screen overlay."""

from __future__ import annotations

import argparse

from simultex.protocol import KittyGraphics, TerminalGeometry
from simultex.render import LatexRenderer
from simultex.screen import ScreenLatexOverlay


class RecordingRenderer:
    def __init__(self, renderer: LatexRenderer):
        self.renderer = renderer
        self.calls: list[tuple[str, bool]] = []

    def render(self, math: str, block: bool):
        self.calls.append((math, block))
        return self.renderer.render(math, block)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    parser.add_argument("--columns", type=int, default=153)
    parser.add_argument("--rows", type=int, default=47)
    parser.add_argument("--chunk-size", type=int, default=257)
    args = parser.parse_args()

    geometry = TerminalGeometry(args.columns, args.rows, 12, 24)
    latex = LatexRenderer()
    renderer = RecordingRenderer(latex)
    overlay = ScreenLatexOverlay(renderer, KittyGraphics(geometry), geometry)
    raw = open(args.capture, "rb").read()
    output_size = 0
    transmissions = 0
    try:
        for offset in range(0, len(raw), args.chunk_size):
            output = overlay.feed(raw[offset : offset + args.chunk_size])
            output_size += len(output)
            transmissions += output.count(b"_Ga=T,f=100")
        final = overlay.finish()
        output_size += len(final)
        transmissions += final.count(b"_Ga=T,f=100")
    finally:
        latex.close()

    unique = {(math, block) for math, block in renderer.calls}
    print(f"input bytes: {len(raw)}")
    print(f"output bytes: {output_size}")
    print(f"image transmissions: {transmissions}")
    print(f"unique equations: {len(unique)}")
    for math, block in sorted(unique, key=lambda item: (not item[1], item[0])):
        print(f"{'block' if block else 'inline'}: {math!r}")


if __name__ == "__main__":
    main()
