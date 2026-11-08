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
    """Text output that discards writes after its downstream reader closes."""

    def __init__(self, stream: TextIO) -> None:
        """Wrap a text stream."""
        self.closed_downstream = False
        self._stream = stream

    def __getattr__(self, name: str) -> object:
        """Return an attribute of the wrapped stream."""
        return getattr(self._stream, name)

    def flush(self) -> None:
        """Flush the wrapped stream unless its reader has closed.

        Raises:
            OSError: If flushing fails for a reason other than closure.
        """
        if self.closed_downstream:
            return
        try:
            self._stream.flush()
        except OSError as error:
            if not _is_downstream_closure(error):
                raise
            self.closed_downstream = True

    def write(self, text: str) -> int:
        """Write text, or discard it after the reader has closed.

        Args:
            text: Text to write.

        Returns:
            The number of characters accepted.

        Raises:
            OSError: If writing fails for a reason other than closure.
        """
        if self.closed_downstream:
            return len(text)
        try:
            return self._stream.write(text)
        except OSError as error:
            if not _is_downstream_closure(error):
                raise
            self.closed_downstream = True
            return len(text)

    def write_bytes(self, data: bytes) -> bool:
        """Write bytes after pending text when a binary stream is available.

        Returns:
            Whether the wrapped stream supports byte output.

        Raises:
            OSError: If writing fails for a reason other than closure.
        """
        buffer = getattr(self._stream, 'buffer', None)
        if buffer is None:
            return False
        if self.closed_downstream:
            return True
        self.flush()
        if self.closed_downstream:
            return True
        try:
            _write_all_bytes(buffer, data)
        except OSError as error:
            if not _is_downstream_closure(error):
                raise
            self.closed_downstream = True
        return True

    def writelines(self, lines: Iterable[str]) -> None:
        """Write lines until the reader closes, then stop consuming them.

        Args:
            lines: Text lines in output order.

        Raises:
            OSError: If writing fails for a reason other than closure.
        """
        if self.closed_downstream:
            return
        for line in lines:
            self.write(line)
            if self.closed_downstream:
                return


def _is_downstream_closure(error: OSError) -> bool:
    """Report whether a write or flush failed because the reader closed the stream."""
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


def _write_all_bytes(buffer: io.BufferedIOBase | io.RawIOBase, data: bytes) -> None:
    """Complete byte output before flushing, or raise when writing cannot progress."""
    remaining = memoryview(data)
    while remaining:
        written = buffer.write(remaining)
        if written is None or written <= 0:
            raise BlockingIOError(errno.EAGAIN, 'byte output made no progress')
        remaining = remaining[written:]
    buffer.flush()


@contextlib.contextmanager
def closable_stdout() -> Generator[_ClosableTextOutput]:
    """Route standard output through a closure-absorbing wrapper.

    On every exit, including `SystemExit` from argument parsing, pending output
    is flushed through the wrapper and the original stream is restored. If the
    reader closed, the original descriptor is pointed at the null device so the
    interpreter's final flush cannot fail again. The closure latch remains
    because display functions write from many places, and the documented exit
    status reflects the work completed when the reader closes.

    Yields:
        The installed wrapper.

    Raises:
        OSError: If flushing fails for a reason other than downstream closure,
            or redirecting the closed descriptor to the null device fails.
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
    """Reconfigure supported standard streams to UTF-8 on every platform.

    Call before reading standard input and before replacing standard output
    with a wrapper. Only `TextIOWrapper` streams are changed. Input and error
    output retain their error handlers; output uses `backslashreplace` to
    escape invalid names.

    Raises:
        OSError: If flushing or reconfiguring a stream fails.
    """
    # Transitional: PEP 686 makes UTF-8 mode the default in Python 3.15. Remove this
    # function when requires-python is >=3.15; PYTHONUTF8=0 then remains a deliberate opt-out.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            errors = 'backslashreplace' if stream is sys.stdout else stream.errors
            stream.reconfigure(encoding='utf-8', errors=errors)


def stdout_closed() -> bool:
    """Report whether standard output's downstream reader has closed.

    Returns:
        Whether the installed wrapper has observed closure.
    """
    output = sys.stdout
    return isinstance(output, _ClosableTextOutput) and output.closed_downstream


def write_stdout_bytes(data: bytes) -> bool:
    """Write bytes to standard output after flushing any pending text.

    Complete short writes and flush the binary stream before returning.
    An installed closure-absorbing wrapper may discard bytes after its reader
    closes.

    Returns:
        `False` without writing when no binary interface exists; otherwise
        `True`, including when the installed wrapper discards the bytes.

    Raises:
        OSError: If flushing or writing fails without being absorbed as
            downstream closure, including when a byte write makes no progress.
    """
    output = sys.stdout
    if isinstance(output, _ClosableTextOutput):
        return output.write_bytes(data)
    buffer = getattr(output, 'buffer', None)
    if buffer is None:
        return False
    output.flush()
    _write_all_bytes(buffer, data)
    return True
