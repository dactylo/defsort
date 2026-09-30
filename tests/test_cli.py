import ast
import errno
import importlib.metadata
import io
import json
import os
import subprocess
import sys
import warnings
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import defsort._git as git_module
from defsort import _cli, _downstream
from defsort._cli import (
    EXIT_CHANGED,
    EXIT_ERROR,
    EXIT_OK,
    _batch_fetch_cli_baselines,
    _build_parser,
    _ConfigResolver,
    _discover_repo_roots,
    _file_result_to_json,
    _FileSortResult,
    _FileStatus,
    _guarded_skip,
    _overrides_from_args,
    _render_safe_config_warnings,
    _ResolvedConfig,
    _run_sort,
    main,
)
from defsort._config import ResolvedConfig, clear_config_cache
from defsort._display._types import SummaryRow
from defsort._types import (
    BANNER_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
    BannerMode,
    BlobReadResult,
    BlobReadStatus,
    ClassBlocker,
    EngineOptions,
    SortOverrides,
    SortPolicy,
    SortResult,
)
from tests._helpers import (
    _CLI,
    _ENV,
    PROCESS_GROUP_FLAGS,
    _init_empty_git_repo,
    decode_text,
    finish_process,
    invoke_cli,
    process_transcript,
    run_cli,
    run_closing_after_first_line,
    run_setup,
    run_with_closed_output,
)

_AXIS_FLAGS = [
    ('order', 'order', SORT_SCOPE_ORDER_VALUES),
    ('definition-order', 'definition_order', SORT_SCOPE_ORDER_VALUES),
    ('method-order', 'method_order', SORT_SCOPE_ORDER_VALUES),
    ('evaluation-order', 'evaluation_order', EVALUATION_ORDER_VALUES),
    ('banners', 'banners', BANNER_MODE_VALUES),
    ('method-bodies', 'method_bodies', METHOD_BODIES_VALUES),
    ('method-visibility', 'method_visibility', METHOD_VISIBILITY_VALUES),
    ('method-dunders', 'method_dunders', METHOD_DUNDERS_VALUES),
    ('main', 'main', MAIN_PLACEMENT_VALUES),
    ('tie-break', 'tie_break', TIE_BREAK_VALUES),
]

# Every value of every policy flag, plus the short option.
_AXIS_FLAG_CASES = [
    ([f'--{flag}', value], field, value) for flag, field, values in _AXIS_FLAGS for value in values
] + [(['-o', 'alphabetical'], 'order', 'alphabetical')]

_BIG_UNSORTED = ''.join(
    f'def f{index:04d}():\n    pass  # {"x" * 1024}\n\n\n' for index in range(2000, 0, -1)
)

_CLI_INEFFECTIVE_CASES = [
    ('method-bodies', ['--method-order', 'alphabetical', '--method-bodies', 'across-categories']),
    (
        'tie-break',
        [
            '--definition-order',
            'alphabetical',
            '--method-order',
            'alphabetical',
            '--tie-break',
            'source',
        ],
    ),
    ('method-visibility', ['--method-order', 'none', '--method-visibility', 'public-first']),
    ('method-dunders', ['--method-order', 'none', '--method-dunders', 'after-regular']),
    ('main', ['--definition-order', 'none', '--main', 'sorted']),
]

_CLOSED_STDOUT_STATUS = {
    'large-before-write': EXIT_CHANGED,
    'small-before-write': EXIT_CHANGED,
    'after-first-line': EXIT_CHANGED,
    'help-before-write': EXIT_OK,
}


_SRC_ALREADY_ORDERED = 'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n'
_SRC_BANNER_CONSTRAINED = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
_SRC_BANNER_CONSTRAINED_CHANGED = (
    'def zulu():\n    pass\n\n\n'
    'def yankee():\n    pass\n\n\n'
    '# --------\n\n\n'
    'def bravo():\n    pass\n\n\n'
    'def alpha():\n    pass\n'
)
_SRC_BUILTIN_BANNER = 'def zulu():\n    pass\n\n\n# ----------\n\n\ndef alpha():\n    pass\n'
_SRC_CLASS_LOCAL_INTERSTITIAL = (
    'class C:\n'
    '    def b(self):\n'
    '        pass\n'
    '\n'
    '    CONST = 1\n'
    '\n'
    '    def c(self):\n'
    '        pass\n'
    '\n'
    '    def a(self):\n'
    '        pass\n'
    '\n'
    '\n'
    'def alpha():\n'
    '    pass\n'
    '\n'
    '\n'
    'def zeta():\n'
    '    pass\n'
    '\n'
    '\n'
    'def beta():\n'
    '    pass\n'
)
_SRC_CUSTOM_BANNER = 'def zulu():\n    pass\n\n\n# CUSTOM\n\n\ndef alpha():\n    pass\n'
_SRC_DUPLICATES = (
    'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
)
_SRC_DUP_METHODS = """class C:
    def helper(self):
        return 1

    def foo(self, cb=helper):
        return cb

    def bar(self):
        return 2

    def foo(self):
        return 3
"""
_SRC_INIT_BODY_CLI = """class C:
    def __init__(self):
        self.load()

    def load(self):
        return 1
"""
_SRC_INTERSTITIAL = 'def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n'
_SRC_NEEDS_SORT = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'


class TestArgParsing:
    def test_json_mutual_exclusivity_with_diff(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--json', '-d', str(f)])
        assert result_status == 2, result_stderr

    def test_json_mutual_exclusivity_with_preview(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--json', '-p', str(f)])
        assert result_status == 2, result_stderr

    def test_json_mutual_exclusivity_with_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--json', '-w', str(f)])
        assert result_status == 2, result_stderr

    def test_mode_mutual_exclusivity(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-p', '-d', str(f)])
        assert result_status == 2, result_stderr

    def test_mode_mutual_exclusivity_dw(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-d', '-w', str(f)])
        assert result_status == 2, result_stderr

    def test_mode_mutual_exclusivity_pw(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-p', '-w', str(f)])
        assert result_status == 2, result_stderr

    def test_no_args_defaults_to_cwd(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([], cwd=tmp_path)
        assert result_status == 1, result_stderr
        assert 'test.py' in result_stdout


class TestBannerFlag:
    @pytest.mark.parametrize('mode_args', [[], ['--diff'], ['--write']])
    def test_banner_constraints_compact_in_text_modes(
        self,
        tmp_path: Path,
        mode_args: list[str],
    ) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_BANNER_CONSTRAINED_CHANGED, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli([*mode_args, str(source)])

        assert result_status == (0 if '--write' in mode_args else 1), result_stderr
        assert f'defsort: {source}: sort constrained by 1 banner' in result_stderr

    def test_banner_constraints_json_is_structured_and_silent(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_BANNER_CONSTRAINED_CHANGED, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(source)])

        record = json.loads(result_stdout)['files'][0]
        assert result_status == 1, result_stderr
        assert result_stderr == ''
        assert record['status'] == 'changed'
        assert record['banner_constraints'][0]['banner'] == {
            'line': 9,
            'text': '# --------',
        }

    def test_banner_constraints_preview_includes_details(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_BANNER_CONSTRAINED_CHANGED, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['--preview', str(source)])

        assert result_status == 1, result_stderr
        assert f'defsort: {source}: sort constrained by 1 banner' in result_stderr
        assert 'line 9: --------:' in result_stdout
        assert 'zulu (after)' in result_stdout
        assert 'alpha (before)' in result_stdout

    def test_banner_constraints_survive_skipped_file(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        original = _SRC_BANNER_CONSTRAINED_CHANGED.replace(
            'def zulu():\n    pass\n',
            'def zulu():\n    pass\n\nregistry = {}\n',
        )
        source.write_text(original, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli([str(source)])

        assert result_status == 1, result_stderr
        assert 'sort constrained by 1 banner' in result_stderr
        assert 'skipped (code between definitions)' in result_stderr
        assert source.read_text(encoding='utf-8') == original

    def test_banner_constraints_verbose_includes_details(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_BANNER_CONSTRAINED_CHANGED, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--verbose', str(source)])

        assert result_status == 1, result_stderr
        assert 'line 9: --------:' in result_stderr
        assert 'zulu (after)' in result_stderr
        assert 'alpha (before)' in result_stderr

    def test_banner_pattern_flags_explain_override_and_reset(self) -> None:
        _result_status, result_stdout, _result_stderr = invoke_cli(['--help'])

        normalized = ' '.join(result_stdout.split())
        assert (
            '--banner-pattern BANNER_PATTERN Add a safe full-match pattern to built-in banner '
            'recognition; overrides config.' in normalized
        )
        assert '--no-banner-pattern Clear the configured custom banner pattern.' in normalized

    @pytest.mark.parametrize('mode', BANNER_MODE_VALUES)
    def test_hook_parser_accepts_modes(self, mode: BannerMode, registered_hosts: Path) -> None:
        assert registered_hosts.is_dir()
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--banners', mode], stdin='{}'
        )
        assert result_status == 0, result_stderr

    @pytest.mark.parametrize('mode', BANNER_MODE_VALUES)
    def test_main_parser_accepts_modes(self, tmp_path: Path, mode: BannerMode) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--banners', mode, str(source)])
        assert result_status == 0, result_stderr

    def test_main_parser_rejects_invalid_mode(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--banners', 'move', str(source)]
        )
        assert result_status == 2, result_stderr

    @pytest.mark.parametrize('mode_args', [[], ['--diff'], ['--preview'], ['--write']])
    def test_unchanged_banner_constraints_require_verbose(
        self,
        tmp_path: Path,
        mode_args: list[str],
    ) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_BANNER_CONSTRAINED, encoding='utf-8')

        compact_status, compact_stdout, compact_stderr = invoke_cli([*mode_args, str(source)])
        verbose_status, verbose_stdout, verbose_stderr = invoke_cli(
            ['--verbose', *mode_args, str(source)]
        )

        assert compact_status == 0, compact_stderr
        assert compact_stdout == ''
        assert compact_stderr == ''
        assert verbose_status == 0, verbose_stderr
        assert f'defsort: {source}: sort constrained by 1 banner' in verbose_stderr
        detail_stream = verbose_stdout if '--preview' in mode_args else verbose_stderr
        assert 'line 5: --------:' in detail_stream

    def test_unconstrained_text_and_json_runs_are_silent(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        _text_result_status, _text_result_stdout, text_result_stderr = invoke_cli([str(source)])
        _json_result_status, json_result_stdout, json_result_stderr = invoke_cli(
            ['--json', str(source)]
        )

        assert 'sort constrained' not in text_result_stderr
        assert json_result_stderr == ''
        assert json.loads(json_result_stdout)['files'][0]['banner_constraints'] == []


class TestBatchFetchCliBaselines:
    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX byte filename semantics')
    def test_all_unencodable_paths_still_reject_old_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bad_name = b'bad-\xff.py'.decode('utf-8', errors='surrogateescape')
        bad = tmp_path / bad_name
        calls: list[list[str]] = []

        def discover_roots(_paths: list[Path]) -> dict[Path, Path | None]:
            return {bad.resolve(): tmp_path}

        def complete_run(
            command: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, 'git version 2.41.7\n', '')

        monkeypatch.setattr('defsort._cli._discover_repo_roots', discover_roots)
        monkeypatch.setattr(subprocess, 'run', complete_run)

        with pytest.raises(git_module.UnsupportedGitVersionError) as caught:
            _batch_fetch_cli_baselines([bad])

        assert caught.value.version == '2.41.7'
        assert calls == [['git', '--version']]

    def test_existing_file_returns_content(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        content = 'def alpha():\n    pass\n'
        (tmp_path / 'mod.py').write_bytes(content.encode('utf-8'))
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)

        result = _batch_fetch_cli_baselines([tmp_path / 'mod.py'])
        entry = result[tmp_path / 'mod.py']
        assert entry.status is BlobReadStatus.CONTENT
        assert entry.content == content

    def test_missing_file_returns_sentinel(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Create an initial commit so HEAD exists
        (tmp_path / 'other.py').write_text('x = 1\n')
        run_setup(['git', 'add', 'other.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        # New file not in HEAD
        (tmp_path / 'new.py').write_text('def a():\n    pass\n')

        result = _batch_fetch_cli_baselines([tmp_path / 'new.py'])
        assert result[tmp_path / 'new.py'].status is BlobReadStatus.MISSING

    def test_mixed_repos(self, tmp_path: Path) -> None:
        # Repo A
        repo_a = tmp_path / 'repo_a'
        repo_a.mkdir()
        _init_empty_git_repo(repo_a)
        (repo_a / 'a.py').write_bytes(b'def a():\n    pass\n')
        run_setup(['git', 'add', 'a.py'], cwd=repo_a)
        run_setup(['git', 'commit', '-m', 'init'], cwd=repo_a)

        # Repo B
        repo_b = tmp_path / 'repo_b'
        repo_b.mkdir()
        _init_empty_git_repo(repo_b)
        (repo_b / 'b.py').write_bytes(b'def b():\n    pass\n')
        run_setup(['git', 'add', 'b.py'], cwd=repo_b)
        run_setup(['git', 'commit', '-m', 'init'], cwd=repo_b)

        result = _batch_fetch_cli_baselines([repo_a / 'a.py', repo_b / 'b.py'])
        entry_a = result[repo_a / 'a.py']
        assert entry_a.status is BlobReadStatus.CONTENT
        assert entry_a.content == 'def a():\n    pass\n'
        entry_b = result[repo_b / 'b.py']
        assert entry_b.status is BlobReadStatus.CONTENT
        assert entry_b.content == 'def b():\n    pass\n'

    def test_non_git_repo_returns_infra_error(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def a():\n    pass\n')

        result = _batch_fetch_cli_baselines([f])
        assert result[f].status is BlobReadStatus.INFRA_ERROR

    def test_non_utf8_baseline_returns_none(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        f = tmp_path / 'binary.py'
        f.write_bytes(b'\x80\x81\x82\xff')
        run_setup(['git', 'add', 'binary.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        # Overwrite with valid content so it would be read by CLI
        f.write_text('def a():\n    pass\n')

        result = _batch_fetch_cli_baselines([f])
        assert result[f].status is BlobReadStatus.DECODE_FAILED

    def test_path_not_lexically_relative_to_root_returns_infra_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / 'repo'
        root.mkdir()
        path = tmp_path / 'outside' / 'mod.py'

        def discover_roots(_paths: list[Path]) -> dict[Path, Path | None]:
            return {path.resolve(): root}

        monkeypatch.setattr(
            'defsort._cli._discover_repo_roots',
            discover_roots,
        )

        with patch('defsort._cli.batch_fetch_worktree_baselines') as fetch:
            result = _batch_fetch_cli_baselines([path])
        fetch.assert_not_called()

        assert result[path].status is BlobReadStatus.INFRA_ERROR

    def test_subdirectory_caching(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        sub = tmp_path / 'pkg'
        sub.mkdir()
        (tmp_path / 'top.py').write_bytes(b'def top():\n    pass\n')
        (sub / 'nested.py').write_bytes(b'def nested():\n    pass\n')
        run_setup(['git', 'add', 'top.py', 'pkg/nested.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)

        result = _batch_fetch_cli_baselines([tmp_path / 'top.py', sub / 'nested.py'])
        top_entry = result[tmp_path / 'top.py']
        assert top_entry.status is BlobReadStatus.CONTENT
        assert top_entry.content == 'def top():\n    pass\n'
        nested_entry = result[sub / 'nested.py']
        assert nested_entry.status is BlobReadStatus.CONTENT
        assert nested_entry.content == 'def nested():\n    pass\n'

    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX byte filename semantics')
    def test_unencodable_path_does_not_poison_repo_batch(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        good = tmp_path / 'good.py'
        bad_name = b'bad-\xff.py'.decode('utf-8', errors='surrogateescape')
        bad = tmp_path / bad_name
        good.write_text('def good():\n    pass\n', encoding='utf-8')
        run_setup(['git', 'add', '--', good.name], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)

        result = _batch_fetch_cli_baselines([good, bad])

        assert result[good].status is BlobReadStatus.CONTENT
        assert result[good].content == 'def good():\n    pass\n'
        assert result[bad].status is BlobReadStatus.INFRA_ERROR


class TestClassBlockerOutput:
    def test_diff_changed_file_lists_class_blocker_once(self, tmp_path: Path) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_CLASS_LOCAL_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['--diff', str(target)])

        blocker_line = '  class C: skipped (code between methods)\n'
        assert result_status == EXIT_CHANGED, result_stderr
        assert '+def beta():' in result_stdout
        assert result_stderr == blocker_line
        assert result_stderr.count(blocker_line) == 1
        assert target.read_text(encoding='utf-8') == _SRC_CLASS_LOCAL_INTERSTITIAL

    def test_incremental_json_serializes_class_blockers(self, tmp_path: Path) -> None:
        _git_init_with_duplicate_method_baseline(tmp_path)

        _result_status, result_stdout, _result_stderr = invoke_cli(
            ['-i', '-j', str(tmp_path / 'mod.py')]
        )

        record = json.loads(result_stdout)['files'][0]
        assert record['class_blockers'] == [
            {'class': 'C', 'occurrence': 1, 'blocker': 'duplicates'}
        ]

    def test_incremental_summary_lists_blocked_class(self, tmp_path: Path) -> None:
        _git_init_with_duplicate_method_baseline(tmp_path)

        _result_status, _result_stdout, result_stderr = invoke_cli(
            ['-i', str(tmp_path / 'mod.py')]
        )

        assert 'class C: skipped (duplicate method names)' in result_stderr

    def test_json_mode_serializes_class_blockers(self, tmp_path: Path) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_DUP_METHODS, encoding='utf-8')

        _status, stdout, _stderr = invoke_cli(['-j', str(target)])
        record = json.loads(stdout)['files'][0]

        assert record['class_blockers'] == [
            {'class': 'C', 'occurrence': 1, 'blocker': 'duplicates'}
        ]

    def test_preview_lists_blocked_class(self, tmp_path: Path) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_DUP_METHODS, encoding='utf-8')

        _result_status, _result_stdout, result_stderr = invoke_cli(['-p', str(target)])

        assert 'class C: skipped (duplicate method names)' in result_stderr

    def test_summary_blocked_class_in_changed_file(self, tmp_path: Path) -> None:
        source = (
            _SRC_DUP_METHODS
            + '\n\ndef zed():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        target = tmp_path / 'mod.py'
        target.write_text(source, encoding='utf-8')

        _result_status, _result_stdout, result_stderr = invoke_cli([str(target)])

        assert 'class C: skipped (duplicate method names)' in result_stderr

    def test_summary_hide_skips_keeps_blocked_class_in_changed_file(
        self,
        tmp_path: Path,
    ) -> None:
        source = (
            _SRC_DUP_METHODS
            + '\n\ndef zed():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        target = tmp_path / 'mod.py'
        target.write_text(source, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--hide-skips', str(target)])

        blocker_line = '  class C: skipped (duplicate method names)\n'
        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stderr == blocker_line
        assert result_stderr.count(blocker_line) == 1

    def test_summary_lists_blocked_class(self, tmp_path: Path) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_DUP_METHODS, encoding='utf-8')

        _result_status, _result_stdout, result_stderr = invoke_cli([str(target)])

        assert 'class C: skipped (duplicate method names)' in result_stderr

    def test_write_changed_file_lists_class_blocker_once(self, tmp_path: Path) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_CLASS_LOCAL_INTERSTITIAL, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--write', str(target)])

        blocker_line = '  class C: skipped (code between methods)\n'
        rewritten = target.read_text(encoding='utf-8')
        assert result_status == EXIT_OK, result_stderr
        assert rewritten.index('def alpha') < rewritten.index('def beta')
        assert rewritten.index('def beta') < rewritten.index('def zeta')
        assert rewritten.startswith(_SRC_CLASS_LOCAL_INTERSTITIAL.split('\n\ndef alpha', 1)[0])
        assert result_stderr == blocker_line
        assert result_stderr.count(blocker_line) == 1

    def test_write_failure_lists_class_blocker_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_CLASS_LOCAL_INTERSTITIAL, encoding='utf-8')

        def fail_write(_path: Path, _content: str, **_kwargs: str) -> None:
            raise OSError('write denied')

        monkeypatch.setattr('defsort._cli.write_source', fail_write)

        exit_code = main(['--write', str(target)])

        captured = capsys.readouterr()
        blocker_line = '  class C: skipped (code between methods)\n'
        assert exit_code == EXIT_ERROR
        assert target.read_text(encoding='utf-8') == _SRC_CLASS_LOCAL_INTERSTITIAL
        assert str(target) not in captured.out
        assert f'error: {target}: write denied\n' in captured.err
        assert captured.err.count(blocker_line) == 1


class TestClosedStdout:
    @pytest.mark.parametrize('case', list(_CLOSED_STDOUT_STATUS))
    def test_closure_is_quiet(self, case: str, tmp_path: Path) -> None:
        result = _closed_stdout_result(case, tmp_path)
        assert result.stderr == b'', process_transcript(result)

    @pytest.mark.parametrize('case', list(_CLOSED_STDOUT_STATUS))
    def test_closure_keeps_the_established_status(self, case: str, tmp_path: Path) -> None:
        result = _closed_stdout_result(case, tmp_path)
        assert result.returncode == _CLOSED_STDOUT_STATUS[case], process_transcript(result)


class TestClosedStdoutInProcess:
    def test_closure_stops_before_processing_the_next_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class _ClosedStream(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        first = tmp_path / 'first.py'
        second = tmp_path / 'second.py'
        first.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        second.write_text('def invalid syntax', encoding='utf-8')
        closed = _ClosedStream()
        monkeypatch.setattr(sys, 'stdout', closed)

        try:
            with patch.object(_cli, '_sort_one_file', wraps=_cli._sort_one_file) as sort_file:
                assert main(('-d', str(first), str(second))) == EXIT_CHANGED
            assert [call.args[0] for call in sort_file.call_args_list] == [first]
            assert capsys.readouterr().err == ''
        finally:
            if sys.stdout is not closed:
                sys.stdout.close()

    def test_einval_from_main_work_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail_cli(_argv: Sequence[str] | None) -> int:
            raise OSError(errno.EINVAL, 'main work')

        monkeypatch.setattr(_cli, '_run_cli', fail_cli)
        monkeypatch.setattr(_downstream, '_WINDOWS', True)

        with pytest.raises(OSError, match='main work') as error:
            main([])
        assert error.value.errno == errno.EINVAL

    def test_main_returns_the_status_after_closure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _ClosedStream(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        def run_cli(_argv: Sequence[str] | None) -> int:
            print('discarded')
            return EXIT_CHANGED

        monkeypatch.setattr(_cli, '_run_cli', run_cli)
        closed = _ClosedStream()
        monkeypatch.setattr(sys, 'stdout', closed)

        try:
            assert main([]) == EXIT_CHANGED
            assert sys.stdout is not closed
            assert sys.stdout.write('discarded') == len('discarded')
            sys.stdout.flush()
        finally:
            if sys.stdout is not closed:
                sys.stdout.close()

    def test_windows_einval_during_output_keeps_the_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _WindowsClosedStream(io.StringIO):
            def write(self, _text: str) -> int:
                raise OSError(errno.EINVAL, 'Invalid argument')

        def run_cli(_argv: Sequence[str] | None) -> int:
            print('discarded')
            return EXIT_CHANGED

        monkeypatch.setattr(_downstream, '_WINDOWS', True)
        monkeypatch.setattr(_cli, '_run_cli', run_cli)
        closed = _WindowsClosedStream()
        monkeypatch.setattr(sys, 'stdout', closed)

        try:
            assert main([]) == EXIT_CHANGED
            assert sys.stdout is not closed
        finally:
            if sys.stdout is not closed:
                sys.stdout.close()


class TestColorDiff:
    def test_color_always_diff(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['--diff', '--color', 'always', str(f)]
        )
        assert result_status == 1, result_stderr
        assert '\033[' in result_stdout

    def test_color_always_overrides_no_color_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        monkeypatch.setenv('NO_COLOR', '1')
        _status, stdout, _stderr = invoke_cli(['--diff', '--color', 'always', str(f)])
        assert '\033[' in stdout

    def test_color_auto_non_tty(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result = run_cli(['--diff', str(f)], expect=1)
        assert '\033[' not in result.stdout

    def test_color_never_diff(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['--diff', '--color', 'never', str(f)]
        )
        assert result_status == 1, result_stderr
        assert '\033[' not in result_stdout


class TestColorPreview:
    def test_preview_color_always_bolds_skipped_header(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-p', '--color', 'always', str(f)]
        )

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stderr.startswith(
            f'\033[1m{f}\033[0m: skipped\n  reason: code between definitions\n'
        )
        assert '\033[1m: skipped' not in result_stderr
        assert '\033[1m  reason:' not in result_stderr

    def test_preview_color_always_has_spectral_on_moved_rows(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['-p', '-v', '--color', 'always', str(f)]
        )
        assert result_status == 1, result_stderr
        lines = result_stdout.strip().splitlines()
        # Header is bold
        assert '\033[1m' in lines[0]
        # At least one data row has spectral color (non-bold ANSI)
        data_lines = lines[1:]
        assert any('\033[3' in line for line in data_lines)  # spectral colors are \033[3Xm
        # No data row is wrapped in bold
        for line in data_lines:
            assert '\033[1m' not in line

    def test_preview_color_always_spectral_multi(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'test2.py'
        f2.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['-p', '-v', '--color', 'always', str(f), str(f2)]
        )
        assert result_status == 1, result_stderr
        # Has spectral color codes (not just bold)
        assert '\033[3' in result_stdout  # spectral colors are \033[3Xm
        # Bold present (headers/path labels) but not wrapping entire data rows
        assert '\033[1m' in result_stdout

    def test_preview_color_never_no_ansi(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['-p', '--color', 'never', str(f)]
        )
        assert result_status == 1, result_stderr
        assert '\033[' not in result_stdout

    def test_verbose_already_ordered_not_bolded(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(
            ['-v', '--color', 'always', str(f)]
        )
        assert '0%' in result_stdout
        lines = result_stdout.strip().splitlines()
        assert len(lines) >= 2
        assert '\033[1m' not in lines[1]  # data row not bold


class TestColorWrite:
    def test_write_color_always_bolds_header(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'test2.py'
        f2.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['-w', '--color', 'always', str(f), str(f2)]
        )
        assert result_status == 0, result_stderr
        lines = result_stdout.strip().splitlines()
        assert '\033[1m' in lines[0]  # header bold


class TestCommandExitCodes:
    def test_exit_0_detect_mode(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--detect', str(f)])
        assert result_status == 0, result_stderr

    def test_exit_0_diff_mode_no_changes(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-d', str(f)])
        assert result_status == 0, result_stderr

    def test_exit_1_changes_pending(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == 1, result_stderr

    def test_exit_1_skipped_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == 1, result_stderr


class TestConfigWarningRendering:
    def test_invalid_key_renders_warning_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nbanners = "bogus"\n')
        target = tmp_path / 'mod.py'
        target.write_text('x = 1\n')
        main([str(target)])
        err = capsys.readouterr().err
        assert (
            "Warning: Invalid [tool.defsort] banners: 'bogus' "
            "(expected 'barriers', 'ignore', or 'strip'), using default" in err
        )

    def test_rendering_survives_pythonwarnings_ignore(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nbanners = "bogus"\n')
        target = tmp_path / 'mod.py'
        target.write_text('x = 1\n')
        env = {**_ENV, 'PYTHONWARNINGS': 'ignore'}
        proc = run_cli([str(target)], env=env)
        assert 'Warning: Invalid [tool.defsort] banners' in proc.stderr

    @pytest.mark.skipif(
        sys.platform == 'win32', reason='Windows forbids control characters in filenames'
    )
    def test_repository_path_controls_are_escaped(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / 'bad\x1b]2;path\x07'
        project.mkdir()
        (project / 'pyproject.toml').write_text('[tool.defsort\n')
        target = project / 'mod.py'
        target.write_text('x = 1\n')

        result_status, _result_stdout, result_stderr = invoke_cli([str(target)])

        assert result_status == 0, result_stderr
        assert r'bad\x1b]2;path\x07/pyproject.toml' in result_stderr
        assert '\x1b]2;path\x07' not in result_stderr

    def test_unrelated_warning_delegates_before_body_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[str] = []

        def record_warning(message: Warning | str, *args: object, **kwargs: object) -> None:
            del args, kwargs
            seen.append(str(message))

        def warn_then_raise() -> None:
            with _render_safe_config_warnings():
                warnings.warn('unrelated', UserWarning, stacklevel=1)
                raise RuntimeError('sentinel')

        monkeypatch.setattr(warnings, 'showwarning', record_warning)

        with pytest.raises(RuntimeError, match='sentinel'):
            warn_then_raise()

        assert seen == ['unrelated']

    def test_unrelated_warning_delegates_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[str] = []

        def record_warning(message: Warning | str, *args: object, **kwargs: object) -> None:
            del args, kwargs
            seen.append(str(message))

        monkeypatch.setattr(warnings, 'showwarning', record_warning)

        with _render_safe_config_warnings():
            warnings.warn('unrelated', UserWarning, stacklevel=1)
            assert seen == ['unrelated']


class TestDetectErrorHandling:
    def test_detect_compile_invalid_exits_2(self, tmp_path: Path) -> None:
        f = tmp_path / 'bad.py'
        f.write_text('return 1\n', encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--detect', str(f)])
        assert result_status == EXIT_ERROR, result_stderr
        assert 'error:' in result_stderr

    def test_detect_missing_file_does_not_abort_others(self, tmp_path: Path) -> None:
        f = tmp_path / 'good.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['--detect', str(tmp_path / 'missing.py'), str(f)]
        )
        assert result_status == 2, result_stderr
        assert 'good.py' in result_stdout

    def test_detect_missing_file_exits_2(self, tmp_path: Path) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--detect', str(tmp_path / 'missing.py')]
        )
        assert result_status == 2, result_stderr
        assert 'error:' in result_stderr

    def test_detect_parse_invalid_exits_2_and_continues(self, tmp_path: Path) -> None:
        bad = tmp_path / 'bad.py'
        good = tmp_path / 'good.py'
        bad.write_text('def broken(:\n', encoding='utf-8')
        good.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['--detect', str(bad), str(good)])

        assert result_status == EXIT_ERROR, result_stderr
        assert f'error: {bad}' in result_stderr
        assert 'good.py' in result_stdout


class TestDetectFlag:
    def test_detect_conflict_with_diff(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--detect', '-d', str(f)])
        assert result_status == 2, result_stderr

    def test_detect_conflict_with_preview(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--detect', '-p', str(f)])
        assert result_status == 2, result_stderr

    def test_detect_ignores_modifiers(self, tmp_path: Path) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        f = tmp_path / 'mod.py'
        f.write_text(src)
        _base_result_status, base_result_stdout, _base_result_stderr = invoke_cli(
            ['--detect', str(f)]
        )
        _order_result_status, order_result_stdout, _order_result_stderr = invoke_cli(
            ['--detect', '-o', 'alphabetical', str(f)]
        )
        assert base_result_stdout == order_result_stdout

    def test_detect_mutually_exclusive_with_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('x = 1\n')
        result_status, _result_stdout, result_stderr = invoke_cli(['--detect', '-w', str(f)])
        assert result_status != 0, result_stderr

    def test_detect_shows_order(self, tmp_path: Path) -> None:
        src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        f = tmp_path / 'mod.py'
        f.write_text(src)
        result_status, result_stdout, result_stderr = invoke_cli(['--detect', str(f)])
        assert result_status == 0, result_stderr
        assert 'dependency' in result_stdout or 'both' in result_stdout

    def test_detect_skips_single_def(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def only():\n    pass\n')
        result_status, result_stdout, result_stderr = invoke_cli(['--detect', str(f)])
        assert result_status == 0, result_stderr
        # File should not appear in output (no --verbose)
        assert 'mod.py' not in result_stdout

    def test_detect_summary(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text('def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n')
        f2 = tmp_path / 'b.py'
        f2.write_text('def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n')
        _result_status, result_stdout, _result_stderr = invoke_cli(['--detect', str(tmp_path)])
        assert '1 unordered' in result_stdout

    def test_detect_unordered(self, tmp_path: Path) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        f = tmp_path / 'mod.py'
        f.write_text(src)
        _result_status, result_stdout, _result_stderr = invoke_cli(['--detect', str(f)])
        assert 'unordered' in result_stdout

    def test_detect_uses_effective_banner_mode(self, tmp_path: Path) -> None:
        source = tmp_path / 'mod.py'
        source.write_text('def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n')
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbanners = "ignore"\n',
            encoding='utf-8',
        )

        _configured_status, configured_stdout, _configured_stderr = invoke_cli(
            ['--detect', str(source)]
        )
        _overridden_status, overridden_stdout, _overridden_stderr = invoke_cli(
            ['--detect', '--banners', 'barriers', '-v', str(source)]
        )

        assert 'unordered' in configured_stdout
        assert 'too few definitions' in overridden_stdout

    def test_detect_uses_effective_banner_pattern(self, tmp_path: Path) -> None:
        source = tmp_path / 'mod.py'
        source.write_text('def bravo():\n    pass\n\n\n# CUSTOM\n\n\ndef alpha():\n    pass\n')
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbanner-pattern = "# CUSTOM"\n',
            encoding='utf-8',
        )

        _result_status, result_stdout, _result_stderr = invoke_cli(['--detect', '-v', str(source)])

        assert 'too few definitions' in result_stdout

    def test_detect_verbose_shows_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def only():\n    pass\n')
        _result_status, result_stdout, _result_stderr = invoke_cli(['--detect', '-v', str(f)])
        assert 'too few definitions' in result_stdout

    def test_detect_with_verbose(self, tmp_path: Path) -> None:
        src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        f = tmp_path / 'mod.py'
        f.write_text(src)
        result_status, result_stdout, result_stderr = invoke_cli(['--detect', '-v', str(f)])
        assert result_status == 0, result_stderr
        # Verbose should include tau scores
        assert 'tau' in result_stdout.lower() or 'score' in result_stdout.lower()


class TestDiffMode:
    def test_diff_does_not_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--diff', str(f)])
        assert result_status == 1, result_stderr
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--diff', str(f)])
        assert result_status == 1, result_stderr
        assert '---' in result_stdout
        assert '+++' in result_stdout

    def test_diff_short_flag(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-d', str(f)])
        assert result_status == 1, result_stderr
        assert '---' in result_stdout


class TestDiscoverRepoRoots:
    def test_discover_repo_roots_failed_parent_does_not_cover_descendants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        parent = tmp_path / 'outside'
        nested = parent / 'nested'
        nested.mkdir(parents=True)
        first = parent / 'a.py'
        second = parent / 'b.py'
        descendant = nested / 'c.py'
        calls: list[str] = []

        def fake_root(start: str) -> None:
            calls.append(start)

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        result = _discover_repo_roots([first, second, descendant])

        assert calls == [str(parent), str(nested)]
        assert result == {
            first.resolve(): None,
            second.resolve(): None,
            descendant.resolve(): None,
        }

    def test_discover_repo_roots_git_file_prevents_outer_root_reuse(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outer = tmp_path / 'repo'
        nested = outer / 'nested'
        nested.mkdir(parents=True)
        (nested / '.git').write_text('gitdir: ../gitdir\n', encoding='utf-8')
        outer_file = outer / 'outer.py'
        nested_file = nested / 'inner.py'
        calls: list[str] = []

        def fake_root(start: str) -> str:
            calls.append(start)
            return str(nested if Path(start) == nested else outer)

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        result = _discover_repo_roots([outer_file, nested_file])

        assert calls == [str(outer), str(nested)]
        assert result == {
            outer_file.resolve(): outer,
            nested_file.resolve(): nested,
        }

    def test_discover_repo_roots_marker_inspection_failure_falls_back_to_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outer = tmp_path / 'repo'
        nested = outer / 'nested'
        nested.mkdir(parents=True)
        outer_file = outer / 'outer.py'
        nested_file = nested / 'inner.py'
        calls: list[str] = []
        original_lstat = Path.lstat

        def fake_lstat(path: Path) -> os.stat_result:
            if path == nested / '.git':
                raise PermissionError('marker access denied')
            return original_lstat(path)

        def fake_root(start: str) -> str:
            calls.append(start)
            return str(nested if Path(start) == nested else outer)

        monkeypatch.setattr(Path, 'lstat', fake_lstat)
        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        result = _discover_repo_roots([outer_file, nested_file])

        assert calls == [str(outer), str(nested)]
        assert result[nested_file.resolve()] == nested

    def test_discover_repo_roots_nested_directory_marker_wins_in_both_orders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outer = tmp_path / 'repo'
        nested = outer / 'nested'
        nested.mkdir(parents=True)
        (nested / '.git').mkdir()
        outer_file = outer / 'outer.py'
        nested_file = nested / 'inner.py'
        outer_file.write_text('', encoding='utf-8')
        nested_file.write_text('', encoding='utf-8')

        def fake_root(start: str) -> str:
            return str(nested if Path(start) == nested else outer)

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        forward = _discover_repo_roots([outer_file, nested_file])
        reverse = _discover_repo_roots([nested_file, outer_file])

        assert forward[outer_file.resolve()] == outer
        assert forward[nested_file.resolve()] == nested
        assert reverse[outer_file.resolve()] == outer
        assert reverse[nested_file.resolve()] == nested

    def test_discover_repo_roots_queries_git_for_uncovered_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_repo = tmp_path / 'first'
        second_repo = tmp_path / 'second'
        first_repo.mkdir()
        second_repo.mkdir()
        first = first_repo / 'a.py'
        second = second_repo / 'b.py'
        calls: list[str] = []

        def fake_root(start: str) -> str:
            calls.append(start)
            return start

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        result = _discover_repo_roots([first, second])

        assert calls == [str(first_repo), str(second_repo)]
        assert result == {
            first.resolve(): first_repo,
            second.resolve(): second_repo,
        }

    def test_discover_repo_roots_reuses_root_for_sibling_directories(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / 'repo'
        first_parent = repo / 'first'
        second_parent = repo / 'second'
        first_parent.mkdir(parents=True)
        second_parent.mkdir()
        first = first_parent / 'a.py'
        second = second_parent / 'b.py'
        calls: list[str] = []

        def fake_root(start: str) -> str:
            calls.append(start)
            return str(repo)

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        result = _discover_repo_roots([first, second])

        assert calls == [str(first_parent)]
        assert result == {
            first.resolve(): repo,
            second.resolve(): repo,
        }

    def test_same_parent_queries_git_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = tmp_path / 'a.py'
        second = tmp_path / 'b.py'
        first.write_text('', encoding='utf-8')
        second.write_text('', encoding='utf-8')
        calls: list[str] = []

        def fake_root(start: str) -> str:
            calls.append(start)
            return str(tmp_path)

        monkeypatch.setattr('defsort._cli.try_get_repo_root', fake_root)

        _discover_repo_roots([first, second])

        assert calls == [str(tmp_path)]

    @pytest.mark.skipif(sys.platform != 'linux', reason='requires native Linux byte paths')
    @pytest.mark.parametrize('incremental', [True, False], ids=['incremental', 'guard'])
    def test_surrogate_repo_root_skips_baseline_without_traceback(
        self, tmp_path: Path, incremental: bool
    ) -> None:
        repo = tmp_path / os.fsdecode(b'repo-\xff')
        repo.mkdir()
        _init_empty_git_repo(repo)
        args = ['--incremental'] if incremental else []
        if not incremental:
            (repo / 'pyproject.toml').write_text(
                '[tool.defsort]\nmax-reorder-count = 0\n', encoding='utf-8'
            )
        path = repo / 'mod.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result = run_cli([*args, '--write', str(path)], expect=EXIT_CHANGED)
        assert 'unable to read baseline' in result.stderr
        assert 'Traceback' not in result.stderr
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize('incremental', [True, False], ids=['incremental', 'guard'])
    @pytest.mark.parametrize('source', [_SRC_NEEDS_SORT, 'def broken(\n'])
    def test_undecodable_repo_root_preserves_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        incremental: bool,
        source: str,
    ) -> None:
        args = ['--incremental'] if incremental else []
        if not incremental:
            (tmp_path / 'pyproject.toml').write_text(
                '[tool.defsort]\nmax-reorder-count = 0\n', encoding='utf-8'
            )
        path = tmp_path / 'mod.py'
        path.write_text(source, encoding='utf-8')

        def fail_decode(command: list[str], *_args: object, **_kwargs: object) -> None:
            assert command == ['git', 'rev-parse', '--show-toplevel']
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_decode)

        for mode in ('--json', '--write'):
            result = main([*args, mode, str(path)])

            captured = capsys.readouterr()
            record = json.loads(captured.out)['files'][0] if mode == '--json' else None
            if source == _SRC_NEEDS_SORT:
                assert result == EXIT_CHANGED
                if record is not None:
                    assert record['status'] == 'skipped'
                    assert 'unable to read baseline' in record['warning']
                else:
                    assert 'unable to read baseline' in captured.err
            else:
                assert result == EXIT_ERROR
                if record is not None:
                    assert record['status'] == 'invalid'
                assert 'unable to read baseline' not in captured.out + captured.err
            assert 'Traceback' not in captured.err
            assert path.read_text(encoding='utf-8') == source


class TestExactSourceWrite:
    def test_write_preserves_crlf(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n')
        invoke_cli(['-o', 'alphabetical', '-w', str(f)])
        raw = f.read_bytes()
        # All line endings should be CRLF (no bare \n)
        without_crlf = raw.replace(b'\r\n', b'<NL>')
        assert b'\n' not in without_crlf
        assert b'\r\n' in raw

    def test_write_preserves_lf(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n')
        invoke_cli(['-o', 'alphabetical', '-w', str(f)])
        raw = f.read_bytes()
        assert b'\r\n' not in raw

    def test_write_preserves_mixed_endings_exactly(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\n \t\rdef alpha():\n    pass\r\n')

        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-o', 'alphabetical', '-w', str(f)]
        )

        assert result_status == 0, result_stderr
        assert f.read_bytes() == b'def alpha():\n    pass\n \t\rdef bravo():\r\n    pass\r\n'


class TestExitCodes:
    def test_exit_code_values(self) -> None:
        assert EXIT_OK == 0
        assert EXIT_CHANGED == 1
        assert EXIT_ERROR == 2

    def test_main_accepts_argv(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        assert main([str(f)]) == EXIT_OK

    def test_main_returns_error_for_missing_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / 'nonexistent.py')]) == EXIT_ERROR
        assert 'error:' in capsys.readouterr().err

    def test_main_returns_ok_for_empty_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / 'empty'
        empty.mkdir()
        assert main([str(empty)]) == EXIT_OK
        assert capsys.readouterr().err == ''

    def test_missing_file_does_not_abort_others(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'good.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        rc = main([str(tmp_path / 'missing.py'), str(f)])
        assert rc == EXIT_ERROR
        captured = capsys.readouterr()
        assert 'error:' in captured.err
        assert 'good.py' in captured.out

    def test_non_table_tool_key_does_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('tool = "x"\n', encoding='utf-8')
        (tmp_path / 'mod.py').write_text('def alpha():\n    pass\n', encoding='utf-8')
        assert main([str(tmp_path)]) == EXIT_OK

    def test_unexpected_sort_exception_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        first = tmp_path / 'first.py'
        first.write_bytes(_SRC_ALREADY_ORDERED.encode('utf-8'))
        second = tmp_path / 'second.py'
        second.write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))
        processed: list[str] = []

        def raise_for_first(
            source: str,
            config: _ResolvedConfig,
            baseline: BlobReadResult | None,
            *,
            tree: ast.Module,
        ) -> SortResult:
            processed.append(source)
            if source == _SRC_ALREADY_ORDERED:
                raise RuntimeError('sentinel')
            return _run_sort(source, config, baseline, tree=tree)

        monkeypatch.setattr('defsort._cli._run_sort', raise_for_first)

        rc = main([str(first), str(second)])

        captured = capsys.readouterr()
        assert rc == EXIT_ERROR
        assert captured.err == f'error: {first}: sentinel\n'
        assert 'Traceback' not in captured.err
        assert processed == [_SRC_ALREADY_ORDERED, _SRC_NEEDS_SORT]
        assert 'second.py' in captured.out


class TestFileHandling:
    def test_directory_expansion(self, tmp_path: Path) -> None:
        d = tmp_path / 'pkg'
        d.mkdir()
        f = d / 'mod.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([str(d)])
        assert result_status == 1, result_stderr
        assert 'mod.py' in result_stdout

    def test_empty_directory_with_valid_file(self, tmp_path: Path) -> None:
        empty = tmp_path / 'empty'
        empty.mkdir()
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([str(empty), str(f)])
        assert result_status == 1, result_stderr
        assert 'test.py' in result_stdout

    def test_non_python_file_silently_ignored(self, tmp_path: Path) -> None:
        f = tmp_path / 'readme.txt'
        f.write_text('hello', encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == 0, result_stderr
        assert result_stderr == ''

    def test_symlink_file_processed(self, tmp_path: Path) -> None:
        target = tmp_path / 'target.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        link = tmp_path / 'link.py'
        link.symlink_to(target)
        result_status, result_stdout, result_stderr = invoke_cli([str(link)])
        assert result_status == 1, result_stderr
        assert 'link.py' in result_stdout

    def test_symlink_in_directory_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / 'pkg'
        d.mkdir()
        real = d / 'real.py'
        real.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        link = d / 'link.py'
        link.symlink_to(real)
        result_status, result_stdout, result_stderr = invoke_cli(['-w', str(d)])
        assert result_status == 0, result_stderr
        assert 'link.py' not in result_stdout


class TestHelpEpilog:
    def test_force_help_names_only_bypassable_blockers(self) -> None:
        result_status, result_stdout, result_stderr = invoke_cli(['--help'])

        assert result_status == 0, result_stderr
        normalized = ' '.join(result_stdout.split())
        assert 'Bypass interstitial and reorder-guard blockers.' in normalized
        assert 'duplicate-name' not in normalized
        assert 'Bypass safety checks.' not in normalized

    def test_help_describes_hook_subcommand(self) -> None:
        result_status, result_stdout, result_stderr = invoke_cli(['--help'])

        assert result_status == 0, result_stderr
        normalized = ' '.join(result_stdout.split())
        assert 'hook Agent hook entry point (see defsort hook --help)' in normalized

    def test_help_groups_detailed_policy_options(self) -> None:
        help_text = _build_parser().format_help()
        policy = help_text.partition('\nordering policy options:\n')[2].partition(
            '\nsubcommands:'
        )[0]
        flags = {
            line.strip().split(',', maxsplit=1)[0].split()[0]
            for line in policy.splitlines()
            if line.startswith('  -')
        }

        assert flags == {
            '--definition-order',
            '--method-order',
            '--evaluation-order',
            '--method-bodies',
            '--method-visibility',
            '--method-dunders',
            '--main',
            '--tie-break',
            '--banners',
            '--banner-pattern',
            '--no-banner-pattern',
        }

    def test_help_places_every_short_option_before_long_only_options(self) -> None:
        help_text = _build_parser().format_help()
        options = help_text.partition('options:\n')[2].partition('\nsubcommands:')[0]
        general, separator, _policy = options.partition('\nordering policy options:\n')
        flags = [
            line.strip().split(',', maxsplit=1)[0].split()[0]
            for line in general.splitlines()
            if line.startswith('  -')
        ]

        short_flags = [flag for flag in flags if not flag.startswith('--')]

        assert separator
        assert short_flags == [
            '-h',
            '-p',
            '-d',
            '-j',
            '-w',
            '-o',
            '-i',
            '-f',
            '-v',
        ]
        assert flags[:2] == ['-h', '--version']
        flags_without_version = [flag for flag in flags if flag != '--version']
        assert flags_without_version[: len(short_flags)] == short_flags
        assert flags_without_version[len(short_flags) : len(short_flags) + 4] == [
            '--color',
            '--no-incremental',
            '--show-skips',
            '--hide-skips',
        ]

    def test_policy_option_help_explains_behavior(self) -> None:
        descriptions = [
            (
                '--evaluation-order {preserve,allow-reordering}',
                'Preserve definition-time evaluation order or allow reordering.',
            ),
            (
                '--method-bodies {within-categories,ignore,across-categories}',
                (
                    'Let method-body dependencies stay within categories, be ignored, or cross '
                    'categories.'
                ),
            ),
            (
                '--method-visibility {mixed,public-first}',
                'Keep regular methods mixed or place public methods before private methods.',
            ),
            (
                '--method-dunders {before-regular,after-regular}',
                'Place non-lifecycle dunders before or after regular methods.',
            ),
            (
                '--main {last,sorted}',
                'Anchor main() last or sort it normally.',
            ),
            (
                '--tie-break {alphabetical,source}',
                'Break dependency ties alphabetically or by source order.',
            ),
        ]
        result_status, result_stdout, result_stderr = invoke_cli(['--help'])

        assert result_status == 0, result_stderr
        help_text = ' '.join(result_stdout.split())
        for option, behavior in descriptions:
            assert f'{option} {behavior}' in help_text


class TestHookSubcommandParser:
    def test_alpha_long_flag_rejected(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--alpha'], stdin='{}'
        )
        assert result_status != 0, result_stderr

    def test_hook_accepts_agent_option(self, registered_hosts: Path) -> None:
        assert registered_hosts.is_dir()
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'], stdin='{}'
        )
        assert result_status == 0, result_stderr

    def test_hook_accepts_event_for_antigravity(self, registered_hosts: Path) -> None:
        assert registered_hosts.is_dir()
        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin='{}'
        )
        assert result_status == 0, result_stderr
        assert result_stdout == ''

    def test_hook_conflicting_incremental_exits_2(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '-i', '--no-incremental']
        )
        assert result_status == 2, result_stderr

    def test_hook_help_exits_0(self) -> None:
        result_status, result_stdout, result_stderr = invoke_cli(['hook', '--help'])
        assert result_status == 0, result_stderr
        assert 'defsort hook' in result_stdout
        assert '--alpha' not in result_stdout
        assert '-a, --agent NAME' in result_stdout
        assert '-e, --event NAME' in result_stdout
        assert '--order' in result_stdout
        assert '--evaluation-order' in result_stdout
        assert '--allow-on-failure' in result_stdout
        assert '--banners' in result_stdout
        assert '--incremental' in result_stdout
        assert '--no-incremental' in result_stdout

    def test_hook_help_explains_agent_and_failure_overrides(self) -> None:
        result_status, result_stdout, result_stderr = invoke_cli(['hook', '--help'])

        assert result_status == 0, result_stderr
        normalized = ' '.join(result_stdout.split())
        assert (
            "Agent hook entry point; reads the coding agent's payload as JSON from stdin."
            in normalized
        )
        assert (
            '--allow-on-failure Warn and allow infrastructure failures after a command is '
            'accepted; safety blockers still deny.' in normalized
        )
        assert (
            '--no-incremental Disable incremental mode, overriding configured incremental mode.'
            in normalized
        )

    def test_hook_host_option_does_not_replace_required_agent(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--host', 'claude'], stdin='{}'
        )
        assert result_status == 2, result_stderr
        assert 'the following arguments are required: -a/--agent' in result_stderr

    def test_hook_rejects_host_option_alongside_agent(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--host', 'claude'], stdin='{}'
        )
        assert result_status == 2, result_stderr
        assert 'unrecognized arguments: --host claude' in result_stderr

    def test_hook_requires_agent(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(['hook'], stdin='{}')
        assert result_status == 2, result_stderr
        assert 'the following arguments are required: -a/--agent' in result_stderr

    def test_hook_short_agent_option_matches_long(self, registered_hosts: Path) -> None:
        assert registered_hosts.is_dir()
        short_status, short_stdout, short_stderr = invoke_cli(['hook', '-a', 'claude'], stdin='{}')
        long_status, long_stdout, long_stderr = invoke_cli(
            ['hook', '--agent', 'claude'], stdin='{}'
        )
        assert (short_status, short_stdout, short_stderr) == (
            long_status,
            long_stdout,
            long_stderr,
        )

    def test_hook_short_event_option_matches_long(self, registered_hosts: Path) -> None:
        assert registered_hosts.is_dir()
        short_status, short_stdout, short_stderr = invoke_cli(
            ['hook', '-a', 'agy', '-e', 'PreToolUse'], stdin='{}'
        )
        long_status, long_stdout, long_stderr = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin='{}'
        )
        assert (short_status, short_stdout, short_stderr) == (
            long_status,
            long_stdout,
            long_stderr,
        )

    def test_hook_tty_prints_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_stdin = io.StringIO()
        fake_stdin.isatty = lambda: True
        monkeypatch.setattr('sys.stdin', fake_stdin)
        result = main(['hook', '--agent', 'claude'])
        assert result == 0
        captured = capsys.readouterr()
        assert (
            "defsort hook: reads the coding agent's hook payload as JSON from stdin"
            in captured.err
        )
        assert 'docs/hook.md' in captured.err

    def test_hook_unknown_arg_exits_2(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--bogus']
        )
        assert result_status == 2, result_stderr


class TestIncrementalFlag:
    def test_incremental_diff_shows_changes(self, tmp_path: Path) -> None:
        _git_init_with_baseline(tmp_path)
        path = tmp_path / 'mod.py'
        source = path.read_text(encoding='utf-8').replace('charlie', 'aardvark')
        path.write_text(source, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(
            ['-i', '--definition-order', 'alphabetical', '-d', str(path)]
        )

        assert result_status == 1, result_stderr
        assert '@@' in result_stdout
        assert '+def aardvark():' in result_stdout
        assert '-def aardvark():' in result_stdout
        assert path.read_text(encoding='utf-8') == source

    def test_incremental_infra_error_agrees_with_json(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n')
        text_status, _text_stdout, text_stderr = invoke_cli(['-i', str(f)])
        payload_status, payload_stdout, _payload_stderr = invoke_cli(['-i', '--json', str(f)])
        record = json.loads(payload_stdout)['files'][0]
        assert record['status'] == 'skipped'
        assert text_status == payload_status, text_stderr

    def test_incremental_infra_error_does_not_misreport_definitions(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-i', '-v', str(f)])
        assert 'mod.py' not in result_stdout

    def test_incremental_infra_error_json_preserves_source_totals(
        self,
        tmp_path: Path,
    ) -> None:
        source = (
            'def bravo():\n'
            '    return alpha()\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 1\n'
            '\n'
            '\n'
            'class Charlie:\n'
            '    def method(self):\n'
            '        return None\n'
        )
        f = tmp_path / 'mod.py'
        f.write_text(source)

        result_status, result_stdout, result_stderr = invoke_cli(['-i', '--json', str(f)])

        record = json.loads(result_stdout)['files'][0]
        assert result_status == 1, result_stderr
        assert record['status'] == 'skipped'
        assert record['definitions'] == {'moved': 0, 'total': 3}
        assert record['methods'] == {'moved': 0, 'total': 1}
        assert record['movements'] == []
        assert record['method_movements'] == []
        assert f.read_text() == source

    def test_incremental_infra_error_reports_the_reason(self, tmp_path: Path) -> None:
        f = tmp_path / 'mod.py'
        f.write_text('def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n')
        _result_status, _result_stdout, result_stderr = invoke_cli(['-i', str(f)])
        assert 'unable to read baseline' in result_stderr

    def test_incremental_infra_error_skips(self, tmp_path: Path) -> None:
        # Create a file in a non-git directory
        f = tmp_path / 'mod.py'
        f.write_text('def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n')
        result_status, _result_stdout, result_stderr = invoke_cli(['-i', str(f)])
        assert result_status == 1, result_stderr
        # File should be skipped, not sorted
        content = f.read_text()
        assert content.index('bravo') < content.index('alpha')  # unchanged

    def test_incremental_new_file_no_head(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'new.py').write_text(
            'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        run_setup(['git', 'add', 'new.py'], cwd=tmp_path)
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-i', '-w', str(tmp_path / 'new.py')]
        )
        assert result_status == 0, result_stderr
        content = (tmp_path / 'new.py').read_text()
        assert content.index('alpha') < content.index('bravo')

    def test_incremental_noop_no_new_defs(self, tmp_path: Path) -> None:
        _git_init_no_changes(tmp_path)
        result_status, _result_stdout, result_stderr = invoke_cli(['-i', str(tmp_path / 'mod.py')])
        assert result_status == 0, result_stderr

    def test_incremental_untracked_file_full_sort(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Create file but don't git add it
        (tmp_path / 'untracked.py').write_text(
            'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-i', '-w', str(tmp_path / 'untracked.py')]
        )
        assert result_status == 0, result_stderr
        content = (tmp_path / 'untracked.py').read_text()
        assert content.index('alpha') < content.index('bravo')

    def test_incremental_write_sorts_new_defs(self, tmp_path: Path) -> None:
        _git_init_with_baseline(tmp_path)
        path = tmp_path / 'mod.py'
        source = path.read_text(encoding='utf-8').replace('charlie', 'aardvark')
        path.write_text(source, encoding='utf-8')
        expected = (
            'def aardvark():\n    return alpha()\n\n\n'
            'def alpha():\n    return 1\n\n\n'
            'def bravo():\n    return alpha()\n'
        )

        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-i', '--definition-order', 'alphabetical', '-w', str(path)]
        )

        assert result_status == 0, result_stderr
        assert path.read_text(encoding='utf-8') == expected

    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX byte filename semantics')
    @pytest.mark.parametrize(
        ('args', 'config_text'),
        [
            (['--incremental'], None),
            ([], '[tool.defsort]\nmax-reorder-count = 0\n'),
        ],
        ids=['incremental', 'reorder-guard'],
    )
    def test_unencodable_baseline_path_skips_without_traceback(
        self,
        tmp_path: Path,
        capfd: pytest.CaptureFixture[str],
        args: list[str],
        config_text: str | None,
    ) -> None:
        _init_empty_git_repo(tmp_path)
        bad_name = b'bad-\xff.py'.decode('utf-8', errors='surrogateescape')
        path = tmp_path / bad_name
        try:
            path.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        except OSError:
            pytest.skip('filesystem does not support non-UTF-8 filename bytes')
        run_setup(['git', 'add', '--', path.name], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        if config_text is not None:
            (tmp_path / 'pyproject.toml').write_text(config_text, encoding='utf-8')
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result = main([*args, str(path)])

        captured = capfd.readouterr()
        assert result == EXIT_CHANGED
        assert 'unable to read baseline' in captured.err
        assert 'Traceback' not in captured.err
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize(
        'mode_args',
        [pytest.param(['--write'], id='text-write'), pytest.param(['--json'], id='json')],
    )
    def test_unsupported_git_exits_before_output_or_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode_args: list[str],
    ) -> None:
        source = _SRC_NEEDS_SORT
        path = tmp_path / 'mod.py'
        path.write_text(source, encoding='utf-8')

        def reject_git(_paths: list[Path]) -> dict[Path, BlobReadResult]:
            raise git_module.UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._cli._batch_fetch_cli_baselines', reject_git)

        result = main(['--incremental', *mode_args, str(path)])

        captured = capsys.readouterr()
        assert result == EXIT_ERROR
        assert captured.out == ''
        assert captured.err == (
            'defsort: Git 2.42 or newer is required for Git-backed sorting (found Git 2.41.7)\n'
        )
        assert path.read_text(encoding='utf-8') == source


class TestJsonErrorStatus:
    def test_missing_file_json_exits_2(self, tmp_path: Path) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--json', str(tmp_path / 'missing.py')]
        )
        assert result_status == 2, result_stderr

    def test_missing_file_json_status_error(self, tmp_path: Path) -> None:
        _result_status, result_stdout, _result_stderr = invoke_cli(
            ['--json', str(tmp_path / 'missing.py')]
        )
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'error'
        assert rec['warning'] is not None


class TestJsonOutput:
    def test_detect_with_json_silently_ignored(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--detect', '--json', str(f)])
        assert result_status == 0, result_stderr
        data = json.loads(result_stdout)
        assert data['version'] == 1
        assert len(data['files']) == 1

    def test_json_compile_invalid(self, tmp_path: Path) -> None:
        f = tmp_path / 'bad.py'
        f.write_text('return 1\n', encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(f)])
        assert result_status == EXIT_ERROR, result_stderr
        data = json.loads(result_stdout)
        assert data['files'][0]['status'] == 'invalid'

    def test_json_detect_fields_use_effective_banner_mode(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(
            'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )

        _barriers_status, barriers_stdout, _barriers_stderr = invoke_cli(['--json', str(source)])
        _ignored_status, ignored_stdout, _ignored_stderr = invoke_cli(
            ['--json', '--banners', 'ignore', str(source)]
        )

        barrier_record = json.loads(barriers_stdout)['files'][0]
        ignore_record = json.loads(ignored_stdout)['files'][0]
        assert barrier_record['status'] == 'unchanged'
        assert barrier_record['order'] is None
        assert ignore_record['status'] == 'changed'
        assert ignore_record['order'] == 'unordered'

    def test_json_detect_fields_use_effective_banner_pattern(self, tmp_path: Path) -> None:
        source = tmp_path / 'test.py'
        source.write_text(
            'def bravo():\n    pass\n\n\n# CUSTOM\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbanner-pattern = "# CUSTOM"\n',
            encoding='utf-8',
        )

        _result_status, result_stdout, _result_stderr = invoke_cli(['--json', str(source)])

        record = json.loads(result_stdout)['files'][0]
        assert record['status'] == 'unchanged'
        assert record['order'] is None

    def test_json_does_not_reparse_sectioned_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = (
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    return bravo()\n\n\n'
            '# --------\n\n\n'
            'def delta():\n    pass\n\n\n'
            'def charlie():\n    return delta()\n'
        )
        tree = ast.parse(source)
        options = EngineOptions(policy=SortPolicy())
        result = _cli.sort_file(source, options=options, _tree=tree)
        file_result = _FileSortResult(
            status=_FileStatus.UNCHANGED,
            sort_result=result,
            encoding='utf-8',
        )

        def fail_parse(*_args: object, **_kwargs: object) -> None:
            pytest.fail('JSON serialization reparsed definitions')

        monkeypatch.setattr('defsort._core._api.parse_definitions', fail_parse)

        assert _file_result_to_json(Path('sectioned.py'), file_result, options) == {
            'path': 'sectioned.py',
            'status': 'unchanged',
            'warning': None,
            'order': 'dependency',
            'dep_score': 1.0,
            'alpha_score': -1.0,
            'definitions': {'moved': 0, 'total': 4},
            'methods': {'moved': 0, 'total': 0},
            'movements': [],
            'method_movements': [],
            'banner_constraints': [],
            'class_blockers': [],
        }

    def test_json_exit_1_mixed_files(self, tmp_path: Path) -> None:
        (tmp_path / 'sorted.py').write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        (tmp_path / 'unsorted.py').write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(
            ['--json', str(tmp_path / 'sorted.py'), str(tmp_path / 'unsorted.py')]
        )
        assert result_status == 1, result_stderr
        data = json.loads(result_stdout)
        assert len(data['files']) == 2
        statuses = {r['status'] for r in data['files']}
        assert 'unchanged' in statuses
        assert 'changed' in statuses

    def test_json_few_defs_null_detect(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text('def only():\n    pass\n', encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['--json', str(f)])
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['order'] is None
        assert rec['dep_score'] is None
        assert rec['alpha_score'] is None

    def test_json_full_current_source_parse_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = _SRC_NEEDS_SORT
        transformed_source = _SRC_ALREADY_ORDERED
        path = tmp_path / 'test.py'
        path.write_bytes(source.encode('utf-8'))
        parse_counts: Counter[str] = Counter()
        original_parse = ast.parse

        def counting_parse(
            candidate: str,
            *args: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
            **kwargs: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
        ) -> ast.AST:
            parse_counts[candidate] += 1
            return cast('ast.AST', original_parse(candidate, *args, **kwargs))

        monkeypatch.setattr(ast, 'parse', counting_parse)

        assert main(['--json', str(path)]) == EXIT_CHANGED
        capsys.readouterr()

        assert parse_counts[source] == 1, parse_counts
        assert parse_counts[transformed_source] == 0

    def test_json_incremental_current_source_parse_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        baseline = 'def alpha():\n    pass\n\n\ndef charlie():\n    return alpha()\n'
        current_source = baseline + '\n\ndef bravo():\n    return alpha()\n'
        transformed_source = (
            'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n\n\n'
            'def charlie():\n    return alpha()\n'
        )
        path = tmp_path / 'test.py'
        path.write_bytes(current_source.encode('utf-8'))
        parse_counts: Counter[str] = Counter()
        original_parse = ast.parse

        def counting_parse(
            candidate: str,
            *args: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
            **kwargs: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
        ) -> ast.AST:
            parse_counts[candidate] += 1
            return cast('ast.AST', original_parse(candidate, *args, **kwargs))

        def fetch_baseline(paths: list[Path]) -> dict[Path, BlobReadResult]:
            assert paths == [path]
            return {
                path: BlobReadResult(
                    status=BlobReadStatus.CONTENT,
                    content=baseline,
                )
            }

        monkeypatch.setattr(ast, 'parse', counting_parse)
        monkeypatch.setattr('defsort._cli._batch_fetch_cli_baselines', fetch_baseline)

        assert main(['--json', '--incremental', str(path)]) == EXIT_CHANGED
        capsys.readouterr()

        assert parse_counts[current_source] == 1, parse_counts
        assert parse_counts[baseline] > 0
        assert parse_counts[transformed_source] > 0

    def test_json_incremental_infra_error(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', '-i', str(f)])
        assert result_status == 1, result_stderr
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'skipped'
        assert rec['warning'] is not None
        assert 'incremental' in rec['warning'].lower()

    def test_json_incremental_reports_movements(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'test.py'
        path.write_text('def bravo():\n    pass\n\n\ndef alpha():\n    pass\n', encoding='utf-8')
        baseline = 'def bravo():\n    pass\n'

        def fetch_baseline(paths: list[Path]) -> dict[Path, BlobReadResult]:
            return {
                paths[0]: BlobReadResult(
                    status=BlobReadStatus.CONTENT,
                    content=baseline,
                )
            }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr('defsort._cli._batch_fetch_cli_baselines', fetch_baseline)
            assert (
                main(['--json', '--incremental', '-o', 'alphabetical', str(path)]) == EXIT_CHANGED
            )

        record = json.loads(capsys.readouterr().out)['files'][0]
        assert record['definitions']['moved'] > 0
        assert record['movements'] == [
            {'name': 'alpha', 'from': 1, 'to': 0},
            {'name': 'bravo', 'from': 0, 'to': 1},
        ]

    def test_json_skipped_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(f)])
        assert result_status == 1, result_stderr
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'skipped'
        assert rec['warning'] is not None
        assert rec['movements'] == []
        assert 'blocking_code' not in rec

    def test_json_sorted_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(f)])
        assert result_status == 0, result_stderr
        data = json.loads(result_stdout)
        assert data['version'] == 1
        rec = data['files'][0]
        assert rec['status'] == 'unchanged'
        assert rec['movements'] == []

    def test_json_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / 'bad.py'
        f.write_text('def foo(:\n    pass\n', encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(f)])
        assert result_status == 2, result_stderr
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'invalid'
        assert rec['warning'] is not None
        assert rec['order'] is None
        assert rec['definitions'] == {'moved': 0, 'total': 0}

    def test_json_unsorted_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['--json', str(f)])
        assert result_status == 1, result_stderr
        data = json.loads(result_stdout)
        assert data['version'] == 1
        rec = data['files'][0]
        assert rec['status'] == 'changed'
        assert rec['warning'] is None
        assert len(rec['movements']) > 0
        assert rec['order'] is not None

    def test_json_with_alphabetical_order(self, tmp_path: Path) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(
            ['--json', '-o', 'alphabetical', str(f)]
        )
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'changed'
        names = [m['name'] for m in rec['movements']]
        assert 'bravo' in names

    def test_json_with_force(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['--json', '-f', str(f)])
        data = json.loads(result_stdout)
        rec = data['files'][0]
        # With --force, interstitial file should not be skipped
        assert rec['status'] != 'skipped'


class TestLargeReorderGuard:
    @staticmethod
    def _setup_guarded_repo(tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        out_of_order = (
            'def c():\n    pass\n\n\ndef b():\n    pass\n\n\n# --------\n\n\ndef a():\n    pass\n'
        )
        (tmp_path / 'mod.py').write_text(out_of_order, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        toml = '[tool.defsort]\nmax-reorder-count = 0\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')

    def test_force_bypasses_guard(self, tmp_path: Path) -> None:
        self._setup_guarded_repo(tmp_path)
        original = (tmp_path / 'mod.py').read_text(encoding='utf-8')
        invoke_cli(['--write', '--force', str(tmp_path / 'mod.py')])
        assert (tmp_path / 'mod.py').read_text(encoding='utf-8') != original

    def test_guard_clone_preserves_class_blockers(self) -> None:
        class_blocker = ClassBlocker(
            class_name='C',
            occurrence=1,
            blocker='duplicates',
            message='non-consecutive duplicate definitions',
        )
        result = SortResult(
            original_source='source',
            new_source='sorted',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
            class_blockers=(class_blocker,),
        )

        guarded = _guarded_skip(result, 'guarded')

        assert guarded.class_blockers == (class_blocker,)

    def test_guard_restores_source_with_initial_comment(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        source = (
            '# about zeta\n'
            'def zeta():\n'
            '    return 1\n'
            '\n\n'
            '# about alpha\n'
            'def alpha():\n'
            '    return 2\n'
        )
        target = tmp_path / 'mod.py'
        target.write_text(source, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 0\n',
            encoding='utf-8',
        )

        _result_status, _result_stdout, result_stderr = invoke_cli(
            ['--write', '-o', 'alphabetical', str(target)]
        )

        assert target.read_text(encoding='utf-8') == source
        assert 'would move 2 of 2' in result_stderr

    def test_guarded_class_blocker_survives_text_and_json(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        source = (
            _SRC_DUP_METHODS
            + '\n\ndef zed():\n    pass\n\n\ndef beta():\n    pass\n\n\ndef alpha():\n    pass\n'
        )
        target = tmp_path / 'mod.py'
        target.write_text(source, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 0\n',
            encoding='utf-8',
        )

        text_result_status, _text_result_stdout, text_result_stderr = invoke_cli([str(target)])
        _json_result_status, json_result_stdout, _json_result_stderr = invoke_cli(
            ['--json', str(target)]
        )
        record = json.loads(json_result_stdout)['files'][0]

        assert text_result_status == EXIT_CHANGED, text_result_stderr
        assert 'would move' in text_result_stderr
        assert 'class C: skipped (duplicate method names)' in text_result_stderr
        assert record['status'] == 'skipped'
        assert record['class_blockers'] == [
            {'class': 'C', 'occurrence': 1, 'blocker': 'duplicates'}
        ]

    def test_json_mode_emits_skipped(self, tmp_path: Path) -> None:
        self._setup_guarded_repo(tmp_path)
        _result_status, result_stdout, _result_stderr = invoke_cli(
            ['--json', str(tmp_path / 'mod.py')]
        )
        data = json.loads(result_stdout)
        assert data['files'][0]['status'] == 'skipped'
        assert 'would move' in data['files'][0]['warning']

    def test_summary_shows_guard_warning(self, tmp_path: Path) -> None:
        self._setup_guarded_repo(tmp_path)
        _result_status, _result_stdout, result_stderr = invoke_cli([str(tmp_path / 'mod.py')])
        assert 'would move' in result_stderr
        assert 'sort constrained by 1 banner' in result_stderr

    def test_write_mode_skips_guarded_file(self, tmp_path: Path) -> None:
        self._setup_guarded_repo(tmp_path)
        original = (tmp_path / 'mod.py').read_text(encoding='utf-8')
        _result_status, _result_stdout, result_stderr = invoke_cli(
            ['--write', str(tmp_path / 'mod.py')]
        )
        assert (tmp_path / 'mod.py').read_text(encoding='utf-8') == original
        assert 'would move' in result_stderr


class TestModuleInvocation:
    @pytest.mark.parametrize('module', ['defsort', 'defsort._cli'])
    def test_help_via_module(self, module: str) -> None:
        result = run_setup([sys.executable, '-m', module, '--help'], env=_ENV, check=False)
        assert result.returncode == EXIT_OK, process_transcript(result)
        assert 'usage: defsort' in decode_text(result.stdout)

    def test_subprocess_host_roots_cleaned_after_normal_exit(self) -> None:
        roots: list[Path] = []
        for _ in range(2):
            result = run_setup(
                [
                    sys.executable,
                    '-c',
                    'from tests._helpers import _HOSTS; assert _HOSTS.is_dir(); print(_HOSTS)',
                ],
                env=_ENV,
            )
            roots.append(Path(decode_text(result.stdout).strip()))

        assert roots[0] != roots[1]
        assert all(not root.exists() for root in roots)


class TestMultiFile:
    def test_multi_file_output(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([str(f1), str(f2)])
        assert result_status == 1, result_stderr
        assert 'a.py' in result_stdout
        assert 'b.py' not in result_stdout


class TestMultiFilePreview:
    def test_multi_file_preview_already_ordered_separated(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', '-v', str(f1), str(f2)])
        lines = result_stdout.splitlines()
        for i, line in enumerate(lines):
            if 'already in' in line:
                assert not line.startswith('  ')  # not indented
                assert i > 0
                assert lines[i - 1].strip() == ''  # blank line before

    def test_multi_file_preview_blank_line_after_filename(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', str(f1), str(f2)])
        lines = result_stdout.splitlines()
        for i, line in enumerate(lines):
            if line.rstrip().endswith(':') and line.strip():
                # Next line should be blank
                assert i + 1 < len(lines)
                assert lines[i + 1].strip() == '', f'expected blank after {line!r}'

    def test_multi_file_preview_indents_tables(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', str(f1), str(f2)])
        lines = [line for line in result_stdout.splitlines() if line.strip()]
        for line in lines:
            if line.endswith(':'):
                # Filename header -- not indented
                assert not line.startswith('  ')
            else:
                # Table content -- indented 2 spaces
                assert line.startswith('  ')

    def test_multi_file_preview_no_leading_blank_line(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f1), str(f2)])
        assert result_status == 1, result_stderr
        assert result_stdout, 'expected non-empty output'
        assert not result_stdout.startswith('\n'), (
            f'output starts with blank line: {result_stdout!r}'
        )

    def test_multi_file_preview_shows_tables(self, tmp_path: Path) -> None:
        f1 = tmp_path / 'a.py'
        f1.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f2 = tmp_path / 'b.py'
        f2.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f1), str(f2)])
        assert result_status == 1, result_stderr
        assert 'Current' in result_stdout
        assert 'Proposed' in result_stdout

    def test_multi_file_preview_skipped_files_are_detailed(self, tmp_path: Path) -> None:
        skipped_a = tmp_path / 'a.py'
        skipped_a.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        skipped_b = tmp_path / 'b.py'
        skipped_b.write_text(
            'def render_settings():\n'
            '    return normalize_settings()\n'
            '\n'
            'if os.environ.get("DEFSORT_EXAMPLE_DEBUG") == "1":\n'
            '    print("debug")\n'
            '\n'
            'def normalize_settings():\n'
            '    return {}\n',
            encoding='utf-8',
        )
        sortable = tmp_path / 'c.py'
        sortable.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(
            ['-p', str(skipped_a), str(skipped_b), str(sortable)]
        )

        assert result_status == EXIT_CHANGED, result_stderr
        assert 'c.py:' in result_stdout
        assert f'{skipped_a}: skipped\n' in result_stderr
        assert f'{skipped_b}: skipped\n' in result_stderr
        assert result_stderr.count('  reason: code between definitions') == 2
        assert result_stderr.count('  fix: use --force to bypass safety checks') == 2
        assert (
            '  fix: use --force to bypass safety checks\n\n  Code between definitions:'
        ) in result_stderr
        assert result_stderr.count('  Code between definitions:') == 2
        assert '    line 4: registry = {}' in result_stderr
        assert '    line 4: if os.environ.get("DEFSORT_EXAMPLE_DEBUG") == "1":' in result_stderr
        assert f'{skipped_a}:4:' not in result_stderr
        assert f'{skipped_b}:4:' not in result_stderr
        assert 'Proposed reorder:' not in result_stderr
        assert result_stderr.count('  Current') == 2


class TestMultiProjectColorConfig:
    def _make_file(self, root: Path, color: str) -> Path:
        root.mkdir()
        (root / 'pyproject.toml').write_text(
            f'[tool.defsort]\ncolor = "{color}"\n', encoding='utf-8'
        )
        source = root / 'm.py'
        source.write_text('def zeta():\n    pass\n\n\ndef alpha():\n    pass\n')
        return source

    def test_conflicting_colors_fall_back_to_auto_in_both_orders(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        always = self._make_file(tmp_path / 'always', 'always')
        never = self._make_file(tmp_path / 'never', 'never')
        for order in ([always, never], [never, always]):
            clear_config_cache()
            main(['-d', *(str(path) for path in order)])
            captured = capsys.readouterr()
            assert '\x1b[' not in captured.out


class TestNoFuture:
    def test_no_future_blocks_forward_refs(self, tmp_path: Path) -> None:
        source = (
            'class Zulu:\n    pass\n\n\n'
            'class Alpha:\n'
            '    def take(self, value: Zulu):\n'
            '        pass\n'
        )
        normal = tmp_path / 'normal.py'
        no_future = tmp_path / 'no_future.py'
        source_bytes = source.encode()
        normal.write_bytes(source_bytes)
        no_future.write_bytes(source_bytes)

        normal_result_status, _normal_result_stdout, normal_result_stderr = invoke_cli(
            ['--definition-order', 'alphabetical', '-w', str(normal)]
        )
        no_future_result_status, _no_future_result_stdout, no_future_result_stderr = invoke_cli(
            ['--definition-order', 'alphabetical', '-w', '--no-future', str(no_future)]
        )

        normal_source = normal.read_text(encoding='utf-8')
        assert normal_result_status == 0, normal_result_stderr
        assert 'from __future__ import annotations' in normal_source
        assert normal_source.index('class Alpha') < normal_source.index('class Zulu')
        assert no_future_result_status == 1, no_future_result_stderr
        assert no_future.read_bytes() == source_bytes
        assert (
            'forward annotation references require future annotations' in no_future_result_stderr
        )


class TestNoIncrementalFlag:
    def test_incremental_and_no_incremental_conflict(self) -> None:
        result_status, _result_stdout, result_stderr = invoke_cli(['-i', '--no-incremental', '.'])
        assert result_status != 0, result_stderr

    def test_no_incremental_accepted_by_parser(self) -> None:
        result_status, result_stdout, result_stderr = invoke_cli(['--no-incremental', '--help'])
        assert result_status == 0, result_stderr
        assert '--no-incremental' in result_stdout

    def test_no_incremental_overrides_config(self, tmp_path: Path) -> None:
        # Baseline: defs in wrong dependency order (bravo calls alpha)
        _init_empty_git_repo(tmp_path)
        baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        (tmp_path / 'mod.py').write_text(baseline)
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Write config that enables incremental mode
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nincremental = true\n')
        # --no-incremental should force full sort, reordering existing defs
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--no-incremental', '-w', str(tmp_path / 'mod.py')]
        )
        assert result_status == 0, result_stderr
        content = (tmp_path / 'mod.py').read_text()
        # Full sort: alpha (dependency) before bravo
        assert content.index('alpha') < content.index('bravo')


class TestPerFileConfig:
    def _make_two_projects(self, tmp_path: Path) -> tuple[Path, Path]:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\ndefinition-order = "alphabetical"\n',
            encoding='utf-8',
        )
        root_file = tmp_path / 'root_mod.py'
        root_file.write_text(
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )

        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\ndefinition-order = "none"\n',
            encoding='utf-8',
        )
        nested_file = nested / 'nested_mod.py'
        nested_file.write_text(
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )
        return root_file, nested_file

    def test_cli_flag_overrides_every_project(self, tmp_path: Path) -> None:
        _root_file, nested_file = self._make_two_projects(tmp_path)
        assert (
            main(
                [
                    '--write',
                    '--definition-order',
                    'alphabetical',
                    str(tmp_path),
                ]
            )
            == EXIT_OK
        )
        nested_text = nested_file.read_text(encoding='utf-8')
        assert nested_text.index('def alpha') < nested_text.index('def bravo')

    def test_directory_argument_resolves_from_first_collected_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\ncolor = "always"\n',
            encoding='utf-8',
        )
        (nested / 'mod.py').write_text(
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )

        main([str(tmp_path)])

        assert '\x1b[' in capsys.readouterr().out

    def test_each_file_uses_its_own_config(self, tmp_path: Path) -> None:
        root_file, nested_file = self._make_two_projects(tmp_path)
        assert main(['--write', str(tmp_path)]) == EXIT_OK
        root_text = root_file.read_text(encoding='utf-8')
        assert root_text.index('def alpha') < root_text.index('def bravo')
        nested_text = nested_file.read_text(encoding='utf-8')
        assert nested_text.index('def bravo') < nested_text.index('def alpha')

    def test_guard_in_nested_project_still_fetches_baseline(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[project]\nname = "root"\n',
            encoding='utf-8',
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 1\n',
            encoding='utf-8',
        )
        target = nested / 'mod.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        seen: list[Path] = []

        def fetch_baseline(paths: list[Path]) -> dict[Path, BlobReadResult]:
            seen.extend(paths)
            return {path: BlobReadResult(status=BlobReadStatus.INFRA_ERROR) for path in paths}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr('defsort._cli._batch_fetch_cli_baselines', fetch_baseline)
            main([str(tmp_path)])

        assert target.resolve() in {path.resolve() for path in seen}

    def test_incremental_is_per_project(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = false\n',
            encoding='utf-8',
        )
        (tmp_path / 'aaa_first.py').write_text(
            'def alpha():\n    pass\n',
            encoding='utf-8',
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\nmax-reorder-count = 99\n',
            encoding='utf-8',
        )
        target = nested / 'mod.py'
        target.write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))

        seen: list[Path] = []
        nested_incremental: list[bool] = []
        original_run_sort = _run_sort

        def fetch_baseline(paths: list[Path]) -> dict[Path, BlobReadResult]:
            seen.extend(paths)
            return {path: BlobReadResult(status=BlobReadStatus.MISSING) for path in paths}

        def record_incremental(
            source: str,
            config: _ResolvedConfig,
            baseline_raw: BlobReadResult | None,
            *,
            tree: ast.Module,
        ) -> SortResult:
            if source == _SRC_NEEDS_SORT:
                nested_incremental.append(config.incremental)
            return original_run_sort(source, config, baseline_raw, tree=tree)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr('defsort._cli._batch_fetch_cli_baselines', fetch_baseline)
            mp.setattr('defsort._cli._run_sort', record_incremental)
            main([str(tmp_path)])

        assert target.resolve() in {path.resolve() for path in seen}
        assert nested_incremental == [True]

    def test_no_python_files_does_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / 'notes.txt').write_text('nothing here\n', encoding='utf-8')
        assert main([str(tmp_path)]) == EXIT_OK

    def test_resolver_caches_each_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / 'mod.py'
        path.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        calls: list[Path] = []

        original_resolve = _cli.resolve_runtime_config

        def resolve_config(candidate: Path) -> ResolvedConfig:
            calls.append(candidate)
            return original_resolve(candidate)

        monkeypatch.setattr(_cli, 'resolve_runtime_config', resolve_config)
        resolver = _ConfigResolver(
            overrides=SortOverrides(),
            force=False,
            hoist=False,
            add_future=True,
        )

        first = resolver._for_path(path)
        second = resolver._for_path(path)

        assert first is second
        assert calls == [path]

    def test_show_skips_is_per_project(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nshow-skips = false\n',
            encoding='utf-8',
        )
        (tmp_path / 'hidden.py').write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nshow-skips = true\n',
            encoding='utf-8',
        )
        (nested / 'shown.py').write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        main([str(tmp_path)])
        err = capsys.readouterr().err
        assert 'shown.py' in err
        assert 'hidden.py' not in err


class TestPerFileIncrementalConfig:
    def _make_repo(self, root: Path, incremental: str) -> Path:
        root.mkdir()
        run_setup(['git', 'init', '-q', str(root)])
        run_setup(['git', '-C', str(root), 'config', 'user.email', 'a@example.com'])
        run_setup(['git', '-C', str(root), 'config', 'user.name', 'a'])
        (root / 'pyproject.toml').write_text(
            f'[tool.defsort]\nincremental = {incremental}\n', encoding='utf-8'
        )
        source = root / 'm.py'
        source.write_text('def zebra():\n    pass\n\n\ndef apple():\n    pass\n')
        run_setup(['git', '-C', str(root), 'add', '.'])
        run_setup(['git', '-C', str(root), 'commit', '-qm', 'init'])
        source.write_text(source.read_text() + '\n\ndef mango():\n    pass\n')
        return source

    def test_incremental_resolved_per_file_in_both_orders(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clear_config_cache()
        file_a = self._make_repo(tmp_path / 'repo_a', 'true')
        file_b = self._make_repo(tmp_path / 'repo_b', 'false')
        for order in ([file_a, file_b], [file_b, file_a]):
            clear_config_cache()
            main(['-d', *(str(path) for path in order)])
            captured = capsys.readouterr()
            # repo_a is incremental: unsorted baseline -> skipped, no diff.
            assert 'repo_a' not in captured.out
            assert 'skipped' in captured.err
            # repo_b is full-sort: the diff moves zebra.
            assert '-def zebra():' in captured.out

    def test_ineffective_policy_warned_for_later_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clear_config_cache()
        file_a = self._make_repo(tmp_path / 'repo_a', 'true')
        file_b = self._make_repo(tmp_path / 'repo_b', 'false')
        (tmp_path / 'repo_b' / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = false\norder = "alphabetical"\ntie-break = "source"\n',
            encoding='utf-8',
        )
        clear_config_cache()
        main(['-d', str(file_a), str(file_b)])
        captured = capsys.readouterr()
        assert 'tie-break has no effect' in captured.err


class TestPreviewMethodTables:
    def test_preview_both_defs_and_methods(self, tmp_path: Path) -> None:
        src = (
            'def zulu():\n    pass\n\n\n'
            'class C:\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n\n'
            'def alpha():\n    pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', str(f)])
        # Should have definition movement AND method movement
        assert 'zulu' in result_stdout
        assert 'C ' in result_stdout  # class header for method table

    def test_preview_methods_only_no_def_table(self, tmp_path: Path) -> None:
        src = (
            'class C:\n    def bravo(self):\n        pass\n\n    def alpha(self):\n        pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', str(f)])
        # Should have method table but check that we don't show
        # a definition table with no movement
        lines = result_stdout.strip().splitlines()
        # First non-empty line should be the class header, not "Current"
        assert lines[0].startswith('C ')

    def test_preview_shows_method_table(self, tmp_path: Path) -> None:
        src = (
            'class C:\n    def bravo(self):\n        pass\n\n    def alpha(self):\n        pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', str(f)])
        assert 'C' in result_stdout
        assert 'alpha' in result_stdout
        assert 'bravo' in result_stdout
        assert 'methods' in result_stdout

    def test_preview_verbose_shows_all_rows(self, tmp_path: Path) -> None:
        src = (
            'def alpha():\n    pass\n\n\n'
            'def charlie():\n    pass\n\n\n'
            'def bravo():\n    return alpha()\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', '-v', str(f)])
        # All 3 definitions should appear (charlie didn't move)
        assert 'alpha' in result_stdout
        assert 'bravo' in result_stdout
        assert 'charlie' in result_stdout


class TestPreviewMode:
    def test_preview_already_ordered_silent(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f)])
        assert result_status == 0, result_stderr
        assert result_stdout.strip() == ''

    def test_preview_does_not_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-p', str(f)])
        assert result_status == 1, result_stderr
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_preview_method_rows_indented(self, tmp_path: Path) -> None:
        src = (
            'class C:\n'
            '    def zebra(self):\n'
            '        pass\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n\n'
            'def zeta():\n'
            '    return alpha()\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f)])
        assert result_status == 1, result_stderr
        lines = result_stdout.splitlines()
        # Class header should not be indented
        class_line = next(ln for ln in lines if 'methods' in ln)
        assert not class_line.startswith(' ')
        # Method column header should be indented
        col_headers = [ln for ln in lines if 'Current' in ln and 'Proposed' in ln]
        assert len(col_headers) >= 2
        assert col_headers[1].startswith('  ')

    def test_preview_short_flag(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _short_status, short_stdout, _short_stderr = invoke_cli(['-p', str(f)])
        _long_status, long_stdout, _long_stderr = invoke_cli(['--preview', str(f)])
        assert short_stdout == long_stdout

    def test_preview_shows_table(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f)])
        assert result_status == 1, result_stderr
        assert 'Current' in result_stdout
        assert 'Proposed' in result_stdout

    def test_preview_source_only_change_shows_fallback(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        # Already sorted but has unnecessary future import -> removed (source-only change)
        f.write_text(
            'from __future__ import annotations\n\n\n'
            'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n',
            encoding='utf-8',
        )
        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f)])
        assert result_status == 1, result_stderr
        # Future import removed (source-only change) -> fallback X/Y format
        assert '0/2 (0%)' in result_stdout

    def test_preview_verbose_shows_already_ordered(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-p', '-v', str(f)])
        assert 'already in' in result_stdout


class TestSafetySkip:
    def test_diff_skipped_output_stays_compact(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['-d', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stdout == ''
        assert result_stderr == f'{f}: skipped (code between definitions)\n'
        assert 'Proposed reorder:' not in result_stderr
        assert 'Use --force' not in result_stderr

    def test_force_bypasses(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-f', '-w', str(f)])
        assert result_status == 0, result_stderr
        new_content = f.read_text(encoding='utf-8')
        assert new_content.index('alpha') < new_content.index('bravo')

    def test_interstitial_does_not_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-w', str(f)])
        assert result_status == 1, result_stderr
        # File should NOT be modified
        assert f.read_text(encoding='utf-8') == _SRC_INTERSTITIAL
        assert result_stderr == f'{f}: skipped (code between definitions)\n'
        assert '--force' not in result_stderr
        assert '--force' not in result_stdout

    @pytest.mark.parametrize('args', [(), ('--write',), ('--diff',)])
    def test_ordered_interstitial_is_a_true_noop(
        self, tmp_path: Path, args: tuple[str, ...]
    ) -> None:
        source = 'def alpha():\n    pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
        path = tmp_path / 'test.py'
        path.write_bytes(source.encode())

        result_status, result_stdout, result_stderr = invoke_cli(
            ['--no-incremental', *args, str(path)]
        )

        assert result_status == EXIT_OK, result_stderr
        assert result_stderr == ''
        assert 'skipped' not in result_stdout
        assert path.read_bytes() == source.encode()

    def test_preview_called_sibling_body_does_not_write(self, tmp_path: Path) -> None:
        source = (
            'def zeta_compute():\n'
            '    return 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )
        path = tmp_path / 'called_sibling.py'
        path.write_text(source, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-o', 'alphabetical', '--preview', '--force', str(path)]
        )

        assert result_status == EXIT_CHANGED, result_stderr
        assert path.read_text(encoding='utf-8') == source
        assert f'{path}: skipped\n' in result_stderr
        assert '  reason: sort would move a decorator' in result_stderr
        assert '--force' not in result_stderr

    def test_preview_duplicate_skip_omits_blocking_code_section(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_DUPLICATES, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['-p', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert f'{f}: skipped\n' in result_stderr
        assert '  reason: non-consecutive duplicate definitions\n' in result_stderr
        assert '  fix: remove the duplicate definition\n' in result_stderr
        assert '--force' not in result_stderr
        assert 'Code between definitions:' not in result_stderr
        assert 'Proposed reorder:' not in result_stderr
        assert 'Current' in result_stderr
        assert 'Proposed' in result_stderr

    def test_preview_force_does_not_bypass_eager_order(self, tmp_path: Path) -> None:
        source = (
            'def zeta_default():\n'
            '    return 1\n'
            '\n\n'
            'def alpha(value=zeta_default()):\n'
            '    return value\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(source, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-o', 'alphabetical', '--preview', '--force', str(f)]
        )

        assert result_status == EXIT_CHANGED, result_stderr
        assert f.read_text(encoding='utf-8') == source
        assert f'{f}: skipped\n' in result_stderr
        assert '  reason: sort would move a decorator' in result_stderr
        assert (
            '  fix: keep each referenced definition before the code that uses it' in result_stderr
        )
        assert '--force' not in result_stderr

    def test_preview_force_keeps_duplicate_skip_diagnostic(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_DUPLICATES, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['-p', '--force', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert f'{f}: skipped\n' in result_stderr
        assert '  reason: non-consecutive duplicate definitions\n' in result_stderr
        assert '  fix: remove the duplicate definition\n' in result_stderr
        assert '--force' not in result_stderr
        assert result_stdout == ''

    def test_preview_force_suppresses_bypassed_skip_diagnostic(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['-p', '--force', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stderr == ''
        assert 'Current' in result_stdout
        assert 'Proposed' in result_stdout
        assert 'skipped' not in result_stdout

    def test_preview_reports_line_number_for_leading_code(self, tmp_path: Path) -> None:
        src = (
            'def bravo():\n'
            '    return alpha()\n'
            '\n'
            '# ordinary comment\n'
            '\n'
            "if sys.platform == 'win32':\n"
            '    patch()\n'
            'def alpha():\n'
            '    pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['-p', str(f)])

        assert result_status == 1, result_stderr
        assert result_stdout == ''
        assert f'{f}: skipped\n' in result_stderr
        assert '  reason: code between definitions\n' in result_stderr
        assert '  fix: use --force to bypass safety checks\n' in result_stderr
        assert 'Proposed reorder:' not in result_stderr
        assert 'Current' in result_stderr
        assert 'Proposed' in result_stderr
        assert 'Code between definitions:' in result_stderr
        assert "    line 6: if sys.platform == 'win32':" in result_stderr
        assert f'{f}:6:' not in result_stderr
        assert (
            result_stderr.index('  reason:')
            < result_stderr.index('  Code between definitions:')
            < result_stderr.index('Current')
        )
        assert 'After ' not in result_stderr
        assert 'between bravo and alpha' not in result_stderr

    def test_preview_skipped_verbose_controls_unchanged_rows(self, tmp_path: Path) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
            '\n'
            'registry = {}\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
        )
        path = tmp_path / 'test.py'
        path.write_text(source, encoding='utf-8')

        compact_status, _compact_stdout, compact_stderr = invoke_cli(
            ['-p', '--color', 'never', str(path)]
        )
        verbose_status, _verbose_stdout, verbose_stderr = invoke_cli(
            ['-p', '-v', '--color', 'never', str(path)]
        )

        unchanged_row = '  alpha    alpha     -'
        assert compact_status == EXIT_CHANGED, compact_stderr
        assert verbose_status == EXIT_CHANGED, verbose_stderr
        assert unchanged_row not in compact_stderr
        assert unchanged_row in verbose_stderr
        assert path.read_text(encoding='utf-8') == source


class TestScopedOrderFlags:
    def test_definition_order_alphabetical(self, tmp_path: Path) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--definition-order', 'alphabetical', '-w', str(f)]
        )
        assert result_status == 0, result_stderr
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_definition_order_none(self, tmp_path: Path) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
            '\n\n'
            'def zeta():\n    pass\n\n\ndef able():\n    pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        invoke_cli(['--definition-order', 'none', '-w', str(f)])
        content = f.read_text(encoding='utf-8')
        # Top-level order preserved: Foo before zeta before able
        assert content.index('class Foo') < content.index('def zeta')
        assert content.index('def zeta') < content.index('def able')
        # Methods sorted: alpha before bravo
        assert content.index('def alpha') < content.index('def bravo')

    def test_method_order_none(self, tmp_path: Path) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
        )
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['--method-order', 'none', '-w', str(f)]
        )
        assert result_status == 0, result_stderr
        content = f.read_text(encoding='utf-8')
        assert content.index('bravo') < content.index('alpha')

    def test_order_with_scoped_override(self, tmp_path: Path) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(
            ['-o', 'alphabetical', '--method-order', 'dependency', '-w', str(f)]
        )
        assert result_status == 0, result_stderr
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')


class TestSkipMessageOrdering:
    def test_all_skipped_no_leading_blank(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        _result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert 'skipped' in result_stderr
        assert not result_stderr.startswith('\n')

    @pytest.mark.parametrize(
        ('flags', 'primary'),
        [(('-d',), '---'), (('-p',), 'Current'), ((), 'a.py')],
        ids=['diff', 'preview', 'summary'],
    )
    def test_skips_follow_primary_output(
        self, tmp_path: Path, flags: tuple[str, ...], primary: str
    ) -> None:
        sortable = tmp_path / 'a.py'
        sortable.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        skipped = tmp_path / 'b.py'
        skipped.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        # Put the skipped input first to exercise deferral as well as flushing.
        process = subprocess.Popen(
            [*_CLI, *flags, str(skipped), str(sortable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            env=_ENV,
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
        result = finish_process(process)
        assert result.returncode == EXIT_CHANGED, process_transcript(result)
        assert result.stdout.index(primary) < result.stdout.index('b.py: skipped')

    def test_unchanged_plus_skipped_no_blank(self, tmp_path: Path) -> None:
        unchanged = tmp_path / 'a.py'
        unchanged.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        skipped = tmp_path / 'b.py'
        skipped.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        _result_status, result_stdout, result_stderr = invoke_cli([str(unchanged), str(skipped)])
        assert result_stdout == ''
        assert 'skipped' in result_stderr
        assert not result_stderr.startswith('\n')


class TestSkipMessageVisibility:
    def test_config_hide_skips_suppresses_summary_message(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nshow-skips = false\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli([str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stdout == ''
        assert result_stderr == ''

    def test_hide_skips_does_not_suppress_json_warning(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(
            ['--hide-skips', '--json', str(f)]
        )

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stderr == ''
        data = json.loads(result_stdout)
        rec = data['files'][0]
        assert rec['status'] == 'skipped'
        assert rec['warning'] == 'code between definitions'

    def test_hide_skips_does_not_suppress_preview_details(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--hide-skips', '-p', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert f'{f}: skipped\n' in result_stderr
        assert '  reason: code between definitions\n' in result_stderr
        assert '  fix: use --force to bypass safety checks\n' in result_stderr
        assert 'Code between definitions:' in result_stderr

    def test_hide_skips_suppresses_diff_message(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, result_stdout, result_stderr = invoke_cli(['--hide-skips', '-d', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stdout == ''
        assert result_stderr == ''

    def test_hide_skips_suppresses_write_message(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--hide-skips', '-w', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert f.read_text(encoding='utf-8') == _SRC_INTERSTITIAL
        assert result_stderr == ''

    def test_show_skips_overrides_config_hide(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nshow-skips = false\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--show-skips', str(f)])

        assert result_status == EXIT_CHANGED, result_stderr
        assert result_stderr == f'{f}: skipped (code between definitions)\n'


class TestSummaryMode:
    def test_default_already_ordered_silent(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == 0, result_stderr
        assert result_stdout.strip() == ''

    def test_default_shows_summary(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli([str(f)])
        assert 'Name' in result_stdout
        assert 'Defs' in result_stdout
        assert '%' in result_stdout
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT  # not modified

    def test_directory_one_changed_no_total(self, tmp_path: Path) -> None:
        changed = tmp_path / 'a.py'
        changed.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        ordered = tmp_path / 'b.py'
        ordered.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli([str(tmp_path)])
        assert 'a.py' in result_stdout
        assert 'TOTAL' not in result_stdout

    def test_skipped_file_excluded_from_summary_totals(self, tmp_path: Path) -> None:
        sortable = tmp_path / 'a.py'
        sortable.write_text(
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n', encoding='utf-8'
        )
        blocked = tmp_path / 'b.py'
        blocked.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        _result_status, result_stdout, result_stderr = invoke_cli([str(tmp_path)])
        assert 'b.py' not in result_stdout
        assert 'skipped' in result_stderr
        assert '%' in result_stdout

    def test_skipped_shows_reason(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        _result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert 'skipped' in result_stderr

    def test_summary_retains_rows_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = tmp_path / 'first.py'
        first.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        second = tmp_path / 'second.py'
        second.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        captured: list[SummaryRow] = []

        def capture_rows(
            rows: list[SummaryRow],
            *,
            verbose: bool,
            color: bool,
        ) -> str:
            del verbose, color
            captured.extend(rows)
            return ''

        monkeypatch.setattr(_cli, 'format_summary_rows', capture_rows, raising=False)
        assert main([str(first), str(second)]) == EXIT_CHANGED
        assert captured
        assert all(isinstance(row, SummaryRow) for row in captured)

    def test_verbose_shows_already_ordered(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-v', str(f)])
        assert '0%' in result_stdout


class TestSyntaxErrorHandling:
    def test_compile_invalid_exits_2(self, tmp_path: Path) -> None:
        f = tmp_path / 'bad.py'
        f.write_text('return 1\n', encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == EXIT_ERROR, result_stderr
        assert 'error:' in result_stderr

    def test_syntax_error_does_not_abort_others(self, tmp_path: Path) -> None:
        bad = tmp_path / 'bad.py'
        bad.write_text('def foo(:\n', encoding='utf-8')
        good = tmp_path / 'good.py'
        good.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli([str(bad), str(good)])
        assert result_status == 2, result_stderr
        assert 'good.py' in result_stdout

    def test_syntax_error_exits_2(self, tmp_path: Path) -> None:
        f = tmp_path / 'bad.py'
        f.write_text('def foo(:\n', encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli([str(f)])
        assert result_status == 2, result_stderr
        assert 'error:' in result_stderr

    def test_syntax_error_exits_2_in_write_mode(self, tmp_path: Path) -> None:
        bad = tmp_path / 'bad.py'
        bad.write_text('def foo(:\n', encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-w', str(bad)])
        assert result_status == 2, result_stderr


class TestVersion:
    def test_help_goes_to_stdout(self) -> None:
        status, stdout, stderr = invoke_cli(['--help'])
        assert (status, stderr) == (0, '')
        assert stdout.startswith('usage: defsort')
        assert '--version' in stdout

    def test_missing_metadata_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, 'version', missing)

        status, stdout, stderr = invoke_cli(['--version'])
        assert (status, stdout) == (EXIT_ERROR, '')
        assert 'installed distribution metadata for defsort is unavailable' in stderr

    def test_normal_runs_do_not_read_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def forbidden(name: str) -> str:
            raise AssertionError(f'metadata read for {name}')

        monkeypatch.setattr(importlib.metadata, 'version', forbidden)
        (tmp_path / 'mod.py').write_text('def a():\n    pass\n', encoding='utf-8')

        assert invoke_cli([str(tmp_path / 'mod.py')])[0] == EXIT_OK

    def test_version_ignores_other_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def version(_name: str) -> str:
            return '9.8.7'

        monkeypatch.setattr(importlib.metadata, 'version', version)

        assert invoke_cli(['--version', '-d', 'missing.py']) == (0, 'defsort 9.8.7\n', '')

    def test_version_prints_the_installed_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def version(name: str) -> str:
            assert name == 'defsort'
            return '9.8.7'

        monkeypatch.setattr(importlib.metadata, 'version', version)

        assert invoke_cli(['--version']) == (0, 'defsort 9.8.7\n', '')


class TestWriteFailureSummary:
    def test_failed_write_excluded_from_summary(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        unsorted = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        read_only = tmp_path / 'ro.py'
        writable = tmp_path / 'ok.py'
        read_only.write_text(unsorted, encoding='utf-8')
        writable.write_text(unsorted, encoding='utf-8')
        read_only.chmod(0o444)
        try:
            exit_code = main(['-w', str(read_only), str(writable)])
        finally:
            read_only.chmod(0o644)
        captured = capsys.readouterr()
        assert exit_code == 2
        assert 'ro.py' in captured.err
        assert 'error:' in captured.err
        assert 'ro.py' not in captured.out
        assert 'ok.py' in captured.out
        assert read_only.read_text(encoding='utf-8') == unsorted


class TestWriteMode:
    def test_deep_dependency_chain_sorts_without_recursion(self, tmp_path: Path) -> None:
        source = '\n\n\n'.join(
            f'def node_{index:04d}():\n    return node_{index + 1:04d}()' for index in range(1_499)
        )
        source += '\n\n\ndef node_1499():\n    pass\n'
        f = tmp_path / 'deep_chain.py'
        f.write_text(source, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--write', str(f)])

        assert result_status == 0, result_stderr
        content = f.read_text(encoding='utf-8')
        assert content.index('def node_1499') < content.index('def node_0000')

    def test_utf8_file_roundtrip(self, tmp_path: Path) -> None:
        src = (
            '# -*- coding: utf-8 -*-\n# Comment: caf\u00e9 \u2603\n\n'
            'def b():\n    pass\n\n\ndef a():\n    return b()\n'
        )
        f = tmp_path / 'utf8.py'
        f.write_text(src, encoding='utf-8')
        run_result_status, _run_result_stdout, run_result_stderr = invoke_cli(['-w', str(f)])
        assert run_result_status == 0, run_result_stderr
        content = f.read_text(encoding='utf-8')
        assert '\u2603' in content
        assert 'def a' in content

    def test_write_already_ordered_silent(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        result_status, result_stdout, result_stderr = invoke_cli(['-w', str(f)])
        assert result_status == 0, result_stderr
        assert result_stdout.strip() == ''

    def test_write_keeps_header_like_first_body_comment_valid(self, tmp_path: Path) -> None:
        source = (
            'def zebra():\n'
            '    # encoding: utf-8 assumed\n'
            '    return 1\n'
            '\n\n'
            'def apple():\n'
            '    return 2\n'
        )
        path = tmp_path / 'test.py'
        path.write_text(source, encoding='utf-8')

        result_status, _result_stdout, result_stderr = invoke_cli(['--write', str(path)])
        written = path.read_text(encoding='utf-8')

        assert result_status == EXIT_OK, result_stderr
        ast.parse(written)
        assert written.count('# encoding: utf-8 assumed') == 1

    def test_write_long_flag(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['--write', str(f)])
        assert result_status == 0, result_stderr
        new_content = f.read_text(encoding='utf-8')
        assert new_content.index('alpha') < new_content.index('bravo')

    def test_write_modifies_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result_status, _result_stdout, result_stderr = invoke_cli(['-w', str(f)])
        assert result_status == 0, result_stderr
        new_content = f.read_text(encoding='utf-8')
        assert new_content.index('alpha') < new_content.index('bravo')

    def test_write_prints_description(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-w', str(f)])
        assert 'Name' in result_stdout
        assert '%' in result_stdout

    def test_write_verbose_shows_already_ordered(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _result_status, result_stdout, _result_stderr = invoke_cli(['-w', '-v', str(f)])
        assert '0%' in result_stdout


def _closed_stdout_result(case: str, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    if case == 'help-before-write':
        return run_with_closed_output(['--help'])
    target = tmp_path / 'target.py'
    source = _SRC_NEEDS_SORT if case == 'small-before-write' else _BIG_UNSORTED
    target.write_bytes(source.encode('utf-8'))
    if case == 'after-first-line':
        return run_closing_after_first_line(['-d', str(target)])
    return run_with_closed_output(['-d', str(target)])


def _git_init_no_changes(tmp_path: Path) -> None:
    _init_empty_git_repo(tmp_path)
    src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
    (tmp_path / 'mod.py').write_text(src)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)


def _git_init_with_baseline(tmp_path: Path) -> None:
    _init_empty_git_repo(tmp_path)
    baseline = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
    (tmp_path / 'mod.py').write_text(baseline)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    # Add a new definition to working copy
    new_content = baseline + '\n\ndef charlie():\n    return alpha()\n'
    (tmp_path / 'mod.py').write_text(new_content)


def _git_init_with_duplicate_method_baseline(tmp_path: Path) -> None:
    _init_empty_git_repo(tmp_path)
    path = tmp_path / 'mod.py'
    path.write_text(_SRC_DUP_METHODS, encoding='utf-8')
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    path.write_text(
        _SRC_DUP_METHODS + '\n    def new(self):\n        return 4\n',
        encoding='utf-8',
    )


def _parse_overrides(argv: list[str]) -> SortOverrides:
    args = _build_parser().parse_args([*argv, 'x.py'])
    return _overrides_from_args(args)


def test_abbreviation_rejected(tmp_path: Path) -> None:
    status, _stdout, stderr = invoke_cli(['--method-b', 'ignore', str(tmp_path)])
    assert status == 2, stderr


def test_alpha_flag_rejected() -> None:
    status, _stdout, stderr = invoke_cli(['-a'])
    assert status == 2, stderr


def test_error_path_and_detail_escape_terminal_controls(tmp_path: Path) -> None:
    target = tmp_path / 'missing\x1b]2;path\x07\n.py'

    result_status, _result_stdout, result_stderr = invoke_cli([str(target)])

    assert result_status == 2, result_stderr
    assert r'missing\x1b]2;path\x07\x0a.py' in result_stderr
    assert '\x1b]2;path\x07' not in result_stderr


def test_invalid_cli_regex_is_usage_error(tmp_path: Path) -> None:
    status, _stdout, stderr = invoke_cli(['--banner-pattern', '(', str(tmp_path)])
    assert status == 2, stderr


@pytest.mark.parametrize('flags', [[], ['--write'], ['--json']])
@pytest.mark.parametrize('mode', ['full', 'force', 'incremental'])
def test_layout_blocker_never_writes(tmp_path: Path, flags: list[str], mode: str) -> None:
    baseline = (
        'class A:\n    def zulu(self):\n        pass\n\n'
        'class C:\n    def alpha(self):\n        pass\n'
        '    # --------\n    def zulu(self):\n        pass\n'
    )
    source = (
        baseline.replace('class C:', '    def alpha(self):\n        pass\n\nclass C:')
        + '    # for bravo\n    def bravo(self):\n        pass\n'
    )
    path = tmp_path / 'test.py'
    if mode == 'incremental':
        _init_empty_git_repo(tmp_path)
        path.write_text(baseline, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'baseline'])
    original = source.replace('\n', '\r\n').encode()
    path.write_bytes(original)
    mode_flags = {'full': [], 'force': ['--force'], 'incremental': ['--incremental']}[mode]

    result_status, result_stdout, result_stderr = invoke_cli([*flags, *mode_flags, str(path)])

    assert result_status == 1, result_stderr
    assert path.read_bytes() == original
    assert 'Traceback' not in result_stderr
    if '--json' in flags:
        data = json.loads(result_stdout)
        assert data['version'] == 1
        record = data['files'][0]
        assert record['status'] == 'skipped'
        assert 'layout and comment ownership' in record['warning']
        assert record['movements'] == record['method_movements'] == []
        assert set(record) == {
            'path',
            'status',
            'warning',
            'order',
            'dep_score',
            'alpha_score',
            'definitions',
            'methods',
            'movements',
            'method_movements',
            'banner_constraints',
            'class_blockers',
        }
    else:
        assert 'layout and comment ownership' in result_stderr


@pytest.mark.parametrize(('key', 'flags'), _CLI_INEFFECTIVE_CASES)
def test_ineffective_flag_warns(key: str, flags: list[str], tmp_path: Path) -> None:
    target = tmp_path / 'mod.py'
    target.write_text('def f():\n    return 1\n', encoding='utf-8')
    _result_status, _result_stdout, result_stderr = invoke_cli([*flags, str(target)])
    assert key in result_stderr


def test_method_bodies_flag_reaches_engine(tmp_path: Path) -> None:
    target = tmp_path / 'mod.py'
    target.write_text(_SRC_INIT_BODY_CLI, encoding='utf-8')
    _default_status, default_stdout, _default_stderr = invoke_cli(['-d', str(target)])
    _across_status, across_stdout, _across_stderr = invoke_cli(
        ['-d', '--method-bodies', 'across-categories', str(target)]
    )
    assert default_stdout == ''  # conforms under the default policy
    assert 'def load' in across_stdout  # across-categories sinks __init__ below load


def test_no_banner_pattern_clears_custom_pattern(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\nbanner-pattern = "^# CUSTOM$"\n', encoding='utf-8'
    )
    target = tmp_path / 'mod.py'
    target.write_text(_SRC_CUSTOM_BANNER, encoding='utf-8')
    _with_pattern_status, with_pattern_stdout, _with_pattern_stderr = invoke_cli(
        ['-d', str(target)], cwd=tmp_path
    )
    _cleared_status, cleared_stdout, _cleared_stderr = invoke_cli(
        ['-d', '--no-banner-pattern', str(target)], cwd=tmp_path
    )
    assert with_pattern_stdout == ''  # the custom banner is a barrier
    assert 'def alpha' in cleared_stdout  # the custom banner is ordinary layout after clearing


def test_no_banner_pattern_conflicts_with_pattern(tmp_path: Path) -> None:
    result_status, _result_stdout, result_stderr = invoke_cli(
        ['--banner-pattern', 'x', '--no-banner-pattern', str(tmp_path)]
    )
    assert result_status == 2, result_stderr


def test_no_banner_pattern_restores_builtin(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\nbanner-pattern = "^# CUSTOM$"\n', encoding='utf-8'
    )
    target = tmp_path / 'mod.py'
    target.write_text(_SRC_BUILTIN_BANNER, encoding='utf-8')
    _cleared_status, cleared_stdout, _cleared_stderr = invoke_cli(
        ['--no-banner-pattern', str(target)], cwd=tmp_path
    )
    assert cleared_stdout == ''  # the built-in banner remains a barrier


@pytest.mark.parametrize(('argv', 'field', 'expected'), _AXIS_FLAG_CASES)
def test_policy_flag_maps_to_override(argv: list[str], field: str, expected: object) -> None:
    overrides = _parse_overrides(argv)
    assert getattr(overrides, field) == expected


def test_show_policy_defaults_block(tmp_path: Path) -> None:
    result_status, result_stdout, result_stderr = invoke_cli(['--show-policy'], cwd=tmp_path)
    assert result_status == 0, result_stderr
    assert result_stdout == (
        'built-in defaults:\n'
        '  definitions: dependency; main last; ties alphabetical\n'
        '  methods: dependency; categories __new__, __init__, dunders, regular (mixed);\n'
        '           body dependencies within categories; ties alphabetical\n'
        '  evaluation order: preserve\n'
        '  banners: barriers\n'
    )
    assert '(with CLI overrides)' not in result_stdout


@pytest.mark.skipif(
    sys.platform == 'win32', reason='Windows forbids control characters in filenames'
)
def test_show_policy_escapes_source_path_controls(tmp_path: Path) -> None:
    project = tmp_path / 'bad\x1b]2;path\x07'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
    target = project / 'mod.py'
    target.write_text('def f():\n    return 1\n')

    result_status, result_stdout, result_stderr = invoke_cli(['--show-policy', str(target)])

    assert result_status == 0, result_stderr
    assert r'bad\x1b]2;path\x07/pyproject.toml' in result_stdout
    assert '\x1b]2;path\x07' not in result_stdout


def test_show_policy_groups_by_source(tmp_path: Path) -> None:
    for name in ('one', 'two'):
        proj = tmp_path / name
        proj.mkdir()
        (proj / 'pyproject.toml').write_text(
            '[tool.defsort]\norder = "alphabetical"\n', encoding='utf-8'
        )
        (proj / 'mod.py').write_text('def f():\n    return 1\n', encoding='utf-8')
    _result_status, result_stdout, _result_stderr = invoke_cli(
        ['--show-policy', 'one/mod.py', 'two/mod.py'], cwd=tmp_path
    )
    assert result_stdout.count('policy from ') == 2
    assert result_stdout.index(str(Path('one') / 'pyproject.toml')) < result_stdout.index(
        str(Path('two') / 'pyproject.toml')
    )


def test_show_policy_labels_cli_override(tmp_path: Path) -> None:
    result_status, result_stdout, result_stderr = invoke_cli(
        ['--show-policy', '--order', 'none', '--evaluation-order', 'allow-reordering'],
        cwd=tmp_path,
    )
    assert result_status == 0, result_stderr
    assert result_stdout == (
        'built-in defaults (with CLI overrides):\n'
        '  definitions: none; main last; ties alphabetical\n'
        '  methods: none; categories __new__, __init__, dunders, regular (mixed);\n'
        '           body dependencies within categories; ties alphabetical\n'
        '  evaluation order: allow-reordering\n'
        '  banners: barriers\n'
    )


def test_show_policy_rejects_detect(tmp_path: Path) -> None:
    status, _stdout, stderr = invoke_cli(['--show-policy', '--detect', str(tmp_path)])
    assert status == 2, stderr


def test_show_policy_sectionless_pyproject_reports_defaults(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n', encoding='utf-8')
    (tmp_path / 'mod.py').write_text('def f():\n    return 1\n', encoding='utf-8')
    _result_status, result_stdout, _result_stderr = invoke_cli(
        ['--show-policy', 'mod.py'], cwd=tmp_path
    )
    assert 'built-in defaults' in result_stdout


def test_show_policy_unreadable_path_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'mod.py'
    target.write_text('def f():\n    return 1\n', encoding='utf-8')
    with patch.object(Path, 'open', side_effect=PermissionError('read denied')):
        result = main(['--show-policy', str(target)])
    assert result == EXIT_ERROR
    assert 'read denied' in capsys.readouterr().err


def test_unsafe_banner_pattern_is_usage_error_before_write(tmp_path: Path) -> None:
    target = tmp_path / 'mod.py'
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

    result_status, _result_stdout, result_stderr = invoke_cli(
        ['--write', '--banner-pattern', '^# (TOP|METHOD)$', str(target)]
    )

    assert result_status == 2, result_stderr
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
