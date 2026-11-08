import os
import subprocess
import sys
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _repo,
)
from defsort._vendor.gitfiles._repo import (
    RepoDiscoveryResult,
    RepoDiscoveryStatus,
    discover_repo_root,
)

from ._support import (
    git,
    latin1_decode,
)


class TestDiscoverRepoRoot:
    def test_bare_repository_is_not_worktree(self, isolated: Path) -> None:
        git('init', '--bare', '-q', cwd=isolated)
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.NOT_WORKTREE
        )

    def test_embedded_nul_working_directory_is_error(self) -> None:
        result = discover_repo_root('invalid\0cwd')
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail is not None
        assert result.detail.startswith('git could not be run: ')

    def test_empty_toplevel_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def empty(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess([], 0, b'', b'')

        monkeypatch.setattr(subprocess, 'run', empty)
        assert discover_repo_root('/w') == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='git rev-parse --show-toplevel printed no path'
        )

    def test_first_failure_detail_survives_bare_probe(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (isolated / '.git').mkdir()
        results = iter(
            [
                subprocess.CompletedProcess([], 128, b'', b'fatal: first'),
                subprocess.CompletedProcess([], 128, b'', b'fatal: second'),
            ]
        )

        def next_result(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return next(results)

        monkeypatch.setattr(subprocess, 'run', next_result)
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='fatal: first'
        )

    def test_git_failure_with_marker_is_error(self, isolated: Path) -> None:
        (isolated / '.git').mkdir()
        result = discover_repo_root(str(isolated))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    @pytest.mark.parametrize('target_name', ['missing', 'file'])
    def test_invalid_target_is_error(self, isolated: Path, target_name: str) -> None:
        target = isolated / target_name
        if target_name == 'file':
            target.write_text('not a directory', encoding='utf-8')
        result = discover_repo_root(str(target))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    @pytest.mark.parametrize('probe', ['toplevel', 'bare'])
    def test_launch_failure_is_error(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch, probe: str
    ) -> None:
        calls = 0

        def fail_probe(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal calls
            calls += 1
            if probe == 'bare' and calls == 1:
                return subprocess.CompletedProcess(command, 128, b'', b'fatal: first')
            flag = '--show-toplevel' if probe == 'toplevel' else '--is-bare-repository'
            assert command[1:] == ['rev-parse', flag]
            raise OSError('git unavailable')

        monkeypatch.setattr(subprocess, 'run', fail_probe)
        result = discover_repo_root(str(isolated))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    @pytest.mark.parametrize('failure', ['decode', 'empty', 'malformed'])
    def test_bad_bare_probe_preserves_toplevel_detail(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        calls = 0

        def bad_probe(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal calls
            calls += 1
            if calls == 1:
                assert command[1:] == ['rev-parse', '--show-toplevel']
                return subprocess.CompletedProcess(command, 128, b'', b'fatal: first')
            assert command[1:] == ['rev-parse', '--is-bare-repository']
            if failure == 'decode':
                raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')
            output = b'\n' if failure == 'empty' else b'not-a-boolean\n'
            return subprocess.CompletedProcess(command, 0, output, b'')

        monkeypatch.setattr(subprocess, 'run', bad_probe)
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='fatal: first'
        )
        assert calls == 2

    def test_marker_inspection_failure_is_error(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def marker_uninspectable(_cwd: str) -> None:
            return None

        monkeypatch.setattr(_repo, '_git_marker_present', marker_uninspectable)
        result = discover_repo_root(str(isolated))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    def test_marker_stat_failure_is_error(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = Path.lstat

        def fail_marker(path: Path) -> os.stat_result:
            if path.name == '.git':
                raise PermissionError
            return original(path)

        monkeypatch.setattr(Path, 'lstat', fail_marker)
        result = discover_repo_root(str(isolated))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    def test_missing_git_is_an_error_with_the_runner_detail(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('PATH', str(isolated))
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR,
            detail='git could not be run: Git executable not found on PATH',
        )

    def test_nested_bare_repository_is_not_worktree(self, repo: Path) -> None:
        bare = repo / 'nested.git'
        git('init', '--bare', '-q', str(bare), cwd=repo)
        assert discover_repo_root(str(bare)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.NOT_WORKTREE
        )

    def test_non_repository_is_not_worktree(self, isolated: Path) -> None:
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.NOT_WORKTREE
        )

    def test_not_bare_without_marker_is_an_error(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = iter(
            [
                subprocess.CompletedProcess([], 128, b'', b'fatal: x'),
                subprocess.CompletedProcess([], 0, b'false\n', b''),
            ]
        )

        def next_result(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return next(results)

        monkeypatch.setattr(subprocess, 'run', next_result)
        assert discover_repo_root(str(isolated)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='fatal: x'
        )

    @pytest.mark.parametrize('root', [b'/w/r \n', b'/w/r\n\n'])
    @pytest.mark.skipif(os.name == 'nt', reason='POSIX path spelling')
    def test_only_the_final_newline_is_removed(
        self, root: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def with_root(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess([], 0, root, b'')

        monkeypatch.setattr(subprocess, 'run', with_root)
        assert discover_repo_root('/w').root == os.fsdecode(root.removesuffix(b'\n'))

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX path spelling')
    def test_root_uses_the_git_path_codec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo, 'decode_git_path', latin1_decode)

        def with_root(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess([], 0, b'/w/caf\xc3\xa9\n', b'')

        monkeypatch.setattr(subprocess, 'run', with_root)
        assert discover_repo_root('/w').root == b'/w/caf\xc3\xa9'.decode('latin-1')

    @pytest.mark.parametrize('name', ['repo ', 'repo\t', 'repo\n'])
    @pytest.mark.skipif(
        os.name == 'nt', reason='Windows filenames cannot contain tabs or newlines'
    )
    def test_root_ending_in_whitespace_is_found(self, isolated: Path, name: str) -> None:
        path = isolated / name
        path.mkdir()
        git('init', '-q', cwd=path)
        result = discover_repo_root(str(path))
        assert result.status is RepoDiscoveryStatus.WORKTREE
        assert result.root is not None
        assert Path(result.root) == path.resolve()

    @pytest.mark.skipif(sys.platform != 'linux', reason='Linux filesystem byte path')
    def test_non_utf8_root_is_found(self, isolated: Path) -> None:
        path = isolated / os.fsdecode(b'repo-\xff')
        path.mkdir()
        git('init', '-q', cwd=path)
        result = discover_repo_root(str(path))
        assert result.status is RepoDiscoveryStatus.WORKTREE
        assert result.root is not None
        assert Path(result.root) == path.resolve()

    def test_subdirectory_returns_root(self, repo: Path) -> None:
        nested = repo / 'nested'
        nested.mkdir()
        result = discover_repo_root(str(nested))
        assert result.status is RepoDiscoveryStatus.WORKTREE
        assert result.root is not None
        assert Path(result.root).resolve() == repo.resolve()

    def test_toplevel_decode_failure_is_an_error(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_decode(*_args: object, **_kwargs: object) -> None:
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_decode)
        result = discover_repo_root(str(isolated))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail

    def test_unexpected_toplevel_status_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = 0

        def unexpected(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess([], 129, b'', b'usage')

        monkeypatch.setattr(subprocess, 'run', unexpected)
        assert discover_repo_root('/w') == RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='usage'
        )
        assert calls == 1

    def test_untrusted_repository_is_an_error_with_gits_reason(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('GIT_TEST_ASSUME_DIFFERENT_OWNER', '1')
        result = discover_repo_root(str(repo))
        assert result.status is RepoDiscoveryStatus.ERROR
        assert result.detail is not None
        assert result.detail.startswith('fatal: detected dubious ownership in repository at ')

    def test_worktree_returns_root(self, repo: Path) -> None:
        assert discover_repo_root(str(repo)) == RepoDiscoveryResult(
            RepoDiscoveryStatus.WORKTREE, repo.resolve().as_posix()
        )


class TestRepoDiscoveryResult:
    @pytest.mark.parametrize(
        ('status', 'root'),
        [
            (RepoDiscoveryStatus.WORKTREE, None),
            (RepoDiscoveryStatus.WORKTREE, ''),
            (RepoDiscoveryStatus.NOT_WORKTREE, '/repo'),
            (RepoDiscoveryStatus.ERROR, '/repo'),
        ],
    )
    def test_invalid_status_root_is_rejected(
        self, status: RepoDiscoveryStatus, root: str | None
    ) -> None:
        with pytest.raises(ValueError, match='invalid repository discovery result'):
            RepoDiscoveryResult(status, root)

    @pytest.mark.parametrize(
        'status', [RepoDiscoveryStatus.WORKTREE, RepoDiscoveryStatus.NOT_WORKTREE]
    )
    def test_detail_without_error_is_rejected(self, status: RepoDiscoveryStatus) -> None:
        root = '/repo' if status is RepoDiscoveryStatus.WORKTREE else None
        with pytest.raises(ValueError, match='invalid repository discovery result detail'):
            RepoDiscoveryResult(status, root, 'fatal')
