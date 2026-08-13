"""Turn a small, untrusted LaTeX fragment into a transparent PNG."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class RenderError(RuntimeError):
    """A LaTeX fragment could not safely be rendered."""


@dataclass(frozen=True)
class RenderedImage:
    data: bytes
    width: int
    height: int
    has_alpha: bool


_FORBIDDEN = re.compile(
    r"(?:\\(?:input|include|openin|openout|read|write|usepackage|documentclass|"
    r"special|immediate|catcode|csname|newread|newwrite|filecontents)\b|"
    r"\\(?:begin|end)\s*\{(?:document|filecontents\*?)\})",
    re.IGNORECASE,
)

_TEMPLATE = r"""\documentclass[border=2pt]{standalone}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{xcolor}
\begin{document}
\color[HTML]{COLOR}
BODY
\end{document}
"""


def _png_info(data: bytes) -> tuple[int, int, bool]:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RenderError("dvipng did not produce a valid PNG")
    width, height = struct.unpack(">II", data[16:24])
    color_type = data[25]
    return width, height, color_type in (4, 6)


class LatexRenderer:
    def __init__(self, color: str = "E6EDF3", dpi: int = 180, timeout: float = 12.0):
        self.color = color
        self.dpi = dpi
        self.timeout = timeout
        self._latex = shutil.which("latex")
        self._dvipng = shutil.which("dvipng")
        self._tmp = tempfile.TemporaryDirectory(prefix="anytex-")
        self._cache: dict[str, RenderedImage] = {}

    def close(self) -> None:
        self._tmp.cleanup()

    def render(self, math: str, block: bool) -> RenderedImage:
        if not self._latex or not self._dvipng:
            raise RenderError("latex and dvipng must both be installed")
        # Markdown renderers can introduce paragraph-like blank lines while
        # laying out a display equation. TeX rejects blank paragraphs in math
        # mode, so reduce them to ordinary source line breaks.
        math = math.replace("\r\n", "\n").replace("\r", "\n")
        math = re.sub(r"\n[ \t]*\n+", "\n", math)
        if not math.strip():
            raise RenderError("empty equation")
        if len(math.encode("utf-8")) > 16_384:
            raise RenderError("equation is too large")
        forbidden = _FORBIDDEN.search(math)
        if forbidden:
            raise RenderError(f"unsafe command {forbidden.group(0)!r}")

        key = hashlib.sha256(f"{self.color}:{self.dpi}:{block}:{math}".encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        body = self._math_body(math, block)
        job = Path(self._tmp.name) / key
        job.mkdir()
        tex = job / "formula.tex"
        tex.write_text(_TEMPLATE.replace("COLOR", self.color).replace("BODY", body), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(Path(self._latex).parent) + os.pathsep + "/usr/bin:/bin",
                "openin_any": "p",
                "openout_any": "p",
                "TEXMFHOME": str(job),
                "TEXMFOUTPUT": str(job),
            }
        )
        try:
            latex = subprocess.run(
                [
                    self._latex,
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "formula.tex",
                ],
                cwd=job,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError("latex timed out") from exc
        if latex.returncode != 0:
            detail = self._latex_error(latex.stdout)
            raise RenderError(detail)

        png_path = job / "formula.png"
        try:
            dvipng = subprocess.run(
                [
                    self._dvipng,
                    "-q",
                    "-T",
                    "tight",
                    "-D",
                    str(self.dpi),
                    "-bg",
                    "Transparent",
                    "-o",
                    str(png_path),
                    str(job / "formula.dvi"),
                ],
                cwd=job,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError("dvipng timed out") from exc
        if dvipng.returncode != 0 or not png_path.exists():
            detail = dvipng.stdout.decode("utf-8", "replace").strip().splitlines()
            raise RenderError(detail[-1] if detail else "dvipng failed")

        data = png_path.read_bytes()
        width, height, has_alpha = _png_info(data)
        if not has_alpha:
            raise RenderError("renderer returned an opaque PNG")
        image = RenderedImage(data, width, height, has_alpha)
        self._cache[key] = image
        return image

    @staticmethod
    def _math_body(math: str, block: bool) -> str:
        stripped = math.strip()
        # standalone typesets its contents in a box, where LaTeX's display-math
        # delimiters are invalid.  \displaystyle gives the same math styling
        # without entering vertical mode.
        if block:
            stripped = re.sub(r"^\\begin\{align\*?\}", r"\\begin{aligned}", stripped)
            stripped = re.sub(r"\\end\{align\*?\}$", r"\\end{aligned}", stripped)
            stripped = re.sub(r"^\\begin\{gather\*?\}", r"\\begin{gathered}", stripped)
            stripped = re.sub(r"\\end\{gather\*?\}$", r"\\end{gathered}", stripped)
            return "$\\displaystyle " + stripped + "$"
        return "$" + stripped + "$"

    @staticmethod
    def _latex_error(output: bytes) -> str:
        text = output.decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("!"):
                return line[1:].strip()
        return "latex failed"
