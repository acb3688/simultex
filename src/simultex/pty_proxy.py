"""Run a child under a real pseudo-terminal and transform its output."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import tty
from collections.abc import Sequence
from types import FrameType
from typing import BinaryIO, Mapping, Protocol


class StreamTransform(Protocol):
    def feed(self, data: bytes) -> bytes: ...
    def finish(self) -> bytes: ...


class StreamObserver(Protocol):
    def feed(self, data: bytes) -> None: ...
    def resize(self, columns: int, rows: int) -> None: ...


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            written = os.write(fd, data)
        except InterruptedError:
            continue
        data = data[written:]


def _copy_window_size(source: int, target: int) -> None:
    try:
        size = fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _resize_transform(transform: StreamTransform | None, fd: int) -> None:
    resize = getattr(transform, "resize", None)
    if resize is None:
        return
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, _xpixel, _ypixel = struct.unpack("HHHH", packed)
    except OSError:
        return
    if rows and columns:
        resize(columns, rows)


def _resize_observer(observer: StreamObserver | None, fd: int) -> None:
    if observer is None:
        return
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, _xpixel, _ypixel = struct.unpack("HHHH", packed)
    except OSError:
        return
    if rows and columns:
        observer.resize(columns, rows)


def run_proxy(
    command: Sequence[str],
    transform: StreamTransform | None,
    raw_output: BinaryIO | None = None,
    observer: StreamObserver | None = None,
    child_env: Mapping[str, str] | None = None,
) -> int:
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(
                command[0],
                list(command),
                dict(os.environ if child_env is None else child_env),
            )
        except OSError as exc:
            print(f"simultex: cannot run {command[0]!r}: {exc}", file=sys.stderr)
        os._exit(127)

    input_fd = sys.stdin.fileno()
    output_fd = sys.stdout.fileno()
    _copy_window_size(input_fd, master)
    _resize_transform(transform, input_fd)
    _resize_observer(observer, input_fd)
    old_tty = None
    if os.isatty(input_fd):
        old_tty = termios.tcgetattr(input_fd)
        tty.setraw(input_fd)

    old_winch = signal.getsignal(signal.SIGWINCH)
    old_handlers: dict[int, signal.Handlers] = {}

    def resize(_signum: int, _frame: FrameType | None) -> None:
        _copy_window_size(input_fd, master)
        _resize_transform(transform, input_fd)
        _resize_observer(observer, input_fd)

    def forward(signum: int, _frame: FrameType | None) -> None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGWINCH, resize)
    for signum in (signal.SIGHUP, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)

    stdin_open = True
    try:
        while True:
            readers = [master]
            if stdin_open:
                readers.append(input_fd)
            try:
                ready, _, _ = select.select(readers, [], [])
            except InterruptedError:
                continue
            if input_fd in ready:
                data = os.read(input_fd, 65_536)
                if data:
                    _write_all(master, data)
                else:
                    stdin_open = False
            if master in ready:
                try:
                    data = os.read(master, 65_536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                if raw_output is not None:
                    raw_output.write(data)
                    raw_output.flush()
                if observer is not None:
                    observer.feed(data)
                _write_all(output_fd, transform.feed(data) if transform else data)
        if transform:
            _write_all(output_fd, transform.finish())
    finally:
        if old_tty is not None:
            termios.tcsetattr(input_fd, termios.TCSAFLUSH, old_tty)
        signal.signal(signal.SIGWINCH, old_winch)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        os.close(master)

    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1
