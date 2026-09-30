import os
from pathlib import Path

import pytest

from defsort._git_executable import _resolve_windows_git, resolve_git_executable


@pytest.mark.skipif(os.name == 'nt', reason='POSIX child PATH and cwd semantics')
class TestPosixGitExecutable:
    def test_absent_path_uses_standard_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = _make_executable(tmp_path / 'default' / 'git')
        monkeypatch.setattr(os, 'defpath', str(expected.parent))
        monkeypatch.setenv('PATH', str(tmp_path / 'missing'))

        assert resolve_git_executable(env={}) == str(expected)

    def test_child_path_and_relative_cwd_override_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_executable(tmp_path / 'bin' / 'git')
        expected = _make_executable(tmp_path / 'child' / 'bin' / 'git')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('PATH', str(tmp_path / 'missing'))

        result = resolve_git_executable(cwd='child', env={'PATH': 'bin'})

        assert Path(result).is_absolute()
        assert Path(result) == expected

    @pytest.mark.parametrize('entry', ['', '.', 'bin'])
    def test_explicit_relative_entries_use_child_cwd(self, tmp_path: Path, entry: str) -> None:
        expected = _make_executable(tmp_path / entry / 'git')

        result = resolve_git_executable(cwd=tmp_path, env={'PATH': entry})

        assert Path(result).is_absolute()
        assert Path(result) == expected

    def test_missing_executable_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_git_executable(env={'PATH': str(tmp_path)})

    def test_ordered_path_skips_missing_directories_and_nonexecutables(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / 'directory' / 'git'
        directory.mkdir(parents=True)
        nonexecutable = _make_executable(tmp_path / 'nonexecutable' / 'git')
        nonexecutable.chmod(0o644)
        first = _make_executable(tmp_path / 'first' / 'git')
        second = _make_executable(tmp_path / 'second' / 'git')
        entries = [tmp_path / 'missing', directory.parent, nonexecutable.parent]

        for selected, other in ((first, second), (second, first)):
            path = os.pathsep.join(map(str, [*entries, selected.parent, other.parent]))
            assert resolve_git_executable(env={'PATH': path}) == str(selected)

    def test_path_changes_between_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for directory in ('first', 'second'):
            expected = _make_executable(tmp_path / directory / 'git')
            monkeypatch.setenv('PATH', str(expected.parent))
            assert resolve_git_executable() == str(expected)

    def test_symlink_is_returned_without_canonicalization(self, tmp_path: Path) -> None:
        target = _make_executable(tmp_path / 'target')
        link = tmp_path / 'git'
        link.symlink_to(target)

        assert resolve_git_executable(env={'PATH': str(tmp_path)}) == str(link)


# These tests exercise qualified file selection on any host. They do not prove
# native Windows CreateProcess behavior; the native integration tests do that.
class TestWindowsGitSelection:
    @pytest.mark.parametrize('path', [None, ''])
    def test_absent_or_empty_parent_path_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str | None
    ) -> None:
        _make_executable(tmp_path / 'git.exe')
        monkeypatch.chdir(tmp_path)
        if path is None:
            monkeypatch.delenv('PATH', raising=False)
        else:
            monkeypatch.setenv('PATH', path)

        with pytest.raises(FileNotFoundError):
            _resolve_windows_git('git')

    @pytest.mark.parametrize('command', ['git', 'git.exe'])
    def test_exact_exe_ignores_current_directory_and_pathext(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        _make_executable(tmp_path / 'git.exe')
        _make_executable(tmp_path / 'first' / 'git.cmd')
        _make_executable(tmp_path / 'first' / 'git.exe.cmd')
        expected = _make_executable(tmp_path / 'second' / 'git.exe')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            'PATH', os.pathsep.join(map(str, [tmp_path / 'first', expected.parent]))
        )
        monkeypatch.setenv('PATHEXT', '.CMD;.BAT')

        assert _resolve_windows_git(command) == str(expected)

    @pytest.mark.parametrize('entry', ['', '.', 'bin'])
    def test_explicit_relative_entry_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
    ) -> None:
        expected = _make_executable(tmp_path / entry / 'git.exe')
        other = _make_executable(tmp_path / 'other' / 'git.exe')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('PATH', os.pathsep.join([entry, str(other.parent)]))

        result = _resolve_windows_git('git')

        assert Path(result).is_absolute()
        assert Path(result) == expected

    def test_missing_exact_exe_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_executable(tmp_path / 'git.cmd')
        monkeypatch.setenv('PATH', str(tmp_path))

        with pytest.raises(FileNotFoundError):
            _resolve_windows_git('git')

    def test_path_order_and_changes_are_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _make_executable(tmp_path / 'first' / 'git.exe')
        second = _make_executable(tmp_path / 'second' / 'git.exe')
        for selected, other in ((first, second), (second, first)):
            monkeypatch.setenv('PATH', os.pathsep.join(map(str, [selected.parent, other.parent])))
            assert _resolve_windows_git('git') == str(selected)

    @pytest.mark.skipif(os.name == 'nt', reason='Portable symlink fixture needs POSIX privileges')
    def test_symlink_is_returned_without_canonicalization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _make_executable(tmp_path / 'target.exe')
        link = tmp_path / 'git.exe'
        link.symlink_to(target)
        monkeypatch.setenv('PATH', str(tmp_path))

        assert _resolve_windows_git('git') == str(link)


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows executable lookup')
def test_native_windows_uses_parent_path_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _make_executable(tmp_path / 'bin' / 'git.exe')
    child = tmp_path / 'child'
    _make_executable(child / 'bin' / 'git.exe')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', 'bin')

    result = resolve_git_executable(cwd=child, env={'PATH': str(child / 'bin')})

    assert Path(result).is_absolute()
    assert Path(result) == expected
