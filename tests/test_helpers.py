import contextlib
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests._helpers import (
    ENV,
    PROCESS_GROUP_FLAGS,
    decode_text,
    finish_process,
    install_stand_in_git,
    run_setup,
)


def test_decode_text_uses_universal_newlines() -> None:
    raw = b'alpha\r\nbeta\rgamma\ndelta'

    assert decode_text(raw) == 'alpha\nbeta\ngamma\ndelta'


def test_finish_process_timeout_reaps_child_and_reports_output() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            '-u',
            '-c',
            (
                'import sys, threading; '
                'print("sentinel", flush=True); '
                'print("ready", file=sys.stderr, flush=True); '
                'threading.Event().wait(30)'
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    stderr = process.stderr
    assert stderr is not None
    marker: list[str] = []
    ready = threading.Event()

    def read_ready() -> None:
        marker.append(stderr.readline())
        ready.set()

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        assert ready.wait(10)
        assert marker == ['ready\n']
        with pytest.raises(AssertionError) as failure:
            finish_process(process, timeout=0.2)
        transcript = str(failure.value)
        assert 'note: timed out after 0.2 s' in transcript
        assert '  | sentinel' in transcript
        assert process.returncode is not None
    finally:
        with contextlib.suppress(OSError):
            process.kill()
        process.wait(timeout=2)
        reader.join(timeout=2)
        assert not reader.is_alive()
        process.communicate(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='the Git stand-in is a POSIX shell script')
def test_stand_in_git_delegates_from_different_cwd_with_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_path = os.environ['PATH']
    installed_git = shutil.which('git')
    assert installed_git is not None
    relative_dir = tmp_path / 'relative'
    relative_dir.mkdir()
    (relative_dir / 'git').symlink_to(installed_git)
    stand_in_root = tmp_path / 'stand-in'
    stand_in_root.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', f'relative{os.pathsep}{original_path}')
    install_stand_in_git(monkeypatch, stand_in_root)

    result = run_setup(['git', '--version'], check=False, cwd=other)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(b'git version ')


def test_subprocess_host_roots_cleaned_after_normal_exit() -> None:
    roots: list[Path] = []
    for _ in range(2):
        result = run_setup(
            [
                sys.executable,
                '-c',
                'from tests._helpers import HOSTS; assert HOSTS.is_dir(); print(HOSTS)',
            ],
            env=ENV,
        )
        roots.append(Path(decode_text(result.stdout).strip()))

    assert roots[0] != roots[1]
    assert all(not root.exists() for root in roots)
