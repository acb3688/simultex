"""Command-line entry point for anytex."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .browser import BrowserCompanion
from .protocol import KittyGraphics, TerminalGeometry, supports_kitty_graphics
from .pty_proxy import StreamObserver, StreamTransform, run_proxy
from .render import LatexRenderer, RenderError
from .screen import ScreenLatexOverlay
from .stream import LatexStreamParser


def _hex_color(value: str) -> str:
    color = value.removeprefix("#")
    if len(color) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in color):
        raise argparse.ArgumentTypeError("color must be a six-digit hex value")
    return color.upper()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anytex",
        description="Run a command in a PTY and replace LaTeX with terminal images.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--graphics",
        choices=("auto", "kitty", "none"),
        default="auto",
        help="terminal graphics protocol (default: auto)",
    )
    parser.add_argument(
        "--color",
        type=_hex_color,
        default="E6EDF3",
        metavar="RRGGBB",
        help="equation foreground color (default: E6EDF3)",
    )
    parser.add_argument("--dpi", type=int, default=180, help="rendering DPI (default: 180)")
    parser.add_argument(
        "--inline-rows",
        type=int,
        default=1,
        metavar="N",
        help="height of inline equations in terminal rows (default: 1)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=12,
        metavar="N",
        help="maximum height of display equations (default: 12)",
    )
    parser.add_argument(
        "--no-dollar",
        action="store_true",
        help=r"disable $...$ recognition",
    )
    parser.add_argument(
        "--parser",
        choices=("screen", "stream"),
        default="screen",
        help="reconstruct the VT screen or parse raw output (default: screen)",
    )
    parser.add_argument(
        "--keep-latex",
        action="store_true",
        help="keep the source LaTeX before each rendered image",
    )
    parser.add_argument("--check", action="store_true", help="check TeX and graphics support, then exit")
    parser.add_argument("--verbose", action="store_true", help="report render failures")
    parser.add_argument(
        "--capture-raw",
        type=Path,
        metavar="PATH",
        help="capture the child's raw PTY output for diagnosis (refuses to overwrite)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="mirror unchanged output to a read-only localhost rich transcript",
    )
    parser.add_argument(
        "--browser-port",
        type=int,
        default=0,
        metavar="PORT",
        help="localhost port for --browser (default: choose an available port)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run")
    return parser


def _graphics_enabled(choice: str) -> bool:
    if choice == "kitty":
        return True
    if choice == "none":
        return False
    return supports_kitty_graphics(os.environ)


def _check(args: argparse.Namespace) -> int:
    latex = shutil.which("latex")
    dvipng = shutil.which("dvipng")
    capable = supports_kitty_graphics(os.environ)
    print(f"latex:  {latex or 'not found'}")
    print(f"dvipng: {dvipng or 'not found'}")
    print(f"terminal graphics: {'Kitty protocol detected' if capable else 'not detected'}")
    if not latex or not dvipng:
        return 1

    renderer = LatexRenderer(color=args.color, dpi=args.dpi)
    try:
        image = renderer.render(r"\int_{-\infty}^{\infty} e^{-x^2}\,dx=\sqrt{\pi}", block=True)
    except RenderError as exc:
        print(f"test render failed: {exc}", file=sys.stderr)
        return 1
    finally:
        renderer.close()

    alpha = "RGBA/transparent" if image.has_alpha else "no alpha channel"
    print(f"test image: {image.width}x{image.height}, {alpha}")
    if args.graphics == "kitty" or capable:
        geometry = TerminalGeometry.from_fd(sys.stdout.fileno())
        sys.stdout.buffer.write(KittyGraphics(geometry).encode(image, block=True))
        sys.stdout.buffer.flush()
    else:
        print("Run this check in Kitty or Ghostty to display the test equation.")
    return 0 if image.has_alpha else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 50 <= args.dpi <= 600:
        raise SystemExit("anytex: --dpi must be between 50 and 600")
    if not 1 <= args.inline_rows <= 4:
        raise SystemExit("anytex: --inline-rows must be between 1 and 4")
    if not 1 <= args.max_rows <= 50:
        raise SystemExit("anytex: --max-rows must be between 1 and 50")
    if not 0 <= args.browser_port <= 65535:
        raise SystemExit("anytex: --browser-port must be between 0 and 65535")
    if args.keep_latex and args.parser == "screen":
        raise SystemExit("anytex: --keep-latex requires --parser stream")
    if args.check:
        return _check(args)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        build_parser().print_help(sys.stderr)
        return 2

    if args.browser:
        try:
            companion = BrowserCompanion(
                args.browser_port,
                parse_dollars=not args.no_dollar,
            )
        except OSError as exc:
            raise SystemExit(f"anytex: cannot start browser companion: {exc}") from None
        with companion:
            print(f"anytex: browser companion: {companion.url}", file=sys.stderr)
            time.sleep(10)
            return _run(
                command,
                transform=None,
                capture_path=args.capture_raw,
                observer=companion,
            )

    enabled = _graphics_enabled(args.graphics)
    if not enabled:
        if args.graphics == "auto":
            print(
                "anytex: this terminal has no supported inline-image protocol; "
                "passing output through unchanged (use Kitty or Ghostty)",
                file=sys.stderr,
            )
        return _run(command, transform=None, capture_path=args.capture_raw)

    renderer = LatexRenderer(color=args.color, dpi=args.dpi)
    geometry = TerminalGeometry.from_fd(sys.stdout.fileno())
    graphics = KittyGraphics(
        geometry,
        inline_rows=args.inline_rows,
        max_rows=args.max_rows,
    )
    warned = False

    def replace(source: bytes, math: str, block: bool) -> bytes | None:
        nonlocal warned
        try:
            image = renderer.render(math, block)
        except RenderError as exc:
            if args.verbose or not warned:
                print(f"\r\nanytex: could not render equation: {exc}\r", file=sys.stderr)
                warned = True
            return None
        rendered = graphics.encode(image, block)
        return source + rendered if args.keep_latex else rendered

    if args.parser == "stream":
        transform: StreamTransform = LatexStreamParser(replace, parse_dollars=not args.no_dollar)
    else:
        def screen_error(exc: RenderError) -> None:
            nonlocal warned
            if args.verbose or not warned:
                print(f"\r\nanytex: could not render equation: {exc}\r", file=sys.stderr)
                warned = True

        transform = ScreenLatexOverlay(
            renderer,
            graphics,
            geometry,
            parse_dollars=not args.no_dollar,
            on_error=screen_error,
        )
    try:
        return _run(command, transform=transform, capture_path=args.capture_raw)
    finally:
        renderer.close()


def _run(
    command: list[str],
    transform: StreamTransform | None,
    capture_path: Path | None,
    observer: StreamObserver | None = None,
) -> int:
    if capture_path is None:
        return run_proxy(command, transform=transform, observer=observer)
    try:
        capture = capture_path.open("xb")
    except FileExistsError:
        raise SystemExit(f"anytex: capture file already exists: {capture_path}") from None
    except OSError as exc:
        raise SystemExit(f"anytex: cannot create capture file {capture_path}: {exc}") from None
    print(f"anytex: capturing raw child output in {capture_path}", file=sys.stderr)
    try:
        return run_proxy(command, transform=transform, raw_output=capture, observer=observer)
    finally:
        capture.close()


if __name__ == "__main__":
    raise SystemExit(main())
