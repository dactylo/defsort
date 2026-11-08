import os
import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _index,
)
from defsort._vendor.gitfiles._index import (
    IndexEntry,
    apply_index_entries,
    hash_object,
)
from defsort._vendor.gitfiles._listings import (
    get_staged_py_info,
)
from defsort._vendor.gitfiles._run import (
    _git_executable,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
)

from ._support import (
    git,
    latin1_encode,
)


def test_empty_entries_launch_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('empty index update launched Git')

    monkeypatch.setattr(subprocess, 'run', unexpected)
    assert apply_index_entries('/repo', []) is None


def test_hash_object_writes_exact_bytes(repo: Path) -> None:
    data = b'x = 1\r\n'
    executable = _git_executable()
    assert executable is not None
    expected = (
        subprocess.run(
            [executable, 'hash-object', '--stdin'],
            input=data,
            cwd=repo,
            capture_output=True,
            check=True,
        )
        .stdout.strip()
        .decode('ascii')
    )
    object_id = hash_object(str(repo), data)
    assert object_id == expected
    assert git('cat-file', 'blob', object_id, cwd=repo).stdout == data


def test_index_entries_use_the_git_path_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[bytes] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert Path(command[0]).is_absolute()
        assert command[1:] == ['update-index', '-z', '--index-info']
        data = kwargs['input']
        assert isinstance(data, bytes)
        captured.append(data)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(_index, 'encode_git_path', latin1_encode)
    monkeypatch.setattr(subprocess, 'run', run)
    apply_index_entries('/repo', [IndexEntry('100644', 'a' * 40, 'caf\xe9.py')])
    assert captured == [b'100644 ' + b'a' * 40 + b'\tcaf\xe9.py\0']


def test_multiple_entries_use_one_nul_delimited_update(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert command[1:] == ['update-index', '-z', '--index-info']
        data = kwargs['input']
        assert isinstance(data, bytes)
        calls.append(data)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(subprocess, 'run', run)
    entries = [
        IndexEntry('100644', 'a' * 40, 'first.py'),
        IndexEntry('100755', 'b' * 40, 'second.py'),
    ]
    apply_index_entries('/repo', entries)
    assert calls == [
        b'100644 ' + b'a' * 40 + b'\tfirst.py\0' + b'100755 ' + b'b' * 40 + b'\tsecond.py\0'
    ]


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain non-UTF-8 bytes')
def test_surrogate_name_is_encoded(repo: Path) -> None:
    object_id = hash_object(str(repo), b'x = 1\n')
    apply_index_entries(str(repo), [IndexEntry('100644', object_id, 'weird\udcff.py')])
    assert b'weird\xff.py' in git('ls-files', '-z', cwd=repo).stdout


def test_undecodable_index_name_round_trips(repo: Path) -> None:
    raw_name = b'bad-\xff.py'
    old_hash = hash_object(str(repo), b'')
    apply_index_entries(str(repo), [IndexEntry('100644', old_hash, decode_git_path(raw_name))])
    assert git('ls-files', '-z', cwd=repo).stdout == raw_name + b'\0'
    staged = get_staged_py_info(str(repo))
    assert len(staged) == 1
    assert staged[0].path == decode_git_path(raw_name)
    new_hash = hash_object(str(repo), b'updated\n')
    apply_index_entries(str(repo), [IndexEntry(staged[0].mode, new_hash, staged[0].path)])
    assert git('ls-files', '-z', '-s', cwd=repo).stdout == (
        b'100644 ' + new_hash.encode('ascii') + b' 0\t' + raw_name + b'\0'
    )
