import unittest

from anytex.protocol import KittyGraphics, TerminalGeometry
from anytex.render import RenderedImage
from anytex.screen import ScreenLatexOverlay, find_equations


class _Renderer:
    def __init__(self):
        self.calls = []

    def render(self, math, block):
        self.calls.append((math, block))
        return RenderedImage(b"png", width=30, height=15, has_alpha=True)


class EquationDetectionTests(unittest.TestCase):
    def test_detects_blocks_and_unambiguous_inline_math(self):
        lines = [
            "  [",
            r"  \rho + (\mathbf{u}\cdot\nabla)\mathbf{u}",
            "  ]",
            r"Here, (\mathbf{u}) is velocity, (p) pressure, and (\rho) density.",
        ]
        regions = find_equations(lines)
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0].math, r"\rho + (\mathbf{u}\cdot\nabla)\mathbf{u}")
        self.assertTrue(regions[0].block)
        self.assertEqual([(item.math, item.column) for item in regions[1:]], [
            (r"\mathbf{u}", 6),
            (r"\rho", 50),
        ])

    def test_does_not_claim_a_normal_bracketed_list(self):
        self.assertEqual(find_equations(["[", "ordinary text", "]"]), [])

    def test_suppresses_inline_math_inside_unfinished_block(self):
        regions = find_equations(["  [", r"  \rho+(\mathbf{u}\cdot\nabla)"])
        self.assertEqual(regions, [])


class ScreenOverlayTests(unittest.TestCase):
    def setUp(self):
        self.geometry = TerminalGeometry(80, 24, 10, 20)
        self.renderer = _Renderer()
        self.overlay = ScreenLatexOverlay(
            self.renderer,
            KittyGraphics(self.geometry),
            self.geometry,
        )

    def test_reconstructs_equation_across_disjoint_repaints(self):
        frames = [
            b"\x1b[?2026h\x1b[5;3H[\x1b[?2026l",
            b"\x1b[?2026h\x1b[6;3H\\rho+\\mu\x1b[?2026l",
            b"\x1b[?2026h\x1b[7;3H]\x1b[?2026l",
        ]
        first = self.overlay.feed(frames[0])
        second = self.overlay.feed(frames[1])
        third = self.overlay.feed(frames[2])
        self.assertNotIn(b"_Ga=T", first + second)
        self.assertIn(b"_Ga=T,f=100", third)
        self.assertEqual(self.renderer.calls, [(r"\rho+\mu", True)])
        self.assertIn(b"\x1b[5;3H", third)

    def test_redraw_deletes_stale_placement(self):
        self.overlay.feed(
            b"\x1b[?2026h\x1b[5;3H[\x1b[6;3H\\rho\x1b[7;3H]\x1b[?2026l"
        )
        output = self.overlay.feed(b"\x1b[?2026h\x1b[5;3H\x1b[2K\x1b[?2026l")
        self.assertIn(b"a=d,d=I", output)

    def test_resize_updates_virtual_grid(self):
        self.overlay.resize(120, 40)
        self.assertEqual(self.overlay.screen.columns, 120)
        self.assertEqual(self.overlay.screen.lines, 40)


if __name__ == "__main__":
    unittest.main()
