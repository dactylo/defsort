import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ._support import (
    PACKAGE,
    TOOL,
    child_env,
    ready_child,
    recovery_location,
)

_HANDLER_BOUNDARY_CHILD = r"""
import errno, importlib, signal, sys
from pathlib import Path

package = importlib.import_module(sys.argv[1])
signals = importlib.import_module(sys.argv[1] + '._signals')
scenario = sys.argv[2]
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
real_signal = signal.signal


def interrupt_boundary(signum, handler):
    if scenario == 'setup-before' and signum == signal.SIGHUP and callable(handler):
        signal.raise_signal(signal.SIGINT)
    if (
        scenario == 'setup-unwind'
        and signum == signal.SIGINT
        and handler is not signal.default_int_handler
    ):
        signal.raise_signal(signal.SIGINT)
    previous = real_signal(signum, handler)
    if scenario == 'setup-after' and signum == signal.SIGTERM and callable(handler):
        signal.raise_signal(signal.SIGINT)
    if scenario == 'setup-unwind' and signum == signal.SIGTERM and handler == signal.SIG_DFL:
        signal.raise_signal(signal.SIGINT)
    if (
        scenario in {'restore', 'restore-error'}
        and signum == signal.SIGINT
        and handler is signal.default_int_handler
    ):
        signal.raise_signal(signal.SIGINT)
    return previous


def notice(error):
    if isinstance(error, package.RestoreError):
        return f'original is saved in {error.recovery_path}'
    return None


signal.signal = interrupt_boundary
try:
    with signals.deferred_signals(notice):
        if scenario in {'restore', 'restore-error'}:
            signal.raise_signal(signal.SIGTERM)
        if scenario == 'restore-error':
            raise package.RestoreError(errno.EIO, 'restore failed', Path('/tmp/recovery'))
except KeyboardInterrupt:
    print('interrupted', flush=True)
print('restored', all((
    signal.getsignal(signal.SIGTERM) == signal.SIG_DFL,
    signal.getsignal(signal.SIGHUP) == signal.SIG_DFL,
    signal.getsignal(signal.SIGINT) is signal.default_int_handler,
)), flush=True)
signal.raise_signal(signal.SIGTERM)
print('survived SIGTERM', flush=True)
"""
_NOTICE_FAILURE_CHILD = r"""
import importlib, os, signal, sys

signals = importlib.import_module(sys.argv[1] + '._signals')
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
if sys.argv[2] == 'broken':
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    sys.stderr = os.fdopen(write_fd, 'w')
else:
    sys.stderr.close()
try:
    with signals.deferred_signals(lambda error: 'recovery notice'):
        signal.raise_signal(signal.SIGTERM)
        raise OSError('write and restore failed')
except (OSError, ValueError):
    print('survived SIGTERM', flush=True)
"""
# The child pauses inside a write to the target until a line arrives on stdin. In the restore
# scenarios its first write fails, so the pause falls inside the restore.
_PAUSING_CHILD = r"""
import errno, importlib, os, signal, sys
from pathlib import Path

# A parent shell may ignore these signals; the scenarios need Python's default handling.
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
package = importlib.import_module(sys.argv[1])
target = Path(sys.argv[2])
scenario = sys.argv[3]
inode = target.stat().st_ino
real_write = os.write
calls = []


def pause():
    print('ready', flush=True)
    sys.stdin.readline()


def scripted_write(fd, data):
    if os.fstat(fd).st_ino != inode:
        return real_write(fd, data)
    calls.append(None)
    if len(calls) == 1:
        written = real_write(fd, bytes(data[: len(data) // 2]))
        if scenario == 'write':
            pause()
            return written
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    if len(calls) == 2 and scenario != 'write':
        pause()
        if scenario == 'restore-fails':
            raise OSError(errno.EIO, os.strerror(errno.EIO))
    return real_write(fd, data)


os.write = scripted_write
original = target.read_bytes()
try:
    package.write_in_place(target, original * 40, expected=original, tool=sys.argv[4])
except KeyboardInterrupt:
    print('KeyboardInterrupt', flush=True)
    sys.exit(5)
except OSError as error:
    print(type(error).__name__, flush=True)
    sys.exit(6)
print('returned', flush=True)
"""
_ORIGINAL = b'def f():\n    return 1\n'


def _run_paused_child(
    tmp_path: Path, scenario: str, send: int, *, creationflags: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    target = tmp_path / 'target.py'
    target.write_bytes(_ORIGINAL)
    overrides, root = recovery_location(tmp_path)
    with ready_child(
        [sys.executable, '-c', _PAUSING_CHILD, PACKAGE, str(target), scenario, TOOL],
        env=child_env(overrides),
        creationflags=creationflags,
    ) as child:
        try:
            child.send_signal(send)
        except OSError as error:
            pytest.skip(f'cannot deliver the signal: {error}')
        stdout, stderr = child.communicate('\n', timeout=60)
    return subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr), target, root


def test_ctrl_break_mid_write_waits_for_the_whole_write(tmp_path: Path) -> None:
    if sys.platform != 'win32':
        pytest.skip('Ctrl+Break is Windows-only')
    result, target, _root = _run_paused_child(
        tmp_path,
        'write',
        signal.CTRL_BREAK_EVENT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    assert target.read_bytes() == _ORIGINAL * 40
    assert result.stdout == ''
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
@pytest.mark.parametrize(
    ('send', 'returncode', 'stdout'),
    [(signal.SIGTERM, -signal.SIGTERM, ''), (signal.SIGINT, 5, 'KeyboardInterrupt\n')],
    ids=['SIGTERM', 'SIGINT'],
)
def test_signal_mid_write_waits_for_the_whole_write(
    tmp_path: Path, send: int, returncode: int, stdout: str
) -> None:
    result, target, _root = _run_paused_child(tmp_path, 'write', send)
    assert target.read_bytes() == _ORIGINAL * 40
    assert (result.returncode, result.stdout) == (returncode, stdout), result.stderr


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
@pytest.mark.parametrize(
    ('send', 'returncode', 'stdout'),
    [(signal.SIGTERM, -signal.SIGTERM, ''), (signal.SIGINT, 5, 'KeyboardInterrupt\n')],
    ids=['SIGTERM', 'SIGINT'],
)
def test_signal_mid_restore_waits_for_the_restore(
    tmp_path: Path, send: int, returncode: int, stdout: str
) -> None:
    result, target, _root = _run_paused_child(tmp_path, 'restore', send)
    assert target.read_bytes() == _ORIGINAL
    assert (result.returncode, result.stdout) == (returncode, stdout), result.stderr


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
def test_termination_after_a_failed_restore_names_the_recovery_file(tmp_path: Path) -> None:
    result, target, root = _run_paused_child(tmp_path, 'restore-fails', signal.SIGTERM)
    assert result.returncode == -signal.SIGTERM
    [saved] = [path for path in root.rglob('*') if path.is_file()]
    assert saved.read_bytes() == _ORIGINAL
    assert result.stderr == (
        f'{TOOL}: {str(target)!a} may be damaged; the original is saved in {str(saved)!a}\n'
    )


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
@pytest.mark.parametrize('scenario', ['setup-before', 'setup-after', 'setup-unwind'])
def test_interrupted_signal_setup_restores_original_handlers(scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, '-c', _HANDLER_BOUNDARY_CHILD, PACKAGE, scenario],
        env=child_env({}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout == 'interrupted\nrestored True\n', result.stderr
    assert result.returncode == -signal.SIGTERM, result.stderr


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
@pytest.mark.parametrize(
    ('scenario', 'stderr'),
    [('restore', ''), ('restore-error', 'original is saved in /tmp/recovery\n')],
)
def test_interrupted_handler_restoration_delivers_pending_termination(
    scenario: str, stderr: str
) -> None:
    result = subprocess.run(
        [sys.executable, '-c', _HANDLER_BOUNDARY_CHILD, PACKAGE, scenario],
        env=child_env({}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout == '', result.stderr
    assert result.returncode == -signal.SIGTERM, result.stderr
    assert result.stderr == stderr


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
@pytest.mark.parametrize('stderr_state', ['broken', 'closed'])
def test_notice_failure_still_delivers_pending_termination(stderr_state: str) -> None:
    result = subprocess.run(
        [sys.executable, '-c', _NOTICE_FAILURE_CHILD, PACKAGE, stderr_state],
        env=child_env({}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == -signal.SIGTERM, result.stderr
    assert result.stdout == ''
