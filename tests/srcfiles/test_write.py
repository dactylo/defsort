import errno
import os
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from defsort._vendor.srcfiles import (
    FileChangedError,
    RestoreError,
    write_in_place,
)

from ._support import (
    PACKAGE,
    TOOL,
    TargetWrite,
    child_env,
    intercept_target_writes,
    recovery_location,
    redirect_recovery,
)

_LIMITED_CHILD = r"""
import importlib, resource, signal, sys
from pathlib import Path

package = importlib.import_module(sys.argv[1])
target = Path(sys.argv[2])
limit = int(sys.argv[3])
original = target.read_bytes()
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
try:
    package.write_in_place(target, b'x' * (limit + 100), expected=original, tool=sys.argv[4])
except package.RestoreError as error:
    print('RestoreError', error.recovery_path)
except OSError as error:
    print('OSError', error.errno)
"""
_ORIGINAL = b'def f():\n    return 1\n'


class _HostInterruptError(BaseException):
    pass


def _fail_after_half(restore_error: int | None) -> TargetWrite:
    def handler(data: bytes, call: int, real_write: Callable[[bytes], int]) -> int:
        if call == 0:
            real_write(data[: len(data) // 2])
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        if restore_error is not None:
            raise OSError(restore_error, os.strerror(restore_error))
        return real_write(data)

    return handler


def _target(tmp_path: Path, content: bytes = _ORIGINAL) -> Path:
    path = tmp_path / 'target.py'
    path.write_bytes(content)
    return path


def test_changed_target_is_left_alone(tmp_path: Path) -> None:
    target = _target(tmp_path, b'edited = True\n')
    with pytest.raises(FileChangedError) as caught:
        write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == b'edited = True\n'
    assert caught.value.filename == str(target)
    assert str(caught.value) == 'changed since it was read; not written'


def test_close_error_after_a_restore_saves_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    intercept_target_writes(monkeypatch, target, _fail_after_half(None))
    inode = target.stat().st_ino
    real_close = os.close

    def close(fd: int) -> None:
        targeted = os.fstat(fd).st_ino == inode
        real_close(fd)
        if targeted:
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(RestoreError, match='may not have been stored') as caught:
        write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    error = caught.value
    assert target.read_bytes() == _ORIGINAL
    assert error.errno == errno.ENOSPC
    message = str(error)
    assert message.count(os.strerror(errno.ENOSPC)) == 1
    assert message.count(os.strerror(errno.EIO)) == 1
    assert error.recovery_path is not None
    assert error.recovery_path.parent.parent == root
    assert error.recovery_path.read_bytes() == _ORIGINAL


def test_close_error_never_writes_through_the_path_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    replacement = tmp_path / 'replacement.py'
    replacement.write_bytes(b'editor = True\n')
    inode = target.stat().st_ino
    real_close = os.close

    def close(fd: int) -> None:
        targeted = os.fstat(fd).st_ino == inode
        real_close(fd)
        if targeted:
            replacement.replace(target)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(RestoreError, match='may not have been stored') as caught:
        write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == b'editor = True\n'
    assert caught.value.errno == errno.EIO
    assert caught.value.recovery_path is not None
    assert caught.value.recovery_path.parent.parent == root
    assert caught.value.recovery_path.read_bytes() == _ORIGINAL


def test_default_handlers_are_held_and_host_handlers_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    seen: list[object] = []

    def record_handler(data: bytes, _call: int, real_write: Callable[[bytes], int]) -> int:
        seen.append(signal.getsignal(signal.SIGINT))
        return real_write(data)

    intercept_target_writes(monkeypatch, target, record_handler)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous)
    assert seen
    assert seen[0] is not signal.default_int_handler

    def host_handler(_signum: int, _frame: object) -> None:
        pass

    seen.clear()
    previous = signal.signal(signal.SIGINT, host_handler)
    try:
        write_in_place(target, b'x = 3\n', expected=b'x = 2\n', tool=TOOL)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert seen == [host_handler]


def test_each_recovery_file_gets_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    saved: list[Path] = []
    for _ in range(2):
        target.write_bytes(_ORIGINAL)
        with pytest.MonkeyPatch.context() as patch:
            intercept_target_writes(patch, target, _fail_after_half(errno.EIO))
            with pytest.raises(RestoreError) as caught:
                write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
        assert caught.value.recovery_path is not None
        saved.append(caught.value.recovery_path)
    assert saved[0].parent != saved[1].parent
    assert {path.parent.parent for path in saved} == {root}


def test_failed_restore_saves_the_original_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    intercept_target_writes(monkeypatch, target, _fail_after_half(errno.EIO))
    with pytest.raises(RestoreError) as caught:
        write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    error = caught.value
    assert error.errno == errno.ENOSPC
    assert isinstance(error.__cause__, OSError)
    assert error.__cause__.errno == errno.ENOSPC
    message = str(error)
    assert message.count(os.strerror(errno.ENOSPC)) == 1
    assert message.count(os.strerror(errno.EIO)) == 1
    assert error.recovery_path is not None
    assert str(error.recovery_path) in message
    assert error.recovery_path.parent.parent == root
    assert error.recovery_path.name == 'target.py.orig'
    assert error.recovery_path.read_bytes() == _ORIGINAL
    if sys.platform != 'win32':
        assert stat.S_IMODE(error.recovery_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(error.recovery_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_hard_linked_target_updates_every_name(tmp_path: Path) -> None:
    target = _target(tmp_path)
    link = tmp_path / 'link.py'
    try:
        os.link(target, link)
    except OSError as error:
        pytest.skip(f'hard links unavailable: {error}')
    write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert link.read_bytes() == b'x = 2\n'
    assert link.stat().st_ino == target.stat().st_ino
    assert target.stat().st_nlink == 2


@pytest.mark.parametrize('failure', ['restore', 'close'])
def test_host_interrupt_keeps_its_type_and_names_the_saved_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    interrupt = _HostInterruptError()

    def host_handler(_signum: int, _frame: object) -> None:
        raise interrupt

    def interrupted(data: bytes, call: int, real_write: Callable[[bytes], int]) -> int:
        if call == 0:
            written = real_write(data[: len(data) // 2])
            signal.raise_signal(signal.SIGINT)
            return written
        if failure == 'restore':
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_write(data)

    inode = target.stat().st_ino
    real_close = os.close

    def close(fd: int) -> None:
        targeted = os.fstat(fd).st_ino == inode
        real_close(fd)
        if targeted and failure == 'close':
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    previous = signal.signal(signal.SIGINT, host_handler)
    try:
        intercept_target_writes(monkeypatch, target, interrupted)
        monkeypatch.setattr(os, 'close', close)
        with pytest.raises(_HostInterruptError) as caught:
            write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert caught.value is interrupt
    saved = list(root.glob('*/target.py.orig'))
    assert len(saved) == 1
    assert saved[0].read_bytes() == _ORIGINAL
    assert len(interrupt.__notes__) == 1
    assert str(saved[0]) in interrupt.__notes__[0]
    if failure == 'close':
        assert target.read_bytes() == _ORIGINAL


def test_host_signal_handler_exception_restores_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)

    def host_handler(_signum: int, _frame: object) -> None:
        raise _HostInterruptError

    def interrupted(data: bytes, call: int, real_write: Callable[[bytes], int]) -> int:
        if call == 0:
            written = real_write(data[: len(data) // 2])
            signal.raise_signal(signal.SIGINT)
            return written
        return real_write(data)

    previous = signal.signal(signal.SIGINT, host_handler)
    try:
        intercept_target_writes(monkeypatch, target, interrupted)
        with pytest.raises(_HostInterruptError):
            write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert target.read_bytes() == _ORIGINAL


def test_missing_target_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / 'missing.py'
    with pytest.raises(FileChangedError, match='no longer exists') as caught:
        write_in_place(missing, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert caught.value.filename == str(missing)
    assert not missing.exists()


def test_non_regular_target_is_refused_without_reading(tmp_path: Path) -> None:
    if sys.platform == 'win32':
        pytest.skip('FIFOs are POSIX')
    fifo = tmp_path / 'pipe.py'
    os.mkfifo(fifo)
    with pytest.raises(OSError, match='not a regular file') as caught:
        write_in_place(fifo, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert caught.value.errno == errno.EINVAL


def test_read_only_target_raises_a_plain_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != 'win32' and os.geteuid() == 0:
        pytest.skip('root can write read-only files')
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    target.chmod(0o444)
    try:
        with pytest.raises(PermissionError) as caught:
            write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    finally:
        target.chmod(0o644)
    assert not isinstance(caught.value, RestoreError)
    assert target.read_bytes() == _ORIGINAL
    assert not root.exists()


def test_symlink_operand_writes_its_target(tmp_path: Path) -> None:
    target = _target(tmp_path)
    link = tmp_path / 'link.py'
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    write_in_place(link, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert link.is_symlink()
    assert target.read_bytes() == b'x = 2\n'


def test_target_already_holding_the_data_counts_as_written(tmp_path: Path) -> None:
    target = _target(tmp_path, b'x = 2\n')
    os.utime(target, (1_000_000_000, 1_000_000_000))
    write_in_place(target, b'x = 2\n', expected=_ORIGINAL, tool=TOOL)
    assert target.stat().st_mtime == 1_000_000_000


def test_unchanged_data_touches_nothing(tmp_path: Path) -> None:
    target = _target(tmp_path)
    os.utime(target, (1_000_000_000, 1_000_000_000))
    before = target.stat()
    write_in_place(target, _ORIGINAL, expected=_ORIGINAL, tool=TOOL)
    write_in_place(tmp_path / 'missing.py', b'x', expected=b'x', tool=TOOL)
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not (tmp_path / 'missing.py').exists()


def test_unusable_recovery_location_saves_nothing_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides, root = recovery_location(tmp_path)
    blocker = root.parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b'not a directory')
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    work = tmp_path / 'work'
    work.mkdir()
    target = _target(work)
    intercept_target_writes(monkeypatch, target, _fail_after_half(errno.EIO))
    with pytest.raises(RestoreError, match='could not be saved') as caught:
        write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    assert caught.value.recovery_path is None
    assert [path.name for path in work.iterdir()] == ['target.py']
    assert blocker.read_bytes() == b'not a directory'


def test_write_error_restores_the_original_and_raises_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = redirect_recovery(tmp_path, monkeypatch)
    target = _target(tmp_path)
    intercept_target_writes(monkeypatch, target, _fail_after_half(None))
    with pytest.raises(OSError, match='No space') as caught:
        write_in_place(target, _ORIGINAL * 50, expected=_ORIGINAL, tool=TOOL)
    assert type(caught.value) is OSError
    assert caught.value.errno == errno.ENOSPC
    assert target.read_bytes() == _ORIGINAL
    assert not root.exists()


def test_write_from_another_thread_runs_without_deferral(tmp_path: Path) -> None:
    target = _target(tmp_path)
    thread = threading.Thread(
        target=write_in_place,
        args=(target, b'x = 2\n'),
        kwargs={'expected': _ORIGINAL, 'tool': TOOL},
    )
    thread.start()
    thread.join()
    assert target.read_bytes() == b'x = 2\n'


@pytest.mark.parametrize(
    'data', [b'x = 1\n', _ORIGINAL + b'# longer\n' * 100], ids=['shorter', 'longer']
)
def test_write_replaces_content_and_keeps_the_file(tmp_path: Path, data: bytes) -> None:
    target = _target(tmp_path)
    before = target.stat()
    write_in_place(target, data, expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == data
    after = target.stat()
    assert (after.st_ino, after.st_nlink, after.st_mode) == (
        before.st_ino,
        before.st_nlink,
        before.st_mode,
    )


@pytest.mark.skipif(sys.platform == 'win32', reason='RLIMIT_FSIZE is POSIX')
@pytest.mark.parametrize(
    ('original_size', 'outcome'),
    [(50, f'OSError {errno.EFBIG}'), (150, 'RestoreError None')],
    ids=['original-fits', 'original-too-large'],
)
def test_file_size_limit_restores_an_original_that_fits(
    tmp_path: Path, original_size: int, outcome: str
) -> None:
    overrides, root = recovery_location(tmp_path)
    target = _target(tmp_path, b'o' * original_size)
    result = subprocess.run(
        [sys.executable, '-c', _LIMITED_CHILD, PACKAGE, str(target), '100', TOOL],
        capture_output=True,
        check=False,
        env=child_env(overrides),
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == outcome, result.stderr
    if original_size < 100:
        assert target.read_bytes() == b'o' * original_size
    assert not any(path.is_file() for path in root.rglob('*'))
