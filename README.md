# anytex

`anytex` is a transparent PTY proxy that turns LaTeX printed by an interactive
program into inline terminal images. It is intended for tools such as Codex and
Claude Code, without requiring either tool to know anything about terminal
graphics.

The rendered equations are tightly cropped transparent PNGs. Display uses the
[Kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/),
which works in both Kitty and Ghostty.

## What works

- Kitty and Ghostty: inline and display equations
- Terminal.app: normal PTY proxying, but no images (Terminal.app has no supported
  inline-image protocol)
- Delimiters: `\(...\)`, `\[...\]`, `$$...$$`, and conservative `$...$`
- Codex-normalized delimiters: standalone `[`/`]` lines and TeX-containing
  parentheses such as `(\mathbf{u})`
- Delimiters split across arbitrary PTY reads
- Full-screen TUI output reconstructed with a VT screen emulator
- ANSI styling, cursor movement, erasing, scrolling, and synchronized repaints
- Window resizing and normal interactive input
- Transparent backgrounds and configurable equation color

## Install

Python 3.10 or newer, `latex`, and `dvipng` are required. Both TeX executables
are included in a normal TeX Live or MacTeX installation.

```sh
python3 -m pip install -e .
anytex --check
```

Run the check from Ghostty or Kitty. It compiles and displays a sample equation,
and also verifies that the generated PNG has an alpha channel.

## Use

Put `anytex` before the program you want to run:

```sh
anytex -- codex
anytex -- claude
anytex -- python -q
```

For a dark terminal, the default near-white equation color is appropriate. For
a light terminal, choose a dark color:

```sh
anytex --color 202020 -- codex
```

Useful diagnostics and adjustments:

```sh
anytex --check
anytex --graphics kitty --check       # force the protocol during diagnosis
anytex --verbose -- codex             # show the first and all later TeX errors
anytex --no-dollar -- codex            # avoid interpreting any $...$ as math
anytex --inline-rows 2 --max-rows 16 -- codex
anytex --capture-raw codex.raw -- codex # capture the actual PTY stream for diagnosis
anytex --parser stream -- some-command  # legacy parser for append-only output
```

`--graphics auto` is the default. Detection is deliberately conservative because
sending graphics escapes to an unsupported terminal prints garbage. In an
unrecognized but compatible terminal, use `--graphics kitty`.

## How it works

`anytex` gives the child a genuine controlling pseudo-terminal and forwards
keystrokes, output, signals, exit status, and window size. By default, it replays
the child's VT control language into an in-memory terminal grid. After each
synchronized TUI repaint, anytex locates complete equations in the reconstructed
screen, clears their source cells, and places images at the same row and column
without disturbing the child's cursor. This is necessary for Codex because its
apparently linear response is assembled through many disjoint screen updates.

A completed fragment is compiled in a temporary directory with shell escape
disabled, converted by `dvipng`, base64 chunked, and emitted as a positioned Kitty
image. Images are deleted and redrawn when the application repaints their cells.
The older append-only byte parser remains available as `--parser stream`.

## Delimiter behavior

Single dollars are ambiguous in terminal output. `anytex` will not treat a dollar
followed by whitespace or a digit as an opener, and will not render numeric-only
contents. Thus prices such as `$12.50` remain text. Use `\(42\)` when a numeric-only
inline expression should render, or `--no-dollar` for strictly unambiguous parsing.

Codex can consume the backslashes in math delimiters while rendering Markdown to
the terminal. For that output, anytex also recognizes `[` and `]` when each is on
its own line and the enclosed text contains a TeX command. A parenthesized span is
recognized only when it begins with a TeX command, such as `(\rho)`; ordinary prose
like `(p)` is deliberately left alone.

If compilation fails, the source cells remain visible, so output is never silently
lost. The first error is reported; `--verbose` reports every error.
Raw captures can contain the full child conversation and terminal metadata; review
them before sharing. Existing files are never overwritten.

## Security and limitations

LaTeX comes from an untrusted child process. Rendering uses `-no-shell-escape`,
paranoid TeX file access, a temporary working directory, a timeout, and rejects
file-I/O and macro-construction primitives. This substantially narrows the attack
surface, but TeX is a large interpreter; do not treat this as a hardened sandbox
for hostile multi-tenant input.

Terminal images are cell placements, not font glyphs. Selection and copy/paste
therefore operate on the surrounding terminal text, not the equation pixels.
Full-screen applications can repaint or scroll an image's cells at any time.
Anytex tracks the reconstructed grid and redraws affected placements, but unusual
terminal extensions that `pyte` does not emulate may still cause temporary drift.

Inside tmux, anytex wraps graphics escapes for tmux passthrough. tmux may need
`set -g allow-passthrough on` in `~/.tmux.conf`.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/replay_capture.py /path/to/codex.raw
```

The test suite covers chunk boundaries, ANSI sequences, ambiguous dollars,
render-failure fallback, VT screen reconstruction, disjoint synchronized repaints,
transparent render safety, image lifecycle, placement, resizing, and terminal
detection. `anytex --check` is the local TeX integration test. The replay tool runs
captured TUI output through the production overlay without displaying its escapes.
