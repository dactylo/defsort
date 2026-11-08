import contextlib
import errno
import os
import stat
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from defsort._vendor.srcfiles import (
    RestoreError,
    _recovery,
    write_in_place,
)

from ._support import (
    TOOL,
    TargetWrite,
    intercept_target_writes,
    redirect_recovery,
)

_ACL_GROUP_OBJ = 0x04
_ACL_MASK = 0x10
_ACL_NO_ID = 0xFFFFFFFF
_ACL_OTHER = 0x20
_ACL_USER = 0x02
_ACL_USER_OBJ = 0x01
_GRANT_UID = 4242
_ORIGINAL = b'def f():\n    return 1\n'


def _failing_restore() -> TargetWrite:
    def handler(_data: bytes, call: int, _real_write: Callable[[bytes], int]) -> int:
        error = errno.ENOSPC if call == 0 else errno.EIO
        raise OSError(error, os.strerror(error))

    return handler


def _linux_acl(entries: list[tuple[int, int, int]]) -> bytes:
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


def _linux_acl_entries(raw: bytes) -> dict[int, list[tuple[int, int]]]:
    entries: dict[int, list[tuple[int, int]]] = {}
    for offset in range(4, len(raw), 8):
        tag, perm, ident = struct.unpack('<HHI', raw[offset : offset + 8])
        entries.setdefault(tag, []).append((perm, ident))
    return entries


def _macos_acl_lines(path: Path) -> list[str]:
    listing = subprocess.run(
        ['ls', '-led', str(path)], capture_output=True, check=True, text=True
    ).stdout
    return [line.strip() for line in listing.splitlines()[1:] if line.strip()]


def _windows_sddl(path: Path, scratch: Path) -> str:
    saved = scratch / 'acl.txt'
    subprocess.run(
        ['icacls', str(path), '/save', str(saved), '/q'], capture_output=True, check=True
    )
    return saved.read_bytes().decode('utf-16')


def test_constructor_cleanup_does_not_close_a_reused_unrelated_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    unrelated = tmp_path / 'unrelated'
    unrelated.write_bytes(b'unrelated')
    real_fdopen = os.fdopen
    opened: list[int] = []
    error = MemoryError('injected failure after internal wrapper cleanup')

    def fail_after_cleanup(fd: int, mode: str, *, closefd: bool = True) -> None:
        # Model constructor cleanup with a real stream, honoring descriptor ownership.
        with real_fdopen(fd, mode, closefd=closefd):
            pass
        other_fd = os.open(unrelated, os.O_RDONLY)
        if closefd and other_fd != fd:
            # Reuse the released number even when a lower directory descriptor is free.
            os.dup2(other_fd, fd)
            os.close(other_fd)
            other_fd = fd
        opened.append(other_fd)
        raise error

    monkeypatch.setattr(os, 'fdopen', fail_after_cleanup)
    try:
        with pytest.raises(MemoryError, match='injected failure') as caught:
            _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
        assert caught.value is error
        assert list(root.iterdir()) == []
        assert len(opened) == 1
        assert os.read(opened[0], len(b'unrelated')) == b'unrelated'
    finally:
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)


def test_failure_to_clear_the_acl_leaves_no_source_bytes_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != 'darwin':
        pytest.skip('macOS ACLs')
    root = redirect_recovery(tmp_path, monkeypatch)

    def refuse(_directory_fd: int) -> None:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

    monkeypatch.setattr(_recovery, '_clear_inherited_acl', refuse)
    target = tmp_path / 'target.py'
    target.write_bytes(_ORIGINAL)
    intercept_target_writes(monkeypatch, target, _failing_restore())
    with pytest.raises(RestoreError, match='could not be saved') as caught:
        write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    assert caught.value.recovery_path is None
    assert list(root.iterdir()) == []


def test_inherited_acl_entries_do_not_reach_the_file_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != 'darwin':
        pytest.skip('macOS ACLs')
    if os.geteuid() == 0:
        pytest.skip('root can read unreadable files')
    root = redirect_recovery(tmp_path, monkeypatch)
    root.parent.mkdir(parents=True)
    entry = 'everyone allow read,list,search,readattr,readextattr,file_inherit,directory_inherit'
    subprocess.run(['chmod', '+a', entry, str(root.parent)], check=True)
    control = root.parent / 'control.orig'
    control.write_bytes(_ORIGINAL)
    assert _macos_acl_lines(control)
    path = _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
    assert path.read_bytes() == _ORIGINAL
    assert _macos_acl_lines(path) == []
    assert _macos_acl_lines(path.parent) == []
    path.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            path.read_bytes()
    finally:
        path.chmod(0o600)


def test_inherited_default_acl_entries_are_masked_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != 'linux':
        pytest.skip('POSIX.1e ACLs')
    root = redirect_recovery(tmp_path, monkeypatch)
    root.parent.mkdir(parents=True)
    grant = _linux_acl(
        [
            (_ACL_USER_OBJ, 7, _ACL_NO_ID),
            (_ACL_USER, 7, _GRANT_UID),
            (_ACL_GROUP_OBJ, 5, _ACL_NO_ID),
            (_ACL_MASK, 7, _ACL_NO_ID),
            (_ACL_OTHER, 0, _ACL_NO_ID),
        ]
    )
    try:
        os.setxattr(root.parent, 'system.posix_acl_access', grant)
        os.setxattr(root.parent, 'system.posix_acl_default', grant)
    except OSError as error:
        pytest.skip(f'POSIX ACLs unavailable: {error}')
    path = _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
    assert path.read_bytes() == _ORIGINAL
    # Narrowing to Linux does not reach a loop body, so the reads come first.
    file_acl = os.getxattr(path, 'system.posix_acl_access')
    directory_acl = os.getxattr(path.parent, 'system.posix_acl_access')
    for raw in (file_acl, directory_acl):
        entries = _linux_acl_entries(raw)
        assert (7, _GRANT_UID) in entries[_ACL_USER]
        assert entries[_ACL_MASK] == [(0, _ACL_NO_ID)]


def test_inherited_entries_do_not_reach_the_file_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows access control')
    root = redirect_recovery(tmp_path, monkeypatch)
    root.parent.mkdir(parents=True)
    subprocess.run(
        ['icacls', str(root.parent), '/grant', '*S-1-1-0:(OI)(CI)R'],
        capture_output=True,
        check=True,
    )
    control = root.parent / 'control.orig'
    control.write_bytes(_ORIGINAL)
    assert ';WD)' in _windows_sddl(control, tmp_path)
    path = _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
    assert path.read_bytes() == _ORIGINAL
    assert ';WD)' not in _windows_sddl(path, tmp_path)
    assert ';WD)' not in _windows_sddl(path.parent, tmp_path)


def test_location_follows_xdg_state_home_only_when_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform in {'darwin', 'win32'}:
        pytest.skip('XDG_STATE_HOME applies to Linux and other POSIX systems')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    for value in ('', 'relative/state'):
        monkeypatch.setenv('XDG_STATE_HOME', value)
        path = _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
        assert path.parent.parent == tmp_path / 'home' / '.local' / 'state' / TOOL / 'recovery'


def test_recovery_flush_failure_preserves_its_error_and_closes_the_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    real_fdopen = os.fdopen
    real_close = os.close
    file_fds: list[int] = []
    file_closes: list[int] = []
    error = OSError(errno.ENOSPC, 'injected recovery flush failure')

    def fail_flush() -> None:
        raise error

    def fdopen(fd: int, mode: str, *, closefd: bool = True) -> IO[bytes]:
        file: IO[bytes] = real_fdopen(fd, mode, closefd=closefd)
        file_fds.append(fd)
        monkeypatch.setattr(file, 'flush', fail_flush)
        return file

    def close(fd: int) -> None:
        real_close(fd)
        if fd in file_fds:
            file_closes.append(fd)
            raise OSError(errno.EIO, 'injected recovery close failure')

    monkeypatch.setattr(os, 'fdopen', fdopen)
    monkeypatch.setattr(os, 'close', close)
    try:
        with pytest.raises(OSError, match='injected recovery flush failure') as caught:
            _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
        assert caught.value is error
        assert len(file_fds) == 1
        assert file_closes == file_fds
        assert list(root.iterdir()) == []
        with pytest.raises(OSError, check=lambda error: error.errno == errno.EBADF):
            os.fstat(file_fds[0])
    finally:
        for fd in file_fds:
            with contextlib.suppress(OSError):
                real_close(fd)


@pytest.mark.parametrize('boundary', ['directory_close', 'fdopen'])
def test_recovery_setup_failure_closes_the_file_and_removes_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    if boundary == 'directory_close' and sys.platform == 'win32':
        pytest.skip('POSIX recovery directory descriptors')
    root = redirect_recovery(tmp_path, monkeypatch)
    real_open = os.open
    real_close = os.close
    file_fds: list[int] = []
    directory_closes: list[int] = []
    error = OSError(errno.EIO, 'injected recovery setup failure')

    def record_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).endswith('.orig'):
            file_fds.append(fd)
        return fd

    def close(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            directory_closes.append(fd)
            if boundary == 'directory_close':
                raise error

    def refuse_fdopen(_fd: int, _mode: str, *, closefd: bool = True) -> None:
        del closefd

        raise error

    monkeypatch.setattr(os, 'open', record_open)
    monkeypatch.setattr(os, 'close', close)
    if boundary == 'fdopen':
        monkeypatch.setattr(os, 'fdopen', refuse_fdopen)
    try:
        with pytest.raises(OSError, match='injected recovery setup failure') as caught:
            _recovery.save_recovery(TOOL, Path('target.py'), _ORIGINAL)
        assert caught.value is error
        assert len(file_fds) == 1
        assert list(root.iterdir()) == []
        assert len(directory_closes) == int(sys.platform != 'win32')
        with pytest.raises(OSError, check=lambda error: error.errno == errno.EBADF):
            os.fstat(file_fds[0])
    finally:
        for fd in file_fds:
            with contextlib.suppress(OSError):
                real_close(fd)
