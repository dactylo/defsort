import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _run,
)
from defsort._vendor.gitfiles._run import (
    GitCommandError,
    _git_executable,
    _windows_git,
    git_failure_detail,
    run_git,
)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable rules')
class TestPosixGitExecutable:
    def test_empty_path_finds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('PATH', '')
        assert _git_executable() is None

    def test_first_absolute_entry_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _make_executable(tmp_path / 'first' / 'git')
        second = _make_executable(tmp_path / 'second' / 'git')
        monkeypatch.setenv('PATH', os.pathsep.join([str(first.parent), str(second.parent)]))
        assert _git_executable() == str(first)

    def test_path_changes_between_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for directory in ('first', 'second'):
            expected = _make_executable(tmp_path / directory / 'git')
            monkeypatch.setenv('PATH', str(expected.parent))
            assert _git_executable() == str(expected)

    def test_skips_empty_relative_and_dot_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_executable(tmp_path / 'git')
        _make_executable(tmp_path / 'rel' / 'bin' / 'git')
        expected = _make_executable(tmp_path / 'absolute' / 'git')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('PATH', os.pathsep.join(['', '.', 'rel/bin', str(expected.parent)]))
        assert _git_executable() == str(expected)

    def test_skips_nonexecutables_and_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nonexecutable = _make_executable(tmp_path / 'plain' / 'git')
        nonexecutable.chmod(0o644)
        (tmp_path / 'directory' / 'git').mkdir(parents=True)
        expected = _make_executable(tmp_path / 'valid' / 'git')
        monkeypatch.setenv(
            'PATH',
            os.pathsep.join(
                [str(nonexecutable.parent), str(tmp_path / 'directory'), str(expected.parent)]
            ),
        )
        assert _git_executable() == str(expected)

    def test_symlink_is_returned_as_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _make_executable(tmp_path / 'target')
        link = tmp_path / 'git'
        link.symlink_to(target)
        monkeypatch.setenv('PATH', str(tmp_path))
        assert _git_executable() == str(link)

    def test_unset_path_uses_default_search_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = _make_executable(tmp_path / 'default' / 'git')
        monkeypatch.delenv('PATH', raising=False)
        monkeypatch.setattr(os, 'defpath', str(expected.parent))
        assert _git_executable() == str(expected)


class TestWindowsGitSelection:
    def test_skips_a_directory_named_git_exe(self, tmp_path: Path) -> None:
        (tmp_path / 'first' / 'git.exe').mkdir(parents=True)
        expected = _make_executable(tmp_path / 'second' / 'git.exe')
        path = os.pathsep.join([str(tmp_path / 'first'), str(expected.parent)])
        assert _windows_git(path) == str(expected)

    def test_skips_relative_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_executable(tmp_path / 'git.exe')
        _make_executable(tmp_path / 'relative' / 'git.exe')
        expected = _make_executable(tmp_path / 'absolute' / 'git.exe')
        monkeypatch.chdir(tmp_path)
        path = os.pathsep.join(['', '.', 'relative', str(expected.parent)])
        assert _windows_git(path) == str(expected)

    def test_takes_only_git_exe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for suffix in ('.cmd', '.bat', '.com'):
            _make_executable(tmp_path / 'first' / f'git{suffix}')
        expected = _make_executable(tmp_path / 'second' / 'git.exe')
        monkeypatch.setenv('PATHEXT', '.CMD;.BAT')
        path = os.pathsep.join([str(tmp_path / 'first'), str(expected.parent)])
        assert _windows_git(path) == str(expected)

    @pytest.mark.parametrize('path', [None, ''])
    def test_unset_or_empty_path_finds_nothing(self, path: str | None) -> None:
        assert _windows_git(path) is None

    def test_without_git_exe_finds_nothing(self, tmp_path: Path) -> None:
        _make_executable(tmp_path / 'git.cmd')
        assert _windows_git(str(tmp_path)) is None


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows executable search')
class TestNativeWindowsGitExecutable:
    def test_ignores_cwd_decoy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real_git = _git_executable()
        assert real_git is not None
        os_executable = Path(os.environ['SYSTEMROOT']) / 'System32' / 'where.exe'
        shutil.copyfile(os_executable, tmp_path / 'git.exe')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('PATH', str(Path(real_git).parent))
        result = run_git(['git', '--version'], tmp_path)
        assert _git_executable() == real_git
        assert result.stdout.startswith(b'git version ')

    def test_prefers_git_exe_over_earlier_git_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_git = _git_executable()
        assert real_git is not None
        cmd_dir = tmp_path / 'first'
        cmd_dir.mkdir()
        (cmd_dir / 'git.cmd').write_bytes(b'not executed')
        monkeypatch.setenv('PATH', os.pathsep.join([str(cmd_dir), str(Path(real_git).parent)]))
        monkeypatch.setenv('PATHEXT', '.CMD;.EXE')
        assert _git_executable() == real_git

    def test_skips_rooted_and_drive_relative_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_git = _git_executable()
        assert real_git is not None
        monkeypatch.setenv(
            'PATH', os.pathsep.join(['C:relative', '\\rooted', str(Path(real_git).parent)])
        )
        assert _git_executable() == real_git

    def test_unset_path_finds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('PATH', raising=False)
        assert _git_executable() is None


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


def test_allowed_returncodes(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(['git'], 2, b'', b'\n denied \nother\n')

    def completed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return result

    monkeypatch.setattr(subprocess, 'run', completed)
    with pytest.raises(GitCommandError, match='denied'):
        run_git(['git', 'status'], None)
    assert run_git(['git', 'status'], None, allowed_returncodes=(2,)) is result
    result.returncode = 129
    assert run_git(['git', 'status'], None, allowed_returncodes=None) is result


def test_embedded_nul_argument_is_a_detail(isolated: Path) -> None:
    with pytest.raises(GitCommandError) as caught:
        run_git(['git', 'status', 'a\0b'], isolated)
    assert type(caught.value) is GitCommandError
    assert caught.value.detail.startswith('git could not be run: ')
    assert len(caught.value.detail) > len('git could not be run: ')


@pytest.mark.parametrize(
    ('stderr', 'expected'),
    [(b'\n fatal: first\nsecond\n', 'fatal: first'), (b'', 'git exited with status 2')],
)
def test_failure_detail_uses_first_nonblank_line_or_status(stderr: bytes, expected: str) -> None:
    result = subprocess.CompletedProcess(['git'], 2, b'', stderr)
    assert git_failure_detail(result) == expected


def test_invalid_argument_detail_names_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*_args: object, **_kwargs: object) -> None:
        raise ValueError('invalid subprocess argument')

    monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(GitCommandError) as caught:
        run_git(['git', 'status'], None)
    assert caught.value.detail == 'git could not be run: invalid subprocess argument'


@pytest.mark.parametrize(
    ('error', 'detail'),
    [
        (PermissionError(13, 'Permission denied'), 'Permission denied'),
        (OSError('launch refused'), 'launch refused'),
    ],
)
def test_launch_failure_names_the_os_reason(
    monkeypatch: pytest.MonkeyPatch, error: OSError, detail: str
) -> None:
    def failed(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(GitCommandError) as caught:
        run_git(['git', 'status'], None)
    assert type(caught.value) is GitCommandError
    assert caught.value.detail == f'git could not be run: {detail}'


def test_missing_git_is_a_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('PATH', str(tmp_path))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('subprocess.run must not be called')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    with pytest.raises(GitCommandError) as caught:
        run_git(['git', 'status'], tmp_path)
    assert type(caught.value) is GitCommandError
    assert caught.value.detail == 'git could not be run: Git executable not found on PATH'
    assert str(caught.value) == caught.value.detail


def test_run_git_replaces_the_placeholder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded: list[tuple[list[str | bytes], dict[str, object]]] = []

    def fake_run(args: list[str | bytes], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'', b'')

    monkeypatch.setattr(_run, '_git_executable', lambda: str(tmp_path / 'git'))
    monkeypatch.setattr(subprocess, 'run', fake_run)
    cwd = tmp_path / 'nested repo'
    run_git(['git', '-C', 'nested repo', 'add', '--', 'space name.py', b'raw-\xff.py'], cwd)
    assert recorded == [
        (
            [
                str(tmp_path / 'git'),
                '-C',
                'nested repo',
                'add',
                '--',
                'space name.py',
                b'raw-\xff.py',
            ],
            {'input': None, 'capture_output': True, 'cwd': cwd, 'check': False},
        )
    ]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX invalid surrogate argument')
def test_unencodable_argument_is_a_detail(isolated: Path) -> None:
    with pytest.raises(GitCommandError) as caught:
        run_git(['git', 'status', 'a\ud800'], isolated)
    assert type(caught.value) is GitCommandError
    assert caught.value.detail.startswith('git could not be run: ')
    assert len(caught.value.detail) > len('git could not be run: ')
