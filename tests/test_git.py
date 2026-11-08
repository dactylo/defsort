import os
import subprocess
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

import defsort._git as git_module
from defsort._git import (
    batch_fetch_worktree_baselines,
    is_hook_enabled,
    is_null_hash,
    prepare_index_entry,
    read_baseline_blobs,
    read_staged_blobs,
    repo_relative,
)
from defsort._types import BlobReadResult, BlobReadStatus
from defsort._vendor.gitfiles._blobs import BlobRead, UnsupportedGitVersionError
from defsort._vendor.gitfiles._listings import StagedFileInfo, get_staged_py_info
from defsort._vendor.gitfiles._run import GitCommandError
from defsort._vendor.hookfront import _terminal
from tests._helpers import (
    init_empty_git_repo,
    init_git_repo,
    run_setup,
)

_SRC_NEEDS_SORT = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'


class TestBatchFetchWorktreeBaselines:
    def test_crlf_preserved(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        (tmp_path / '.gitattributes').write_text('*.py -text\n', encoding='utf-8')
        f = tmp_path / 'mod.py'
        f.write_bytes(b'x = 1\r\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['mod.py'])
        assert result is not None
        assert result['mod.py'].status is BlobReadStatus.CONTENT
        assert result['mod.py'].content == 'x = 1\r\n'

    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        result = batch_fetch_worktree_baselines(str(tmp_path), [])
        assert result == {}

    def test_existing_file_returns_content(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        (tmp_path / 'mod.py').write_bytes(b'x = 1\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['mod.py'])
        assert result is not None
        assert result['mod.py'].status is BlobReadStatus.CONTENT
        assert result['mod.py'].content == 'x = 1\n'

    def test_head_tree_fails_only_its_path(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        tree_path = tmp_path / 'changed.py'
        tree_path.mkdir()
        (tree_path / 'entry').write_text('tree payload\n', encoding='utf-8')
        (tmp_path / 'after.py').write_bytes(b'after = 2\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        (tree_path / 'entry').unlink()
        tree_path.rmdir()
        tree_path.write_text('now a file\n', encoding='utf-8')

        result = batch_fetch_worktree_baselines(str(tmp_path), ['changed.py', 'after.py'])

        assert result is not None
        assert result['changed.py'].status is BlobReadStatus.INFRA_ERROR
        assert result['after.py'].content == 'after = 2\n'

    def test_missing_file_returns_sentinel(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        (tmp_path / 'placeholder.py').write_text('# p\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['new.py'])
        assert result is not None
        assert result['new.py'].status is BlobReadStatus.MISSING

    @pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain line feeds')
    def test_missing_newline_path_does_not_shift_following_response(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        (tmp_path / 'after.py').write_text('after = 2\n', encoding='utf-8')
        run_setup(['git', 'add', 'after.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        missing = 'missing\nname.py'

        result = batch_fetch_worktree_baselines(str(tmp_path), [missing, 'after.py'])

        assert result is not None
        assert result[missing].status is BlobReadStatus.MISSING
        assert result['after.py'].content == 'after = 2\n'

    def test_non_utf8_file_returns_none(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        f = tmp_path / 'binary.py'
        f.write_bytes(b'x = "\xff\xfe"\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['binary.py'])
        assert result is not None
        assert result['binary.py'].status is BlobReadStatus.DECODE_FAILED

    @pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain line feeds')
    def test_tracked_newline_path_does_not_shift_following_response(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        unusual = 'we\nird.py'
        (tmp_path / unusual).write_text('unusual = 1\n', encoding='utf-8')
        (tmp_path / 'after.py').write_text('after = 2\n', encoding='utf-8')
        run_setup(['git', 'add', '--', unusual, 'after.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)

        result = batch_fetch_worktree_baselines(str(tmp_path), [unusual, 'after.py'])

        assert result is not None
        assert result[unusual].content == 'unusual = 1\n'
        assert result['after.py'].content == 'after = 2\n'

    @pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot end in carriage return')
    def test_trailing_carriage_return_path_is_exact(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        unusual = 'ends-in-cr.py\r'
        (tmp_path / unusual).write_text('unusual = 1\n', encoding='utf-8')
        (tmp_path / 'after.py').write_text('after = 2\n', encoding='utf-8')
        run_setup(['git', 'add', '--', unusual, 'after.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)

        result = batch_fetch_worktree_baselines(str(tmp_path), [unusual, 'after.py'])

        assert result is not None
        assert result[unusual].content == 'unusual = 1\n'
        assert result['after.py'].content == 'after = 2\n'

    def test_unborn_repo_all_missing(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['a.py'])
        # All entries should be missing sentinel
        assert result is not None
        assert result['a.py'].status is BlobReadStatus.MISSING


class TestIsHookEnabled:
    def test_defsort_hook_false_uses_explicit_cwd(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        run_setup(['git', 'config', 'defsort.hook', 'false'], cwd=tmp_path)
        assert is_hook_enabled(cwd=str(tmp_path)) is False


class TestIsNullHash:
    def test_empty_string(self) -> None:
        assert is_null_hash('') is False

    def test_mixed_zeros(self) -> None:
        assert is_null_hash('0' * 39 + '1') is False

    def test_non_null(self) -> None:
        assert is_null_hash('abc123') is False

    def test_real_sha1(self) -> None:
        assert is_null_hash('a' * 40) is False

    def test_sha1_null(self) -> None:
        assert is_null_hash('0' * 40) is True

    def test_sha256_null(self) -> None:
        assert is_null_hash('0' * 64) is True

    def test_starts_with_zeros(self) -> None:
        assert is_null_hash('0000' + 'a' * 36) is False


class TestPrepareIndexEntry:
    def test_creates_valid_blob(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        content = 'def foo():\n    pass\n'
        entry = prepare_index_entry(str(tmp_path), 'test.py', content, '100644')
        assert entry is not None
        mode, blob_hash, path = entry
        assert mode == '100644'
        assert path == 'test.py'
        # Read the blob back and verify exact content
        result = run_setup(['git', '-C', str(tmp_path), 'cat-file', 'blob', blob_hash])
        assert result.stdout == content.encode('utf-8')
        assert b'\r\n' not in result.stdout

    def test_mixed_line_endings_preserved_in_blob(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        content = 'def foo():\r\n    pass\n \t\rdef bar():\n    pass'
        entry = prepare_index_entry(
            str(tmp_path),
            'test.py',
            content,
            '100644',
        )
        assert entry is not None
        _, blob_hash, _ = entry
        result = run_setup(['git', '-C', str(tmp_path), 'cat-file', 'blob', blob_hash])
        assert result.stdout == b'def foo():\r\n    pass\n \t\rdef bar():\n    pass'

    def test_reports_cause_on_git_failure(self, tmp_path: Path) -> None:
        with pytest.raises(GitCommandError):
            prepare_index_entry(str(tmp_path), 'test.py', 'content\n', '100644')


class TestReadBaselineBlobs:
    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        result = read_baseline_blobs(str(tmp_path), [])
        assert result == {}

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            pytest.param(b'def foo():\r\n    pass\r\n', 'def foo():\r\n    pass\r\n', id='crlf'),
            pytest.param(b'def foo():\r    pass\r', 'def foo():\r    pass\r', id='lone-cr'),
            pytest.param(
                b'def foo():\r\n    pass\n \t\rdef bar():\n    pass',
                'def foo():\r\n    pass\n \t\rdef bar():\n    pass',
                id='mixed-no-final-ending',
            ),
        ],
    )
    def test_exact_content_preserved(
        self,
        tmp_path: Path,
        raw: bytes,
        expected: str,
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=raw
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [StagedFileInfo('exact.py', '100644', 'new_hash', blob_hash, 'M')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        entry = result['exact.py']
        assert entry.status is BlobReadStatus.CONTENT
        assert entry.content == expected

    def test_git_failure_reports_cause(self, tmp_path: Path) -> None:
        info = [StagedFileInfo('test.py', '100644', 'h', 'bad_hash', 'M')]
        with pytest.raises(GitCommandError):
            read_baseline_blobs(str(tmp_path / 'nonexistent'), info)

    def test_reads_baseline_blobs(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'],
            stdin=b'def foo():\n    pass\n',
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [StagedFileInfo('test.py', '100644', 'new_hash', blob_hash, 'M')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        assert result['test.py'].status is BlobReadStatus.CONTENT
        assert result['test.py'].content == 'def foo():\n    pass\n'

    def test_skips_new_files(self, tmp_path: Path) -> None:
        null_hash = '0' * 40
        info = [StagedFileInfo('new.py', '100644', 'some_hash', null_hash, 'A')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        assert result == {}


class TestReadStagedBlobs:
    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        result = read_staged_blobs(str(tmp_path), [])
        assert result is not None
        assert result == {}

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            pytest.param(b'def foo():\r\n    pass\r\n', 'def foo():\r\n    pass\r\n', id='crlf'),
            pytest.param(b'def foo():\r    pass\r', 'def foo():\r    pass\r', id='lone-cr'),
            pytest.param(
                b'def foo():\r\n    pass\n \t\rdef bar():\n    pass',
                'def foo():\r\n    pass\n \t\rdef bar():\n    pass',
                id='mixed-no-final-ending',
            ),
        ],
    )
    def test_exact_content_preserved(
        self,
        tmp_path: Path,
        raw: bytes,
        expected: str,
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=raw
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [StagedFileInfo('exact.py', '100644', blob_hash, '0' * 40, 'A')]
        result = read_staged_blobs(str(tmp_path), info)
        assert result is not None
        entry = result['exact.py']
        assert entry.status is BlobReadStatus.CONTENT
        assert entry.content == expected

    def test_git_failure_reports_cause(self, tmp_path: Path) -> None:
        info = [StagedFileInfo('test.py', '100644', 'bad_hash', '0' * 40, 'A')]
        with pytest.raises(GitCommandError):
            read_staged_blobs(str(tmp_path / 'nonexistent'), info)

    def test_missing_blob_returns_none_value(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        fake_hash = '0' * 40
        info = [StagedFileInfo('ghost.py', '100644', fake_hash, '0' * 40, 'A')]
        result = read_staged_blobs(str(tmp_path), info)
        assert result is not None
        assert result['ghost.py'].status is BlobReadStatus.MISSING

    def test_non_utf8_blob_returns_none_value(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'binary.py').write_bytes(b'\x80\x81\x82\n')
        run_setup(['git', '-C', str(tmp_path), 'add', 'binary.py'])
        info_list = get_staged_py_info(str(tmp_path))
        assert info_list is not None
        assert len(info_list) == 1
        result = read_staged_blobs(str(tmp_path), info_list)
        assert result is not None
        assert result['binary.py'].status is BlobReadStatus.DECODE_FAILED

    def test_reads_multiple_blobs(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'alpha.py').write_bytes(b'a = 1\n')
        (tmp_path / 'bravo.py').write_bytes(b'b = 2\n')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        info_list = get_staged_py_info(str(tmp_path))
        assert info_list is not None
        result = read_staged_blobs(str(tmp_path), info_list)
        assert result is not None
        assert result['alpha.py'].status is BlobReadStatus.CONTENT
        assert result['alpha.py'].content == 'a = 1\n'
        assert result['bravo.py'].status is BlobReadStatus.CONTENT
        assert result['bravo.py'].content == 'b = 2\n'


class TestRepoRelative:
    @pytest.mark.skipif(os.name == 'nt', reason='POSIX backslash is a filename character')
    def test_posix_backslash_is_kept(self) -> None:
        assert repo_relative('/repo/a\\b.py', '/repo') == 'a\\b.py'

    def test_windows_separators_become_slashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def backslash_relpath(_path: str, _start: str | None = None) -> str:
            return r'src\test.py'

        monkeypatch.setattr(os.path, 'relpath', backslash_relpath)
        monkeypatch.setattr(_terminal, 'PurePath', PureWindowsPath)

        assert repo_relative('/repo/src/test.py', '/repo') == 'src/test.py'


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain non-UTF-8 bytes')
class TestSurrogatePathProtocols:
    def test_batch_fetch_accepts_surrogate_path(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        path = b'weird\xff.py'.decode('utf-8', errors='surrogateescape')
        blob = (
            run_setup(
                ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=b'x = 1\n'
            )
            .stdout.decode('ascii')
            .strip()
        )
        run_setup(
            [
                'git',
                '-C',
                str(tmp_path),
                'update-index',
                '--add',
                '--cacheinfo',
                f'100644,{blob},{path}',
            ]
        )
        run_setup(['git', '-C', str(tmp_path), 'commit', '-qm', 'init'])

        results = batch_fetch_worktree_baselines(str(tmp_path), [path])

        assert results is not None
        assert list(results) == [path]
        assert results[path].status is BlobReadStatus.CONTENT
        assert results[path].content == 'x = 1\n'


def _latin1_encode(path: str) -> bytes:
    return path.encode('latin-1')


def _no_rename_sources(_root: str) -> dict[str, str]:
    return {}


@pytest.mark.parametrize(
    ('raw', 'object_type', 'status', 'detail'),
    [
        (b'', b'tree', BlobReadStatus.INFRA_ERROR, 'git object is not a blob'),
        (b'\xff', b'blob', BlobReadStatus.DECODE_FAILED, 'encoding'),
        (b'# coding: utf-8\n\xff', b'blob', BlobReadStatus.DECODE_FAILED, 'invalid start byte'),
        (b'# coding: bad-codec\nx', b'blob', BlobReadStatus.DECODE_FAILED, 'encoding'),
        (b'broken', None, BlobReadStatus.INFRA_ERROR, 'unexpected git cat-file output'),
    ],
)
def test_blob_read_result_detail_names_cause(
    raw: bytes, object_type: bytes | None, status: BlobReadStatus, detail: str
) -> None:
    read = (
        BlobRead('unavailable')
        if object_type is None
        else (BlobRead('content', raw) if object_type == b'blob' else BlobRead('not-blob'))
    )
    result = git_module._blob_result(read)
    assert result.status is status
    assert result.detail is not None
    assert detail in result.detail


class TestReadHeadBaselines:
    def test_exact_head_spelling_is_final(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'Mod.py'
        path.write_bytes(b'exact = 1\n')
        for name, source in [('Mod.py', b'exact = 1\n'), ('mod.py', b'other = 2\n')]:
            blob = (
                run_setup(['git', 'hash-object', '-w', '--stdin'], cwd=tmp_path, stdin=source)
                .stdout.decode()
                .strip()
            )
            run_setup(
                ['git', 'update-index', '--add', '--cacheinfo', f'100644,{blob},{name}'],
                cwd=tmp_path,
            )
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)

        other = tmp_path / 'mod.py'
        if not other.exists():
            other.hardlink_to(path)

        def unexpected(_root: str) -> frozenset[str]:
            pytest.fail('exact HEAD matches must not fetch tracked paths')

        monkeypatch.setattr(git_module, 'get_tracked_files', unexpected)
        monkeypatch.setattr(git_module, 'head_rename_sources', unexpected)
        result = git_module.read_head_baselines(str(tmp_path), [path])
        assert result[path].content == 'exact = 1\n'

    def test_renamed_miss_retries_at_its_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / 'new.py'
        path.touch()
        requests: list[list[str]] = []
        baseline = BlobReadResult(status=BlobReadStatus.CONTENT, content='baseline = 1\n')

        def fetch(_root: str, paths: Iterable[str]) -> dict[str, BlobReadResult]:
            names = list(paths)
            requests.append(names)
            return {
                name: baseline
                if name == 'old.py'
                else BlobReadResult(status=BlobReadStatus.MISSING)
                for name in names
            }

        def sources(_root: str) -> dict[str, str]:
            assert requests == [['new.py']]
            return {'new.py': 'old.py'}

        def unexpected(_root: str) -> frozenset[str]:
            pytest.fail('rename source must be read before tracked spellings')

        monkeypatch.setattr(git_module, 'batch_fetch_worktree_baselines', fetch)
        monkeypatch.setattr(git_module, 'head_rename_sources', sources, raising=False)
        monkeypatch.setattr(git_module, 'get_tracked_files', unexpected)
        assert git_module.read_head_baselines(str(tmp_path), [path]) == {path: baseline}
        assert requests == [['new.py'], ['old.py']]

    @pytest.mark.parametrize(
        'retry_result',
        [
            BlobReadResult(status=BlobReadStatus.CONTENT, content='baseline = 1\n'),
            BlobReadResult(status=BlobReadStatus.MISSING),
            BlobReadResult(status=BlobReadStatus.DECODE_FAILED, detail='bad encoding'),
            BlobReadResult(status=BlobReadStatus.INFRA_ERROR, detail='bad framing'),
        ],
    )
    def test_respelled_batch_preserves_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_result: BlobReadResult
    ) -> None:
        path = tmp_path / 'mod.py'
        path.touch()
        other = tmp_path / 'Mod.py'
        if not other.exists():
            other.hardlink_to(path)

        def tracked(_root: str) -> frozenset[str]:
            return frozenset({'Mod.py'})

        monkeypatch.setattr(git_module, 'get_tracked_files', tracked)
        monkeypatch.setattr(git_module, 'head_rename_sources', _no_rename_sources)
        requests: list[list[str]] = []

        def fetch(_root: str, paths: Iterable[str]) -> dict[str, BlobReadResult]:
            names = list(paths)
            requests.append(names)
            return {
                name: retry_result
                if name == 'Mod.py'
                else BlobReadResult(status=BlobReadStatus.MISSING)
                for name in names
            }

        monkeypatch.setattr(git_module, 'batch_fetch_worktree_baselines', fetch)
        result = git_module.read_head_baselines(str(tmp_path), [path])
        assert result[path] == retry_result
        assert requests == [['mod.py'], ['Mod.py']]

    @pytest.mark.parametrize('batch', [1, 2])
    @pytest.mark.parametrize(
        'error',
        [
            GitCommandError('denied'),
            UnsupportedGitVersionError('2.41', tool='defsort', purpose='Git-backed sorting'),
        ],
    )
    def test_batch_command_failures_propagate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int, error: Exception
    ) -> None:
        path = tmp_path / 'mod.py'
        path.touch()
        other = tmp_path / 'Mod.py'
        if not other.exists():
            other.hardlink_to(path)

        def tracked(_root: str) -> frozenset[str]:
            return frozenset({'Mod.py'})

        monkeypatch.setattr(git_module, 'get_tracked_files', tracked)
        monkeypatch.setattr(git_module, 'head_rename_sources', _no_rename_sources)
        calls = 0

        def fetch(_root: str, paths: Iterable[str]) -> dict[str, BlobReadResult]:
            nonlocal calls
            calls += 1
            if calls == batch:
                raise error
            return {name: BlobReadResult(status=BlobReadStatus.MISSING) for name in paths}

        monkeypatch.setattr(git_module, 'batch_fetch_worktree_baselines', fetch)
        with pytest.raises(type(error), match=r'denied|2\.41'):
            git_module.read_head_baselines(str(tmp_path), [path])

    @pytest.mark.parametrize('staged', [False, True])
    def test_new_file_stays_missing(self, tmp_path: Path, *, staged: bool) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'new.py'
        path.write_bytes(b'new = 1\n')
        if staged:
            run_setup(['git', 'add', 'new.py'], cwd=tmp_path)

        result = git_module.read_head_baselines(str(tmp_path), [path])

        assert result[path].status is BlobReadStatus.MISSING

    def test_respelled_rename_reads_its_source(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        old = tmp_path / 'old.py'
        old.write_bytes(b'baseline = 1\n')
        run_setup(['git', 'add', 'old.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        run_setup(['git', 'mv', 'old.py', 'New.py'], cwd=tmp_path)
        target = tmp_path / 'New.py'
        alias = tmp_path / 'new.py'
        if not alias.exists():
            alias.hardlink_to(target)
        result = git_module.read_head_baselines(str(tmp_path), [alias])
        assert result[alias].content == 'baseline = 1\n'

    def test_tracked_list_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'Mod.py'
        path.write_bytes(b'exact = 1\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        other = tmp_path / 'new.py'
        other.touch()

        def denied(_root: str) -> frozenset[str]:
            raise GitCommandError('denied')

        monkeypatch.setattr(git_module, 'get_tracked_files', denied)
        with pytest.raises(GitCommandError, match='denied'):
            git_module.read_head_baselines(str(tmp_path), [path, other])

    def test_unavailable_repository_key_is_not_a_new_file(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'missing.py'

        result = git_module.read_head_baselines(str(tmp_path), [path])

        assert result[path] == BlobReadResult(
            status=BlobReadStatus.INFRA_ERROR,
            detail='cannot match the file to a single path in Git',
        )


def test_decode_failed() -> None:
    result = git_module._blob_result(BlobRead('content', b'\x80'))
    assert result.status is BlobReadStatus.DECODE_FAILED
    assert result.content is None
    following = git_module._blob_result(BlobRead('content', b'hello'))
    assert following.status is BlobReadStatus.CONTENT
    assert following.content == 'hello'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX filesystem codec model')
def test_head_baseline_requests_use_the_git_path_codec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_empty_git_repo(tmp_path)
    requests: list[object] = []
    original_run = subprocess.run

    def capture_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        requests.append(kwargs.get('input'))
        return original_run(
            command,
            input=cast('bytes | None', kwargs.get('input')),
            cwd=cast('str', kwargs['cwd']),
            capture_output=True,
            check=False,
        )

    monkeypatch.setattr(git_module, 'encode_git_path', _latin1_encode, raising=False)
    monkeypatch.setattr(subprocess, 'run', capture_run)
    batch_fetch_worktree_baselines(str(tmp_path), ['café.py'])
    assert requests == [b'HEAD:caf\xe9.py\0']


@pytest.mark.parametrize(
    'query',
    [
        partial(batch_fetch_worktree_baselines, paths=['mod.py']),
        partial(prepare_index_entry, path='mod.py', content='x = 1\n', mode='100644'),
    ],
)
def test_required_queries_report_launch_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: Callable[[str], object]
) -> None:
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(
        GitCommandError, match='git could not be run: Git executable not found on PATH'
    ):
        query(str(tmp_path))


@pytest.mark.parametrize(
    ('raw', 'content', 'encoding'),
    [
        (b'a\0b', 'a\0b', 'utf-8'),
        (b'\xef\xbb\xbfx = 1\n', 'x = 1\n', 'utf-8-sig'),
        (b'# coding: latin-1\nx = "caf\xe9"\n', '# coding: latin-1\nx = "café"\n', 'iso-8859-1'),
    ],
)
def test_blob_result_preserves_source_decoding(raw: bytes, content: str, encoding: str) -> None:
    result = git_module._blob_result(BlobRead('content', raw))
    assert result == BlobReadResult(
        status=BlobReadStatus.CONTENT, content=content, encoding=encoding
    )
