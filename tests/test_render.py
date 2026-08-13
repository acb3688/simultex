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

    def test_complete_document_uses_only_its_body(self):
        source = r"""\documentclass{article}
\usepackage{tikz}
\begin{document}
\[E=mc^2\]
\end{document}"""
        body, is_document = LatexRenderer._normalize_source(source)
        self.assertTrue(is_document)
        self.assertEqual(body, r"\[E=mc^2\]")

    def test_fenced_complete_document_renders_transparently(self):
        renderer = LatexRenderer()
        try:
            image = renderer.render(
                """```latex
\\documentclass{article}
\\usepackage{amsmath}
\\begin{document}
The result is
\\[\\int x^2\\,dx=\\frac{x^3}{3}+C.\\]
\\end{document}
```""",
                block=True,
            )
            self.assertTrue(image.has_alpha)
        finally:
            renderer.close()

    def test_document_shorthand_is_normalized(self):
        body, is_document = LatexRenderer._normalize_source(
            r"\document{article}\[x^2\]\end{document}"
        )
        self.assertTrue(is_document)
        self.assertEqual(body, r"\[x^2\]")

    def test_incomplete_document_is_rejected(self):
        with self.assertRaisesRegex(RenderError, "incomplete"):
            LatexRenderer._normalize_source(
                r"\documentclass{article}\begin{document}x"
            )


if __name__ == "__main__":
    unittest.main()
