import errno
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast

import pytest

import defsort._git as git_module
from defsort._git import (
    RepoDiscoveryStatus,
    _CatFileRequest,
    _git_config_bool,
    _parse_cat_file_batch,
    _RepoDiscoveryResult,
    _StagedFileInfo,
    apply_index_entries,
    batch_fetch_worktree_baselines,
    detect_renames,
    discover_repo_root,
    get_modified_tracked_py_files,
    get_repo_root,
    get_staged_py_info,
    is_hook_enabled,
    is_null_hash,
    prepare_index_entry,
    read_baseline_blobs,
    read_staged_blobs,
    repo_relative,
    resolve_pathspec_py_files,
    resolve_sync_paths,
    try_get_repo_root,
)
from defsort._types import BlobReadStatus, PreCommitAdd
from tests._helpers import (
    _ENV,
    _init_empty_git_repo,
    _init_git_repo,
    process_transcript,
    run_setup,
)

_SRC_NEEDS_SORT = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'


class TestBatchFetchWorktreeBaselines:
    def test_crlf_preserved(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
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
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'mod.py').write_bytes(b'x = 1\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['mod.py'])
        assert result is not None
        assert result['mod.py'].status is BlobReadStatus.CONTENT
        assert result['mod.py'].content == 'x = 1\n'

    def test_head_tree_fails_only_its_path(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
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
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'placeholder.py').write_text('# p\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['new.py'])
        assert result is not None
        assert result['new.py'].status is BlobReadStatus.MISSING

    @pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain line feeds')
    def test_missing_newline_path_does_not_shift_following_response(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'after.py').write_text('after = 2\n', encoding='utf-8')
        run_setup(['git', 'add', 'after.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        missing = 'missing\nname.py'

        result = batch_fetch_worktree_baselines(str(tmp_path), [missing, 'after.py'])

        assert result is not None
        assert result[missing].status is BlobReadStatus.MISSING
        assert result['after.py'].content == 'after = 2\n'

    def test_non_utf8_file_returns_none(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        f = tmp_path / 'binary.py'
        f.write_bytes(b'x = "\xff\xfe"\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add'], cwd=tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['binary.py'])
        assert result is not None
        assert result['binary.py'].status is BlobReadStatus.DECODE_FAILED

    @pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain line feeds')
    def test_tracked_newline_path_does_not_shift_following_response(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
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
        _init_empty_git_repo(tmp_path)
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
        _init_empty_git_repo(tmp_path)
        result = batch_fetch_worktree_baselines(str(tmp_path), ['a.py'])
        # All entries should be missing sentinel
        assert result is not None
        assert result['a.py'].status is BlobReadStatus.MISSING


class TestCatFileBatchCommand:
    def test_cat_file_launch_error_does_not_probe_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fail_run(command: list[str], *_args: object, **_kwargs: object) -> None:
            calls.append(command)
            raise OSError('git unavailable')

        monkeypatch.setattr(subprocess, 'run', fail_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None
        assert calls == [['git', 'cat-file', '--batch', '-Z']]

    def test_failed_version_probe_keeps_generic_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            calls.append(command)
            if command == ['git', '--version']:
                return subprocess.CompletedProcess(command, 1, '', 'failure')
            return subprocess.CompletedProcess(command, 1, b'', b'failure')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None
        assert calls == [
            ['git', 'cat-file', '--batch', '-Z'],
            ['git', '--version'],
        ]

    def test_hash_reader_uses_path_key_and_nul_hash_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[list[str], bytes | None]] = []
        selected = str(Path('/trusted/git').absolute())

        def resolve(**_kwargs: object) -> str:
            return selected

        monkeypatch.setattr(git_module, 'resolve_git_executable', resolve)

        def complete_run(
            command: list[str], *_args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            raw_input = kwargs.get('input')
            assert kwargs['executable'] == selected
            calls.append((command, raw_input if isinstance(raw_input, bytes) else None))
            return subprocess.CompletedProcess(command, 0, b'abc123 blob 1\0x\0', b'')

        monkeypatch.setattr(subprocess, 'run', complete_run)
        info = [_StagedFileInfo('mod.py', '100644', 'abc123', '0' * 40, 'A')]

        result = read_staged_blobs('/repo', info)

        assert result is not None
        assert result['mod.py'].content == 'x'
        assert calls == [(['git', 'cat-file', '--batch', '-Z'], b'abc123\0')]

    def test_non_ascii_digit_version_keeps_generic_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            if command == ['git', '--version']:
                return subprocess.CompletedProcess(
                    command, 0, 'git version \N{SUPERSCRIPT TWO}.41\n', ''
                )
            return subprocess.CompletedProcess(command, 1, b'', b'failure')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None

    @pytest.mark.parametrize(
        'version_output',
        ['git version 2.42.0\n', 'git version 2.55.0.windows.3\n', 'unknown\n'],
        ids=['minimum', 'vendor-suffix', 'unknown-format'],
    )
    def test_non_old_or_unknown_version_keeps_generic_failure(
        self, monkeypatch: pytest.MonkeyPatch, version_output: str
    ) -> None:

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            if command == ['git', '--version']:
                return subprocess.CompletedProcess(command, 0, version_output, '')
            return subprocess.CompletedProcess(command, 1, b'', b'failure')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None

    def test_old_git_rejection_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            calls.append(command)
            if command == ['git', '--version']:
                return subprocess.CompletedProcess(command, 0, 'git version 2.41.7\n', '')
            return subprocess.CompletedProcess(command, 129, b'', b'unknown option')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        with pytest.raises(git_module.UnsupportedGitVersionError) as caught:
            batch_fetch_worktree_baselines('/repo', ['mod.py'])

        assert str(caught.value) == (
            'defsort: Git 2.42 or newer is required for Git-backed sorting (found Git 2.41.7)'
        )
        assert calls == [
            ['git', 'cat-file', '--batch', '-Z'],
            ['git', '--version'],
        ]

    @pytest.mark.parametrize('field', ['major', 'minor'])
    def test_oversized_ascii_version_field_keeps_generic_failure(
        self, monkeypatch: pytest.MonkeyPatch, field: str
    ) -> None:
        oversized = '9' * 5_000
        version = f'{oversized}.41' if field == 'major' else f'2.{oversized}'

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            if command == ['git', '--version']:
                return subprocess.CompletedProcess(command, 0, f'git version {version}\n', '')
            return subprocess.CompletedProcess(command, 1, b'', b'failure')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None

    def test_success_uses_uppercase_z_nul_input_and_no_version_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[list[str], bytes | None]] = []

        def complete_run(
            command: list[str], *_args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            raw_input = kwargs.get('input')
            calls.append((command, raw_input if isinstance(raw_input, bytes) else None))
            return subprocess.CompletedProcess(command, 0, b'HEAD:mod.py missing\0', b'')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        result = batch_fetch_worktree_baselines('/repo', ['mod.py'])

        assert result is not None
        assert result['mod.py'].status is BlobReadStatus.MISSING
        assert calls == [(['git', 'cat-file', '--batch', '-Z'], b'HEAD:mod.py\0')]

    @pytest.mark.parametrize(
        'error',
        [
            OSError('git unavailable'),
            UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'),
        ],
        ids=['launch-error', 'decode-error'],
    )
    def test_unreadable_version_probe_keeps_generic_failure(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        calls: list[list[str]] = []

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(command)
            if command == ['git', '--version']:
                raise error
            return subprocess.CompletedProcess(command, 1, b'', b'failure')

        monkeypatch.setattr(subprocess, 'run', complete_run)

        assert batch_fetch_worktree_baselines('/repo', ['mod.py']) is None
        assert calls == [
            ['git', 'cat-file', '--batch', '-Z'],
            ['git', '--version'],
        ]


class TestDecodePathspecRecord:
    @pytest.mark.parametrize(
        ('record', 'expected'),
        [
            (b'plain\\name.py', b'plain\\name.py'),
            (b'"plain.py"', b'plain.py'),
            (b'"\\a\\b\\f\\n\\r\\t\\v\\\\\\""', b'\a\b\f\n\r\t\v\\"'),
            (b'"caf\\303\\251.py"', b'caf\xc3\xa9.py'),
            (b'"\\001\\377.py"', b'\x01\xff.py'),
            (b'"file.py"ignored\\invalid', b'file.py'),
            (b'"file.py\\000ignored"', b'file.py'),
            (b'"file.py\x00ignored"', None),
            (b'""', None),
            (b'"\\000ignored"', None),
            (b'"missing.py', None),
            (b'"trailing\\', None),
            (b'"\\q.py"', None),
            (b'"\\x41.py"', None),
            (b'"\\1.py"', None),
            (b'"\\12.py"', None),
            (b'"\\128.py"', None),
            (b'"\\400.py"', None),
            (b'"\\777.py"', None),
            (b'"file.py\\000\\q"', None),
        ],
    )
    def test_byte_record_contract(self, record: bytes, expected: bytes | None) -> None:
        assert git_module._decode_pathspec_record(record) == expected


class TestDetectRenames:
    def test_git_mv_returns_destination(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'old.py').write_text('value = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'old.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        run_setup(['git', 'mv', 'old.py', 'new.py'], cwd=tmp_path)

        assert detect_renames(str(tmp_path)) == {'new.py'}

    def test_launch_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def raise_oserror(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise OSError('git unavailable')

        monkeypatch.setattr(git_module.subprocess, 'run', raise_oserror)

        assert detect_renames('/repo') is None

    def test_malformed_head_returns_none(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'sample.py').write_text('value = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'sample.py'], cwd=tmp_path)
        (tmp_path / '.git' / 'HEAD').write_text('invalid HEAD contents\n', encoding='ascii')

        assert detect_renames(str(tmp_path)) is None

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def return_nonzero(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(
                args=['git'],
                returncode=1,
                stdout=b'',
                stderr=b'failure',
            )

        monkeypatch.setattr(git_module.subprocess, 'run', return_nonzero)

        assert detect_renames('/repo') is None

    def test_unborn_index_returns_no_renames(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'sample.py').write_text('value = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'sample.py'], cwd=tmp_path)

        assert detect_renames(str(tmp_path)) == set()


class TestDiscoverRepoRoot:
    def test_bare_probe_decode_failure_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        show_toplevel_failure = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )

        def fail_bare_probe(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if '--show-toplevel' in command:
                return show_toplevel_failure
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_bare_probe)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_bare_probe_empty_output_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        show_toplevel_failure = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )
        bare_probe_success = subprocess.CompletedProcess(
            args=['git'], returncode=0, stdout='\n', stderr=''
        )

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if '--show-toplevel' in command:
                return show_toplevel_failure
            return bare_probe_success

        monkeypatch.setattr(subprocess, 'run', complete_run)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_bare_probe_launch_failure_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        show_toplevel_failure = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )

        def fail_bare_probe(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if '--show-toplevel' in command:
                return show_toplevel_failure
            raise OSError('git unavailable')

        monkeypatch.setattr(subprocess, 'run', fail_bare_probe)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_bare_probe_malformed_output_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        show_toplevel_failure = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )
        bare_probe_success = subprocess.CompletedProcess(
            args=['git'], returncode=0, stdout='not-a-boolean\n', stderr=''
        )

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if '--show-toplevel' in command:
                return show_toplevel_failure
            return bare_probe_success

        monkeypatch.setattr(subprocess, 'run', complete_run)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_bare_repository_is_not_worktree(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '--bare'], cwd=tmp_path)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)

    def test_bare_repository_nested_in_worktree_is_not_worktree(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        bare_repo = tmp_path / 'nested.git'
        run_setup(['git', 'init', '--bare', str(bare_repo)])

        result = discover_repo_root(str(bare_repo))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)

    def test_empty_git_output_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='\n', stderr='')

        def complete_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return completed

        monkeypatch.setattr(subprocess, 'run', complete_run)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_git_decode_failure_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def fail_decode(command: list[str], *_args: object, **_kwargs: object) -> None:
            assert command == ['git', 'rev-parse', '--show-toplevel']
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_decode)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_git_launch_failure_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def fail_launch(*_args: object, **_kwargs: object) -> None:
            raise OSError('git unavailable')

        monkeypatch.setattr(subprocess, 'run', fail_launch)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    @pytest.mark.parametrize('target_name', ['missing', 'file'])
    def test_invalid_target_is_error(self, tmp_path: Path, target_name: str) -> None:
        target = tmp_path / target_name
        if target_name == 'file':
            target.write_text('not a directory\n', encoding='utf-8')

        result = discover_repo_root(str(target))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_marker_inspection_failure_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )

        def complete_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return completed

        def fail_marker_inspection(_cwd: str) -> None:
            return None

        monkeypatch.setattr(subprocess, 'run', complete_run)
        monkeypatch.setattr('defsort._git._git_marker_present', fail_marker_inspection)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_non_repository_is_not_worktree(self, tmp_path: Path) -> None:
        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)

    def test_nonzero_git_with_marker_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / '.git').mkdir()
        completed = subprocess.CompletedProcess(
            args=['git'], returncode=128, stdout='', stderr='failure'
        )

        def complete_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return completed

        monkeypatch.setattr(subprocess, 'run', complete_run)

        result = discover_repo_root(str(tmp_path))

        assert result == _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

    def test_subdirectory_returns_normalized_worktree_root(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        subdirectory = tmp_path / 'nested'
        subdirectory.mkdir()

        result = discover_repo_root(str(subdirectory))

        assert result.status is RepoDiscoveryStatus.WORKTREE
        assert result.root is not None
        assert Path(result.root).resolve() == tmp_path.resolve()

    def test_worktree_returns_normalized_root(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')

        result = discover_repo_root(str(tmp_path))

        assert result.status is RepoDiscoveryStatus.WORKTREE
        assert result.root is not None
        assert Path(result.root).resolve() == tmp_path.resolve()


class TestGetModifiedTrackedPyFiles:
    def test_excludes_deleted_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        (tmp_path / 'test.py').unlink()
        result = get_modified_tracked_py_files(str(tmp_path))
        assert result is not None
        assert 'test.py' not in result

    def test_excludes_non_py(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        readme = tmp_path / 'README.md'
        readme.write_text('changed', encoding='utf-8')
        run_setup(['git', 'add', 'README.md'], cwd=tmp_path)
        result = get_modified_tracked_py_files(str(tmp_path))
        assert result is not None
        assert not any(p.endswith('.md') for p in result)

    def test_git_failure_returns_none(self, tmp_path: Path) -> None:
        # tmp_path is not a git repo, so git diff will fail
        result = get_modified_tracked_py_files(str(tmp_path))
        assert result is None

    def test_initial_commit_uses_empty_tree(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        f = tmp_path / 'new.py'
        f.write_text('def a():\n    pass\n', encoding='utf-8')
        run_setup(['git', 'add', 'new.py'], cwd=tmp_path)
        result = get_modified_tracked_py_files(str(tmp_path))
        assert result is not None
        assert 'new.py' in result

    def test_non_utf8_filename_decoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = b'valid.py\0\xff\xfebad.py\0'
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)
        head_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b'abc123\n')

        def _mock_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
            if 'rev-parse' in cmd:
                return head_result
            return mock_result

        monkeypatch.setattr(subprocess, 'run', _mock_run)
        result = get_modified_tracked_py_files('/fake')
        assert result is not None
        assert 'valid.py' in result
        assert b'\xff\xfebad.py'.decode('utf-8', errors='surrogateescape') in result

    def test_returns_modified_py_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, 'def b():\n    pass\n')
        result = get_modified_tracked_py_files(str(tmp_path))
        assert result is not None
        assert 'test.py' in result


class TestGetRepoRoot:
    def test_fallback_on_non_repo(self, tmp_path: Path) -> None:
        result = get_repo_root(str(tmp_path))
        assert Path(result).resolve() == tmp_path.resolve()

    def test_normalizes_msys_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'platform', 'win32')
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout='/c/repo\n')

        def mock_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return mock_result

        monkeypatch.setattr(subprocess, 'run', mock_run)
        result = get_repo_root(str(tmp_path))
        assert result == 'C:/repo'

    def test_returns_repo_root(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        sub = tmp_path / 'sub'
        sub.mkdir()
        result = get_repo_root(str(sub))
        # Use resolve() to handle macOS /tmp -> /private/tmp symlink
        assert Path(result).resolve() == tmp_path.resolve()

    def test_undecodable_repo_root_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_decode(command: list[str], *_args: object, **_kwargs: object) -> None:
            assert command == ['git', 'rev-parse', '--show-toplevel']
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_decode)

        assert try_get_repo_root(str(tmp_path)) is None


class TestGetStagedPyInfo:
    def test_filters_non_py_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'readme.md').write_text('# hi\n', encoding='utf-8')
        (tmp_path / 'test.py').write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        paths = [r.path for r in result]
        assert 'test.py' in paths
        assert 'readme.md' not in paths

    def test_git_failure_returns_none(self, tmp_path: Path) -> None:
        result = get_staged_py_info(str(tmp_path / 'nonexistent'))
        assert result is None

    def test_multiple_files_sorted(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'bravo.py').write_text('x = 1\n', encoding='utf-8')
        (tmp_path / 'alpha.py').write_text('y = 2\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        paths = [r.path for r in result]
        assert paths == ['alpha.py', 'bravo.py']

    def test_no_staged_files_returns_empty(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        assert result == []

    def test_non_utf8_path_decoded_with_surrogateescape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate --raw -z output: header\0path\0
        # :100644 100644 <old> <new> M\0<path>\0
        header = b':100644 100644 ' + b'a' * 40 + b' ' + b'b' * 40 + b' M'
        path = b'\xff\xfebad.py'
        stdout = header + b'\0' + path + b'\0'
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

        def mock_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return mock_result

        monkeypatch.setattr(subprocess, 'run', mock_run)
        result = get_staged_py_info('/fake')
        assert result is not None
        assert len(result) == 1
        assert result[0].path == path.decode('utf-8', errors='surrogateescape')

    def test_rename_entry_uses_destination_path(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'old.py').write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'old.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add old'])
        run_setup(['git', '-C', str(tmp_path), 'mv', 'old.py', 'new.py'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        paths = [r.path for r in result]
        assert 'new.py' in paths
        assert 'old.py' not in paths

    def test_rename_parser_sync_with_following_entry(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'old.py').write_text('x = 1\n', encoding='utf-8')
        (tmp_path / 'other.py').write_text('y = 2\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add files'])
        run_setup(['git', '-C', str(tmp_path), 'mv', 'old.py', 'new.py'])
        (tmp_path / 'other.py').write_text('y = 3\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        paths = [r.path for r in result]
        assert 'new.py' in paths
        assert 'other.py' in paths

    def test_rename_status_captured(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'old.py').write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'old.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add old'])
        run_setup(['git', '-C', str(tmp_path), 'mv', 'old.py', 'new.py'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        entry = next(r for r in result if r.path == 'new.py')
        assert entry.status.startswith('R')

    def test_returns_py_files_with_modes(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text('def foo():\n    pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        assert len(result) == 1
        assert result[0].path == 'test.py'
        assert result[0].mode == '100644'
        assert len(result[0].blob_hash) >= 40  # 40 for SHA-1, 64 for SHA-256

    def test_symlink_mode_filtered(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        target = tmp_path / 'real.py'
        target.write_text('x = 1\n', encoding='utf-8')
        original_target = target.read_bytes()
        link = tmp_path / 'link.py'
        link.symlink_to(target)
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        original_index = run_setup(
            ['git', '-C', str(tmp_path), 'ls-files', '--stage', '--', 'link.py']
        ).stdout
        assert original_index.startswith(b'120000 ')
        result = get_staged_py_info(str(tmp_path))
        assert result is not None
        paths = [r.path for r in result]
        assert 'real.py' in paths
        assert 'link.py' not in paths
        assert all(r.mode in ('100644', '100755') for r in result)
        assert target.read_bytes() == original_target
        assert (
            run_setup(['git', '-C', str(tmp_path), 'ls-files', '--stage', '--', 'link.py']).stdout
            == original_index
        )


class TestGitConfigBool:
    def test_reads_key_from_explicit_cwd(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        run_setup(['git', 'config', 'test.key', 'true'], cwd=tmp_path)
        assert _git_config_bool('test.key', cwd=str(tmp_path)) is True

    def test_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_empty_git_repo(tmp_path)
        run_setup(['git', 'config', 'test.key', 'false'], cwd=tmp_path)
        monkeypatch.chdir(tmp_path)
        assert _git_config_bool('test.key') is False

    def test_returns_none_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_empty_git_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert _git_config_bool('defsort-test.nonexistent-key') is None

    def test_returns_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_empty_git_repo(tmp_path)
        run_setup(['git', 'config', 'test.key', 'true'], cwd=tmp_path)
        monkeypatch.chdir(tmp_path)
        assert _git_config_bool('test.key') is True


class TestGitExecutableFailure:
    def test_discovery_keeps_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_module, 'resolve_git_executable', _missing_git)

        assert discover_repo_root('/repo').status is RepoDiscoveryStatus.ERROR

    @pytest.mark.parametrize(
        'query',
        [
            partial(git_module._git_config_bool, 'defsort.hook'),
            git_module._probe_git_version,
            detect_renames,
            get_staged_py_info,
            partial(git_module.get_unstaged_paths, paths=['mod.py']),
            partial(batch_fetch_worktree_baselines, paths=['mod.py']),
            try_get_repo_root,
        ],
    )
    def test_optional_queries_keep_none(
        self, monkeypatch: pytest.MonkeyPatch, query: Callable[[str], object]
    ) -> None:
        monkeypatch.setattr(git_module, 'resolve_git_executable', _missing_git)

        assert query('/repo') is None

    def test_replay_suppresses_resolution_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_module, 'resolve_git_executable', _missing_git)
        add = PreCommitAdd(tokens=('git', 'add', '.'), exec_cwd='/repo', git_cwd='/repo')

        assert git_module.run_pre_commit_adds((add,)) is None

    @pytest.mark.parametrize(
        'query',
        [
            git_module._get_empty_tree_hash,
            git_module._get_staged_py_files,
            partial(apply_index_entries, entries=[('100644', 'abc123', 'mod.py')]),
            get_modified_tracked_py_files,
            partial(prepare_index_entry, path='mod.py', content='x = 1\n', mode='100644'),
            partial(resolve_pathspec_py_files, pathspecs=['mod.py'], repo_root='/repo'),
        ],
    )
    def test_unguarded_queries_keep_propagation(
        self, monkeypatch: pytest.MonkeyPatch, query: Callable[[str], object]
    ) -> None:
        monkeypatch.setattr(git_module, 'resolve_git_executable', _missing_git)

        with pytest.raises(FileNotFoundError, match='git unavailable'):
            query('/repo')


class TestIsHookEnabled:
    def test_defsort_hook_false_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def config_bool(key: str, *, cwd: str | None = None) -> bool | None:
            assert cwd is None
            return False if key == 'defsort.hook' else None

        monkeypatch.setattr('defsort._git._git_config_bool', config_bool)
        assert is_hook_enabled() is False

    def test_defsort_hook_false_uses_explicit_cwd(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        run_setup(['git', 'config', 'defsort.hook', 'false'], cwd=tmp_path)
        assert is_hook_enabled(cwd=str(tmp_path)) is False

    def test_defsort_hook_true_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def config_bool(key: str, *, cwd: str | None = None) -> bool | None:
            assert cwd is None
            return True if key == 'defsort.hook' else None

        monkeypatch.setattr('defsort._git._git_config_bool', config_bool)
        assert is_hook_enabled() is True

    def test_unset_defaults_true(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def config_bool(_key: str, *, cwd: str | None = None) -> bool | None:
            assert cwd is None
            return None

        monkeypatch.setattr('defsort._git._git_config_bool', config_bool)
        assert is_hook_enabled() is True


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


class TestParseCatFileBatch:
    def test_decode_failed(self) -> None:
        # 0x80 is invalid UTF-8 start byte.
        data = b'abc123 blob 1\0\x80\0abc456 blob 5\0hello\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.DECODE_FAILED
        assert result['k1'].content is None
        # Decode failure is per-blob, so the second response must still parse.
        assert result['k2'].status is BlobReadStatus.CONTENT
        assert result['k2'].content == 'hello'

    def test_desync_preserves_earlier_entries(self) -> None:
        data = b'abc123 blob 5\0hello\0badheader'
        requests = [
            _cat_file_request('k1'),
            _cat_file_request('k2'),
            _cat_file_request('k3'),
        ]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.CONTENT
        assert result['k1'].content == 'hello'
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR
        assert result['k3'].status is BlobReadStatus.INFRA_ERROR

    def test_embedded_payload_nul_does_not_desynchronize(self) -> None:
        data = b'abc123 blob 3\0a\0b\0abc456 blob 5\0hello\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.CONTENT
        assert result['k1'].content == 'a\0b'
        assert result['k2'].status is BlobReadStatus.CONTENT
        assert result['k2'].content == 'hello'

    def test_empty_keys_returns_empty(self) -> None:
        result = _parse_cat_file_batch(b'', [])

        assert result == {}

    def test_happy_path_missing(self) -> None:
        data = b'k1 missing\0'

        result = _parse_cat_file_batch(data, [_cat_file_request('k1')])

        assert result['k1'].status is BlobReadStatus.MISSING
        assert result['k1'].content is None

    def test_happy_path_single_content(self) -> None:
        # Content responses are `<sha> blob <size>\0<size bytes>\0`.
        data = b'abc123 blob 5\0hello\0'

        result = _parse_cat_file_batch(data, [_cat_file_request('k1')])

        assert result['k1'].status is BlobReadStatus.CONTENT
        assert result['k1'].content == 'hello'

    def test_known_length_non_blob_fails_only_its_key(self) -> None:
        data = b'abc123 tree 3\0a\0b\0abc456 blob 5\0hello\0'
        requests = [_cat_file_request('tree'), _cat_file_request('blob')]

        result = _parse_cat_file_batch(data, requests)

        assert result['tree'].status is BlobReadStatus.INFRA_ERROR
        assert result['blob'].status is BlobReadStatus.CONTENT
        assert result['blob'].content == 'hello'

    def test_missing_echo_must_match_exact_request(self) -> None:
        requests = [
            _cat_file_request('first', b'HEAD:right.py'),
            _cat_file_request('second', b'HEAD:second.py'),
        ]
        data = b'HEAD:wrong.py missing\0HEAD:second.py missing\0'

        result = _parse_cat_file_batch(data, requests)

        assert result['first'].status is BlobReadStatus.INFRA_ERROR
        assert result['second'].status is BlobReadStatus.INFRA_ERROR

    def test_missing_echo_uses_object_name_not_result_key(self) -> None:
        request = _cat_file_request('display.py', b'HEAD:actual.py')

        result = _parse_cat_file_batch(b'HEAD:actual.py missing\0', [request])

        assert result['display.py'].status is BlobReadStatus.MISSING

    def test_missing_trailing_payload_nul_fails_closed(self) -> None:
        result = _parse_cat_file_batch(
            b'abc123 blob 5\0hello',
            [_cat_file_request('k1'), _cat_file_request('k2')],
        )

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR

    def test_mixed_missing_and_content(self) -> None:
        data = b'k1 missing\0abc123 blob 5\0hello\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.MISSING
        assert result['k2'].status is BlobReadStatus.CONTENT
        assert result['k2'].content == 'hello'

    @pytest.mark.parametrize('size', [b'', b'-1', b'1.5', b'five'])
    def test_non_decimal_size_fails_current_and_remaining(self, size: bytes) -> None:
        data = b'abc123 blob ' + size + b'\0hello\0abc456 blob 5\0world\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR

    def test_oversized_decimal_size_preserves_earlier_and_fails_rest(self) -> None:
        data = b'abc123 blob 5\0hello\0abc456 blob ' + b'9' * 4301 + b'\0abc789 blob 5\0world\0'
        requests = [
            _cat_file_request('first'),
            _cat_file_request('current'),
            _cat_file_request('remaining'),
        ]

        result = _parse_cat_file_batch(data, requests)

        assert result['first'].status is BlobReadStatus.CONTENT
        assert result['first'].content == 'hello'
        assert result['current'].status is BlobReadStatus.INFRA_ERROR
        assert result['remaining'].status is BlobReadStatus.INFRA_ERROR

    def test_too_short_header_fails_closed(self) -> None:
        data = b'abc123 blob\0hello\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR

    def test_truncated_header_fails_closed(self) -> None:
        data = b'abc123 blob 5'
        requests = [
            _cat_file_request('k1'),
            _cat_file_request('k2'),
            _cat_file_request('k3'),
        ]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR
        assert result['k3'].status is BlobReadStatus.INFRA_ERROR

    def test_truncated_payload_fails_closed(self) -> None:
        data = b'abc123 blob 100\0short\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR

    def test_unknown_length_non_blob_header_fails_closed(self) -> None:
        data = b'abc123 tree\0hello\0'
        requests = [_cat_file_request('k1'), _cat_file_request('k2')]

        result = _parse_cat_file_batch(data, requests)

        assert result['k1'].status is BlobReadStatus.INFRA_ERROR
        assert result['k2'].status is BlobReadStatus.INFRA_ERROR


class TestPrepareIndexEntry:
    def test_creates_valid_blob(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
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
        _init_git_repo(tmp_path, '# placeholder\n')
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

    def test_returns_none_on_git_failure(self, tmp_path: Path) -> None:
        result = prepare_index_entry(str(tmp_path), 'test.py', 'content\n', '100644')
        assert result is None


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
        _init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=raw
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [_StagedFileInfo('exact.py', '100644', 'new_hash', blob_hash, 'M')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        entry = result['exact.py']
        assert entry.status is BlobReadStatus.CONTENT
        assert entry.content == expected

    def test_git_failure_returns_none(self, tmp_path: Path) -> None:
        info = [_StagedFileInfo('test.py', '100644', 'h', 'bad_hash', 'M')]
        result = read_baseline_blobs(str(tmp_path / 'nonexistent'), info)
        assert result is None

    def test_reads_baseline_blobs(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'],
            stdin=b'def foo():\n    pass\n',
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [_StagedFileInfo('test.py', '100644', 'new_hash', blob_hash, 'M')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        assert result['test.py'].status is BlobReadStatus.CONTENT
        assert result['test.py'].content == 'def foo():\n    pass\n'

    def test_skips_new_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        null_hash = '0' * 40
        info = [_StagedFileInfo('new.py', '100644', 'some_hash', null_hash, 'A')]
        result = read_baseline_blobs(str(tmp_path), info)
        assert result is not None
        assert result == {}


class TestReadPathspecFile:
    def test_directory(self, tmp_path: Path) -> None:
        with pytest.raises(git_module.NonRegularPathspecError, match='regular file'):
            git_module.read_pathspec_file(str(tmp_path), '.')

    def test_inaccessible_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / 'paths').write_bytes(b'test.py\n')

        def inaccessible(*_args: object, **_kwargs: object) -> None:
            raise PermissionError

        monkeypatch.setattr(os, 'open', inaccessible)
        assert git_module.read_pathspec_file(str(tmp_path), 'paths') is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert git_module.read_pathspec_file(str(tmp_path), 'missing') is None

    def test_opened_descriptor_checked_before_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / 'paths').write_bytes(b'test.py\n')
        opened: list[int] = []
        real_open = os.open
        real_fstat = os.fstat

        def open_resource(path: Path, flags: int) -> int:
            assert flags & getattr(os, 'O_NONBLOCK', 0) == getattr(os, 'O_NONBLOCK', 0)
            fd = real_open(path, flags)
            opened.append(fd)
            return fd

        def changed_type(fd: int) -> os.stat_result:
            values = list(real_fstat(fd))
            values[0] = stat.S_IFIFO
            return os.stat_result(values)

        def unexpected_read(*_args: object, **_kwargs: object) -> None:
            pytest.fail('resource read before checking opened descriptor')

        monkeypatch.setattr(os, 'open', open_resource)
        monkeypatch.setattr(os, 'fstat', changed_type)
        monkeypatch.setattr(os, 'fdopen', unexpected_read)
        with pytest.raises(git_module.NonRegularPathspecError, match='regular file'):
            git_module.read_pathspec_file(str(tmp_path), 'paths')
        assert len(opened) == 1
        with pytest.raises(OSError, match=r'.+') as error:
            real_fstat(opened[0])
        assert error.value.errno == errno.EBADF

    @pytest.mark.parametrize('content', [b'', b'test.py\n', b'test.py\r\n', b'test.py\0'])
    @pytest.mark.parametrize('relative', [False, True])
    def test_ordinary_bytes(self, tmp_path: Path, content: bytes, *, relative: bool) -> None:
        path = tmp_path / 'paths'
        path.write_bytes(content)
        assert (
            git_module.read_pathspec_file(str(tmp_path), 'paths' if relative else str(path))
            == content
        )

    @pytest.mark.parametrize('kind', ['fifo', 'device'])
    def test_resource_rejected_in_bounded_child(self, tmp_path: Path, kind: str) -> None:
        if kind == 'fifo':
            mkfifo = getattr(os, 'mkfifo', None)
            if not callable(mkfifo):
                pytest.skip('FIFOs unavailable')
            path = tmp_path / 'fifo'
            mkfifo(path)
        else:
            path = Path(os.devnull)
            if not stat.S_ISCHR(path.stat().st_mode):
                pytest.skip('Character device unavailable')
        result = run_setup(
            [
                sys.executable,
                '-c',
                (
                    'import sys\n'
                    'from defsort._git import read_pathspec_file, NonRegularPathspecError\n'
                    'try:\n'
                    '    read_pathspec_file(sys.argv[1], sys.argv[2])\n'
                    'except NonRegularPathspecError:\n'
                    '    pass\n'
                    'else:\n'
                    '    raise AssertionError("nonregular resource accepted")\n'
                ),
                str(tmp_path),
                str(path),
            ],
            timeout=5,
            env=_ENV,
            check=False,
        )
        assert result.returncode == 0, process_transcript(result)

    def test_socket(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        family = getattr(socket, 'AF_UNIX', None)
        if not isinstance(family, int):
            pytest.skip('Unix sockets unavailable')
        path = tmp_path / 'socket'
        monkeypatch.chdir(tmp_path)
        with socket.socket(family) as resource:
            resource.bind('socket')
            with pytest.raises(git_module.NonRegularPathspecError, match='regular file'):
                git_module.read_pathspec_file(str(tmp_path), str(path))

    def test_symlink_to_ordinary_file(self, tmp_path: Path) -> None:
        path = tmp_path / 'paths'
        path.write_bytes(b'test.py\n')
        link = tmp_path / 'link'
        link.symlink_to(path)
        assert git_module.read_pathspec_file(str(tmp_path), 'link') == b'test.py\n'


class TestReadStagedBlobs:
    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
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
        _init_git_repo(tmp_path, '# placeholder\n')
        hash_result = run_setup(
            ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=raw
        )
        blob_hash = hash_result.stdout.decode('ascii').strip()
        info = [_StagedFileInfo('exact.py', '100644', blob_hash, '0' * 40, 'A')]
        result = read_staged_blobs(str(tmp_path), info)
        assert result is not None
        entry = result['exact.py']
        assert entry.status is BlobReadStatus.CONTENT
        assert entry.content == expected

    def test_git_failure_returns_none(self, tmp_path: Path) -> None:
        info = [_StagedFileInfo('test.py', '100644', 'bad_hash', '0' * 40, 'A')]
        result = read_staged_blobs(str(tmp_path / 'nonexistent'), info)
        assert result is None

    def test_missing_blob_returns_none_value(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        fake_hash = '0' * 40
        info = [_StagedFileInfo('ghost.py', '100644', fake_hash, '0' * 40, 'A')]
        result = read_staged_blobs(str(tmp_path), info)
        assert result is not None
        assert result['ghost.py'].status is BlobReadStatus.MISSING

    def test_non_utf8_blob_returns_none_value(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'binary.py').write_bytes(b'\x80\x81\x82\n')
        run_setup(['git', '-C', str(tmp_path), 'add', 'binary.py'])
        info_list = get_staged_py_info(str(tmp_path))
        assert info_list is not None
        assert len(info_list) == 1
        result = read_staged_blobs(str(tmp_path), info_list)
        assert result is not None
        assert result['binary.py'].status is BlobReadStatus.DECODE_FAILED

    def test_reads_multiple_blobs(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
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

    def test_reads_single_blob(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        content = 'def foo():\n    pass\n'
        (tmp_path / 'test.py').write_bytes(content.encode('utf-8'))
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        info_list = get_staged_py_info(str(tmp_path))
        assert info_list is not None
        assert len(info_list) == 1
        result = read_staged_blobs(str(tmp_path), info_list)
        assert result is not None
        assert result['test.py'].status is BlobReadStatus.CONTENT
        assert result['test.py'].content == content


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
    def test_rejects_invalid_status_root_combinations(
        self, status: RepoDiscoveryStatus, root: str | None
    ) -> None:
        with pytest.raises(ValueError, match='repository discovery'):
            _RepoDiscoveryResult(status=status, root=root)


class TestRepoRelative:
    def test_backslash_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def backslash_relpath(_path: str, _start: str | None = None) -> str:
            return r'src\test.py'

        monkeypatch.setattr(os.path, 'relpath', backslash_relpath)

        assert repo_relative('/repo/src/test.py', '/repo') == 'src/test.py'


class TestResolvePathspecPyFiles:
    def test_empty_pathspec_file_selects_no_paths(self, tmp_path: Path) -> None:
        assert (
            resolve_pathspec_py_files(
                str(tmp_path), [], repo_root=str(tmp_path), pathspec_file_content=b''
            )
            == []
        )

    def test_excludes_deleted(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        (tmp_path / 'test.py').unlink()
        result = resolve_pathspec_py_files(str(tmp_path), ['test.py'], repo_root=str(tmp_path))
        assert result is not None
        assert 'test.py' not in result

    def test_excludes_non_py(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        readme = tmp_path / 'README.md'
        readme.write_text('# Project\n', encoding='utf-8')
        run_setup(['git', 'add', 'README.md'], cwd=tmp_path)

        assert resolve_pathspec_py_files(str(tmp_path), ['.'], repo_root=str(tmp_path)) == [
            'test.py'
        ]

    def test_excludes_symlinks(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        link = tmp_path / 'link.py'
        link.symlink_to(tmp_path / 'test.py')
        run_setup(['git', 'add', 'link.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add link'], cwd=tmp_path)
        result = resolve_pathspec_py_files(str(tmp_path), ['link.py'], repo_root=str(tmp_path))
        assert result is not None
        assert 'link.py' not in result

    def test_git_failure_returns_none(self, tmp_path: Path) -> None:
        # tmp_path is not a git repo, so git ls-files will fail
        result = resolve_pathspec_py_files(
            str(tmp_path), ['nonexistent.py'], repo_root=str(tmp_path)
        )
        assert result is None

    @pytest.mark.parametrize('ending', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
    @pytest.mark.parametrize(
        ('filename', 'record'),
        [
            ('plain.py', b'plain.py'),
            ('space name.py', b'"space name.py"'),
            ('quote"name.py', b'"quote\\"name.py"'),
            ('back\\slash.py', b'"back\\\\slash.py"'),
            ('tab\tname.py', b'"tab\\tname.py"'),
            ('line\nname.py', b'"line\\nname.py"'),
            ('\u20ac.py', b'"\\342\\202\\254.py"'),
            ('magic.py', b'":(literal)magic.py"'),
            ('nested/inner.py', b'"nested/"'),
        ],
    )
    def test_lf_records_select_same_files_as_git(
        self, tmp_path: Path, filename: str, record: bytes, ending: bytes
    ) -> None:
        if os.name == 'nt' and any(char in filename for char in '"\\\t\n'):
            pytest.skip('Filename contains characters unsupported on Windows')
        _init_git_repo(tmp_path, '# unrelated worktree change\n')
        # Exercise exact UTF-8 bytes without requiring Git's optional UTF-8-MAC iconv.
        run_setup(['git', 'config', 'core.precomposeunicode', 'false'], cwd=tmp_path)
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'# baseline\n')
        run_setup(['git', 'add', '--', filename], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'target'], cwd=tmp_path)
        target.write_bytes(b'# changed\n')
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(record + ending)

        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
        )
        run_setup(['git', 'add', f'--pathspec-from-file={pf}'], cwd=tmp_path)
        selected = run_setup(['git', 'diff', '--cached', '--name-only', '-z'], cwd=tmp_path).stdout
        assert result == [filename]
        assert [os.fsdecode(name) for name in selected.split(b'\0') if name] == result

    @pytest.mark.parametrize('record', [b'"bad\\q.py"', b'"missing.py', b'""', b'"\\000"'])
    def test_malformed_quoted_record_returns_failure(self, tmp_path: Path, record: bytes) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(b'test.py\n' + record + b'\n')
        assert (
            resolve_pathspec_py_files(
                str(tmp_path),
                [],
                repo_root=str(tmp_path),
                pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
            )
            is None
        )

    def test_non_utf8_filename_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        # Monkeypatch only the ls-files subprocess call
        real_run = subprocess.run
        stdout = b'valid.py\0\xff\xfebad.py\0'
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

        def _mock_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
            if 'ls-files' in cmd:
                return mock_result
            return cast('Callable[..., subprocess.CompletedProcess[bytes]]', real_run)(cmd, **kw)

        monkeypatch.setattr(subprocess, 'run', _mock_run)
        # valid.py exists on disk, bad.py does not
        (tmp_path / 'valid.py').write_text('x = 1\n', encoding='utf-8')
        result = resolve_pathspec_py_files(str(tmp_path), ['valid.py'], repo_root=str(tmp_path))
        assert result is not None
        assert 'valid.py' in result

    @pytest.mark.skipif(os.name == 'nt', reason='Windows forbids quotes and line feeds in names')
    def test_nul_records_are_literal_and_match_git(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# unrelated worktree change\n')
        filename = '"literal\\t\n\r.py'
        target = tmp_path / filename
        target.write_bytes(b'# baseline\n')
        run_setup(['git', 'add', '--', filename], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'target'], cwd=tmp_path)
        target.write_bytes(b'# changed\n')
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(os.fsencode(filename) + b'\0')
        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
            pathspec_file_nul=True,
        )
        run_setup(
            ['git', 'add', f'--pathspec-from-file={pf}', '--pathspec-file-nul'], cwd=tmp_path
        )
        selected = run_setup(['git', 'diff', '--cached', '--name-only', '-z'], cwd=tmp_path).stdout
        assert result == [filename]
        assert selected == os.fsencode(filename) + b'\0'

    def test_pathspec_from_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        pf = tmp_path / 'paths.txt'
        pf.write_text('test.py\n', encoding='utf-8')
        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
        )
        assert result is not None
        assert 'test.py' in result

    def test_pathspec_from_file_crlf(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(b'test.py\r\n')
        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
        )
        assert result is not None
        assert 'test.py' in result

    def test_pathspec_from_file_nul(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(b'test.py\x00')
        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), str(pf)),
            pathspec_file_nul=True,
        )
        assert result is not None
        assert 'test.py' in result

    def test_relative_pathspec_from_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        pf = tmp_path / 'paths.txt'
        pf.write_text('test.py\n', encoding='utf-8')
        # Pass relative path -- should resolve against cwd (tmp_path)
        result = resolve_pathspec_py_files(
            str(tmp_path),
            [],
            repo_root=str(tmp_path),
            pathspec_file_content=git_module.read_pathspec_file(str(tmp_path), 'paths.txt'),
        )
        assert result is not None
        assert 'test.py' in result

    def test_resolves_directory(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        sub = tmp_path / 'sub'
        sub.mkdir()
        f = sub / 'inner.py'
        f.write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'sub/inner.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add sub'], cwd=tmp_path)
        result = resolve_pathspec_py_files(str(tmp_path), ['sub/'], repo_root=str(tmp_path))
        assert result is not None
        assert 'sub/inner.py' in result

    def test_resolves_explicit_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        result = resolve_pathspec_py_files(str(tmp_path), ['test.py'], repo_root=str(tmp_path))
        assert result is not None
        assert 'test.py' in result


class TestResolveSyncPaths:
    def test_directory_triggers_all_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        sub = tmp_path / 'src'
        sub.mkdir()
        f = sub / 'test.py'
        f.write_text('def a(): pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        add = PreCommitAdd(
            tokens=('git', 'add', 'src'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path),
        )
        result = resolve_sync_paths((add,), _discovered_root(tmp_path))
        assert result == frozenset({'src/test.py'})

    def test_dot_triggers_all_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text('def a(): pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        add = PreCommitAdd(
            tokens=('git', 'add', '.'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path),
        )
        result = resolve_sync_paths((add,), _discovered_root(tmp_path))
        assert result == frozenset({'test.py'})

    def test_dotslash_normalized(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text('def a(): pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        add = PreCommitAdd(
            tokens=('git', 'add', './test.py'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path),
        )
        result = resolve_sync_paths((add,), _discovered_root(tmp_path))
        assert result == frozenset({'test.py'})

    def test_flag_u_triggers_all_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text('def a(): pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        add = PreCommitAdd(
            tokens=('git', 'add', '-u'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path),
        )
        result = resolve_sync_paths((add,), _discovered_root(tmp_path))
        assert result == frozenset({'test.py'})

    def test_non_py_file_ignored(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text('def a(): pass\n', encoding='utf-8')
        readme = tmp_path / 'README.md'
        readme.write_text('# Project\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py', 'README.md'])
        add = PreCommitAdd(
            tokens=('git', 'add', 'README.md'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path),
        )
        result = resolve_sync_paths((add,), _discovered_root(tmp_path))
        assert result == frozenset()


class TestResolveSyncPathspecs:
    def _repo(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q', str(tmp_path)])
        (tmp_path / 'sub').mkdir()
        (tmp_path / 'top.py').write_text('x = 1\n')
        (tmp_path / 'sub' / 'deep.py').write_text('y = 1\n')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])

    def test_all_flag_still_uses_staged_set(self, tmp_path: Path) -> None:
        self._repo(tmp_path)
        adds = (
            PreCommitAdd(
                tokens=('git', 'add', '-A'),
                exec_cwd=str(tmp_path),
                git_cwd=str(tmp_path),
            ),
        )
        assert resolve_sync_paths(adds, _discovered_root(tmp_path)) == frozenset(
            {'top.py', 'sub/deep.py'}
        )

    def test_cross_drive_pathspec_uses_native_git_result(self, tmp_path: Path) -> None:
        self._repo(tmp_path)
        adds = (
            PreCommitAdd(
                tokens=('git', 'add', 'D:\\other\\foo.py'),
                exec_cwd=str(tmp_path),
                git_cwd=str(tmp_path),
            ),
        )
        # POSIX sees a literal no-match; Windows Git rejects it and triggers staged fallback.
        expected: frozenset[str] = (
            frozenset({'top.py', 'sub/deep.py'}) if os.name == 'nt' else frozenset()
        )
        assert resolve_sync_paths(adds, _discovered_root(tmp_path)) == expected

    @pytest.mark.parametrize('path', ['-x.py', '-A', '--pathspec-from-file'])
    def test_dash_leading_pathspec_after_double_dash(self, tmp_path: Path, path: str) -> None:
        self._repo(tmp_path)
        target = tmp_path / path
        target.mkdir()
        (target / 'literal.py').write_text('z = 1\n')
        run_setup(['git', '-C', str(tmp_path), 'add', '--', path])
        adds = (
            PreCommitAdd(
                tokens=('git', 'add', '--', path),
                exec_cwd=str(tmp_path),
                git_cwd=str(tmp_path),
            ),
        )
        assert resolve_sync_paths(adds, _discovered_root(tmp_path)) == frozenset(
            {f'{path}/literal.py'}
        )

    @pytest.mark.parametrize('flag', ['-u', '--all', '--update'])
    def test_other_all_file_flags_use_staged_set(self, tmp_path: Path, flag: str) -> None:
        self._repo(tmp_path)
        adds = (
            PreCommitAdd(
                tokens=('git', 'add', flag),
                exec_cwd=str(tmp_path),
                git_cwd=str(tmp_path),
            ),
        )
        assert resolve_sync_paths(adds, _discovered_root(tmp_path)) == frozenset(
            {'top.py', 'sub/deep.py'}
        )

    def test_other_repository_same_name_is_not_safe_to_sync(self, tmp_path: Path) -> None:
        current_repo = tmp_path / 'current'
        other_repo = tmp_path / 'other'
        current_repo.mkdir()
        other_repo.mkdir()
        self._repo(current_repo)
        self._repo(other_repo)
        (current_repo / 'same.py').write_text('current = 1\n')
        (other_repo / 'same.py').write_text('other = 1\n')
        run_setup(['git', '-C', str(current_repo), 'add', 'same.py'])
        run_setup(['git', '-C', str(other_repo), 'add', 'same.py'])
        adds = (
            PreCommitAdd(
                tokens=('git', '-C', str(other_repo), 'add', 'same.py'),
                exec_cwd=str(current_repo),
                git_cwd=str(other_repo),
            ),
        )

        assert 'same.py' not in resolve_sync_paths(adds, _discovered_root(current_repo))

    def test_wildcard_pathspec_resolves_through_git(self, tmp_path: Path) -> None:
        self._repo(tmp_path)
        adds = (
            PreCommitAdd(
                tokens=('git', 'add', '*.py'),
                exec_cwd=str(tmp_path),
                git_cwd=str(tmp_path),
            ),
        )
        assert resolve_sync_paths(adds, _discovered_root(tmp_path)) == frozenset(
            {'top.py', 'sub/deep.py'}
        )


class TestRunPreCommitAdds:
    def test_replays_literal_argv_in_exec_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selected = str(tmp_path / 'trusted' / 'git')
        add = PreCommitAdd(
            tokens=('git', '-C', 'sub', 'add', 'mod.py'),
            exec_cwd=str(tmp_path),
            git_cwd=str(tmp_path / 'sub'),
        )

        def resolve(spelling: str, *, cwd: str) -> str:
            assert spelling == 'git'
            assert cwd == add.exec_cwd
            return selected

        calls: list[list[str]] = []

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(argv)
            assert kwargs == {
                'cwd': add.exec_cwd,
                'executable': selected,
                'capture_output': True,
                'check': False,
            }
            return subprocess.CompletedProcess(argv, 0, b'', b'')

        monkeypatch.setattr(git_module, 'resolve_git_executable', resolve)
        monkeypatch.setattr(subprocess, 'run', run)

        git_module.run_pre_commit_adds((add,))

        assert calls == [['git', '-C', 'sub', 'add', 'mod.py']]


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain non-UTF-8 bytes')
class TestSurrogatePathProtocols:
    def test_apply_index_entries_accepts_surrogate_path(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q', str(tmp_path)])
        blob = (
            run_setup(
                ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], stdin=b'x = 1\n'
            )
            .stdout.decode('ascii')
            .strip()
        )
        assert apply_index_entries(str(tmp_path), [('100644', blob, 'weird\udcff.py')])
        listed = run_setup(['git', '-C', str(tmp_path), 'ls-files', '-z'])
        assert b'weird\xff.py' in listed.stdout

    def test_batch_fetch_accepts_surrogate_path(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
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


class TestTryGetRepoRoot:
    def test_returns_none_outside_repo(self, tmp_path: Path) -> None:
        result = try_get_repo_root(str(tmp_path))
        assert result is None

    def test_returns_root_in_git_repo(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        result = try_get_repo_root(str(tmp_path))
        assert result is not None
        assert Path(result) == tmp_path


def _cat_file_request(key: str, object_name: bytes | None = None) -> _CatFileRequest:
    return _CatFileRequest(key, key.encode() if object_name is None else object_name)


def _discovered_root(path: Path) -> str:
    root = try_get_repo_root(str(path))
    assert root is not None
    return root


def _missing_git(*_args: object, **_kwargs: object) -> str:
    raise FileNotFoundError('git unavailable')
