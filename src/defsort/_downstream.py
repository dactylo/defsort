"""Absorb closure of a downstream reader of standard output."""

import contextlib
import errno
import io
import os
import sys
from collections.abc import Generator, Iterable
from typing import TextIO, cast

_WINDOWS = sys.platform == 'win32'


class _ClosableTextOutput:
    """Discard writes after the downstream reader closes."""

    def __init__(self, stream: TextIO) -> None:
        """Wrap a text stream."""
        self._closed_downstream = False
        self._stream = stream

    def __getattr__(self, name: str) -> object:
        """Return an attribute of the wrapped stream."""
        return getattr(self._stream, name)

    @property
    def closed_downstream(self) -> bool:
        """Whether the downstream reader has closed the stream."""
        return self._closed_downstream

    def flush(self) -> None:
        """Flush the wrapped stream unless its reader has closed.

        Raises:
            OSError: If flushing fails for a reason other than closure.
        """
        if self._closed_downstream:
            return
        try:
            self._stream.flush()
        except OSError as error:
            if not _is_downstream_closure(error):
                raise
            self._closed_downstream = True

    def write(self, text: str) -> int:
        """Write text, or discard it after the reader has closed.

        Args:
            text: Text to write.

        Returns:
            The number of characters accepted.

        Raises:
            OSError: If writing fails for a reason other than closure.
        """
        if self._closed_downstream:
            return len(text)
        try:
            return self._stream.write(text)
        except OSError as error:
            if not _is_downstream_closure(error):
                raise
            self._closed_downstream = True
            return len(text)

    def writelines(self, lines: Iterable[str]) -> None:
        """Write lines until the reader closes, then stop consuming them.

        Args:
            lines: Text lines in output order.

        Raises:
            OSError: If writing fails for a reason other than closure.
        """
        if self._closed_downstream:
            return
        for line in lines:
            self.write(line)
            if self._closed_downstream:
                return


def _is_downstream_closure(error: OSError) -> bool:
    """Report whether the downstream reader closed the stream."""
    # CPython on Windows can report a closed pipe as EINVAL.
    return isinstance(error, BrokenPipeError) or (_WINDOWS and error.errno == errno.EINVAL)


def _silence_descriptor(stream: TextIO) -> None:
    """Point a stream's descriptor at the null device when it has one."""
    try:
        target = stream.fileno()
    except (OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, target)
    finally:
        os.close(devnull)


@contextlib.contextmanager
def closable_stdout() -> Generator[_ClosableTextOutput]:
    """Route standard output through a closure-absorbing wrapper.

    On every exit, including `SystemExit` from argument parsing, pending output
    is flushed through the wrapper and the original stream is restored. If the
    reader closed and the original stream has a usable descriptor, that
    descriptor is pointed at the null device so the interpreter's final flush
    cannot fail again. Streams without a usable descriptor are left unchanged.

    Yields:
        The installed wrapper.
    """
    original = sys.stdout
    output = _ClosableTextOutput(original)
    sys.stdout = cast('TextIO', output)
    try:
        yield output
    finally:
        try:
            output.flush()
        finally:
            sys.stdout = original
            if output.closed_downstream:
                _silence_descriptor(original)


def ensure_utf8_stdio() -> None:
    """Reconfigure stdio: UTF-8 on Windows, escaped stdout everywhere.

    Standard output renders unencodable characters (for example
    surrogateescaped filesystem names) with `backslashreplace`, matching the
    standard-error default, instead of raising `UnicodeEncodeError`. Windows
    additionally forces UTF-8 for all three streams, preserving each existing
    error handler where it is not overridden here.
    """
    # PEP 686 makes UTF-8 mode the default in Python 3.15; remove Windows
    # forcing once requires-python is >=3.15. Keep stdout's backslashreplace
    # because UTF-8 mode uses surrogateescape.
    stdout = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        if sys.platform == 'win32':
            stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
        else:
            stdout.reconfigure(errors='backslashreplace')
    if sys.platform != 'win32':
        return
    stdin = sys.stdin
    if isinstance(stdin, io.TextIOWrapper):
        stdin.reconfigure(encoding='utf-8', errors=stdin.errors)
    stderr = sys.stderr
    if isinstance(stderr, io.TextIOWrapper):
        stderr.reconfigure(encoding='utf-8', errors=stderr.errors)


def stdout_closed() -> bool:
    """Report whether standard output's downstream reader has closed.

    Returns:
        Whether the installed wrapper has observed closure.
    """
    output = sys.stdout
    return isinstance(output, _ClosableTextOutput) and output.closed_downstream
