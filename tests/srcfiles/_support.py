import os
import subprocess
import sys
from collections.abc import Buffer, Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Timer

import pytest

from defsort._vendor.srcfiles import (
    write_in_place,
)

# Child processes import the package under the name these tests imported it by.
PACKAGE = write_in_place.__module__.rpartition('.')[0]
TOOL = 'demo-tool'

type TargetWrite = Callable[[bytes, int, Callable[[bytes], int]], int]


def child_env(overrides: Mapping[str, str]) -> dict[str, str]:
    return {
        **os.environ,
        **overrides,
        'PYTHONPATH': os.pathsep.join(entry for entry in sys.path if entry),
    }


def intercept_target_writes(
    monkeypatch: pytest.MonkeyPatch, target: Path, handler: TargetWrite
) -> None:
    # The handler gets each write to the target, its call number, and the real write.
    inode = target.stat().st_ino
    real_write = os.write
    calls: list[None] = []

    def write(fd: int, data: Buffer, /) -> int:
        if os.fstat(fd).st_ino != inode:
            return real_write(fd, data)
        calls.append(None)
        return handler(bytes(data), len(calls) - 1, lambda chunk: real_write(fd, chunk))

    monkeypatch.setattr(os, 'write', write)


@contextmanager
def ready_child(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    creationflags: int = 0,
    timeout: float = 60,
) -> Generator[subprocess.Popen[str]]:
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=creationflags,
    ) as child:
        try:
            # Killing a stalled child releases the blocking pipe read on every platform.
            watchdog = Timer(timeout, child.kill)
            watchdog.start()
            try:
                assert child.stdout is not None
                assert child.stdout.readline() == 'ready\n', 'child did not report readiness'
            finally:
                watchdog.cancel()
                watchdog.join()
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def recovery_location(tmp_path: Path) -> tuple[dict[str, str], Path]:
    # Environment that moves the per-user location under tmp_path, and the root it yields.
    home = tmp_path / 'home'
    home.mkdir()
    if sys.platform == 'win32':
        return {'LOCALAPPDATA': str(home)}, home / TOOL / 'recovery'
    if sys.platform == 'darwin':
        return {'HOME': str(home)}, home / 'Library' / 'Application Support' / TOOL / 'recovery'
    return {'XDG_STATE_HOME': str(home)}, home / TOOL / 'recovery'


def redirect_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    overrides, root = recovery_location(tmp_path)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    return root
