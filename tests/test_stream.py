import unittest

from simultex.stream import LatexStreamParser


class StreamParserTests(unittest.TestCase):
    def setUp(self):
        self.matches = []

        def replacement(source, math, block):
            self.matches.append((source, math, block))
            return b"<BLOCK>" if block else b"<INLINE>"

        self.replacement = replacement

    def test_passthrough_is_byte_exact(self):
        parser = LatexStreamParser(self.replacement)
        actual = parser.feed(b"hello \x1b[31mworld\x1b[0m\n") + parser.finish()
        self.assertEqual(actual, b"hello \x1b[31mworld\x1b[0m\n")

    def test_delimiters_can_cross_chunks(self):
        parser = LatexStreamParser(self.replacement)
        parts = [b"before \\", b"[x^", b"2 + y^2", b"\\] after"]
        actual = b"".join(parser.feed(part) for part in parts) + parser.finish()
        self.assertEqual(actual, b"before <BLOCK> after")
        self.assertEqual(self.matches[0][1:], ("x^2 + y^2", True))

    def test_all_supported_delimiters(self):
        parser = LatexStreamParser(self.replacement)
        source = b"\\(x\\) $y$ $$z$$ \\[w\\]"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, b"<INLINE> <INLINE> <BLOCK> <BLOCK>")

    def test_ansi_inside_math_is_not_sent_to_latex(self):
        parser = LatexStreamParser(self.replacement)
        parser.feed(b"\\[x\x1b[31m+ y\x1b[0m\\]")
        self.assertEqual(self.matches[0][1], "x+ y")

    def test_currency_and_shell_prompt_are_not_math(self):
        parser = LatexStreamParser(self.replacement)
        source = b"cost is $12.50 and prompt is $ then $42$"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, source)
        self.assertEqual(self.matches, [])

    def test_escaped_dollar_is_ignored(self):
        parser = LatexStreamParser(self.replacement)
        source = br"price \$5 and $x$"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, br"price \$5 and <INLINE>")

    def test_failed_render_preserves_source(self):
        parser = LatexStreamParser(lambda *_: None)
        source = br"keep \[bad\] exactly"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, source)

    def test_unterminated_candidate_is_flushed_on_finish(self):
        parser = LatexStreamParser(self.replacement)
        source = br"unfinished \[x + 1"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, source)

    def test_dollar_parsing_can_be_disabled(self):
        parser = LatexStreamParser(self.replacement, parse_dollars=False)
        source = b"$x$ \\(y\\)"
        actual = parser.feed(source) + parser.finish()
        self.assertEqual(actual, b"$x$ <INLINE>")

    def test_display_replacement_consumes_one_source_newline(self):
        parser = LatexStreamParser(self.replacement)
        actual = parser.feed(b"\\[x\\]\r") + parser.feed(b"\nnext") + parser.finish()
        self.assertEqual(actual, b"<BLOCK>next")

    def test_codex_normalized_delimiters(self):
        source = br"""For an incompressible fluid,
  [
  \rho\left(
  \frac{\partial \mathbf{u}}{\partial t}
  \right)

  -\nabla p + \mu\nabla^2\mathbf{u},
  ]

  with condition

  [
  \nabla\cdot\mathbf{u}=0.
  ]

  Here, (\mathbf{u}) is velocity, (p) pressure, (\rho) density,
  (\mu) viscosity, and (\mathbf{f}) force.
"""
        parser = LatexStreamParser(self.replacement)
        # Exercise arbitrary PTY boundaries, including the delimiter lines.
        transformed = b"".join(parser.feed(source[i : i + 7]) for i in range(0, len(source), 7))
        transformed += parser.finish()
        self.assertEqual(transformed.count(b"<BLOCK>"), 2)
        self.assertEqual(transformed.count(b"<INLINE>"), 4)
        self.assertIn(b"(p) pressure", transformed)
        blocks = [match for match in self.matches if match[2]]
        self.assertIn(r"\rho\left(", blocks[0][1])
        self.assertIn(r"\nabla\cdot\mathbf{u}=0.", blocks[1][1])

    def test_codex_styled_bracket_lines(self):
        parser = LatexStreamParser(self.replacement)
        chunks = [
            b"before\r\n\x1b[39m  \x1b[1m[",
            b"\x1b[22m\x1b[39m\r\n\\rho + (\\mathbf{u}\\cdot\\nabla)",
            b"\\mathbf{u}\r\n\x1b[39m  ]\x1b[0",
            b"m\r\nafter",
        ]
        transformed = b"".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        self.assertEqual(transformed.count(b"<BLOCK>"), 1)
        self.assertEqual(transformed.count(b"<INLINE>"), 0)
        self.assertIn(b"before\r\n<BLOCK>after", transformed)
        self.assertEqual(self.matches[0][1], r"\rho + (\mathbf{u}\cdot\nabla)\mathbf{u}")


if __name__ == "__main__":
    unittest.main()
