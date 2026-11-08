import os
from pathlib import Path

import pytest

from defsort._vendor.hookfront import (
    _writes,
)
from defsort._vendor.hookfront._front import (
    Outcome,
)
from defsort._vendor.hookfront._writes import (
    CommitWrites,
)
from defsort._vendor.srcfiles import (
    FileChangedError,
)


def test_failed_rollback_lists_unrestored_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = tmp_path / 'a.py'
    earlier.write_bytes(b'changed again')
    writes = CommitWrites('tool', tmp_path)
    writes.record(earlier, b'old', b'new')

    def changed(path: Path, data: bytes, *, expected: bytes, tool: str) -> None:
        assert (path, data, expected, tool) == (earlier, b'old', b'new', 'tool')
        raise FileChangedError(path)

    monkeypatch.setattr(_writes, 'write_in_place', changed)
    result = writes.failed(
        tmp_path / 'b.py', PermissionError(13, 'Permission denied'), allow_on_failure=False
    )

    assert earlier.read_bytes() == b'changed again'
    assert result == Outcome(
        'deny',
        'tool: cannot write b.py: Permission denied; '
        'not restored: a.py (changed since it was read; not written)',
    )


def test_failed_write_denies_with_repository_path(tmp_path: Path) -> None:
    target = tmp_path / 'pkg' / 'm.py'
    target.parent.mkdir()
    target.write_bytes(b'old')
    result = CommitWrites('tool', tmp_path).failed(
        target, PermissionError(13, 'Permission denied'), allow_on_failure=False
    )
    assert result == Outcome('deny', 'tool: cannot write pkg/m.py: Permission denied')


def test_failed_write_rolls_back_earlier_writes(tmp_path: Path) -> None:
    earlier = tmp_path / 'a.py'
    earlier.write_bytes(b'new')
    writes = CommitWrites('tool', tmp_path)
    writes.record(earlier, b'old', b'new')

    result = writes.failed(
        tmp_path / 'b.py', PermissionError(13, 'Permission denied'), allow_on_failure=False
    )

    assert earlier.read_bytes() == b'old'
    assert result == Outcome('deny', 'tool: cannot write b.py: Permission denied')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX backslash is a filename character')
def test_posix_backslash_name_is_kept(tmp_path: Path) -> None:
    target = tmp_path / 'a\\b.py'
    target.write_bytes(b'old')
    assert CommitWrites('tool', tmp_path).failed(
        target, OSError(13, 'denied'), allow_on_failure=False
    ) == Outcome('deny', 'tool: cannot write a\\b.py: denied')


def test_reason_escapes_controls_once(tmp_path: Path) -> None:
    target = tmp_path / 'a\x1bb.py'
    result = CommitWrites('tool', tmp_path).failed(
        target, OSError(13, 'denied'), allow_on_failure=False
    )
    assert result == Outcome('deny', r'tool: cannot write a\x1bb.py: denied')


def test_warning_form_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / 'pkg' / 'm.py'
    target.parent.mkdir()
    target.write_bytes(b'old')

    result = CommitWrites('tool', tmp_path).failed(
        target, PermissionError(13, 'Permission denied'), allow_on_failure=True
    )

    assert result is None
    assert capsys.readouterr() == ('', 'WARNING: tool: cannot write pkg/m.py: Permission denied\n')


@pytest.mark.skipif(os.name != 'nt', reason='Windows path separators')
def test_windows_separators_become_slashes(tmp_path: Path) -> None:
    target = tmp_path / 'pkg' / 'm.py'
    target.parent.mkdir()
    target.write_bytes(b'old')
    assert CommitWrites('tool', tmp_path).failed(
        target, OSError(13, 'denied'), allow_on_failure=False
    ) == Outcome('deny', 'tool: cannot write pkg/m.py: denied')
