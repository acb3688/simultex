import unittest

from anytex.render import LatexRenderer, RenderError


class RendererTests(unittest.TestCase):
    def test_display_math_uses_box_safe_display_style(self):
        body = LatexRenderer._math_body(r"\frac{a}{b}", block=True)
        self.assertEqual(body, r"$\displaystyle \frac{a}{b}$")

    def test_align_is_converted_to_aligned(self):
        body = LatexRenderer._math_body(
            r"\begin{align}x&=1\\y&=2\end{align}", block=True
        )
        self.assertIn(r"\begin{aligned}", body)
        self.assertIn(r"\end{aligned}", body)

    def test_unsafe_input_is_rejected_before_tex(self):
        renderer = LatexRenderer()
        try:
            with self.assertRaisesRegex(RenderError, "unsafe command"):
                renderer.render(r"\input{/etc/passwd}", block=False)
        finally:
            renderer.close()

    def test_normal_environment_end_is_allowed(self):
        renderer = LatexRenderer()
        try:
            # This reaches TeX (rather than being rejected by the safety filter)
            # and is representative of matrices emitted by language models.
            image = renderer.render(r"\begin{matrix}a&b\\c&d\end{matrix}", block=True)
            self.assertTrue(image.has_alpha)
        finally:
            renderer.close()

    def test_markdown_blank_lines_inside_display_are_renderable(self):
        renderer = LatexRenderer()
        try:
            image = renderer.render(
                "\\rho\\left(\\frac{\\partial u}{\\partial t}\\right)\n\n-\\nabla p",
                block=True,
            )
            self.assertTrue(image.has_alpha)
        finally:
            renderer.close()


if __name__ == "__main__":
    unittest.main()
