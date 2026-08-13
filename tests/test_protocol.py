import base64
import os
import unittest
from unittest.mock import patch

from anytex.protocol import KittyGraphics, TerminalGeometry, supports_kitty_graphics
from anytex.render import RenderedImage


class ProtocolTests(unittest.TestCase):
    def test_detection_is_conservative(self):
        self.assertTrue(supports_kitty_graphics({"TERM_PROGRAM": "ghostty"}))
        self.assertTrue(supports_kitty_graphics({"TERM": "xterm-kitty"}))
        self.assertFalse(supports_kitty_graphics({"TERM_PROGRAM": "Apple_Terminal"}))

    @patch.dict(os.environ, {}, clear=True)
    def test_png_is_chunked_and_block_reserves_rows(self):
        png = b"transparent png payload"
        image = RenderedImage(png, width=90, height=36, has_alpha=True)
        protocol = KittyGraphics(TerminalGeometry(80, 24, 9, 18), chunk_size=8)
        encoded = protocol.encode(image, block=True)
        sequences = encoded.split(b"\x1b_G")[1:]
        payload = bytearray()
        for sequence in sequences:
            body = sequence.split(b"\x1b\\", 1)[0]
            control, chunk = body.split(b";", 1)
            payload.extend(chunk)
        self.assertEqual(base64.b64decode(payload), png)
        self.assertIn(b"a=T,f=100", encoded)
        self.assertIn(b",C=1;", encoded)
        self.assertTrue(encoded.endswith(b"\r\n\r\n"))

    @patch.dict(os.environ, {}, clear=True)
    def test_inline_image_does_not_add_newline(self):
        image = RenderedImage(b"x", width=10, height=10, has_alpha=True)
        encoded = KittyGraphics(TerminalGeometry()).encode(image, block=False)
        self.assertFalse(encoded.endswith(b"\r\n"))
        self.assertNotIn(b",C=1", encoded)

    @patch.dict(os.environ, {}, clear=True)
    def test_positioned_image_does_not_move_cursor_and_respects_limits(self):
        image = RenderedImage(b"x", width=200, height=20, has_alpha=True)
        encoded, image_id, columns, rows = KittyGraphics(
            TerminalGeometry(80, 24, 10, 20)
        ).encode_at(image, block=False, row_limit=1, column_limit=8)
        self.assertEqual((image_id, columns, rows), (1, 8, 1))
        self.assertIn(b",c=8,r=1,", encoded)
        self.assertIn(b",C=1;", encoded)
        self.assertFalse(encoded.endswith(b"\r\n"))

    @patch.dict(os.environ, {}, clear=True)
    def test_delete_targets_only_the_owned_image_id(self):
        encoded = KittyGraphics(TerminalGeometry()).delete(42)
        self.assertEqual(encoded, b"\x1b_Ga=d,d=I,q=2,i=42;\x1b\\")


if __name__ == "__main__":
    unittest.main()
