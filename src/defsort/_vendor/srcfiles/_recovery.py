"""Save a file's original bytes privately when a write leaves its content uncertain."""

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == 'darwin':
    import ctypes

_ACL_TYPE_EXTENDED = 0x00000100
_NAME_LIMIT = 32


def _clear_inherited_acl(directory_fd: int) -> None:
    """Replace a directory's ACL with an empty one on macOS, where ACL entries ignore the mode."""
    if sys.platform != 'darwin':
        return
    libc = ctypes.CDLL(None, use_errno=True)
    # acl_init returns a pointer, which the default int return type would truncate.
    libc.acl_init.restype = ctypes.c_void_p
    acl = ctypes.c_void_p(libc.acl_init(0))
    try:
        if libc.acl_set_fd_np(directory_fd, acl, _ACL_TYPE_EXTENDED) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    finally:
        libc.acl_free(acl)


def _make_private_directories(directory: Path) -> None:
    """Create a directory and its missing parents with mode 0700."""
    missing = [path for path in (directory, *directory.parents) if not path.exists()]
    for path in reversed(missing):
        path.mkdir(mode=0o700, exist_ok=True)


def _recovery_root(tool: str) -> Path:
    """Return the per-user directory that holds a tool's recovery directories."""
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        state = Path(os.environ.get('XDG_STATE_HOME', ''))
        base = state if state.is_absolute() else Path.home() / '.local' / 'state'
    return base / tool / 'recovery'


def _write_private(directory: Path, name: str, data: bytes) -> None:
    """Create a file in a new directory once nothing inherited can grant access, then write."""
    fd = None
    try:
        if sys.platform == 'win32':
            # os.mkdir with mode 0700 gave the directory a protected owner-only access list.
            fd = os.open(
                directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600
            )
        else:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _clear_inherited_acl(directory_fd)
                # Mode 0600 also zeroes the mask of ACL entries inherited on Linux.
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            finally:
                os.close(directory_fd)
        with os.fdopen(fd, 'wb', closefd=False) as file:
            file.write(data)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    else:
        os.close(fd)


def save_recovery(tool: str, target: Path, original: bytes) -> Path:
    r"""Save a target's original bytes in a new private per-user directory.

    The location is `%LOCALAPPDATA%\<tool>\recovery` on Windows, falling back
    to `~/AppData/Local/<tool>/recovery` when `LOCALAPPDATA` is unset or empty.
    It is `~/Library/Application Support/<tool>/recovery` on macOS and
    `$XDG_STATE_HOME/<tool>/recovery` elsewhere, where an unset, empty, or
    relative `XDG_STATE_HOME` means `~/.local/state`. Each file gets its own
    directory, named for the UTC time and made private before the file is
    created in it.

    Args:
        tool: The calling tool's name, used as a directory name.
        target: The file whose bytes are saved; only its name is used.
        original: The bytes to save.

    Returns:
        The new recovery file.

    Raises:
        OSError: If the file cannot be created privately or written. Removing
            the partial file and its directory is attempted but not guaranteed.
        RuntimeError: If the home directory cannot be determined.
    """
    root = _recovery_root(tool)
    _make_private_directories(root)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    directory = Path(tempfile.mkdtemp(prefix=f'{stamp}-', dir=root))
    path = directory / f'{target.name[:_NAME_LIMIT]}.orig'
    try:
        _write_private(directory, path.name, original)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()
        raise
    return path
