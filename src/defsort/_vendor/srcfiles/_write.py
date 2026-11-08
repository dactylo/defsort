"""Write existing files in place and attempt restoration after write errors."""

import contextlib
import errno
import os
import stat
import sys
from pathlib import Path

from ._errors import FileChangedError, RestoreError
from ._recovery import save_recovery
from ._signals import deferred_signals

if sys.platform == 'win32':
    _OPEN_FLAGS = os.O_RDWR | os.O_BINARY
else:
    _OPEN_FLAGS = os.O_RDWR
_READ_SIZE = 1 << 16


def _close_quietly(fd: int) -> None:
    """Close a descriptor nothing was written through, or whose failure is already reported."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _describe(error: BaseException) -> str:
    """Return an error's reason without its number or file name."""
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__


def _read_all(fd: int) -> bytes:
    """Read a descriptor from its start to its end."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, _READ_SIZE):
        chunks.append(chunk)
    return b''.join(chunks)


def _check_target(fd: int, path: Path, data: bytes, expected: bytes) -> bool:
    """Return whether the target already holds data, refusing a non-regular or changed file."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise OSError(errno.EINVAL, 'not a regular file; not written')
    current = _read_all(fd)
    if current == data:
        return True
    if current != expected:
        raise FileChangedError(path)
    return False


def _save_original(tool: str, path: Path, original: bytes) -> tuple[Path | None, str]:
    """Save the original bytes and describe where they are, or why they are not."""
    try:
        recovery_path = save_recovery(tool, path, original)
    except (OSError, RuntimeError) as error:
        return None, f'the original could not be saved ({_describe(error)})'
    return recovery_path, f'the original is saved in {recovery_path}'


def _close_written(
    fd: int, path: Path, expected: bytes, tool: str, error: BaseException | None
) -> None:
    """Close the target after writing to it, and report a close error with the original saved."""
    # `error` is the failed write whose original was written back, or None after a whole write.
    try:
        os.close(fd)
    except OSError as close_error:
        # The path may name another file by now, so the original is saved, never written back.
        recovery_path, saved = _save_original(tool, path, expected)
        if error is None:
            message = (
                f'closing the file failed ({_describe(close_error)}), so the new content may '
                f'not have been stored; {saved}'
            )
            raise RestoreError(close_error.errno, message, recovery_path) from close_error
        message = (
            f'writing failed ({_describe(error)}); the original was written back, but closing '
            f'the file failed ({_describe(close_error)}), so it may not have been stored; {saved}'
        )
        if not isinstance(error, Exception):
            error.add_note(f'{tool}: {message}')
            return
        number = error.errno if isinstance(error, OSError) else None
        raise RestoreError(number, message, recovery_path) from error


def _termination_notice(tool: str, path: Path, error: BaseException) -> str | None:
    """Describe a failed restore for stderr, before a held termination signal ends the process."""
    if not isinstance(error, RestoreError):
        return None
    if error.recovery_path is None:
        saved = 'the original could not be saved'
    else:
        saved = f'the original is saved in {str(error.recovery_path)!a}'
    return f'{tool}: {str(path)!a} may be damaged; {saved}'


def _write_all(fd: int, data: bytes) -> None:
    """Write data from offset 0, continuing after short writes."""
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _restore(fd: int, path: Path, expected: bytes, tool: str, error: BaseException) -> bool:
    """Write the original bytes back after a failed write; return whether they are back."""
    # A failed restore raises, or returns False after noting itself on `error`, which propagates.
    try:
        _write_all(fd, expected)
        os.ftruncate(fd, len(expected))
    except BaseException as restore_error:
        recovery_path, saved = _save_original(tool, path, expected)
        message = (
            f'writing failed ({_describe(error)}) and restoring the original also failed '
            f'({_describe(restore_error)}); {saved}'
        )
        # An interrupt from a host's own handler keeps its type, so control flow is unchanged.
        if not isinstance(restore_error, Exception):
            restore_error.add_note(f'{tool}: {message}')
            raise
        if not isinstance(error, Exception):
            error.add_note(f'{tool}: {message}')
            return False
        number = error.errno if isinstance(error, OSError) else None
        raise RestoreError(number, message, recovery_path) from error
    return True


def _write_file(path: Path, data: bytes, expected: bytes, tool: str) -> None:
    """Open the target without truncating it and write data over the expected bytes."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError as error:
        raise FileChangedError(path, removed=True) from error
    try:
        written = _check_target(fd, path, data, expected)
    except BaseException:
        _close_quietly(fd)
        raise
    if written:
        _close_quietly(fd)
        return
    try:
        _write_all(fd, data)
        if len(data) < len(expected):
            os.ftruncate(fd, len(data))
    except BaseException as error:
        try:
            restored = _restore(fd, path, expected, tool, error)
        except BaseException:
            _close_quietly(fd)
            raise
        if restored:
            _close_written(fd, path, expected, tool, error)
        else:
            _close_quietly(fd)
        raise
    _close_written(fd, path, expected, tool, None)


def write_in_place(path: Path, data: bytes, *, expected: bytes, tool: str) -> None:
    """Replace an existing file's bytes through its own descriptor.

    The file is opened without truncation and written from its start, so its
    inode, hard links, owner, permissions, ACLs, and extended attributes stay
    as any ordinary write leaves them. The current bytes are read through the
    same descriptor and compared with `expected` first; a writer between that
    read and the write is not detected.

    On an error while writing or truncating, restoration of the original bytes
    is attempted. A close error instead attempts to save the original for
    recovery, without writing through a path that may now identify another
    file.

    For main-thread writes, Ctrl+C, and SIGTERM and SIGHUP on POSIX or
    Ctrl+Break on Windows, wait until the call finishes and then take effect,
    only with supported default handlers. Other threads and custom handlers
    receive no signal deferral. Failed restoration or uncertain close can leave
    partial content even when signals are deferred.

    Args:
        path: The file to write; a symlink writes its target.
        data: The new bytes.
        expected: The bytes the caller read from `path`. When equal to `data`,
            nothing is done.
        tool: The calling tool's name, used for recovery files and the stderr
            notice printed before a held termination signal ends the process.

    Raises:
        FileChangedError: If the file no longer holds `expected` or no longer
            exists. A file that already holds `data` counts as written.
        RestoreError: If the original could not be written back, or closing the
            file failed, so the file may hold neither version intact. Its
            `recovery_path` names the saved original, or is `None` when saving
            also failed; the error reports that failure.
        OSError: If the file cannot be opened, inspected, or read, is not a
            regular file, or writing failed and the original was written back.
        KeyboardInterrupt: If a held Ctrl+C takes effect after the operation.
        BaseException: If a custom signal handler interrupts writing or
            recovery. Control-flow exceptions keep their types; an unsuccessful
            restore or an uncertain close while handling one adds a recovery
            note.
    """
    if data == expected:
        return
    with deferred_signals(lambda error: _termination_notice(tool, path, error)):
        _write_file(path, data, expected, tool)
