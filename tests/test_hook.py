import json
import os
import shlex
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path, PureWindowsPath
from types import FunctionType
from typing import cast, get_args
from unittest.mock import patch

import pytest

import defsort._hook as hook_mod
import defsort._hook_front as front_mod
from defsort._config import ResolvedConfig
from defsort._core._guard import ReorderGuardParams
from defsort._core._parse import parse_definitions
from defsort._git import (
    RepoDiscoveryStatus,
    UnsupportedGitVersionError,
    _RepoDiscoveryResult,
    _StagedFileInfo,
    prepare_index_entry,
)
from defsort._hook import (
    _BLOCKER_MSGS,
    _Disposition,
    _file_config,
    _is_fallback_blocker,
    _plan_file_sort,
    _resolve_and_sort,
    _resolve_edited_paths,
    _resolve_hook_context,
    _sort_worktree_files,
    _try_sort,
)
from defsort._hook_front import _REASON_PREFIX, Outcome, run_hook
from defsort._hosts import load_adapter
from defsort._types import (
    BANNER_MODE_VALUES,
    EXIT_OK,
    BannerMode,
    BlobReadResult,
    BlobReadStatus,
    Blocker,
    FallbackMode,
    HookContext,
    SortOverrides,
    SortPolicy,
    SortScopeOrder,
    is_soft_blocker,
)
from defsort._vendor.hookshell._grammar import _ELEMENT_UNKNOWN_CWD, _HINT_ROUTE
from defsort.hooks import HookAdapter, HookResult, HostInput, HostResponse
from tests._helpers import (
    DETECTOR_REGRESSION_TIMEOUT_SECONDS,
    _init_empty_git_repo,
    _init_git_repo,
    decode_text,
    execute_source,
    invoke_cli,
    json_stdout,
    process_transcript,
    register_distribution,
    run_cli,
    run_setup,
    shell_path,
)

pytestmark = pytest.mark.usefixtures('registered_hosts')

_EAGER_AND_FORWARD_CASES = (
    (
        (
            'def zeta_default():\n'
            '    return 1\n'
            '\n\n'
            'def alpha(value=zeta_default()):\n'
            '    return value\n'
        ),
        'alphabetical',
        'none',
        'eager_order',
    ),
    (
        (
            'class Example:\n'
            '    def zeta_default():\n'
            '        return 1\n'
            '\n'
            '    def alpha(self, value=zeta_default()):\n'
            '        return value\n'
        ),
        'none',
        'alphabetical',
        'eager_order',
    ),
    (
        'class ZetaType:\n    pass\n\n\nclass Alpha:\n    value: ZetaType\n',
        'alphabetical',
        'none',
        'forward_refs',
    ),
    (
        (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n'
            '\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        ),
        'none',
        'alphabetical',
        'forward_refs',
    ),
)
_GUARD_BASELINE_SOURCE = (
    'def delta():\n    pass\n\n\ndef charlie():\n    pass\n\n\n'
    'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
)
_GUARD_REORDERED_SOURCE = _GUARD_BASELINE_SOURCE + '\n\ndef echo():\n    pass\n'
_INCREMENTAL_PRECEDENCE_CURRENT = (
    'def bravo():\n'
    '    return alpha()\n'
    '\n\n'
    'def alpha():\n'
    '    return 1\n'
    '\n\n'
    'def charlie():\n'
    '    pass\n'
)
_INCREMENTAL_PRECEDENCE_FULL_SORTED = (
    'def alpha():\n'
    '    return 1\n'
    '\n\n'
    'def bravo():\n'
    '    return alpha()\n'
    '\n\n'
    'def charlie():\n'
    '    pass\n'
)
_RECEIPT_ONE = {'systemMessage': 'defsort: sorted 1 staged Python file for this commit'}
_SRC_ALREADY_ORDERED = 'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n'
_SRC_DUPLICATES = (
    'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
)
_SRC_FORWARD_REF = (
    'class Bravo:\n    def method(self, x: Alpha) -> None:\n        pass\n\n\n'
    'class Alpha:\n    def method(self, x: Bravo) -> None:\n        pass\n'
)
_SRC_INIT_BODY_CLI = """class C:
    def __init__(self):
        self.load()

    def load(self):
        return 1
"""
_SRC_INTERSTITIAL = 'def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n'
_SRC_NEEDS_SORT = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'
_SRC_SYNTAX_ERROR = 'def foo(\n'
_UNKNOWN_CWD_REASON = f'{_REASON_PREFIX}: {_ELEMENT_UNKNOWN_CWD}; {_HINT_ROUTE}'


class TestAlternateIndexPassthrough:
    def test_alternate_index_commit_denies_and_leaves_default_index(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        before = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'GIT_INDEX_FILE=alt.index git commit -m x'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'], stdin=payload
        )
        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert "assignment 'GIT_INDEX_FILE=alt.index'" in reason
        after = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert after == before


class TestAntigravityPostToolUse:
    def test_antigravity_post_tool_use_ignores_non_python_and_unsupported(
        self, tmp_path: Path
    ) -> None:
        text_file = tmp_path / 'notes.txt'
        text_file.write_text('unchanged', encoding='utf-8')

        non_python_status, non_python_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload('write_to_file', {'TargetFile': str(text_file)}, tmp_path),
        )
        unsupported_status, unsupported_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload('read_file', {'TargetFile': str(text_file)}, tmp_path),
        )

        assert non_python_status == 0
        assert non_python_stdout == ''
        assert unsupported_status == 0
        assert unsupported_stdout == ''
        assert text_file.read_text(encoding='utf-8') == 'unchanged'

    def test_antigravity_replace_file_content_resolves_relative_target(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                'replace_file_content',
                {'TargetFile': 'test.py', 'ReplacementContent': f.read_text(encoding='utf-8')},
                tmp_path,
            ),
            cwd=Path('/'),
        )

        assert result_status == 0
        assert result_stdout == ''
        assert f.read_text(encoding='utf-8').index('alpha') < f.read_text(encoding='utf-8').index(
            'bravo'
        )

    def test_antigravity_write_to_file_sorts_python_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                'write_to_file',
                {'TargetFile': str(f), 'CodeContent': f.read_text(encoding='utf-8')},
                tmp_path,
            ),
            expect=0,
        )

        assert result.returncode == 0
        assert result.stdout == ''
        assert f.read_text(encoding='utf-8').index('alpha') < f.read_text(encoding='utf-8').index(
            'bravo'
        )

    def test_target_repo_gate_sorts_when_workspace_disabled(self, tmp_path: Path) -> None:
        workspace_repo = tmp_path / 'workspace'
        target_repo = tmp_path / 'target'
        _init_git_repo(workspace_repo, '# baseline\n')
        _init_git_repo(target_repo, _SRC_NEEDS_SORT)
        _git_config(workspace_repo, 'defsort.hook', 'false')
        _git_config(target_repo, 'defsort.hook', 'true')
        f = target_repo / 'test.py'

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                'write_to_file',
                {'TargetFile': str(f), 'CodeContent': f.read_text(encoding='utf-8')},
                workspace_repo,
            ),
            cwd=workspace_repo,
        )

        assert result_status == 0
        assert result_stdout == ''
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_target_repo_gate_workspace_enabled_skips_disabled_target(
        self, tmp_path: Path
    ) -> None:
        workspace_repo = tmp_path / 'workspace'
        target_repo = tmp_path / 'target'
        _init_git_repo(workspace_repo, '# baseline\n')
        _init_git_repo(target_repo, _SRC_NEEDS_SORT)
        _git_config(workspace_repo, 'defsort.hook', 'true')
        _git_config(target_repo, 'defsort.hook', 'false')
        f = target_repo / 'test.py'

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                'write_to_file',
                {'TargetFile': str(f), 'CodeContent': f.read_text(encoding='utf-8')},
                workspace_repo,
            ),
            cwd=workspace_repo,
        )

        assert result_status == 0
        assert result_stdout == ''
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestAntigravityPreToolUse:
    def test_antigravity_commit_does_not_trust_workspace_fallback(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# baseline\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = _antigravity_payload(
            'run_command', {'CommandLine': 'git commit -m "test"'}, tmp_path
        )

        before = _repo_state(tmp_path)
        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin=payload, expect=0
        )

        assert result.returncode == 0
        assert json_stdout(result) == {'decision': 'deny', 'reason': _UNKNOWN_CWD_REASON}
        assert result.stderr == f'{_UNKNOWN_CWD_REASON}\n'
        assert _repo_state(tmp_path) == before

    def test_antigravity_commit_sorts_staged_file_with_a_receipt(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# baseline\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'],
            stdin=_antigravity_pretooluse_payload('git commit -m "test"', tmp_path),
            expect=0,
        )

        assert result.returncode == 0
        assert json_stdout(result) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_antigravity_commit_uses_target_cwd_not_process_cwd(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        target_repo = tmp_path / 'target'
        process_repo.mkdir()
        target_repo.mkdir()
        _init_git_repo(process_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        run_setup(['git', '-C', str(process_repo), 'config', 'defsort.hook', 'false'])
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'],
            stdin=_antigravity_pretooluse_payload('git commit -m "test"', target_repo),
            cwd=process_repo,
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_antigravity_infrastructure_denial_uses_top_level_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, '# baseline\n')

        def no_staged_py_info(_cwd: str) -> list[_StagedFileInfo] | None:
            return None

        monkeypatch.setattr('defsort._hook.get_staged_py_info', no_staged_py_info)

        result = _run_in_process(
            provider='antigravity',
            stdin_data=_antigravity_pretooluse_payload('git commit -m "test"', tmp_path),
        )

        assert result == 0
        output = json.loads(capsys.readouterr().out)
        assert output['decision'] == 'deny'
        assert output['reason'] == 'defsort: failed to list staged files'

    def test_antigravity_non_commit_run_command_is_silent(self, tmp_path: Path) -> None:
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'],
            stdin=_antigravity_pretooluse_payload('pytest -q', tmp_path),
        )
        assert result_status == 0
        assert result_stdout == ''

    def test_antigravity_safety_denial_uses_top_level_json(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# baseline\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'],
            stdin=_antigravity_pretooluse_payload('git commit -m "test"', tmp_path),
        )

        assert result_status == 0
        output = json.loads(result_stdout)
        assert output['decision'] == 'deny'
        assert 'code between definitions' in output['reason']

    def test_target_repo_gate_sorts_when_workspace_disabled(self, tmp_path: Path) -> None:
        workspace_repo = tmp_path / 'workspace'
        target_repo = tmp_path / 'target'
        _init_git_repo(workspace_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        _git_config(workspace_repo, 'defsort.hook', 'false')
        _git_config(target_repo, 'defsort.hook', 'true')
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])
        payload = _antigravity_payload(
            'run_command',
            {
                'CommandLine': f'git -C {shell_path(target_repo)} commit -m "test"',
                'Cwd': str(workspace_repo),
            },
            workspace_repo,
        )

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin=payload, cwd=workspace_repo
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_target_repo_gate_workspace_enabled_skips_disabled_target(
        self, tmp_path: Path
    ) -> None:
        workspace_repo = tmp_path / 'workspace'
        target_repo = tmp_path / 'target'
        _init_git_repo(workspace_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        _git_config(workspace_repo, 'defsort.hook', 'true')
        _git_config(target_repo, 'defsort.hook', 'false')
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])
        payload = _antigravity_payload(
            'run_command',
            {
                'CommandLine': f'git -C {shell_path(target_repo)} commit -m "test"',
                'Cwd': str(workspace_repo),
            },
            workspace_repo,
        )

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin=payload, cwd=workspace_repo
        )

        assert result_status == 0
        assert result_stdout == ''
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged == _SRC_NEEDS_SORT


class TestBlockerClassification:
    def test_blocker_dispositions_partition_every_blocker(self) -> None:
        fallback = {b for b in get_args(Blocker) if _is_fallback_blocker(b)}
        soft = {b for b in get_args(Blocker) if is_soft_blocker(b)}
        assert fallback == {'non_conforming', 'target_incompatible'}
        assert fallback < soft
        assert soft.isdisjoint(_BLOCKER_MSGS)
        assert soft | set(_BLOCKER_MSGS) == set(get_args(Blocker))


class TestCodexPostToolUse:
    def test_codex_apply_patch_sorts_python_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'apply_patch',
                'tool_input': {
                    'command': (
                        '*** Begin Patch\n'
                        '*** Update File: test.py\n'
                        '@@\n'
                        '-def bravo():\n'
                        '+def bravo():\n'
                        '*** End Patch\n'
                    )
                },
                'cwd': str(tmp_path),
                'model': 'gpt-5.5',
                'turn_id': 'turn_test',
            }
        )
        result = run_cli(['hook', '--agent', 'codex'], stdin=payload, expect=0)
        assert result.returncode == 0
        assert result.stdout == ''
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_mixed_repo_post_tool_use_gates_each_target_repo(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        enabled_repo = tmp_path / 'enabled'
        disabled_repo = tmp_path / 'disabled'
        _init_git_repo(process_repo, '# baseline\n')
        _init_git_repo(enabled_repo, _SRC_NEEDS_SORT)
        _init_git_repo(disabled_repo, _SRC_NEEDS_SORT)
        _git_config(process_repo, 'defsort.hook', 'true')
        _git_config(enabled_repo, 'defsort.hook', 'true')
        _git_config(disabled_repo, 'defsort.hook', 'false')
        enabled_file = enabled_repo / 'test.py'
        disabled_file = disabled_repo / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'apply_patch',
                'tool_input': {
                    'command': (
                        '*** Begin Patch\n'
                        f'*** Update File: {enabled_file}\n'
                        f'*** Update File: {disabled_file}\n'
                        '*** End Patch\n'
                    )
                },
                'cwd': str(process_repo),
                'model': 'gpt-5.5',
                'turn_id': 'turn_test',
            }
        )

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'], stdin=payload, cwd=process_repo
        )

        assert result_status == 0
        assert result_stdout == ''
        enabled_content = enabled_file.read_text(encoding='utf-8')
        assert enabled_content.index('alpha') < enabled_content.index('bravo')
        assert disabled_file.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestCodexPreToolUse:
    def test_codex_commit_checks_target_repo_enabled_for_dash_c(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        target_repo = tmp_path / 'target'
        _init_git_repo(process_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        _git_config(process_repo, 'defsort.hook', 'false')
        _git_config(target_repo, 'defsort.hook', 'true')
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(target_repo)} commit -m "test"', process_repo
            ),
            cwd=process_repo,
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_codex_commit_checks_target_repo_enabled_for_shell_cd(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        target_repo = tmp_path / 'target'
        _init_git_repo(process_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        _git_config(process_repo, 'defsort.hook', 'false')
        _git_config(target_repo, 'defsort.hook', 'true')
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'cd {shell_path(target_repo)} && git commit -m "test"', process_repo
            ),
            cwd=process_repo,
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_codex_commit_leaves_target_repo_disabled(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        target_repo = tmp_path / 'target'
        _init_git_repo(process_repo, '# baseline\n')
        _init_git_repo(target_repo, '# baseline\n')
        _git_config(process_repo, 'defsort.hook', 'true')
        _git_config(target_repo, 'defsort.hook', 'false')
        f = target_repo / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(target_repo), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(target_repo)} commit -m "test"', process_repo
            ),
            cwd=process_repo,
        )

        assert result_status == 0
        assert result_stdout == ''
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged == _SRC_NEEDS_SORT

    def test_codex_commit_receipt_counts_rewritten_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        for name in ('one.py', 'two.py'):
            (tmp_path / name).write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        (tmp_path / 'clean.py').write_text('def alpha():\n    pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'one.py', 'two.py', 'clean.py'])
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(tmp_path)} commit -m "test"', tmp_path
            ),
        )
        assert result_status == 0
        assert json.loads(result_stdout) == {
            'systemMessage': 'defsort: sorted 2 staged Python files for this commit'
        }

    def test_codex_commit_sorts_staged_file_with_a_receipt(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        result = run_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(tmp_path)} commit -m "test"', tmp_path
            ),
            expect=0,
        )
        assert result.returncode == 0
        assert json_stdout(result) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_codex_commit_with_nothing_to_sort_stays_silent(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'clean.py').write_text('def alpha():\n    pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'clean.py'])
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(tmp_path)} commit -m "test"', tmp_path
            ),
        )
        assert (result_status, result_stdout) == (0, '')


class TestEnableDisableConfig:
    def test_disabled_hook_is_silent(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        _git_config(tmp_path, 'defsort.hook', 'false')
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        assert result_status == 0
        assert result_stdout == ''


class TestEngineImportFailure:
    def test_commit_denies_when_engine_import_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, 'defsort._hook', None)
        outcome = front_mod._run_pre_tool_use(
            'git commit -m message', str(tmp_path), overrides=None, allow_on_failure=False
        )
        assert (outcome.decision, outcome.reason) == (
            'deny',
            'defsort: unexpected error during sort',
        )

    def test_commit_engine_call_import_error_escapes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fail_after_import(*_args: object, **_kwargs: object) -> None:
            raise ImportError('engine call failed')

        monkeypatch.setattr(hook_mod, 'run_commit', fail_after_import)
        with pytest.raises(ImportError, match='engine call failed'):
            front_mod._run_pre_tool_use('git commit -m message', str(tmp_path))

    def test_commit_warns_and_allows_with_allow_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setitem(sys.modules, 'defsort._hook', None)
        outcome = front_mod._run_pre_tool_use(
            'git commit -m message', str(tmp_path), overrides=None, allow_on_failure=True
        )
        assert outcome.decision == 'allow'
        assert 'WARNING: defsort: unexpected error during sort' in capsys.readouterr().err

    def test_post_tool_allows_with_diagnostic_when_engine_import_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setitem(sys.modules, 'defsort._hook', None)
        outcome = front_mod._run_post_tool((), str(tmp_path), overrides=None)
        assert outcome.decision == 'allow'
        assert 'defsort: unexpected error during PostToolUse' in capsys.readouterr().err

    def test_post_tool_engine_call_import_error_escapes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fail_after_import(*_args: object, **_kwargs: object) -> None:
            raise ImportError('engine call failed')

        monkeypatch.setattr(hook_mod, 'run_post_tool', fail_after_import)
        with pytest.raises(ImportError, match='engine call failed'):
            front_mod._run_post_tool((), str(tmp_path))


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_shift_heredoc_hook_preserves_classification(
    tmp_path: Path, *, enabled: bool, allow_on_failure: bool
) -> None:
    _init_empty_git_repo(tmp_path)
    _git_config(tmp_path, 'defsort.hook', str(enabled).lower())
    for has_commit in (False, True):
        body = 'value = 1 << 2\n' * 1000
        if has_commit:
            body += 'g""it com""mit -m x\n'
        payload = _pretooluse_payload("python - <<'PY'\n" + body + 'PY\n', tmp_path)
        result = run_cli(
            ['hook', '--agent', 'claude', *(['--allow-on-failure'] if allow_on_failure else [])],
            stdin=payload,
            cwd=tmp_path,
            timeout=DETECTOR_REGRESSION_TIMEOUT_SECONDS,
            expect=0,
        )
        if has_commit:
            _assert_denied((result.returncode, result.stdout, result.stderr))
        else:
            assert result.returncode == 0, result.stderr
            assert result.stdout == ''
            assert result.stderr == ''


class TestEvaluationOrderConfig:
    @pytest.mark.parametrize('incremental', [False, True])
    @pytest.mark.parametrize('allow_reordering', [False, True])
    def test_post_tool_uses_file_evaluation_policy(
        self, tmp_path: Path, *, incremental: bool, allow_reordering: bool
    ) -> None:
        prefix = 'import attrs\n\n\n'
        zeta = '@attrs.define(frozen=True)\nclass Zeta:\n    value: int\n'
        alpha = '@attrs.define(frozen=True)\nclass Alpha:\n    value: int\n'
        _init_git_repo(tmp_path, prefix + zeta)
        evaluation_order = 'allow-reordering' if allow_reordering else 'preserve'
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\n'
            f'evaluation-order = "{evaluation_order}"\n'
            f'incremental = {str(incremental).lower()}\n',
            encoding='utf-8',
        )
        target = tmp_path / 'test.py'
        source = prefix + zeta + '\n\n' + alpha
        target.write_text(source, encoding='utf-8')

        rc = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        assert rc == 0
        expected = prefix + alpha + '\n\n' + zeta if allow_reordering else source
        assert target.read_text(encoding='utf-8') == expected


class TestIgnoredEvents:
    def test_missing_event_name_no_output(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert result_stdout == ''
        # File NOT modified (no event name, not routed to PostToolUse)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_session_start_no_output(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'SessionStart',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert result_stdout == ''
        # File NOT modified (event ignored, not routed to PostToolUse)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_stop_event_no_output(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'Stop',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert result_stdout == ''
        # File NOT modified (event ignored, not routed to PostToolUse)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestIncrementalMode:
    def test_incremental_non_conforming_skips_silently(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Commit a file that is NOT in target order
        bad_order = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        (tmp_path / 'mod.py').write_text(bad_order)
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Now add a new def and stage it
        new_content = bad_order + '\n\ndef charlie():\n    pass\n'
        (tmp_path / 'mod.py').write_text(new_content)
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--incremental'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Non-conforming is a skip condition, NOT a denial.
        # Hook silently skips (no-op), so it emits no JSON output.
        assert result_status == 0
        assert result_stdout == ''
        assert _staged_text(tmp_path) == new_content

    def test_incremental_renamed_file_skipped(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Commit a file that is NOT in target order
        bad_order = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        (tmp_path / 'old.py').write_text(bad_order)
        run_setup(['git', 'add', 'old.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Rename the file (git mv) -- new path has no HEAD entry
        run_setup(['git', 'mv', 'old.py', 'new.py'], cwd=tmp_path)
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--incremental'],
            stdin=_pretooluse_payload('git commit -m rename', tmp_path),
            cwd=tmp_path,
        )
        # Renamed file should be skipped (no-op), NOT full-sorted.
        # Hook silently skips, so it emits no JSON output.
        assert result_status == 0
        assert result_stdout == ''
        # Verify the file was NOT reordered (bravo still before alpha)
        content = (tmp_path / 'new.py').read_text()
        assert content.index('bravo') < content.index('alpha')

    def test_incremental_sorts_new_defs_only(self, tmp_path: Path) -> None:
        # Setup: commit baseline, stage file with new def
        _setup_incremental_repo(tmp_path)
        _, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--incremental'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Verify: existing defs stay in place, new def sorted
        assert json.loads(result_stdout) == _RECEIPT_ONE
        assert _staged_text(tmp_path) == (
            'def aardvark():\n'
            '    pass\n'
            '\n\n'
            'def alpha():\n'
            '    return 1\n'
            '\n\n'
            'def bravo():\n'
            '    return alpha()\n'
        )

    def test_incremental_syntax_error_skips_silently(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Commit a valid baseline
        (tmp_path / 'mod.py').write_text('def alpha():\n    return 1\n')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Stage a broken version
        (tmp_path / 'mod.py').write_text('def alpha(\n')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--incremental'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # SyntaxError must not crash the hook.
        assert result_status == 0
        assert result_stdout == ''

    def test_incremental_unborn_repo_full_sort(self, tmp_path: Path) -> None:
        run_setup(['git', 'init'], cwd=tmp_path)
        (tmp_path / 'mod.py').write_text(
            'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        _, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--incremental'],
            stdin=_pretooluse_payload('git commit -m initial', tmp_path),
            cwd=tmp_path,
        )
        assert json.loads(result_stdout) == _RECEIPT_ONE
        # Verify full sort applied: alpha before bravo in staged content.
        staged = run_setup(['git', 'show', ':mod.py'], cwd=tmp_path)
        assert decode_text(staged.stdout).index('alpha') < decode_text(staged.stdout).index(
            'bravo'
        )


class TestIncrementalPrecedence:
    def test_cli_no_incremental_disables(self, tmp_path: Path) -> None:
        _setup_incremental_precedence_repo(tmp_path)
        # Write config enabling incremental
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nincremental = true\n', encoding='utf-8')
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--no-incremental'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Full sort applied in non-incremental mode.
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_FULL_SORTED

    def test_config_false_disables_when_no_flag(self, tmp_path: Path) -> None:
        _setup_incremental_precedence_repo(tmp_path)
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nincremental = false\n', encoding='utf-8')
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Full sort applied (non-incremental mode from config)
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_FULL_SORTED

    def test_config_true_enables_when_no_flag(self, tmp_path: Path) -> None:
        _setup_incremental_precedence_repo(tmp_path)
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nincremental = true\n', encoding='utf-8')
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Incremental sort applied.
        assert result_status == 0
        assert result_stdout == ''
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_CURRENT

    def test_default_is_full_sort(self, tmp_path: Path) -> None:
        _setup_incremental_precedence_repo(tmp_path)
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        # Full sort reorders existing defs: alpha before bravo
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_FULL_SORTED

    def test_i_flag_enables(self, tmp_path: Path) -> None:
        _setup_incremental_precedence_repo(tmp_path)
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', '-i'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        assert result_status == 0
        assert result_stdout == ''
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_CURRENT


class TestInvalidUtf8TomlHook:
    def test_hook_survives_invalid_utf8_pyproject(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'pyproject.toml').write_bytes(b'[tool.defsort]\nba\xff\xfe = 1\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m x'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, result_stderr = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert 'Traceback' not in result_stderr


class TestPathspecPreflight:
    def test_disabled_pathspec_does_not_open_resource(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', 'config', 'defsort.hook', 'false'], cwd=tmp_path)

        def unexpected_read(*_args: object) -> None:
            pytest.fail('disabled hook opened pathspec resource')

        monkeypatch.setattr(hook_mod, 'read_pathspec_file', unexpected_read)
        index = (tmp_path / '.git' / 'index').read_bytes()
        result = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add test.py && git commit --pathspec-from-file=. -m x'
                },
                'cwd': str(tmp_path),
            },
        )
        assert result == Outcome('allow')
        assert (tmp_path / '.git' / 'index').read_bytes() == index

    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_nonregular_pathspec_denied_before_replay(
        self, tmp_path: Path, *, allow_on_failure: bool
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        index = (tmp_path / '.git' / 'index').read_bytes()
        source = (tmp_path / 'test.py').read_bytes()
        result = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add test.py && git commit --pathspec-from-file=. -m x'
                },
                'cwd': str(tmp_path),
            },
            allow_on_failure=allow_on_failure,
        )
        assert result.decision == 'deny'
        assert 'regular file' in (result.reason or '')
        assert (tmp_path / '.git' / 'index').read_bytes() == index
        assert (tmp_path / 'test.py').read_bytes() == source

    @pytest.mark.parametrize('include', [False, True])
    def test_pathspec_content_read_once_before_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, include: bool
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        path = tmp_path / 'paths'
        path.write_bytes(b'test.py\n')
        read = hook_mod.read_pathspec_file
        calls: list[str] = []

        def read_once(cwd: str, pathspec: str) -> bytes | None:
            calls.append(pathspec)
            content = read(cwd, pathspec)
            path.unlink()
            return content

        monkeypatch.setattr(hook_mod, 'read_pathspec_file', read_once)
        result = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add test.py && git commit '
                    + ('--include ' if include else '')
                    + '--pathspec-from-file=paths -m x'
                },
                'cwd': str(tmp_path),
            },
        )
        assert result.decision == 'allow'
        assert calls == ['paths']
        assert (tmp_path / 'test.py').read_text() == _SRC_ALREADY_ORDERED

    @pytest.mark.parametrize('allow_on_failure', [False, True])
    @pytest.mark.parametrize('failure', ['missing', 'inaccessible'])
    def test_pathspec_io_failure_policy_before_replay(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failure: str,
        *,
        allow_on_failure: bool,
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)

        def inaccessible(*_args: object) -> None:
            return None

        if failure == 'inaccessible':
            monkeypatch.setattr(hook_mod, 'read_pathspec_file', inaccessible)
        index = (tmp_path / '.git' / 'index').read_bytes()
        source = (tmp_path / 'test.py').read_bytes()
        result = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add test.py && git commit --pathspec-from-file=paths -m x'
                },
                'cwd': str(tmp_path),
            },
            allow_on_failure=allow_on_failure,
        )
        assert result.decision == ('allow' if allow_on_failure else 'deny')
        captured = capsys.readouterr()
        if allow_on_failure:
            assert 'WARNING: defsort: failed to read pathspec file' in captured.err
        else:
            assert 'failed to read pathspec file' in (result.reason or '')
        assert (tmp_path / '.git' / 'index').read_bytes() == index
        assert (tmp_path / 'test.py').read_bytes() == source


class TestPerFileHookConfig:
    def test_banner_override_uses_nested_pattern(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text('[project]\nname = "root"\n', encoding='utf-8')
        nested = repo / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\n'
            'banners = "barriers"\n'
            'banner-pattern = "^# CUSTOM$"\n'
            'definition-order = "none"\n'
            'method-order = "none"\n',
            encoding='utf-8',
        )
        target = nested / 'mod.py'
        target.write_text(
            'def alpha():\n    pass\n\n\n# CUSTOM\n\n\ndef bravo():\n    pass\n',
            encoding='utf-8',
        )

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--banners', 'strip'],
            stdin=_posttooluse_payload('claude', target, repo),
            cwd=repo,
        )

        assert result_status == 0
        text = target.read_text(encoding='utf-8')
        assert '# CUSTOM' not in text
        assert text.count('def ') == 2
        assert text.index('def alpha') < text.index('def bravo')

    def test_nested_guard_blocks_in_single_file_route(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text('[project]\nname = "root"\n', encoding='utf-8')
        nested = repo / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 1\n', encoding='utf-8'
        )
        target = nested / 'mod.py'
        target.write_text(_GUARD_BASELINE_SOURCE, encoding='utf-8')
        run_setup(['git', 'add', '.'], cwd=repo)
        run_setup(['git', 'commit', '-m', 'initial'], cwd=repo)
        target.write_text(_GUARD_REORDERED_SOURCE, encoding='utf-8')

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_posttooluse_payload('claude', target, repo),
            cwd=repo,
        )

        assert result_status == 0
        text = target.read_text(encoding='utf-8')
        assert text.index('def delta') < text.index('def alpha')

    def test_nested_guard_blocks_in_worktree_route(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text('[project]\nname = "root"\n', encoding='utf-8')
        nested = repo / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 1\n', encoding='utf-8'
        )
        target = nested / 'mod.py'
        target.write_text(_GUARD_BASELINE_SOURCE, encoding='utf-8')
        run_setup(['git', 'add', '.'], cwd=repo)
        run_setup(['git', 'commit', '-m', 'initial'], cwd=repo)
        target.write_text(_GUARD_REORDERED_SOURCE, encoding='utf-8')

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -a -m x', repo),
            cwd=repo,
        )

        assert result_status == 0
        text = target.read_text(encoding='utf-8')
        assert text.index('def delta') < text.index('def alpha')

    def test_nested_guard_still_blocks(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text('[project]\nname = "root"\n', encoding='utf-8')
        nested = repo / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 1\n', encoding='utf-8'
        )
        target = nested / 'mod.py'
        target.write_text(_GUARD_BASELINE_SOURCE, encoding='utf-8')
        run_setup(['git', 'add', '.'], cwd=repo)
        run_setup(['git', 'commit', '-m', 'initial'], cwd=repo)
        target.write_text(_GUARD_REORDERED_SOURCE, encoding='utf-8')
        run_setup(['git', 'add', '.'], cwd=repo)

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m x', repo),
            cwd=repo,
        )

        assert result_status == 0
        staged = decode_text(run_setup(['git', 'show', ':pkg/mod.py'], cwd=repo).stdout)
        assert staged.index('def delta') < staged.index('def alpha')

    def test_nested_project_config_applies(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text('[project]\nname = "root"\n', encoding='utf-8')
        nested = repo / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\ndefinition-order = "none"\n', encoding='utf-8'
        )
        target = nested / 'mod.py'
        target.write_text(
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            encoding='utf-8',
        )
        run_setup(['git', 'add', '.'], cwd=repo)

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m x', repo),
            cwd=repo,
        )

        assert result_status == 0
        staged = decode_text(run_setup(['git', 'show', ':pkg/mod.py'], cwd=repo).stdout)
        assert staged.index('def bravo') < staged.index('def alpha')

    def test_unsafe_banner_pattern_warns_and_uses_builtin_fallback(self, tmp_path: Path) -> None:
        repo = tmp_path
        _init_empty_git_repo(repo)
        (repo / 'pyproject.toml').write_text(
            '[tool.defsort]\n'
            'banners = "strip"\n'
            'banner-pattern = "^# (a+)+$"\n'
            'definition-order = "none"\n'
            'method-order = "none"\n',
            encoding='utf-8',
        )
        target = repo / 'mod.py'
        source = 'def alpha():\n    pass\n\n\n# aaaa\n\n\ndef bravo():\n    pass\n'
        target.write_text(source, encoding='utf-8')

        result_status, _, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_posttooluse_payload('claude', target, repo),
            cwd=repo,
        )

        assert result_status == 0
        assert 'Invalid [tool.defsort] banner-pattern' in result_stderr
        assert target.read_text(encoding='utf-8') == source


class TestPlanFileSort:
    def test_banner_constraints_report_changed_file(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = (
            'def zulu():\n    pass\n\n\ndef yankee():\n    pass\n\n\n'
            '# --------\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        )

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )

        assert result.new_source is not None
        assert result.new_source != source
        assert result.banner_constraints
        assert 'sort constrained by 1 banner' in capsys.readouterr().err

    def test_banner_constraints_report_constraint_only_noop(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )

        assert result.new_source is None
        assert len(result.banner_constraints) == 1
        assert 'defsort: test.py: sort constrained by 1 banner' in capsys.readouterr().err

    def test_banner_constraints_survive_blocker(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = (
            'def zulu():\n    pass\n\nregistry = {}\n\n'
            'def yankee():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        )

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )

        assert result.blocker == 'interstitial'
        assert result.banner_constraints
        assert 'sort constrained by 1 banner' in capsys.readouterr().err

    def test_banner_constraints_survive_guarded_attempt(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = (
            'def zulu():\n    pass\n\n\ndef yankee():\n    pass\n\n\n'
            '# --------\n\n\ndef alpha():\n    pass\n'
        )
        baseline_defs = parse_definitions(
            source,
            banners='barriers',
        ).definitions

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=ReorderGuardParams(
                baseline_defs=tuple(baseline_defs),
                max_reorder_count=0,
            ),
        )

        assert result.new_source is None
        assert result.banner_constraints
        assert 'sort constrained by 1 banner' in capsys.readouterr().err

    def test_banner_constraints_survive_incremental_fallback_skip(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        baseline = (
            'def zulu():\n    pass\n\n\ndef yankee():\n    pass\n\n\n'
            '# --------\n\n\ndef alpha():\n    pass\n'
        )
        source = baseline + '\n\ndef beta():\n    pass\n'

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
        )

        assert result.blocker is None
        assert result.new_source is None
        assert result.banner_constraints
        assert 'sort constrained by 1 banner' in capsys.readouterr().err

    def test_fallback_full_sort(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        source = baseline + '\ndef c():\n    return 1\n'
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
            incremental_fallback='full-sort',
        )
        assert result.new_source is not None
        assert 'falling back' in capsys.readouterr().err

    def test_fallback_full_sort_retains_layout_blocker(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = (
            'class C:\n    def alpha(self):\n        pass\n'
            '    # --------\n    def zulu(self):\n        pass\n'
            '    # for bravo\n    def bravo(self):\n        pass\n'
        )
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=True,
            baseline='def broken(',
            display_path='test.py',
            incremental_fallback='full-sort',
        )

        assert result.blocker == 'layout'
        assert result.new_source is None
        assert not result.sorted_defs
        assert 'falling back' in capsys.readouterr().err

    def test_full_sort_no_baseline(self) -> None:
        source = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )
        assert result.new_source is not None

    def test_guard_accepts_small_reorder(self) -> None:
        baseline = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = parse_definitions(baseline).definitions
        guard = ReorderGuardParams(
            baseline_defs=tuple(bl_defs),
            max_reorder_count=10,
        )
        result = _plan_file_sort(
            baseline,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=guard,
        )
        assert result.new_source is not None

    def test_guard_disabled_when_no_thresholds(self) -> None:
        source = 'def c():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = parse_definitions(source).definitions
        guard = ReorderGuardParams(baseline_defs=tuple(bl_defs))
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=guard,
        )
        assert result.new_source is not None

    def test_guard_on_fallback_full_sort(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        source = baseline + '\ndef c():\n    return 1\n'
        bl_defs = parse_definitions(baseline).definitions
        guard = ReorderGuardParams(
            baseline_defs=tuple(bl_defs),
            max_reorder_count=0,
        )
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
            incremental_fallback='full-sort',
            guard=guard,
        )
        assert result.new_source is None
        assert 'would move' in capsys.readouterr().err

    def test_guard_or_logic(self, capsys: pytest.CaptureFixture[str]) -> None:
        source = 'def c():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = parse_definitions(source).definitions
        guard = ReorderGuardParams(
            baseline_defs=tuple(bl_defs),
            max_reorder_count=100,
            max_reorder_percent=10,
        )
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=guard,
        )
        assert result.new_source is None
        assert 'would move' in capsys.readouterr().err

    def test_guard_percent_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        source = 'def c():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = parse_definitions(source).definitions
        guard = ReorderGuardParams(
            baseline_defs=tuple(bl_defs),
            max_reorder_percent=10,
        )
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=guard,
        )
        assert result.new_source is None
        assert 'would move' in capsys.readouterr().err

    def test_guard_skips_large_reorder(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def c():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = parse_definitions(baseline).definitions
        guard = ReorderGuardParams(
            baseline_defs=tuple(bl_defs),
            max_reorder_count=1,
        )
        result = _plan_file_sort(
            baseline,
            policy=SortPolicy(),
            incremental=False,
            baseline=None,
            display_path='test.py',
            guard=guard,
        )
        assert result.new_source is None
        assert 'would move' in capsys.readouterr().err

    def test_hard_blocker_passes_through(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        current = 'def a():\n    pass\n\nx = 1\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n'
        result = _plan_file_sort(
            current,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
        )
        assert result.blocker == 'interstitial'

    def test_incremental_skip_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        source = baseline + '\ndef c():\n    return 1\n'
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
        )
        assert result.blocker is None
        assert result.new_source is None
        assert 'skipped' in capsys.readouterr().err

    def test_strip_only_nonincremental_attempt_returns_source_changes(self) -> None:
        source = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n\n'
            '    # ========\n\n'
            '    def bravo(self):\n'
            '        pass\n\n\n'
            '# --------\n\n\n'
            'def tail():\n'
            '    pass\n'
        )

        result = _plan_file_sort(
            source,
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
            ),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )

        assert result.new_source == source.replace('    # ========\n', '').replace(
            '# --------\n', ''
        )
        assert result.banner_constraints == ()

    def test_unconstrained_changed_file_is_silent(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _plan_file_sort(
            _SRC_NEEDS_SORT,
            policy=SortPolicy(method_order='none'),
            incremental=False,
            baseline=None,
            display_path='test.py',
        )

        assert result.new_source is not None
        assert result.banner_constraints == ()
        assert 'sort constrained' not in capsys.readouterr().err

    def test_user_reordered_always_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        # Swap a and b (user reorder), add new def c
        source = 'def b():\n    pass\n\n\ndef a():\n    pass\n\n\ndef c():\n    pass\n'
        result = _plan_file_sort(
            source,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
            incremental_fallback='full-sort',
        )
        assert result.blocker is None
        assert result.new_source is None
        assert 'skipped' in capsys.readouterr().err


class TestPolicyFlags:
    def test_method_bodies_reaches_staged_engine(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_INIT_BODY_CLI, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'mod.py'])

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--method-bodies', 'across-categories'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert result_status == 0
        rewritten = target.read_text(encoding='utf-8')
        assert rewritten.index('def load') < rewritten.index('def __init__')

    def test_order_alphabetical_applies(self, tmp_path: Path) -> None:
        src = 'def alpha():\n    return bravo()\n\n\ndef bravo():\n    pass\n'
        f = tmp_path / 'test.py'
        f.write_text(src, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--order', 'alphabetical'], stdin=payload
        )
        assert result_status == 0
        assert f.read_text(encoding='utf-8') == src


class TestHookSurfaceContracts:
    @staticmethod
    def _hook_args(
        definition_order: SortScopeOrder,
        method_order: SortScopeOrder,
        *,
        incremental: bool,
    ) -> list[str]:
        return [
            '--definition-order',
            definition_order,
            '--method-order',
            method_order,
            '--incremental' if incremental else '--no-incremental',
        ]

    @staticmethod
    def _posttooluse_payload(provider: str, path: Path, cwd: Path) -> str:
        return _posttooluse_payload(provider, path, cwd)

    @staticmethod
    def _pretooluse_payload(provider: str, cwd: Path) -> str:
        return _provider_pretooluse_payload(
            provider,
            'git commit -m "test"',
            cwd,
            codex_command=f'git -C {shell_path(cwd)} commit -m "test"',
        )

    @staticmethod
    def _staged_source(cwd: Path, path: str = 'test.py') -> str:
        return decode_text(run_setup(['git', '-C', str(cwd), 'show', f':{path}']).stdout)

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    def test_future_live_binding_survives_staged_hook(
        self, tmp_path: Path, *, incremental: bool
    ) -> None:
        source = (
            'from __future__ import annotations\n\n'
            'def feature():\n    return annotations.compiler_flag\n'
        )
        _init_git_repo(tmp_path, source)
        original = execute_source(source)['feature']
        assert isinstance(original, FunctionType)
        assert original() == 16777216
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        result_status, result_stdout, _ = invoke_cli(
            [
                'hook',
                '--agent',
                'codex',
                *(self._hook_args('dependency', 'dependency', incremental=incremental)),
            ],
            stdin=self._pretooluse_payload('codex', tmp_path),
            cwd=tmp_path,
        )

        assert result_status == EXIT_OK
        assert result_stdout == ''
        staged = self._staged_source(tmp_path)
        transformed = execute_source(staged)['feature']
        assert isinstance(transformed, FunctionType)
        assert transformed() == 16777216
        assert staged == source
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == source

    # Adapter boundaries have per-host coverage; exercise shared engine policy once.
    @pytest.mark.parametrize('event', ['pretooluse', 'posttooluse'])
    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize('annotated', [False, True], ids=['future-free', 'future-annotated'])
    def test_future_import_mutation_contract(
        self,
        tmp_path: Path,
        event: str,
        *,
        incremental: bool,
        annotated: bool,
    ) -> None:
        body = (
            'class ZetaType:\n    pass\n\n\nclass Alpha:\n    value: ZetaType\n'
            if annotated
            else ('def zeta():\n    pass\n\n\ndef alpha():\n    pass\n')
        )
        source = f'from __future__ import annotations\n\n\n{body}'
        _init_git_repo(tmp_path, source)
        path = tmp_path / 'test.py'
        if event == 'pretooluse':
            run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
            payload = self._pretooluse_payload('claude', tmp_path)
        else:
            payload = self._posttooluse_payload('claude', path, tmp_path)
        args = self._hook_args(
            'alphabetical',
            'none',
            incremental=incremental,
        )

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude', *args],
            stdin=payload,
            cwd=tmp_path,
        )

        assert result_status == EXIT_OK
        if event == 'pretooluse':
            assert json.loads(result_stdout) == _RECEIPT_ONE
        else:
            assert result_stdout == ''
        transformed = (
            self._staged_source(tmp_path)
            if event == 'pretooluse'
            else path.read_text(encoding='utf-8')
        )
        if annotated:
            assert transformed.index('Alpha') < transformed.index('Zeta')
            assert transformed.count('from __future__ import annotations') == 1
        else:
            assert transformed.index('def alpha') < transformed.index('def zeta')
            assert 'from __future__ import annotations' not in transformed

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize(
        'case',
        _EAGER_AND_FORWARD_CASES,
        ids=['eager-top-level', 'eager-method', 'future-top-level', 'future-method'],
    )
    def test_posttooluse_eager_and_future_blockers_do_not_write(
        self,
        tmp_path: Path,
        case: tuple[str, SortScopeOrder, SortScopeOrder, Blocker],
        *,
        incremental: bool,
    ) -> None:
        source, definition_order, method_order, blocker = case
        _init_git_repo(tmp_path, source)
        path = tmp_path / 'test.py'
        args = self._hook_args(
            definition_order,
            method_order,
            incremental=incremental,
        )

        result_status, _, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', *args],
            stdin=self._posttooluse_payload('claude', path, tmp_path),
            cwd=tmp_path,
        )

        assert result_status == EXIT_OK
        assert path.read_text(encoding='utf-8') == source
        if blocker == 'eager_order':
            assert result_stderr == (
                f'ERROR: {path.as_posix()} would move a definition-time reference before the '
                'definition '
                'it uses. Restructure that reference or choose an order that keeps the referenced '
                'definition before the reader.\n'
            )
            assert all(name not in result_stderr for name in ['Claude', 'Codex', 'Antigravity'])
        else:
            assert 'needs sort with forward annotation references' in result_stderr

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize(
        'case',
        _EAGER_AND_FORWARD_CASES,
        ids=['eager-top-level', 'eager-method', 'future-top-level', 'future-method'],
    )
    def test_pretooluse_eager_and_future_blockers_are_atomic(
        self,
        tmp_path: Path,
        case: tuple[str, SortScopeOrder, SortScopeOrder, Blocker],
        *,
        incremental: bool,
    ) -> None:
        source, definition_order, method_order, blocker = case
        _init_git_repo(tmp_path, '# placeholder\n')
        safe_path = tmp_path / 'safe.py'
        safe_path.write_text('# placeholder\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'safe.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'baseline'])
        safe_source = (
            'from __future__ import annotations\n'
            '\n\n'
            'def zeta():\n'
            '    pass\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
        )
        blocker_path = tmp_path / 'test.py'
        safe_path.write_text(safe_source, encoding='utf-8')
        blocker_path.write_text(source, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'safe.py', 'test.py'])
        partial_source = f'{source}\n# unstaged\n'
        blocker_path.write_text(partial_source, encoding='utf-8')
        args = self._hook_args(
            definition_order,
            method_order,
            incremental=incremental,
        )

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', *args],
            stdin=self._pretooluse_payload('claude', tmp_path),
            cwd=tmp_path,
        )

        reason = _assert_denied(
            (result_status, result_stdout, result_stderr), 'claude', prefix='ERROR:'
        )
        assert self._staged_source(tmp_path, 'safe.py') == safe_source
        assert self._staged_source(tmp_path) == source
        assert safe_path.read_text(encoding='utf-8') == safe_source
        assert blocker_path.read_text(encoding='utf-8') == partial_source
        if blocker == 'eager_order':
            assert reason == (
                'ERROR: test.py would move a definition-time reference before the definition '
                'it uses. Restructure that reference or choose an order that keeps the referenced '
                'definition before the reader.'
            )
            assert all(name not in reason for name in ['Claude', 'Codex', 'Antigravity'])
        else:
            assert 'needs sort with forward annotation references' in reason


class TestPostToolUse:
    def test_blocked_interstitial(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        rc = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        assert rc == 0
        captured = capsys.readouterr()
        assert 'code between definitions' in captured.err

    def test_codex_batches_repositories_and_preserves_payload_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_repo = tmp_path / 'first-repo'
        second_repo = tmp_path / 'second-repo'
        first_repo.mkdir()
        second_repo.mkdir()
        _init_empty_git_repo(first_repo)
        _init_empty_git_repo(second_repo)
        for repo in (first_repo, second_repo):
            (repo / 'pyproject.toml').write_text(
                '[tool.defsort]\nincremental = true\n',
                encoding='utf-8',
            )
        nested = first_repo / 'nested'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\ndefinition-order = "none"\n',
            encoding='utf-8',
        )
        nested_file = nested / 'nested.py'
        second_first = second_repo / 'first.py'
        first_root = first_repo / 'root.py'
        second_last = second_repo / 'last.py'
        paths = (nested_file, second_first, first_root, second_last)
        for path in paths:
            path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        baseline_calls: list[tuple[str, tuple[str, ...]]] = []
        processed: list[Path] = []
        original_read_file = hook_mod.read_file_result

        def record_baselines(
            cwd: str,
            rel_paths: list[str],
        ) -> dict[str, BlobReadResult]:
            baseline_calls.append((cwd, tuple(rel_paths)))
            return {path: BlobReadResult(status=BlobReadStatus.MISSING) for path in rel_paths}

        def record_read(path: Path) -> object:
            processed.append(path)
            return original_read_file(path)

        monkeypatch.setattr(hook_mod, 'batch_fetch_worktree_baselines', record_baselines)
        monkeypatch.setattr(hook_mod, 'read_file_result', record_read)
        payload: dict[str, object] = {
            'tool_name': 'apply_patch',
            'tool_input': {
                'command': (
                    '*** Begin Patch\n'
                    f'*** Update File: {nested_file}\n'
                    f'*** Update File: {second_first}\n'
                    f'*** Update File: {first_root}\n'
                    f'*** Update File: {second_last}\n'
                    '*** End Patch\n'
                )
            },
            'cwd': str(tmp_path),
        }

        result = _post_tool(payload)

        assert result == EXIT_OK
        assert baseline_calls == [
            (first_repo.as_posix(), ('nested/nested.py', 'root.py')),
            (second_repo.as_posix(), ('first.py', 'last.py')),
        ]
        assert processed == list(paths)
        assert nested_file.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        for path in (second_first, first_root, second_last):
            content = path.read_text(encoding='utf-8')
            assert content.index('alpha') < content.index('bravo')

    def test_codex_batches_same_repository_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n',
            encoding='utf-8',
        )
        first = tmp_path / 'first.py'
        second = tmp_path / 'second.py'
        first.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        second.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        baseline_calls: list[tuple[str, tuple[str, ...]]] = []

        def record_baselines(
            cwd: str,
            rel_paths: list[str],
        ) -> dict[str, BlobReadResult]:
            baseline_calls.append((cwd, tuple(rel_paths)))
            return {path: BlobReadResult(status=BlobReadStatus.MISSING) for path in rel_paths}

        monkeypatch.setattr(hook_mod, 'batch_fetch_worktree_baselines', record_baselines)
        payload: dict[str, object] = {
            'tool_name': 'apply_patch',
            'tool_input': {
                'command': (
                    '*** Begin Patch\n'
                    '*** Update File: first.py\n'
                    '*** Update File: second.py\n'
                    '*** End Patch\n'
                )
            },
            'cwd': str(tmp_path),
        }

        result = _post_tool(payload)

        assert result == EXIT_OK
        assert baseline_calls == [(tmp_path.as_posix(), ('first.py', 'second.py'))]
        for path in (first, second):
            content = path.read_text(encoding='utf-8')
            assert content.index('alpha') < content.index('bravo')

    def test_crlf_preserved_on_edit(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n')
        payload: dict[str, object] = {
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(f)},
        }
        _post_tool(payload)
        raw = f.read_bytes()
        without_crlf = raw.replace(b'\r\n', b'<NL>')
        assert b'\n' not in without_crlf
        assert b'\r\n' in raw

    def test_diagnostic_preserves_original_absolute_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / 'pkg').mkdir()
        target = tmp_path / 'broken.py'
        target.write_text(_SRC_SYNTAX_ERROR, encoding='utf-8')
        payload_path = tmp_path / 'pkg' / '..' / 'broken.py'
        read_paths: list[Path] = []
        original_read_file = hook_mod.read_file_result

        def record_read(path: Path) -> object:
            read_paths.append(path)
            return original_read_file(path)

        monkeypatch.setattr(hook_mod, 'read_file_result', record_read)

        result = _post_tool({'tool_name': 'Write', 'tool_input': {'file_path': str(payload_path)}})

        assert result == EXIT_OK
        assert read_paths == [target.resolve()]
        assert json.loads(capsys.readouterr().out) == {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': (
                    f"Cannot sort {payload_path.as_posix()}: '(' was never closed (line 1)."
                ),
            }
        }

    def test_mixed_endings_preserved_exactly(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\n \t\rdef alpha():\n    pass\r\n')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '-o', 'alphabetical'], stdin=payload
        )

        assert result_status == 0
        assert f.read_bytes() == b'def alpha():\n    pass\n \t\rdef bravo():\r\n    pass\r\n'

    def test_no_changes_needed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''

    def test_ordered_interstitial_is_a_true_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = (
            'def alpha():\r\n    pass\r\n\r\nregistry = {}\r\n\r\ndef bravo():\r\n    pass\r\n'
        )
        path = tmp_path / 'test.py'
        path.write_bytes(source.encode())

        rc = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}})

        assert rc == 0
        assert capsys.readouterr() == ('', '')
        assert path.read_bytes() == source.encode()

    def test_skip_missing_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / 'missing.py'

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        assert result == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''
        assert not target.exists()

    def test_skip_non_edit_tool(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / 'test.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        original = target.read_bytes()

        result = _post_tool({'tool_name': 'Read', 'tool_input': {'file_path': str(target)}})

        assert result == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''
        assert target.read_bytes() == original

    def test_skip_non_py_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / 'readme.txt'
        target.write_text('hello', encoding='utf-8')
        original = target.read_bytes()

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        assert result == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''
        assert target.read_bytes() == original

    def test_successful_sort(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        captured = capsys.readouterr()
        assert captured.out == ''
        assert f.read_text(encoding='utf-8').index('alpha') < f.read_text(encoding='utf-8').index(
            'bravo'
        )

    def test_symlink_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / 'target.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        original = target.read_text(encoding='utf-8')
        link = tmp_path / 'link.py'
        link.symlink_to(target)
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(link)}})
        assert target.read_text(encoding='utf-8') == original

    def test_syntax_error_additional_context(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_SYNTAX_ERROR, encoding='utf-8')
        _post_tool({'tool_name': 'Write', 'tool_input': {'file_path': str(f)}})
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
        assert f.as_posix() in output['hookSpecificOutput']['additionalContext']


class TestPostToolUseHardening:
    def test_non_utf8_file_is_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'binary.py'
        source = b'\x80\x81\x82'
        f.write_bytes(source)
        payload: dict[str, object] = {
            'tool_name': 'Write',
            'tool_input': {'file_path': str(f)},
        }
        assert _post_tool(payload) == EXIT_OK
        assert f.read_bytes() == source
        assert capsys.readouterr() == ('', '')

    def test_undecodable_repo_root_skips_without_guessing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'mod.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        calls: list[list[str]] = []

        def fail_decode(command: list[str], *_args: object, **_kwargs: object) -> None:
            calls.append(command)
            assert command == ['git', 'rev-parse', '--show-toplevel']
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

        monkeypatch.setattr(subprocess, 'run', fail_decode)

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}})

        captured = capsys.readouterr()
        assert result == EXIT_OK
        assert captured.out == captured.err == ''
        assert calls == [['git', 'rev-parse', '--show-toplevel']]
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unexpected_exception_no_crash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        f = tmp_path / 'crash.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        def _raise_boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError('boom')

        monkeypatch.setattr('defsort._hook.sort_for_hook_detailed', _raise_boom)
        payload: dict[str, object] = {
            'tool_name': 'Write',
            'tool_input': {'file_path': str(f)},
        }
        # Should not raise
        _post_tool(payload)
        captured = capsys.readouterr()
        assert 'defsort: PostToolUse error' in captured.err


class TestPostToolCwdNormalization:
    @pytest.mark.parametrize(
        'payload',
        [
            {
                'tool_name': 'Edit',
                'tool_input': {'file_path': 'src/mod.py'},
                'cwd': '/c/Users/dev/project',
            },
            {
                'tool_name': 'apply_patch',
                'tool_input': {
                    'command': ('*** Begin Patch\n*** Update File: src/mod.py\n*** End Patch')
                },
                'cwd': '/c/Users/dev/project',
            },
        ],
        ids=['claude-edit', 'codex-apply-patch'],
    )
    def test_relative_path_uses_normalized_msys_cwd(
        self,
        payload: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr('defsort._vendor.hookshell._shell.sys.platform', 'win32')
        host = 'codex' if payload['tool_name'] == 'apply_patch' else 'claude'
        adapter = load_adapter(host)
        parsed = adapter.parse({'hook_event_name': 'PostToolUse', **payload}, None)
        assert parsed is not None

        result = _resolve_edited_paths(parsed.edited_paths, parsed.cwd)

        assert len(result) == 1
        assert PureWindowsPath(result[0]) == PureWindowsPath('C:/Users/dev/project/src/mod.py')


class TestPostToolUseSubprocess:
    def test_deep_dependency_chain_sorts_without_recursion(self, tmp_path: Path) -> None:
        source = '\n\n\n'.join(
            f'def node_{index:04d}():\n    return node_{index + 1:04d}()' for index in range(1_499)
        )
        source += '\n\n\ndef node_1499():\n    pass\n'
        f = tmp_path / 'deep_chain.py'
        f.write_text(source, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )

        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)

        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('def node_1499') < content.index('def node_0000')

    def test_utf8_file_roundtrip(self, tmp_path: Path) -> None:
        src = '# caf\u00e9 \u2603\n\ndef b():\n    pass\n\n\ndef a():\n    return b()\n'
        f = tmp_path / 'utf8.py'
        f.write_text(src, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Write',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert '\u2603' in content

    def test_valid_sort(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result = run_cli(['hook', '--agent', 'claude'], stdin=payload, expect=0)
        assert result.returncode == 0
        assert f.read_text(encoding='utf-8').index('alpha') < f.read_text(encoding='utf-8').index(
            'bravo'
        )


class TestPostToolUseIncremental:
    @pytest.mark.parametrize(
        ('config_text', 'expected_message'),
        [
            ('[tool.defsort]\nincremental = true\n', None),
            (
                '[tool.defsort]\nincremental = true\nmax-reorder-count = 0\n',
                'unable to read baseline',
            ),
        ],
        ids=['incremental-only', 'incremental-with-guard'],
    )
    def test_batch_fetch_failure_does_not_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        config_text: str,
        expected_message: str | None,
    ) -> None:
        _init_empty_git_repo(tmp_path)
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            config_text,
            encoding='utf-8',
        )
        original = target.read_text(encoding='utf-8')
        fetch_calls: list[tuple[object, ...]] = []
        write_calls: list[tuple[object, ...]] = []

        def fail_fetch(*args: object, **_kwargs: object) -> None:
            fetch_calls.append(args)

        def record_write(*args: object, **_kwargs: object) -> None:
            write_calls.append(args)

        monkeypatch.setattr('defsort._hook.batch_fetch_worktree_baselines', fail_fetch)
        monkeypatch.setattr('defsort._hook.write_source', record_write)

        rc = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        assert rc == 0
        assert len(fetch_calls) == 1
        assert write_calls == []
        assert target.read_text(encoding='utf-8') == original
        captured = capsys.readouterr()
        if expected_message is None:
            assert 'unable to read baseline' not in captured.err
        else:
            assert expected_message in captured.err

    def test_post_tool_use_baseline_missing_full_sort(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Commit a placeholder so repo is not unborn
        (tmp_path / 'placeholder.py').write_text('# p\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n',
            encoding='utf-8',
        )
        # Create new file not in HEAD
        f = tmp_path / 'new.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        content = f.read_text(encoding='utf-8')
        # Full sort applied
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_cli_no_incremental_overrides_config(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Baseline: bravo before alpha (wrong dependency order).
        baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        f = tmp_path / 'mod.py'
        f.write_text(baseline, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Config says incremental=true
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nincremental = true\n', encoding='utf-8')
        # Add new def at end -- if incremental, only aardvark moves; bravo/alpha
        # stay in wrong order.  If full sort (--no-incremental), all three get
        # reordered: aardvark, alpha, bravo.
        new_content = baseline + '\n\ndef aardvark():\n    pass\n'
        f.write_text(new_content, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--no-incremental'], stdin=payload
        )
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        # Full sort: dependency order = aardvark, alpha, bravo
        assert content.index('aardvark') < content.index('alpha')
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_config_incremental_false(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Baseline in non-standard order
        baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        f = tmp_path / 'mod.py'
        f.write_text(baseline, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Config disables incremental
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nincremental = false\n', encoding='utf-8')
        # Add new def and trigger PostToolUse
        new_content = baseline + '\n\ndef aardvark():\n    pass\n'
        f.write_text(new_content, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        content = f.read_text(encoding='utf-8')
        # Full sort: dependency order = aardvark, alpha, bravo
        assert content.index('aardvark') < content.index('alpha')
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_fallback_full_sort(self, tmp_path: Path) -> None:
        # File outside git repo -- no .git directory
        f = tmp_path / 'mod.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        content = f.read_text(encoding='utf-8')
        # Full sort applied: alpha before bravo (dependency order)
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_full_sort_default(self, tmp_path: Path) -> None:
        _init_empty_git_repo(tmp_path)
        # Baseline with defs in WRONG dependency order (bravo before alpha,
        # but bravo calls alpha). Incremental mode would leave these alone.
        baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        f = tmp_path / 'mod.py'
        f.write_text(baseline, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        # Write same content (no new defs) -- full sort should reorder existing
        f.write_text(baseline, encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        content = f.read_text(encoding='utf-8')
        # Full sort: alpha before bravo (dependency order)
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_reads_config_order(self, tmp_path: Path) -> None:
        # File outside git repo -- no incremental complications, pure config test.
        # Source where dependency order != alphabetical order:
        # alpha() calls bravo(), so dependency order = bravo, alpha.
        # Alphabetical order = alpha, bravo.
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    return bravo()\n'
        f = tmp_path / 'mod.py'
        f.write_text(src, encoding='utf-8')
        # Write config requesting alphabetical order.
        # Even outside a git repo, discovery retains the cwd fallback and
        # resolve_config_at_root checks for pyproject.toml there.
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\norder = "alphabetical"\n', encoding='utf-8')
        _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        content = f.read_text(encoding='utf-8')
        # Alphabetical: alpha before bravo
        assert content.index('alpha') < content.index('bravo')

    def test_post_tool_use_write_failure_warns_and_leaves_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'placeholder.py').write_text('# p\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        f = tmp_path / 'mod.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        original = f.read_bytes()
        f.chmod(0o444)
        try:
            rc = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(f)}})
        finally:
            f.chmod(0o644)
        assert rc == 0
        assert f.read_bytes() == original
        assert (
            capsys.readouterr().err
            == f'defsort: cannot write {f.as_posix()}; file left unsorted\n'
        )

    def test_unsupported_git_warns_without_writing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_empty_git_repo(tmp_path)
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )
        source = target.read_text(encoding='utf-8')

        def reject_git(_cwd: str, _paths: list[str]) -> None:
            raise UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._hook.batch_fetch_worktree_baselines', reject_git)

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        captured = capsys.readouterr()
        assert result == EXIT_OK
        assert captured.err == (
            'defsort: Git 2.42 or newer is required for Git-backed sorting (found Git 2.41.7)\n'
        )
        assert json.loads(captured.out) == {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': captured.err.rstrip('\n'),
            }
        }
        assert target.read_text(encoding='utf-8') == source


class TestPreToolUse:
    def test_already_ordered_no_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        assert captured.out == ''

    def test_bare_commit_keeps_header_like_body_comment_valid_in_index(
        self, tmp_path: Path
    ) -> None:
        source = (
            'def zebra():\n'
            '    # encoding: utf-8 assumed\n'
            '    return 1\n'
            '\n\n'
            'def apple():\n'
            '    return 2\n'
        )
        _init_git_repo(tmp_path, source)
        path = tmp_path / 'test.py'
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        worktree = source + '\n# unstaged suffix\n'
        path.write_text(worktree, encoding='utf-8')

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
            cwd=tmp_path,
        )
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)

        assert result_status == EXIT_OK
        compile(staged, ':test.py', 'exec')
        assert staged.count('# encoding: utf-8 assumed') == 1
        assert path.read_text(encoding='utf-8') == worktree

    def test_bare_commit_non_utf8_working_tree_sorts_with_a_receipt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # Overwrite working tree with non-UTF-8 bytes (unstaged change)
        f.write_bytes(b'\x80\x81\x82 invalid utf-8')
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Working tree must be left alone (non-UTF-8, not overwritten)
        assert f.read_bytes().startswith(b'\x80\x81\x82')

    @pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain control characters')
    def test_bare_commit_sync_note_escapes_filename(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_empty_git_repo(tmp_path)
        name = 'escape\x1b\n.py'
        f = tmp_path / name
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', name])
        original_read_file = hook_mod.read_file_result

        def deny_worktree_read(path: Path) -> object:
            if path.name == name:
                raise OSError('denied')
            return original_read_file(path)

        monkeypatch.setattr(hook_mod, 'read_file_result', deny_worktree_read)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out) == _RECEIPT_ONE
        assert captured.err == (
            'defsort: cannot sync escape\\x1b\\x0a.py; worktree copy left unsorted\n'
        )
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_bare_commit_syncs_clean_working_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # No unstaged changes -- working tree matches staged content
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Working tree must have sorted content (alpha before bravo)
        wt = f.read_text(encoding='utf-8')
        assert wt.index('alpha') < wt.index('bravo')

    def test_bare_commit_trailing_comment_denies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test # note', tmp_path),
            cwd=tmp_path,
        )
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)

        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert 'comment' in reason
        assert staged == _SRC_NEEDS_SORT

    def test_bare_commit_unreadable_working_tree_sorts_with_a_note(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        original_read_file = hook_mod.read_file_result

        def deny_worktree_read(path: Path) -> object:
            if path.name == 'test.py':
                raise OSError('denied')
            return original_read_file(path)

        monkeypatch.setattr(hook_mod, 'read_file_result', deny_worktree_read)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert json.loads(captured.out) == _RECEIPT_ONE
        assert captured.err == 'defsort: cannot sync test.py; worktree copy left unsorted\n'
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert staged.index('alpha') < staged.index('bravo')

    def test_bare_commit_unwritable_working_tree_sorts_with_a_receipt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # Make file read-only so write_text will fail
        f.chmod(0o444)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        f.chmod(0o644)  # Restore for cleanup
        captured = capsys.readouterr()
        assert json.loads(captured.out) == _RECEIPT_ONE
        assert captured.err == 'defsort: cannot write test.py; worktree copy left unsorted\n'

    def test_bare_heredoc_denies_without_sorting_index(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        source_path = tmp_path / 'test.py'
        source_path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': "cat <<'EOF'\ngit commit -m fake\nEOF\n",
                },
                'cwd': str(tmp_path),
            }
        )

        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        reason = _assert_classification_denied(decision)
        assert "operator '<<'" in reason
        assert staged == _SRC_NEEDS_SORT

    def test_blocks_interstitial(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert (
            'code between definitions' in output['hookSpecificOutput']['permissionDecisionReason']
        )

    def test_compound_add_comment_denies_without_staging(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git add test.py # stage it\ngit commit -m test', tmp_path),
            cwd=tmp_path,
        )
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)

        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert 'comment' in reason
        assert staged == '# placeholder\n'

    def test_compound_add_commit_preserves_other_unstaged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        # file_a.py: already staged, with unstaged changes on top
        fa = tmp_path / 'file_a.py'
        fa.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'file_a.py'])
        extra = _SRC_NEEDS_SORT + '\n# unstaged extra\n'
        fa.write_text(extra, encoding='utf-8')
        # file_b.py: NOT staged, part of compound command
        fb = tmp_path / 'file_b.py'
        fb.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add file_b.py && git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == {
            'systemMessage': 'defsort: sorted 2 staged Python files for this commit'
        }
        # file_a.py's working tree must preserve unstaged changes
        assert '# unstaged extra' in fa.read_text(encoding='utf-8')

    def test_compound_add_commit_sorts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # File is NOT pre-staged -- compound command has git add
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add test.py && git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Index must have sorted content
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_compound_add_dotslash_path_syncs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add ./test.py && git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Working tree must have sorted content (synced for the subsequent git add)
        wt = f.read_text(encoding='utf-8')
        assert wt.index('alpha') < wt.index('bravo')

    def test_compound_add_non_py_preserves_unstaged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        extra = _SRC_NEEDS_SORT + '\n# unstaged extra\n'
        f.write_text(extra, encoding='utf-8')
        readme = tmp_path / 'README.md'
        readme.write_text('hello\n', encoding='utf-8')
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add README.md && git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Working tree must still have the unstaged comment
        assert '# unstaged extra' in f.read_text(encoding='utf-8')

    def test_compound_add_with_arbitrary_prefix_denies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        generated = tmp_path / 'generated.py'
        generated.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'python generate.py && git add generated.py && git commit -m test'
                },
                'cwd': str(tmp_path),
            }
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'python generate.py'" in reason
        assert generated.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':generated.py'], check=False)
        assert staged.returncode != 0, process_transcript(staged)

    def test_discovery_error_denies_by_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        def fail_discovery(_cwd: str) -> _RepoDiscoveryResult:
            return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(
            'defsort._hook.discover_repo_root',
            fail_discovery,
        )

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )

        assert decision.decision == 'deny'
        output = json.loads(capsys.readouterr().out)
        hook_output = output['hookSpecificOutput']
        assert hook_output['permissionDecision'] == 'deny'
        assert hook_output['permissionDecisionReason'] == (
            'defsort: failed to discover git work tree'
        )

    @pytest.mark.parametrize('policy_source', ['cli', 'config'])
    def test_discovery_error_honors_failure_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        policy_source: str,
    ) -> None:

        def fail_discovery(_cwd: str) -> _RepoDiscoveryResult:
            return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(
            'defsort._hook.discover_repo_root',
            fail_discovery,
        )
        if policy_source == 'config':
            (tmp_path / 'pyproject.toml').write_text(
                '[tool.defsort]\nallow-on-failure = true\n', encoding='utf-8'
            )

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=policy_source == 'cli',
        )

        assert decision == Outcome('allow')
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == 'WARNING: defsort: failed to discover git work tree\n'

    @pytest.mark.parametrize('redirect', ['cd', 'dash-c'])
    def test_discovery_uses_resolved_invocation_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        redirect: str,
    ) -> None:
        process_directory = tmp_path / 'process'
        target_directory = tmp_path / 'target'
        process_directory.mkdir()
        target_directory.mkdir()
        observed: list[str] = []

        def record_target(cwd: str) -> _RepoDiscoveryResult:
            observed.append(cwd)
            return _RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)

        monkeypatch.setattr('defsort._hook.discover_repo_root', record_target)
        command = (
            f'cd {shell_path(target_directory)} && git commit -m "test"'
            if redirect == 'cd'
            else f'git -C {shell_path(target_directory)} commit -m "test"'
        )

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': command},
                'cwd': str(process_directory),
            }
        )

        assert decision == Outcome('allow')
        assert observed == [str(target_directory)]

    def test_failed_index_update_preserves_working_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch apply_index_entries to simulate failure
        def fail_apply_index_entries(
            _cwd: str, _entries: Collection[tuple[str, str, str]]
        ) -> bool:
            return False

        monkeypatch.setattr('defsort._hook.apply_index_entries', fail_apply_index_entries)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        # Working tree must still have original (pre-sort) content
        wt = f.read_text(encoding='utf-8')
        assert wt == _SRC_NEEDS_SORT

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_git_resolution_error_during_blob_preparation_keeps_failure_policy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        allow_on_failure: bool,
    ) -> None:
        _init_git_repo(tmp_path, '# initial\n')
        path = tmp_path / 'test.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        staged_before = _staged_text(tmp_path, 'test.py')

        def prepare_without_git(
            cwd: str, path: str, content: str, mode: str, *, encoding: str = 'utf-8'
        ) -> tuple[str, str, str] | None:
            with monkeypatch.context() as scoped:
                scoped.setenv('PATH', str(tmp_path / 'missing-git'))
                return prepare_index_entry(cwd, path, content, mode, encoding=encoding)

        monkeypatch.setattr(hook_mod, 'prepare_index_entry', prepare_without_git)
        command = 'git commit -m test'
        payload = _provider_pretooluse_payload(
            provider,
            command,
            tmp_path,
            codex_command=f'git -C {shell_path(tmp_path)} commit -m test',
        )

        result = _run_in_process(payload, provider, allow_on_failure=allow_on_failure)

        captured = capsys.readouterr()
        reason = 'defsort: unexpected error during sort'
        assert 'FileNotFoundError' in captured.err
        if allow_on_failure:
            assert result == 0
            assert captured.out == ''
            assert f'WARNING: {reason}' in captured.err
        elif provider == 'antigravity':
            assert result == 0
            assert json.loads(captured.out) == {'decision': 'deny', 'reason': reason}
            assert captured.err.endswith(f'{reason}\n')
        else:
            assert result == 0
            output = json.loads(captured.out)['hookSpecificOutput']
            assert output['permissionDecision'] == 'deny'
            assert output['permissionDecisionReason'] == reason
            assert captured.err.endswith(f'{reason}\n')
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert _staged_text(tmp_path, 'test.py') == staged_before

    def test_global_git_option_segment_before_commit_denies(
        self,
        tmp_path: Path,
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git --version init && git commit -m "next"'},
                'cwd': str(tmp_path),
            }
        )

        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        reason = _assert_classification_denied(decision)
        assert "segment 'git --version init'" in reason
        assert staged == _SRC_NEEDS_SORT

    def test_include_modified_discovery_failure_denies(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'

        def fail_discovery(_cwd: str) -> list[str] | None:
            return None

        monkeypatch.setattr(
            'defsort._hook.get_modified_tracked_py_files',
            fail_discovery,
        )
        rc = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include test.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        assert rc.decision == 'deny'
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        # File NOT modified (denied before sorting)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_no_staged_py_files(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        readme = tmp_path / 'README.md'
        readme.write_text('hello\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'README.md'])
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        # No .py files staged, so no output (fast path)
        assert captured.out == ''

    def test_non_worktree_bootstrap_compound_denies(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git init && git add x && git commit -m "initial"'},
                'cwd': str(tmp_path),
            }
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'git init'" in reason
        assert _assert_denied_output(capsys) == reason

    def test_non_worktree_commit_is_silent(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )

        assert decision == Outcome('allow')
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''

    def test_ordered_interstitial_preserves_partial_staging_silently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        source = 'def alpha():\n    pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
        path = tmp_path / 'test.py'
        path.write_bytes(source.encode())
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        worktree = source + '\n# unstaged comment\n'
        path.write_bytes(worktree.encode())

        outcome = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )

        assert outcome.decision == 'allow'
        assert capsys.readouterr() == ('', '')
        assert path.read_bytes() == worktree.encode()
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged == source.encode()

    def test_pathspec_modified_discovery_failure_denies(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'

        def fail_discovery(_cwd: str) -> list[str] | None:
            return None

        monkeypatch.setattr(
            'defsort._hook.get_modified_tracked_py_files',
            fail_discovery,
        )
        rc = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- test.py'},
                'cwd': str(tmp_path),
            }
        )
        assert rc.decision == 'deny'
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        # File NOT modified (denied before sorting)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_pre_commit_init_denies_before_repository_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository-dependent operation was called')

        monkeypatch.setattr('defsort._hook.is_hook_enabled', fail_if_called)
        monkeypatch.setattr('defsort._hook.discover_repo_root', fail_if_called)
        monkeypatch.setattr('defsort._hook._resolve_hook_context', fail_if_called)
        monkeypatch.setattr('defsort._hook.run_pre_commit_adds', fail_if_called)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git init child && git -C child add . '
                    '&& git -C child commit -m "initial"'
                },
                'cwd': str(tmp_path),
            },
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'git init child'" in reason
        assert _assert_denied_output(capsys) == reason

    def test_preserves_executable_bit(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        run_setup(['git', '-C', str(tmp_path), 'update-index', '--chmod=+x', 'test.py'])
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        # Verify index entry still has 100755 mode
        ls_result = run_setup(['git', '-C', str(tmp_path), 'ls-files', '-s', 'test.py'])
        assert decode_text(ls_result.stdout).startswith('100755')

    def test_preserves_partial_staging(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # Add unstaged change to working tree
        extra = _SRC_NEEDS_SORT + '\n# unstaged comment\n'
        f.write_text(extra, encoding='utf-8')
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        # Working tree should still have the unstaged comment
        assert '# unstaged comment' in f.read_text(encoding='utf-8')

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    def test_provider_discovery_error_denial_shape(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_discovery(_cwd: str) -> _RepoDiscoveryResult:
            return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(hook_mod, 'discover_repo_root', fail_discovery)
        missing = tmp_path / 'missing'
        payload = _provider_pretooluse_payload(
            provider,
            'git commit -m "test"',
            missing,
            codex_command=f'git -C {shell_path(tmp_path)} commit -m "test"',
        )

        result = _run_in_process(payload, provider)

        assert result == EXIT_OK
        reason = _assert_denied_output(capsys, provider, prefix='defsort: ')
        assert reason == 'defsort: failed to discover git work tree'

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    def test_provider_discovery_error_is_silent_with_failure_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_discovery(_cwd: str) -> _RepoDiscoveryResult:
            return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(hook_mod, 'discover_repo_root', fail_discovery)
        missing = tmp_path / 'missing'
        payload = _provider_pretooluse_payload(
            provider,
            'git commit -m "test"',
            missing,
            codex_command=f'git -C {shell_path(tmp_path)} commit -m "test"',
        )

        result = _run_in_process(payload, provider, allow_on_failure=True)

        assert result == 0
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == 'WARNING: defsort: failed to discover git work tree\n'

    def test_raw_different_sync_resolves_file_config_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / '.gitattributes').write_text('*.py text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add attributes'])
        path = tmp_path / 'test.py'
        staged = b'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        path.write_bytes(staged)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        path.write_bytes(staged.replace(b'\n', b'\r\n'))
        calls: list[str] = []

        original_file_config = _file_config

        def resolve_file_config(ctx: HookContext, rel_path: str) -> ResolvedConfig:
            calls.append(rel_path)
            return original_file_config(ctx, rel_path)

        monkeypatch.setattr('defsort._hook._file_config', resolve_file_config)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            },
            overrides=SortOverrides(definition_order='alphabetical'),
        )

        assert decision == Outcome('allow', 'defsort: sorted 1 staged Python file for this commit')
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        assert calls == ['test.py']

    def test_read_failure_continues_with_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        def fail_read_staged_blobs(
            _cwd: str, _info: Iterable[_StagedFileInfo]
        ) -> dict[str, BlobReadResult] | None:
            return None

        monkeypatch.setattr('defsort._hook.read_staged_blobs', fail_read_staged_blobs)
        rc = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=True,
        )
        captured = capsys.readouterr()
        assert captured.out == ''  # No deny JSON
        assert 'WARNING' in captured.err
        assert 'failed to read' in captured.err
        assert rc == Outcome('allow')  # allow, not deny (C1 fix)

    def test_read_failure_denies_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch read_staged_blobs to return None (simulates git failure)
        def fail_read_staged_blobs(
            _cwd: str, _info: Iterable[_StagedFileInfo]
        ) -> dict[str, BlobReadResult] | None:
            return None

        monkeypatch.setattr('defsort._hook.read_staged_blobs', fail_read_staged_blobs)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert 'failed to read' in output['hookSpecificOutput']['permissionDecisionReason']

    def test_rename_discovery_failure_denies_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        original = target.read_bytes()

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('baseline or write operation was called')

        def fail_rename_discovery(_cwd: str) -> None:
            return None

        monkeypatch.setattr(hook_mod, 'detect_renames', fail_rename_discovery)
        for name in (
            'apply_index_entries',
            'batch_fetch_worktree_baselines',
            'prepare_index_entry',
            'run_pre_commit_adds',
            'write_source',
        ):
            monkeypatch.setattr(hook_mod, name, fail_if_called)

        result = _run_in_process(
            _pretooluse_payload('git commit --all -m test', tmp_path),
            overrides=SortOverrides(incremental=True),
        )

        assert result == EXIT_OK
        captured = capsys.readouterr()
        assert captured.err == 'defsort: failed to detect staged renames\n'
        output = json.loads(captured.out)['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        assert output['permissionDecisionReason'] == ('defsort: failed to detect staged renames')
        assert target.read_bytes() == original

    def test_rename_discovery_failure_warns_and_allows_with_failure_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        original = target.read_bytes()

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('baseline or write operation was called')

        def fail_rename_discovery(_cwd: str) -> None:
            return None

        monkeypatch.setattr(hook_mod, 'detect_renames', fail_rename_discovery)
        for name in (
            'apply_index_entries',
            'batch_fetch_worktree_baselines',
            'prepare_index_entry',
            'run_pre_commit_adds',
            'write_source',
        ):
            monkeypatch.setattr(hook_mod, name, fail_if_called)

        result = _run_in_process(
            _pretooluse_payload('git commit --all -m test', tmp_path),
            overrides=SortOverrides(incremental=True),
            allow_on_failure=True,
        )

        assert result == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == 'WARNING: defsort: failed to detect staged renames\n'
        assert target.read_bytes() == original

    def test_skip_non_bash_tool(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': 'test.py'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        assert captured.out == ''

    def test_skip_non_commit_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'pytest -q'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        assert captured.out == ''

    def test_sorts_staged_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        _pre_tool(
            {
                'session_id': 'session-test',
                'transcript_path': str(tmp_path / 'transcript.jsonl'),
                'permission_mode': 'default',
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_use_id': 'toolu_test',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        # Check the INDEX has sorted content (not the working tree)
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_syntax_error_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_SYNTAX_ERROR, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        outcome = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        assert outcome.decision == 'allow'
        assert captured.out == captured.err == ''

    def test_unexpected_exception_denies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch get_staged_py_info to raise UnicodeDecodeError
        def _raise(_cwd: str) -> list[_StagedFileInfo]:
            msg = 'invalid start byte'
            raise UnicodeDecodeError('utf-8', b'\x80', 0, 1, msg)

        monkeypatch.setattr('defsort._hook.get_staged_py_info', _raise)
        _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert 'unexpected error' in output['hookSpecificOutput']['permissionDecisionReason']

    def test_unknown_git_option_denies_before_repository_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository-dependent operation was called')

        for name in (
            'is_hook_enabled',
            'discover_repo_root',
            'resolve_config_at_root',
            '_resolve_hook_context',
            'run_pre_commit_adds',
            'get_staged_py_info',
            'read_staged_blobs',
            'apply_index_entries',
        ):
            monkeypatch.setattr(hook_mod, name, fail_if_called)
        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git --definitely-not-real init ; git commit -m "x"'},
                'cwd': str(tmp_path),
            },
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'git --definitely-not-real init'" in reason
        assert _assert_denied_output(capsys) == reason

    def test_unknown_git_option_denies_despite_allow_on_failure(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git --definitely-not-real commit -m "x"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=True,
        )

        reason = _assert_classification_denied(decision)
        assert '--definitely-not-real' in reason
        assert _assert_denied_output(capsys) == reason

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    def test_unknown_git_option_provider_denial_shape(
        self,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        tmp_path: Path,
    ) -> None:
        command = 'git --definitely-not-real commit -m "test"'
        payload = _provider_pretooluse_payload(provider, command, tmp_path)

        result = _run_in_process(payload, provider)

        assert result == EXIT_OK
        reason = _assert_denied_output(capsys, provider)
        assert '--definitely-not-real' in reason

    def test_unknown_git_option_segment_denies_before_repository_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository-dependent operation was called')

        monkeypatch.setattr('defsort._hook.is_hook_enabled', fail_if_called)
        monkeypatch.setattr('defsort._hook.discover_repo_root', fail_if_called)
        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git --future status ; git init child ; git commit -m "x"'
                },
                'cwd': str(tmp_path),
            },
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'git --future status'" in reason
        assert _assert_denied_output(capsys) == reason

    def test_unsupported_git_denies_with_actionable_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, '# initial\n')
        source = _SRC_NEEDS_SORT
        path = tmp_path / 'test.py'
        path.write_text(source, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        staged_before = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout

        def reject_git(_cwd: str, _info: list[_StagedFileInfo]) -> None:
            raise UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._hook.read_staged_blobs', reject_git)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )

        captured = capsys.readouterr()
        denial = json.loads(captured.out)
        assert decision.decision == 'deny'
        assert captured.err == f'{decision.reason}\n'
        assert denial['hookSpecificOutput']['permissionDecisionReason'] == (
            'defsort: Git 2.42 or newer is required for Git-backed sorting (found Git 2.41.7)'
        )
        assert path.read_text(encoding='utf-8') == source
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged_after == staged_before

    def test_unsupported_git_warns_and_allows_with_failure_policy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, '# initial\n')
        source = _SRC_NEEDS_SORT
        path = tmp_path / 'test.py'
        path.write_text(source, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        staged_before = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout

        def reject_git(_cwd: str, _info: list[_StagedFileInfo]) -> None:
            raise UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._hook.read_staged_blobs', reject_git)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=True,
        )

        captured = capsys.readouterr()
        assert decision == Outcome('allow')
        assert captured.out == ''
        assert captured.err == (
            'WARNING: defsort: Git 2.42 or newer is required for Git-backed sorting '
            '(found Git 2.41.7)\n'
        )
        assert path.read_text(encoding='utf-8') == source
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged_after == staged_before

    @pytest.mark.parametrize(
        'command',
        [
            'python generate.py && git commit -am test',
            'python generate.py && git commit -m test -- test.py',
        ],
        ids=['auto-stage', 'pathspec'],
    )
    def test_worktree_commit_with_unrecognized_prefix_denies(
        self,
        tmp_path: Path,
        command: str,
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        source_path = tmp_path / 'test.py'

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': command},
                'cwd': str(tmp_path),
            }
        )

        reason = _assert_classification_denied(decision)
        assert "segment 'python generate.py'" in reason
        assert source_path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestPreToolUseCommitModes:
    def test_commit_a_blocker_denies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_INTERSTITIAL)
        f = tmp_path / 'test.py'
        original = f.read_text(encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -a -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == EXIT_OK
        output = json.loads(result_stdout)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert f.read_text(encoding='utf-8') == original

    def test_commit_a_deleted_file_skipped(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        # Add a second tracked file
        other = tmp_path / 'other.py'
        other.write_text('# placeholder\n', encoding='utf-8')
        run_setup(['git', 'add', 'other.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add other'], cwd=tmp_path)
        # Modify other.py with unsorted content, delete test.py
        other.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        (tmp_path / 'test.py').unlink()
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -a -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # Sibling file still sorted despite deleted file
        content = other.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_a_sorts_working_tree(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -a -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_a_staged_only(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -a -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_am_sorts_working_tree(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -am "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_include_both_sorted_is_silent(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_ALREADY_ORDERED)
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        other = tmp_path / 'other.py'
        other.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', 'add', 'other.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add other'], cwd=tmp_path)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include other.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert result_stdout == ''

    def test_commit_include_generic_staged_read_failure_sorts_pathspec(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pathspec, staged_before = _setup_include_failure_repo(tmp_path)

        def fail_read(_cwd: str, _info: list[_StagedFileInfo]) -> None:
            return None

        monkeypatch.setattr('defsort._hook.read_staged_blobs', fail_read)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include pathspec.py -m "msg"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=True,
        )

        captured = capsys.readouterr()
        assert decision == Outcome('allow')
        assert captured.out == ''
        assert captured.err == 'WARNING: defsort: failed to read staged contents\n'
        assert pathspec.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
        assert staged_after == staged_before

    def test_commit_include_overlap(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        # Stage test.py
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include test.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = (tmp_path / 'test.py').read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_include_sorts_both(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        # Create another tracked file as pathspec target
        other = tmp_path / 'other.py'
        other.write_text('# placeholder\n', encoding='utf-8')
        run_setup(['git', 'add', 'other.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add other'], cwd=tmp_path)
        # Now modify other.py with unsorted content (unstaged)
        other.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # Stage test.py (SRC_NEEDS_SORT, different from HEAD's placeholder)
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include other.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # Pathspec file sorted on working tree
        content = other.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')
        # Staged file sorted in index (and synced to working tree)
        content_staged = (tmp_path / 'test.py').read_text(encoding='utf-8')
        assert content_staged.index('alpha') < content_staged.index('bravo')

    def test_commit_include_staged_blocker_keeps_worktree_sort(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pathspec, _ = _setup_include_failure_repo(tmp_path)
        staged = tmp_path / 'staged.py'
        staged.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'staged.py'])
        index_before = (tmp_path / '.git' / 'index').read_bytes()
        staged_mtime = staged.stat().st_mtime_ns

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include pathspec.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )

        captured = capsys.readouterr()
        assert decision.decision == 'deny'
        assert captured.err == f'{decision.reason}\n'
        denial = json.loads(captured.out)['hookSpecificOutput']
        assert denial['permissionDecision'] == 'deny'
        assert 'staged.py' in denial['permissionDecisionReason']
        assert pathspec.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        assert staged.read_text(encoding='utf-8') == _SRC_INTERSTITIAL
        assert staged.stat().st_mtime_ns == staged_mtime
        assert (tmp_path / '.git' / 'index').read_bytes() == index_before

    def test_commit_include_unsupported_git_denies_before_worktree_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pathspec, staged_before = _setup_include_failure_repo(tmp_path)

        def reject_git(_cwd: str, _info: list[_StagedFileInfo]) -> None:
            raise UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._hook.read_staged_blobs', reject_git)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include pathspec.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )

        captured = capsys.readouterr()
        denial = json.loads(captured.out)
        assert decision.decision == 'deny'
        assert captured.err == f'{decision.reason}\n'
        assert denial == {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': (
                    'defsort: Git 2.42 or newer is required for Git-backed sorting '
                    '(found Git 2.41.7)'
                ),
            }
        }
        assert pathspec.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
        assert staged_after == staged_before

    def test_commit_include_unsupported_git_warns_before_worktree_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pathspec, staged_before = _setup_include_failure_repo(tmp_path)

        def reject_git(_cwd: str, _info: list[_StagedFileInfo]) -> None:
            raise UnsupportedGitVersionError('2.41.7')

        monkeypatch.setattr('defsort._hook.read_staged_blobs', reject_git)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include pathspec.py -m "msg"'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=True,
        )

        captured = capsys.readouterr()
        assert decision == Outcome('allow')
        assert captured.out == ''
        assert captured.err == (
            'WARNING: defsort: Git 2.42 or newer is required for Git-backed sorting '
            '(found Git 2.41.7)\n'
        )
        assert pathspec.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
        assert staged_after == staged_before

    def test_commit_only_with_empty_pathspec_file_preserves_staged_changes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        index_before = _staged_bytes(tmp_path)
        worktree_before = (tmp_path / 'test.py').read_bytes()
        pathspec_file = tmp_path / 'empty.txt'
        pathspec_file.write_bytes(b'')

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': (
                        f'git commit --only --pathspec-from-file={shell_path(pathspec_file)}'
                        ' --amend --no-edit'
                    )
                },
                'cwd': str(tmp_path),
            }
        )

        captured = capsys.readouterr()
        assert decision == Outcome('allow')
        assert captured.out == ''
        assert captured.err == ''
        assert _staged_bytes(tmp_path) == index_before
        assert (tmp_path / 'test.py').read_bytes() == worktree_before

    @pytest.mark.parametrize(
        'staged_source',
        [
            pytest.param(_SRC_NEEDS_SORT, id='sortable'),
            pytest.param(_SRC_INTERSTITIAL, id='blocked'),
        ],
    )
    def test_commit_only_without_paths_denies_and_preserves_staged_changes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        staged_source: str,
    ) -> None:
        _init_git_repo(tmp_path, staged_source)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        baseline_tree = run_setup(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD^{tree}']).stdout
        index_before = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        worktree_before = (tmp_path / 'test.py').read_bytes()

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --only --amend --no-edit'},
                'cwd': str(tmp_path),
            }
        )

        reason = _assert_classification_denied(decision)
        assert '--only without pathspec' in reason
        assert _assert_denied_output(capsys) == reason
        assert _staged_bytes(tmp_path) == index_before
        assert (tmp_path / 'test.py').read_bytes() == worktree_before

        run_setup(['git', '-C', str(tmp_path), 'commit', '--only', '--amend', '--no-edit'])

        amended_tree = run_setup(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD^{tree}']).stdout
        assert amended_tree == baseline_tree
        assert _staged_bytes(tmp_path) == index_before
        assert (tmp_path / 'test.py').read_bytes() == worktree_before

    @pytest.mark.parametrize(
        'case',
        [
            pytest.param((_SRC_NEEDS_SORT, _SRC_ALREADY_ORDERED), id='worktree-only'),
            pytest.param((_SRC_ALREADY_ORDERED, _SRC_NEEDS_SORT), id='index-only'),
            pytest.param((_SRC_NEEDS_SORT, _SRC_NEEDS_SORT), id='both'),
            pytest.param((_SRC_ALREADY_ORDERED, _SRC_ALREADY_ORDERED), id='neither'),
        ],
    )
    def test_commit_include_index_rewrites_emit_receipts(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        case: tuple[str, str],
    ) -> None:
        worktree_source, index_source = case
        _init_git_repo(tmp_path, '# placeholder\n')
        pathspec_file = tmp_path / 'pathspec.py'
        pathspec_file.write_text('# placeholder\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'pathspec.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add pathspec'])
        (tmp_path / 'test.py').write_text(index_source, encoding='utf-8')
        pathspec_file.write_text(worktree_source, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --include pathspec.py -m "msg"'},
                'cwd': str(tmp_path),
            }
        )

        rewritten = index_source == _SRC_NEEDS_SORT
        expected_reason = (
            'defsort: sorted 1 staged Python file for this commit' if rewritten else None
        )
        assert decision == Outcome('allow', expected_reason)
        stdout = capsys.readouterr().out
        if rewritten:
            assert json.loads(stdout) == _RECEIPT_ONE
        else:
            assert stdout == ''

    @pytest.mark.parametrize(
        'commit_args',
        ['-a', '--include sample.py', 'sample.py'],
        ids=['auto-stage', 'include', 'pathspec'],
    )
    @pytest.mark.parametrize(
        'config_text',
        [
            'incremental = true\n',
            'max-reorder-count = 0\n',
            'incremental = true\nmax-reorder-count = 0\n',
        ],
        ids=['incremental', 'guard', 'incremental-with-guard'],
    )
    def test_commit_initial_worktree_missing_baseline_full_sorts(
        self, tmp_path: Path, commit_args: str, config_text: str
    ) -> None:
        _init_empty_git_repo(tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\n' + config_text, encoding='utf-8'
        )
        target = tmp_path / 'sample.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'sample.py'], cwd=tmp_path)

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload(f'git commit {commit_args} -m initial', tmp_path),
            cwd=tmp_path,
        )

        assert result_status == 0, result_stdout
        assert result_stdout == ''
        assert result_stderr == ''
        assert target.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED

    @pytest.mark.parametrize(
        'case',
        [
            pytest.param(('git commit -m "msg"', True, _SRC_NEEDS_SORT), id='bare-rewrite'),
            pytest.param(('git commit -m "msg"', True, _SRC_ALREADY_ORDERED), id='bare-no-op'),
            pytest.param(
                ('git commit -a -m "msg"', False, _SRC_NEEDS_SORT),
                id='auto-stage-rewrite',
            ),
            pytest.param(
                ('git commit -a -m "msg"', False, _SRC_ALREADY_ORDERED),
                id='auto-stage-no-op',
            ),
            pytest.param(
                ('git commit -m "msg" -- test.py', False, _SRC_NEEDS_SORT),
                id='pathspec-rewrite',
            ),
            pytest.param(
                ('git commit -m "msg" -- test.py', False, _SRC_ALREADY_ORDERED),
                id='pathspec-no-op',
            ),
        ],
    )
    def test_commit_modes_report_only_index_rewrites(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        case: tuple[str, bool, str],
    ) -> None:
        command, stage_file, source = case
        _init_git_repo(tmp_path, source)
        if stage_file:
            run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': command},
                'cwd': str(tmp_path),
            }
        )

        rewritten = stage_file and source == _SRC_NEEDS_SORT
        expected_reason = (
            'defsort: sorted 1 staged Python file for this commit' if rewritten else None
        )
        assert decision == Outcome('allow', expected_reason)
        stdout = capsys.readouterr().out
        if rewritten:
            assert json.loads(stdout) == _RECEIPT_ONE
        else:
            assert stdout == ''

    def test_commit_patch_skips_sorting(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -p'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # File NOT modified (interactive skip)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_commit_pathspec_blocker_denies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_INTERSTITIAL)
        f = tmp_path / 'test.py'
        original = f.read_text(encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- test.py'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == EXIT_OK
        output = json.loads(result_stdout)
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert f.read_text(encoding='utf-8') == original

    def test_commit_pathspec_dir_deleted_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        sub = tmp_path / 'sub'
        sub.mkdir()
        inner = sub / 'inner.py'
        inner.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        sibling = sub / 'sibling.py'
        sibling.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', 'add', 'sub/inner.py', 'sub/sibling.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add sub'], cwd=tmp_path)
        # Modify sibling (unsorted content), delete inner
        sibling.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        inner.unlink()
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- sub/'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--no-incremental'], stdin=payload
        )
        assert result_status == 0
        # Sibling still sorted despite deleted file in same directory
        content = sibling.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_pathspec_directory(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        sub = tmp_path / 'sub'
        sub.mkdir()
        f = sub / 'inner.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', 'add', 'sub/inner.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add sub'], cwd=tmp_path)
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- sub/'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '--no-incremental'], stdin=payload
        )
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_pathspec_from_file(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        pf = tmp_path / 'paths.txt'
        pf.write_text('test.py\n', encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': f'git commit --pathspec-from-file={shell_path(pf)} -m "msg"'
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_pathspec_from_file_empty(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        pf = tmp_path / 'empty.txt'
        pf.write_text('', encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': f'git commit --pathspec-from-file={shell_path(pf)} -m "msg"'
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        # Index should be sorted (via fall-through to normal commit path)
        staged = run_setup(['git', 'show', ':test.py'], cwd=tmp_path)
        assert decode_text(staged.stdout).index('alpha') < decode_text(staged.stdout).index(
            'bravo'
        )

    def test_commit_pathspec_from_file_non_py(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # Create a non-.py file and add a pathspec file referencing it
        (tmp_path / 'readme.md').write_text('hello\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'readme.md'])
        pf = tmp_path / 'paths.txt'
        pf.write_text('readme.md\n', encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': f'git commit --pathspec-from-file={shell_path(pf)} -m "msg"'
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # Staged .py file should NOT be sorted (not in pathspec scope)
        staged = run_setup(['git', 'show', ':test.py'], cwd=tmp_path)
        assert decode_text(staged.stdout).index('bravo') < decode_text(staged.stdout).index(
            'alpha'
        )

    def test_commit_pathspec_from_file_nul(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(b'test.py\x00')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': f'git commit --pathspec-from-file={shell_path(pf)}'
                    ' --pathspec-file-nul -m "msg"',
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_pathspec_from_file_quoted_sorts_only_selected_file(
        self, tmp_path: Path
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'quoted target.py'
        target.write_bytes(b'# baseline\n')
        run_setup(['git', 'add', target.name], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'target'], cwd=tmp_path)
        target.write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        staged_before = _staged_bytes(tmp_path)
        pf = tmp_path / 'paths.txt'
        pf.write_bytes(b'"quoted target.py"\n')

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload(
                'git commit --pathspec-from-file=paths.txt -m target', tmp_path
            ),
            cwd=tmp_path,
        )

        assert result_status == 0
        assert result_stdout == result_stderr == ''
        assert target.read_bytes() == _SRC_ALREADY_ORDERED.encode()
        assert _staged_bytes(tmp_path) == staged_before
        assert (tmp_path / 'test.py').read_bytes() == staged_before
        run_setup(
            ['git', 'commit', '--pathspec-from-file=paths.txt', '-m', 'sorted target'],
            cwd=tmp_path,
        )
        committed = run_setup(['git', 'show', f'HEAD:{target.name}'], cwd=tmp_path).stdout
        assert committed == target.read_bytes() == _SRC_ALREADY_ORDERED.encode()
        committed_unrelated = run_setup(['git', 'show', 'HEAD:test.py'], cwd=tmp_path).stdout
        assert committed_unrelated == b'# placeholder\n'
        assert _staged_bytes(tmp_path) == staged_before
        remaining_staged = run_setup(
            ['git', 'diff', '--cached', '--name-only', '-z'], cwd=tmp_path
        ).stdout
        assert remaining_staged == b'test.py\0'

    def test_commit_pathspec_from_stdin_skips(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit --pathspec-from-file=- -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # File NOT modified
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_commit_pathspec_glob(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': "git commit -m 'msg' -- '*.py'"},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_pathspec_skips_unmodified(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        # Add a second unsorted file and commit both
        other = tmp_path / 'other.py'
        other.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'other.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add other'])
        # Modify only test.py (other.py stays as committed -- unsorted but unchanged)
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- .'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # other.py must NOT be modified -- it has no pending changes
        assert other.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_commit_pathspec_sorts_working_tree(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" -- test.py'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_commit_redirect_denies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg" > /tmp/out.txt'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'], stdin=payload
        )
        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert "operator '>'" in reason
        assert _staged_bytes(tmp_path) == _SRC_NEEDS_SORT.encode()

    def test_compound_add_before_only_without_paths_denies_without_replay(
        self, tmp_path: Path
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add test.py && git commit --only --amend --no-edit'
                },
                'cwd': str(tmp_path),
            }
        )

        reason = _assert_classification_denied(decision)
        assert '--only without pathspec' in reason
        assert _staged_bytes(tmp_path) == b'# placeholder\n'

    def test_compound_add_commit_a_sorts(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        # Create an untracked file
        new_file = tmp_path / 'new.py'
        new_file.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add new.py && git commit -a -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # new.py should now be tracked (pre_commit_adds ran git add)
        # and sorted on the working tree
        content = new_file.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_compound_add_commit_include_sorts(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        # Create two tracked files
        fa = tmp_path / 'file_a.py'
        fa.write_text('# placeholder\n', encoding='utf-8')
        fb = tmp_path / 'file_b.py'
        fb.write_text('# placeholder\n', encoding='utf-8')
        run_setup(['git', 'add', 'file_a.py', 'file_b.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'add files'], cwd=tmp_path)
        # Modify both with unsorted content
        fa.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        fb.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # Compound: add A (index) + commit --include B (working tree + index)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git add file_a.py && git commit --include file_b.py -m "msg"',
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # B sorted on working tree (pathspec)
        content_b = fb.read_text(encoding='utf-8')
        assert content_b.index('alpha') < content_b.index('bravo')
        # A sorted in index and synced to working tree (sync_working_tree_paths)
        content_a = fa.read_text(encoding='utf-8')
        assert content_a.index('alpha') < content_a.index('bravo')

    def test_compound_add_commit_patch_skips_sorting(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add test.py && git commit -p'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # File NOT modified (interactive mode skipped sorting)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize(
        'case',
        [
            pytest.param(_SRC_ALREADY_ORDERED, id='replayed-add-only'),
            pytest.param(_SRC_NEEDS_SORT, id='defsort-rewrite'),
        ],
    )
    def test_compound_add_reports_only_index_rewrites(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        case: str,
    ) -> None:
        _init_git_repo(tmp_path, case)

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git add test.py && git commit -m "msg"'},
                'cwd': str(tmp_path),
            }
        )

        rewritten = case == _SRC_NEEDS_SORT
        expected_reason = (
            'defsort: sorted 1 staged Python file for this commit' if rewritten else None
        )
        assert decision == Outcome('allow', expected_reason)
        stdout = capsys.readouterr().out
        if rewritten:
            assert json.loads(stdout) == _RECEIPT_ONE
        else:
            assert stdout == ''

    def test_normal_commit_still_uses_index(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "msg"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE


class TestReadOnlyCommitModes:
    def _staged_repo(self, tmp_path: Path) -> bytes:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py'])
        return staged.stdout

    def test_auto_stage_dry_run_mutates_nothing(self, tmp_path: Path) -> None:
        self._staged_repo(tmp_path)
        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -a --dry-run -m x', tmp_path),
        )
        assert result_status == 0
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_dry_run_commit_mutates_nothing(self, tmp_path: Path) -> None:
        before = self._staged_repo(tmp_path)
        for command in (
            'git commit --dry-run',
            'git commit --short',
            'git commit -h',
            'git commit --short --no-dry-run',
        ):
            result_status, _, _ = invoke_cli(
                ['hook', '--agent', 'claude'], stdin=_pretooluse_payload(command, tmp_path)
            )
            assert result_status == 0, command
            after = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py'])
            assert after.stdout == before, command
            worktree = (tmp_path / 'test.py').read_text(encoding='utf-8')
            assert worktree == _SRC_NEEDS_SORT, command

    def test_unknown_commit_option_denied(self, tmp_path: Path) -> None:
        before = self._staged_repo(tmp_path)
        for command in ('git commit --frobnicate -m x', 'git commit --dry-run=1 -m x'):
            result_status, result_stdout, result_stderr = invoke_cli(
                ['hook', '--agent', 'claude'], stdin=_pretooluse_payload(command, tmp_path)
            )
            reason = _assert_denied((result_status, result_stdout, result_stderr))
            assert command.split()[2] in reason, command
            staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py'])
            assert staged.stdout == before, command
            assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unknown_commit_option_denies_despite_allow_on_failure(self, tmp_path: Path) -> None:
        before = self._staged_repo(tmp_path)
        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--allow-on-failure'],
            stdin=_pretooluse_payload('git commit --amend=1 -m x', tmp_path),
        )
        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert '--amend=1' in reason
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py'])
        assert staged.stdout == before
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unlisted_negation_denies_before_repository_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository-dependent operation was called')

        monkeypatch.setattr('defsort._hook.discover_repo_root', fail_if_called)
        monkeypatch.setattr('defsort._hook.run_pre_commit_adds', fail_if_called)
        monkeypatch.setattr('defsort._hook.get_staged_py_info', fail_if_called)

        decision = _pre_tool(
            json.loads(
                _pretooluse_payload(
                    'git add test.py && git commit --short --no-dry-run',
                    tmp_path,
                )
            )
        )

        reason = _assert_classification_denied(decision)
        assert 'option --no-dry-run' in reason
        assert _assert_denied_output(capsys) == reason


class TestResolveAndSort:
    def test_already_sorted_skips(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_ALREADY_ORDERED,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.SKIP

    def test_blocker_returns_block(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_INTERSTITIAL,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.BLOCK
        assert outcome.blocker_message is not None
        assert 'code between definitions' in outcome.blocker_message

    def test_decode_failed_with_guard_skips(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        bl_entry = BlobReadResult(status=BlobReadStatus.DECODE_FAILED)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(max_reorder_count=5),
            display_path='test.py',
            baseline_entry=bl_entry,
            is_new=False,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.SKIP
        assert outcome.skip_message is not None
        assert 'unable to read baseline' in outcome.skip_message
        assert 'unable to read baseline' in capsys.readouterr().err

    def test_guard_triggered_skips(self, capsys: pytest.CaptureFixture[str]) -> None:
        baseline = 'def c():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_entry = BlobReadResult(status=BlobReadStatus.CONTENT, content=baseline)
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            baseline,
            file_cfg=ResolvedConfig(max_reorder_count=0),
            display_path='test.py',
            baseline_entry=bl_entry,
            is_new=False,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.SKIP
        assert 'would move' in capsys.readouterr().err

    def test_infra_error_with_incremental_skips(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=True)
        bl_entry = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=bl_entry,
            is_new=False,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.SKIP

    def test_new_file_full_sort(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.CHANGE
        assert outcome.new_source is not None
        assert outcome.new_source.index('alpha') < outcome.new_source.index('bravo')

    def test_renamed_incremental_skips(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=True)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=False,
            is_renamed=True,
        )
        assert outcome.disposition is _Disposition.SKIP

    def test_syntax_error_skips(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_SYNTAX_ERROR,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
            is_renamed=False,
        )
        assert outcome.disposition is _Disposition.SKIP


class TestResolveHookContext:
    def test_failure_override_or_merged(self, tmp_path: Path) -> None:
        result = _resolve_hook_context('cwd', str(tmp_path), allow_on_failure=True)
        assert result.allow_on_failure is True

    def test_incremental_defaults_false(self, tmp_path: Path) -> None:
        result = _resolve_hook_context('cwd', str(tmp_path))
        assert result.incremental is False

    def test_incremental_override(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nincremental = true\n')
        result = _resolve_hook_context(
            'cwd', str(tmp_path), overrides=SortOverrides(incremental=False)
        )
        assert result.incremental is False


class TestPreToolUseSubprocess:
    @pytest.mark.parametrize('replay_add', [False, True])
    def test_blocked_commit_reports_retained_replayed_staging(
        self, tmp_path: Path, replay_add: bool
    ) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'test.py').write_bytes(_SRC_INTERSTITIAL.encode('utf-8'))
        if not replay_add:
            run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        command = 'git commit -m test'
        if replay_add:
            command = f'git add test.py && {command}'
        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=json.dumps(
                {
                    'hook_event_name': 'PreToolUse',
                    'tool_name': 'Bash',
                    'tool_input': {'command': command},
                    'cwd': str(tmp_path),
                }
            ),
        )
        reason = _assert_denied((result_status, result_stdout, result_stderr), prefix='ERROR:')
        assert 'code between definitions' in reason
        assert ('Any files staged by the preceding git add remain staged.' in reason) is replay_add
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged == _SRC_INTERSTITIAL.encode()
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_INTERSTITIAL

    def test_compound_add_sorts_exact_worktree_before_replayed_add(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\n \t\rdef alpha():\n    pass\r\n')

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '-o', 'alphabetical'],
            stdin=_pretooluse_payload('git add test.py && git commit -m test', tmp_path),
        )

        expected = b'def alpha():\n    pass\n \t\rdef bravo():\r\n    pass\r\n'
        assert result_status == 0
        assert f.read_bytes() == expected
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged == expected

    def test_compound_add_with_dash_c_sorts(self, tmp_path: Path) -> None:
        # Create a subdirectory with a git repo
        sub = tmp_path / 'sub'
        sub.mkdir()
        _init_git_repo(sub, '# placeholder\n')
        f = sub / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # The payload cwd is the PARENT directory (tmp_path).
        # The command uses -C sub to target the repo inside sub/.
        # find_pre_commit_adds captures ['git', '-C', 'sub', 'add', 'test.py'].
        # The bug: run_pre_commit_adds receives cwd=resolved (sub/), making
        # git resolve -C sub relative to sub/ -> sub/sub/ (does not exist).
        # The fix: pass payload_cwd (tmp_path) so -C sub resolves correctly.
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {
                    'command': 'git -C sub add test.py && git -C sub commit -m "test"',
                },
                'cwd': str(tmp_path),
            }
        )
        result_status, result_stdout, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        # Verify index has sorted content
        staged = decode_text(run_setup(['git', '-C', str(sub), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_crlf_blob_with_lf_worktree_no_text_normalization(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        ga = tmp_path / '.gitattributes'
        ga.write_text('*.py -text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add gitattributes'])
        # Stage a CRLF file that needs sorting
        f = tmp_path / 'test.py'
        crlf_content = b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n'
        f.write_bytes(crlf_content)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # Rewrite worktree to LF (identical after normalization, different raw bytes)
        lf_content = crlf_content.replace(b'\r\n', b'\n')
        f.write_bytes(lf_content)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # Worktree should NOT be synced (newline mismatch = unstaged change).
        # The hook must detect that LF worktree != CRLF staged blob and skip
        # the sync, leaving the unsorted LF content untouched.
        raw = f.read_bytes()
        assert raw == lf_content, 'worktree was overwritten despite newline mismatch'

    def test_git_clean_raw_different_worktree_sorted_independently(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / '.gitattributes').write_text('*.py text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add attributes'])
        f = tmp_path / 'test.py'
        staged_original = b'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        worktree_original = b'def bravo():\r\n    pass\r\n\n\ndef alpha():\r\n    pass\n'
        f.write_bytes(staged_original)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        f.write_bytes(worktree_original)
        clean = run_setup(
            ['git', '-C', str(tmp_path), 'diff', '--quiet', '--', 'test.py'], check=False
        )
        assert clean.returncode == 0, process_transcript(clean)

        result_status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude', '-o', 'alphabetical'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert result_status == 0
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert staged == b'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        assert f.read_bytes() == b'def alpha():\r\n    pass\r\n\n\ndef bravo():\r\n    pass\n'
        clean = run_setup(
            ['git', '-C', str(tmp_path), 'diff', '--quiet', '--', 'test.py'], check=False
        )
        assert clean.returncode == 0, process_transcript(clean)

    def test_mixed_blob_preserved_no_text_normalization(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        # Configure no text normalization for .py files
        ga = tmp_path / '.gitattributes'
        ga.write_text('*.py -text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add gitattributes'])
        # Stage a mixed-ending file that needs sorting (bravo before alpha)
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\n \t\rdef alpha():\n    pass\r\n')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        # Check staged blob (raw bytes -- NOT text=True)
        blob_result = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py'])
        blob_bytes = blob_result.stdout
        assert blob_bytes == b'def alpha():\n    pass\n \t\rdef bravo():\r\n    pass\r\n'
        # Worktree must be clean
        diff_result = run_setup(
            ['git', '-C', str(tmp_path), 'diff', '--quiet', '--', 'test.py'], check=False
        )
        assert diff_result.returncode == 0, (
            f'worktree is dirty after CRLF sort\n{process_transcript(diff_result)}'
        )

    def test_sync_only_resort_does_not_duplicate_banner_diagnostic(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / '.gitattributes').write_text('*.py text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add attributes'])
        source = (
            'def zulu():\n    pass\n\n\ndef yankee():\n    pass\n\n\n'
            '# --------\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        )
        expected = (
            b'def yankee():\r\n    pass\r\n\r\n\r\ndef zulu():\r\n    pass\r\n\r\n\r\n'
            b'# --------\r\n\r\n\r\ndef alpha():\r\n    pass\r\n\r\n\r\n'
            b'def bravo():\r\n    pass\r\n'
        )
        f = tmp_path / 'test.py'
        f.write_bytes(source.encode())
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        f.write_bytes(source.replace('\n', '\r\n').encode())
        clean = run_setup(
            ['git', '-C', str(tmp_path), 'diff', '--quiet', '--', 'test.py'], check=False
        )
        assert clean.returncode == 0, process_transcript(clean)

        result_status, _, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude', '-o', 'alphabetical'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert result_status == 0
        assert result_stderr.count('sort constrained by 1 banner') == 1
        assert f.read_bytes() == expected

    def test_worktree_sync_preserves_crlf(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        raw = f.read_bytes()
        without_crlf = raw.replace(b'\r\n', b'<NL>')
        assert b'\n' not in without_crlf, 'bare LF found -- CRLF was not preserved'
        assert b'\r\n' in raw
        # Worktree must be clean relative to index
        diff_result = run_setup(
            ['git', '-C', str(tmp_path), 'diff', '--quiet', '--', 'test.py'], check=False
        )
        assert diff_result.returncode == 0, (
            f'worktree is dirty after CRLF sort\n{process_transcript(diff_result)}'
        )


class TestRunHook:
    @pytest.mark.parametrize('exception_type', [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize('boundary', ['metadata', 'parse', 'input', 'render', 'response'])
    def test_adapter_process_control_exceptions_propagate(
        self,
        boundary: str,
        exception_type: type[BaseException],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        failure = exception_type('stop')
        parsed: object = HostInput('pre_tool', None, '.', None)
        response: object = HostResponse('', '', 0)
        if boundary == 'parse':
            parsed = failure
        elif boundary == 'input':
            parsed = _AdapterFields({'command_cwd': failure})
        elif boundary == 'render':
            response = failure
        elif boundary == 'response':
            response = _AdapterFields({'stdout': 'partial', 'stderr': failure, 'exit_code': 0})
        adapter = _AdapterProbe(parsed, response)
        _install_adapter_probe(
            monkeypatch,
            _AdapterFields({'requires_event': failure}) if boundary == 'metadata' else adapter,
        )

        with pytest.raises(exception_type, match='stop'):
            run_hook('{}', host='probe')
        assert capsys.readouterr() == ('', '')

    def test_antigravity_null_tool_call_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _run_in_process(json.dumps({'toolCall': None}), 'antigravity')
        assert result == 0
        assert capsys.readouterr().out == ''

    @pytest.mark.parametrize('provider', ['codex', 'antigravity'])
    def test_banner_mode_survives_posttooluse_provider_dispatch(
        self,
        provider: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[BannerMode | None] = []
        original_resolver = _resolve_hook_context

        def capture_resolver(
            cwd: str,
            repo_root: str,
            *,
            overrides: SortOverrides | None = None,
            allow_on_failure: bool = False,
        ) -> HookContext:
            mode = overrides.banners if overrides is not None else None
            assert mode is None or mode in BANNER_MODE_VALUES
            captured.append(mode)
            return original_resolver(
                cwd,
                repo_root,
                overrides=overrides,
                allow_on_failure=allow_on_failure,
            )

        _init_empty_git_repo(tmp_path)
        (tmp_path / 'test.py').write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        monkeypatch.setattr('defsort._hook.is_hook_enabled', _hook_enabled)
        monkeypatch.setattr('defsort._hook._resolve_hook_context', capture_resolver)
        payload = (
            json.dumps(
                {
                    'hook_event_name': 'PostToolUse',
                    'tool_name': 'apply_patch',
                    'tool_input': {
                        'command': (
                            '*** Begin Patch\n'
                            '*** Update File: test.py\n'
                            '@@\n'
                            '-def alpha():\n'
                            '+def alpha():\n'
                            '*** End Patch\n'
                        )
                    },
                    'cwd': str(tmp_path),
                    'model': 'gpt-5.5',
                    'turn_id': 'turn-test',
                }
            )
            if provider == 'codex'
            else _antigravity_payload(
                'write_to_file',
                {'TargetFile': str(tmp_path / 'test.py')},
                tmp_path,
            )
        )

        result = _run_in_process(
            payload, provider, event='PostToolUse', overrides=SortOverrides(banners='ignore')
        )

        assert result == EXIT_OK
        assert captured == ['ignore']

    @pytest.mark.parametrize('provider', ['codex', 'antigravity'])
    def test_banner_mode_survives_pretooluse_provider_dispatch(
        self,
        provider: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[BannerMode | None] = []
        original_resolver = _resolve_hook_context

        def capture_resolver(
            cwd: str,
            repo_root: str,
            *,
            overrides: SortOverrides | None = None,
            allow_on_failure: bool = False,
        ) -> HookContext:
            mode = overrides.banners if overrides is not None else None
            assert mode is None or mode in BANNER_MODE_VALUES
            captured.append(mode)
            return original_resolver(
                cwd,
                repo_root,
                overrides=overrides,
                allow_on_failure=allow_on_failure,
            )

        _init_empty_git_repo(tmp_path)
        monkeypatch.setattr('defsort._hook.is_hook_enabled', _hook_enabled)
        monkeypatch.setattr('defsort._hook._resolve_hook_context', capture_resolver)
        payload = (
            _codex_pretooluse_payload(f'git -C {shell_path(tmp_path)} commit -m test', tmp_path)
            if provider == 'codex'
            else _antigravity_pretooluse_payload('git commit -m test', tmp_path)
        )

        result = _run_in_process(payload, provider, overrides=SortOverrides(banners='strip'))

        assert result == EXIT_OK
        assert captured == ['strip']

    @pytest.mark.parametrize('exception_type', [KeyboardInterrupt, SystemExit])
    def test_failure_renderer_process_control_exceptions_propagate(
        self,
        exception_type: type[BaseException],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class InterruptedFailureAdapter(_AdapterProbe):
            def render_failure(self, reason: str) -> object:
                del reason
                raise exception_type('stop')

        adapter = InterruptedFailureAdapter(
            HostInput('pre_tool', None, '.', None), RuntimeError('render failed')
        )
        _install_adapter_probe(monkeypatch, adapter)
        with pytest.raises(exception_type, match='stop'):
            run_hook('{}', host='probe')
        assert capsys.readouterr() == ('', '')

    def test_invalid_json_exit_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _run_in_process('not json')
        assert result == 0
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''

    @pytest.mark.parametrize('raises', [False, True])
    @pytest.mark.parametrize(
        'field', ['requires_event', 'command_cwd', 'event', 'command', 'cwd', 'edited_paths']
    )
    def test_malformed_adapter_fields_fail_closed(
        self,
        field: str,
        raises: bool,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fields: dict[str, object] = {
            'event': 'post_tool' if field in {'cwd', 'edited_paths'} else 'pre_tool',
            'command': 'ls',
            'command_cwd': None,
            'cwd': '.',
            'edited_paths': (),
        }
        if field != 'requires_event':
            fields.pop(field)
            if raises:
                fields[field] = RuntimeError('first\nsecond')
        adapter = _AdapterProbe(_AdapterFields(fields), HostResponse('', '', 0))
        if field == 'requires_event':
            metadata: dict[str, object] = {'parse': adapter.parse}
            if raises:
                metadata[field] = RuntimeError('first\nsecond')
            _install_adapter_probe(monkeypatch, _AdapterFields(metadata))
        else:
            _install_adapter_probe(monkeypatch, adapter)

        assert run_hook('{}', host='probe\nname') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert len(captured.err.splitlines()) == 1
        assert captured.err.startswith("defsort: host 'probe\\x0aname'")
        assert 'Traceback' not in captured.err

    @pytest.mark.parametrize(
        ('field', 'value'),
        [('event', 'PostToolUse'), ('event', 42), ('command', 42)],
    )
    def test_invalid_adapter_values_fail_closed(
        self,
        field: str,
        value: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fields: dict[str, object] = {
            'event': 'pre_tool',
            'command': 'ls',
            'command_cwd': None,
            'cwd': '.',
            'edited_paths': (),
        }
        fields[field] = value
        _install_adapter_probe(
            monkeypatch, _AdapterProbe(_AdapterFields(fields), HostResponse('', '', 0))
        )

        assert run_hook('{}', host='probe') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err.startswith("defsort: host 'probe' failed to parse the payload:")

    @pytest.mark.parametrize(('field', 'value'), [('cwd', 42), ('edited_paths', (42,))])
    def test_malformed_post_tool_paths_fail_before_repository_work(
        self,
        field: str,
        value: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = tmp_path / 'mod.py'
        source.write_bytes(b'def zulu():\n    pass\n')
        fields: dict[str, object] = {
            'event': 'post_tool',
            'command_cwd': None,
            'cwd': str(tmp_path),
            'edited_paths': ('mod.py',),
        }
        fields[field] = value
        adapter = _AdapterProbe(_AdapterFields(fields), HostResponse('partial', '', 0))
        _install_adapter_probe(monkeypatch, adapter)

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository or source work was called')

        for name in ('discover_repo_root', 'read_file_result', 'write_source'):
            monkeypatch.setattr(hook_mod, name, fail_if_called)

        assert run_hook('{}', host='probe\nname') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert len(captured.err.splitlines()) == 1
        assert captured.err.startswith("defsort: host 'probe\\x0aname'")
        assert 'Traceback' not in captured.err
        assert source.read_bytes() == b'def zulu():\n    pass\n'

    @pytest.mark.parametrize('field', ['stdout', 'stderr', 'exit_code'])
    @pytest.mark.parametrize('failure', ['missing', 'raising', 'wrong_type'])
    def test_malformed_response_emits_no_partial_output(
        self,
        field: str,
        failure: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fields: dict[str, object] = {
            'stdout': '{"decision":"allow"}\n',
            'stderr': 'note\n',
            'exit_code': 0,
        }
        fields.pop(field)
        if failure == 'raising':
            fields[field] = RuntimeError('first\nsecond')
        elif failure == 'wrong_type':
            fields[field] = 'zero' if field == 'exit_code' else 7
        adapter = _AdapterProbe(HostInput('pre_tool', None, '.', None), _AdapterFields(fields))
        _install_adapter_probe(monkeypatch, adapter)

        assert run_hook('{}', host='probe\nname') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert len(captured.err.splitlines()) == 1
        assert captured.err.startswith("defsort: host 'probe\\x0aname'")
        assert 'Traceback' not in captured.err

    def test_malformed_response_recovers_with_valid_denial(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class RecoveringAdapter(_AdapterProbe):
            def render_failure(self, reason: str) -> HostResponse:
                return HostResponse('denied', reason, 2)

        adapter = RecoveringAdapter(
            HostInput('pre_tool', None, '.', None),
            _AdapterFields({'stdout': 'partial allow', 'stderr': ''}),
        )
        _install_adapter_probe(monkeypatch, adapter)
        assert run_hook('{}', host='probe') == 2
        assert capsys.readouterr() == (
            'denied',
            "defsort: host 'probe' failed to render the response",
        )

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    @pytest.mark.parametrize(
        'command',
        [
            'git add --pathspec-from-file=- < paths && git commit -m x',
            'PATH=./bin:$PATH git add mod.py && git commit -m x',
            'MODE=test@more git add mod.py && git commit -m x',
            'MODE="test more" git add mod.py && git commit -m x',
            'MODE=$((1+1)) git add mod.py && git commit -m x',
            'MODE=$(printf value) git add mod.py && git commit -m x',
            'PATH=${PATH:-/bin} git add mod.py && git commit -m x',
            'env MODE=test git add mod.py && git commit -m x',
            'git add mod.py 2> add.err && git commit -m x',
        ],
    )
    def test_non_replayable_add_compound_denies_before_repo_work(
        self,
        command: str,
        provider: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('repository-dependent operation was called')

        for name in (
            'discover_repo_root',
            'resolve_config_at_root',
            'run_pre_commit_adds',
            'is_hook_enabled',
        ):
            monkeypatch.setattr(hook_mod, name, fail_if_called)
        payload = _provider_pretooluse_payload(provider, command, tmp_path)

        result = _run_in_process(payload, provider)

        assert result == EXIT_OK
        _assert_denied_output(capsys, provider)

    def test_redirected_add_with_environment_prefix_denies_without_replay(
        self,
        tmp_path: Path,
    ) -> None:
        _init_empty_git_repo(tmp_path)
        target = tmp_path / 'mod.py'
        target.write_bytes(b'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n')
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        git_wrapper = bin_dir / 'git'
        git_wrapper.write_text('#!/bin/sh\n: > sentinel\nexit 1\n', encoding='utf-8')
        git_wrapper.chmod(0o755)
        sentinel = tmp_path / 'sentinel'

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload(
                'PATH=./bin git add mod.py < missing && git commit -m x', tmp_path
            ),
            cwd=tmp_path,
        )

        reason = _assert_denied((result_status, result_stdout, result_stderr))
        assert "operator '<'" in reason
        assert not sentinel.exists()

    @pytest.mark.parametrize('failure', ['missing', 'raising'])
    def test_unusable_renderers_fail_closed(
        self,
        failure: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        adapter = _AdapterProbe(HostInput('pre_tool', None, '.', None), HostResponse('', '', 0))
        fields: dict[str, object] = {'requires_event': False, 'parse': adapter.parse}
        if failure == 'raising':
            fields.update(
                render=RuntimeError('render failed'), render_failure=RuntimeError('failure failed')
            )
        _install_adapter_probe(monkeypatch, _AdapterFields(fields))
        assert run_hook('{}', host='probe') == 2
        assert capsys.readouterr() == (
            '',
            "defsort: host 'probe' failed to render the failure response\n",
        )


class TestSortWorktreeFiles:
    def test_already_sorted_no_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        mtime_before = f.stat().st_mtime
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result == Outcome('allow')
        # File should not be rewritten (mtime unchanged)
        assert f.stat().st_mtime == mtime_before
        assert f.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED

    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_baseline_failure_preserves_worktree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        allow_on_failure: bool,
    ) -> None:
        _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        mtime_before = target.stat().st_mtime_ns

        def fail_baselines(_repo_root: str, _paths: list[str]) -> None:
            return None

        monkeypatch.setattr('defsort._hook.batch_fetch_worktree_baselines', fail_baselines)

        result = _sort_worktree_files(
            _hook_ctx(tmp_path, incremental=True, allow_on_failure=allow_on_failure),
            ['test.py'],
        )

        assert result.decision == ('allow' if allow_on_failure else 'deny')
        assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert target.stat().st_mtime_ns == mtime_before
        captured = capsys.readouterr()
        assert captured.out == ''
        if allow_on_failure:
            assert captured.err == 'WARNING: defsort: failed to read baselines\n'
        else:
            assert captured.err == ''
            assert result.reason == 'defsort: failed to read baselines'

    def test_blocker_denies_without_modifying(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        original = f.read_text(encoding='utf-8')
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result.decision == 'deny'
        assert f.read_text(encoding='utf-8') == original

    def test_crlf_preserved(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_bytes(b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n')
        result = _sort_worktree_files(
            _hook_ctx(
                tmp_path,
                overrides=SortOverrides(definition_order='alphabetical'),
            ),
            ['test.py'],
        )
        assert result == Outcome('allow')
        raw = f.read_bytes()
        without_crlf = raw.replace(b'\r\n', b'<NL>')
        assert b'\n' not in without_crlf
        assert b'\r\n' in raw
        assert raw.index(b'alpha') < raw.index(b'bravo')

    def test_empty_list_succeeds(self, tmp_path: Path) -> None:
        result = _sort_worktree_files(_hook_ctx(tmp_path), [])
        assert result == Outcome('allow')

    def test_multi_file_write_failure_best_effort(self, tmp_path: Path) -> None:
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # Make b unwritable so the second write fails
        b.chmod(0o444)
        try:
            result = _sort_worktree_files(
                _hook_ctx(tmp_path, allow_on_failure=True), ['a.py', 'b.py']
            )
            assert result == Outcome('allow')
            # a.py stays sorted (no rollback in best-effort mode)
            assert a.read_text(encoding='utf-8') != _SRC_NEEDS_SORT
            assert b.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        finally:
            b.chmod(0o644)

    def test_multi_file_write_failure_rollback(self, tmp_path: Path) -> None:
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        original_a = a.read_text(encoding='utf-8')
        # Make b unwritable so the second write fails
        b.chmod(0o444)
        try:
            result = _sort_worktree_files(_hook_ctx(tmp_path), ['a.py', 'b.py'])
            assert result.decision == 'deny'
            # a.py should be rolled back to original (not left sorted)
            assert a.read_text(encoding='utf-8') == original_a
            assert b.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        finally:
            b.chmod(0o644)

    def test_rollback_restore_failure_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        c = tmp_path / 'c.py'
        original = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        a.write_bytes(original.encode('utf-8'))
        b.write_bytes(original.encode('utf-8'))
        c.write_bytes(original.encode('utf-8'))

        def selective_write(path: Path, content: str, *, encoding: str = 'utf-8') -> None:
            if path.name == 'c.py':
                raise OSError('injected write failure')
            if path.name == 'a.py' and content == original:
                raise OSError('injected restore failure')
            path.write_bytes(content.encode(encoding))

        monkeypatch.setattr('defsort._hook.write_source', selective_write)

        result = _sort_worktree_files(
            _hook_ctx(
                tmp_path,
                overrides=SortOverrides(definition_order='alphabetical'),
            ),
            ['a.py', 'b.py', 'c.py'],
        )

        assert result.decision == 'deny'
        assert a.read_text(encoding='utf-8') != original
        assert b.read_text(encoding='utf-8') == original
        assert c.read_text(encoding='utf-8') == original
        assert capsys.readouterr().out == ''
        assert result.reason is not None
        assert 'cannot write c.py' in result.reason
        assert 'rollback failed for: a.py' in result.reason
        assert 'sorted content kept' in result.reason

    def test_sorts_files_on_disk(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result == Outcome('allow')
        content = f.read_text(encoding='utf-8')
        assert content.index('alpha') < content.index('bravo')

    def test_symlink_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / 'target.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        link = tmp_path / 'link.py'
        link.symlink_to(f)
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['link.py'])
        assert result == Outcome('allow')
        assert link.is_symlink()
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_syntax_error_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_SYNTAX_ERROR, encoding='utf-8')
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result == Outcome('allow')
        assert f.read_text(encoding='utf-8') == _SRC_SYNTAX_ERROR

    def test_unreadable_file_denies_by_default(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('read denied')):
            result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result == Outcome('deny', 'defsort: cannot read test.py')
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unreadable_file_skips_with_failure_override(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('read denied')):
            result = _sort_worktree_files(_hook_ctx(tmp_path, allow_on_failure=True), ['test.py'])
        assert result == Outcome('allow')
        assert capsys.readouterr().err == 'WARNING: defsort: cannot read test.py\n'
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unwritable_file_denies_on_write(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        # Make file readable but not writable
        f.chmod(0o444)
        try:
            result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
            assert result.decision == 'deny'
        finally:
            f.chmod(0o644)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unwritable_file_skips_write_with_failure_override(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        f.chmod(0o444)
        try:
            result = _sort_worktree_files(_hook_ctx(tmp_path, allow_on_failure=True), ['test.py'])
            assert result == Outcome('allow')
        finally:
            f.chmod(0o644)
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_write_failure_denies_and_preserves_all_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        original = b'def bravo():\r\n    pass\n \t\rdef alpha():\n    pass\r\n'
        a.write_bytes(original)
        b.write_bytes(original)
        real_replace = Path.replace

        def fail_replace_for_b(self: Path, target: str | Path) -> Path:
            if str(target).endswith('b.py'):
                raise OSError('injected replace failure')
            return real_replace(self, target)

        monkeypatch.setattr('defsort._files.Path.replace', fail_replace_for_b)

        result = _sort_worktree_files(
            _hook_ctx(
                tmp_path,
                overrides=SortOverrides(definition_order='alphabetical'),
            ),
            ['a.py', 'b.py'],
        )

        assert result.decision == 'deny'
        assert a.read_bytes() == original
        assert b.read_bytes() == original


class TestTrySort:
    def test_already_sorted_returns_none(self) -> None:
        result = _try_sort(
            _SRC_ALREADY_ORDERED,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker is None
        assert result._syntax_error is False

    def test_duplicates_blocker(self) -> None:
        result = _try_sort(
            _SRC_DUPLICATES,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'duplicates'
        assert result._syntax_error is False

    def test_forward_ref_blocker(self) -> None:
        result = _try_sort(
            _SRC_FORWARD_REF,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'forward_refs'
        assert result._syntax_error is False

    def test_interstitial_blocker(self) -> None:
        result = _try_sort(
            _SRC_INTERSTITIAL,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'interstitial'
        assert result._syntax_error is False

    def test_returns_sorted_content(self) -> None:
        result = _try_sort(
            _SRC_NEEDS_SORT,
            policy=SortPolicy(),
        )
        assert result.new_source is not None
        assert result.blocker is None
        assert result._syntax_error is False

    def test_syntax_error_returns_skip(self) -> None:
        result = _try_sort(
            _SRC_SYNTAX_ERROR,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker is None
        assert result._syntax_error is True


class _AdapterFields:
    def __init__(self, fields: dict[str, object]) -> None:
        self._fields = fields

    def __getattr__(self, name: str) -> object:
        if name not in self._fields:
            raise AttributeError(name)
        value = self._fields[name]
        if isinstance(value, BaseException):
            raise value
        return value


class _AdapterProbe:
    requires_event = False

    def __init__(self, parsed: object, response: object) -> None:
        self._parsed = parsed
        self._response = response

    def parse(self, payload: Mapping[str, object], event: str | None) -> object:
        del payload, event
        if isinstance(self._parsed, BaseException):
            raise self._parsed
        return self._parsed

    def render(self, result: HookResult) -> object:
        del result
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    def render_failure(self, reason: str) -> object:
        del reason
        return self._response


def _antigravity_payload(
    tool_name: str,
    args: dict[str, object],
    workspace: Path,
) -> str:
    return json.dumps(
        {
            'toolCall': {
                'name': tool_name,
                'args': args,
            },
            'workspacePaths': [str(workspace)],
        }
    )


def _antigravity_pretooluse_payload(cmd: str, cwd: Path) -> str:
    return _antigravity_payload(
        'run_command',
        {'CommandLine': cmd, 'Cwd': str(cwd)},
        cwd,
    )


def _assert_classification_denied(decision: Outcome) -> str:
    assert decision.decision == 'deny'
    assert decision.reason is not None
    assert decision.reason.startswith(_REASON_PREFIX)
    return decision.reason


def _codex_pretooluse_payload(cmd: str, cwd: Path) -> str:
    return json.dumps(
        {
            'hook_event_name': 'PreToolUse',
            'turn_id': 'turn-test',
            'tool_name': 'Bash',
            'tool_use_id': 'tool-test',
            'tool_input': {'command': cmd},
            'cwd': str(cwd),
            'model': 'codex-test',
            'permission_mode': 'workspace-write',
        }
    )


def _deny_reason(stdout: str, stderr: str, provider: str, prefix: str) -> str:
    data = json.loads(stdout)
    if provider == 'antigravity':
        assert data['decision'] == 'deny'
        reason = str(data['reason'])
    else:
        hook = data['hookSpecificOutput']
        assert hook['permissionDecision'] == 'deny'
        reason = str(hook['permissionDecisionReason'])
    assert reason.startswith(prefix)
    assert stderr == f'{reason}\n'
    return reason


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain non-UTF-8 bytes')
class TestSurrogatePathCommit:
    def test_commit_with_non_utf8_indexed_path_not_crashed(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path, '# placeholder\n')
        blob = (
            run_setup(
                ['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'],
                stdin=b'def b():\n    return a()\n\n\ndef a():\n    return 1\n',
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
                '100644,' + blob + ',weird\udcff.py',
            ]
        )
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m test'},
                'cwd': str(tmp_path),
            }
        )
        result_status, _, result_stderr = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert 'Traceback' not in result_stderr
        listed = run_setup(['git', '-C', str(tmp_path), 'ls-files', '-z'])
        assert b'weird\xff.py\0' in listed.stdout
        staged = run_setup(['git', '-C', str(tmp_path), 'show', ':weird\udcff.py'])
        assert staged.stdout == b'def a():\n    return 1\n\n\ndef b():\n    return a()\n'


def _hook_ctx(
    tmp_path: Path,
    *,
    allow_on_failure: bool = False,
    incremental: bool = False,
    overrides: SortOverrides | None = None,
) -> HookContext:
    return HookContext(
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
        allow_on_failure=allow_on_failure,
        incremental=incremental,
        overrides=SortOverrides() if overrides is None else overrides,
    )


def _git_config(path: Path, key: str, value: str) -> None:
    run_setup(['git', 'config', key, value], cwd=path)


def _hook_enabled(_cwd: str | None = None) -> bool:
    return True


def _host_kwargs(provider: str, *, event: str = 'PreToolUse') -> dict[str, str]:
    if provider == 'antigravity':
        return {'host': 'agy', 'event': event}
    return {'host': provider}


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('fallback', ['warn', 'full-sort'])
def test_layout_blocker_denies_staged_sort_atomically(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fallback: FallbackMode,
    *,
    incremental: bool,
    allow_on_failure: bool,
) -> None:
    baseline = (
        'class C:\n    def alpha(self):\n        pass\n'
        '    # --------\n    def zulu(self):\n        pass\n'
    )
    source = baseline + '    # for bravo\n    def bravo(self):\n        pass\n'
    _init_git_repo(tmp_path, baseline)
    safe_path = tmp_path / 'a.py'
    safe_path.write_text('def zulu():\n    pass\n', encoding='utf-8')
    run_setup(['git', '-C', str(tmp_path), 'add', '.'])
    run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'baseline'])
    original = source.replace('\n', '\r\n').encode()
    path = tmp_path / 'test.py'
    path.write_bytes(original)
    safe_source = b'def zulu():\n    pass\ndef alpha():\n    pass\n'
    safe_path.write_bytes(safe_source)
    run_setup(['git', '-C', str(tmp_path), 'add', '.'])
    path.write_bytes(original + b'# unstaged\r\n')
    before = _repo_state(tmp_path)

    result = run_hook(
        _pretooluse_payload('git commit -m test', tmp_path),
        host='claude',
        overrides=SortOverrides(incremental=incremental, incremental_fallback=fallback),
        allow_on_failure=allow_on_failure,
    )

    assert result == EXIT_OK
    reason = _assert_denied_output(capsys, prefix='ERROR:')
    assert 'layout and comment ownership' in reason
    assert 'blank lines' in reason
    assert _repo_state(tmp_path) == before
    assert safe_path.read_bytes() == safe_source


def _install_adapter_probe(monkeypatch: pytest.MonkeyPatch, adapter: object) -> None:
    def load_probe(_host: str) -> HookAdapter:
        # Deliberately cross the third-party protocol boundary in one place.
        return cast('HookAdapter', adapter)

    monkeypatch.setattr(front_mod, 'load_adapter', load_probe)


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('incremental', [False, True])
def test_posttooluse_layout_blocker_preserves_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], provider: str, *, incremental: bool
) -> None:
    baseline = (
        'class A:\n    def zulu(self):\n        pass\n\n'
        'class C:\n    def alpha(self):\n        pass\n'
        '    # --------\n    def zulu(self):\n        pass\n'
    )
    source = (
        baseline.replace('class C:', '    def alpha(self):\n        pass\n\nclass C:')
        + '    # for bravo\n    def bravo(self):\n        pass\n'
    )
    _init_git_repo(tmp_path, baseline)
    run_setup(['git', '-C', str(tmp_path), 'add', '.'])
    run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'baseline'])
    original = source.replace('\n', '\r\n').encode()
    path = tmp_path / 'test.py'
    path.write_bytes(original)

    result = run_hook(
        _posttooluse_payload(provider, path, tmp_path),
        host=provider,
        overrides=SortOverrides(incremental=incremental),
        allow_on_failure=True,
    )

    assert result == EXIT_OK
    assert path.read_bytes() == original
    captured = capsys.readouterr()
    specific = json.loads(captured.out)['hookSpecificOutput']
    assert specific['hookEventName'] == 'PostToolUse'
    assert 'cannot preserve' in specific['additionalContext']
    assert specific['additionalContext'] == captured.err.rstrip('\n')
    assert 'layout and comment ownership' in captured.err
    assert 'blank lines' in captured.err
    assert 'Traceback' not in captured.err


def _posttooluse_payload(provider: str, path: Path, cwd: Path) -> str:
    if provider == 'claude':
        return json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(path)},
                'cwd': str(cwd),
            }
        )
    if provider == 'codex':
        return json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'apply_patch',
                'tool_input': {
                    'command': f'*** Begin Patch\n*** Update File: {path}\n*** End Patch\n'
                },
                'cwd': str(cwd),
                'model': 'codex-test',
                'turn_id': 'turn-test',
            }
        )
    return _antigravity_payload(
        'write_to_file',
        {'TargetFile': str(path), 'CodeContent': path.read_text(encoding='utf-8')},
        cwd,
    )


def _run_in_process(
    stdin_data: str,
    provider: str = 'claude',
    *,
    event: str = 'PreToolUse',
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> int:
    kwargs = _host_kwargs(provider, event=event)
    return run_hook(
        stdin_data,
        host=kwargs['host'],
        event=kwargs.get('event'),
        overrides=overrides,
        allow_on_failure=allow_on_failure,
    )


def _pretooluse_payload(cmd: str, cwd: Path) -> str:
    return json.dumps(
        {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': cmd},
            'cwd': str(cwd),
        }
    )


def _provider_pretooluse_payload(
    provider: str,
    command: str,
    cwd: Path,
    *,
    codex_command: str | None = None,
) -> str:
    if provider == 'codex':
        return _codex_pretooluse_payload(command if codex_command is None else codex_command, cwd)
    if provider == 'antigravity':
        return _antigravity_pretooluse_payload(command, cwd)
    assert provider == 'claude'
    return _pretooluse_payload(command, cwd)


def _repo_state(repo: Path) -> tuple[bytes, bytes]:
    index = run_setup(['git', '-C', str(repo), 'ls-files', '-s']).stdout
    return index, (repo / 'test.py').read_bytes()


def _pre_tool(
    payload: dict[str, object],
    *,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> Outcome:
    adapter = load_adapter('claude')
    parsed = adapter.parse(payload, None)
    assert parsed is not None
    if parsed.command is None:
        return Outcome('allow')
    outcome = front_mod._run_pre_tool_use(
        parsed.command,
        parsed.command_cwd,
        overrides=overrides,
        allow_on_failure=allow_on_failure,
    )
    response = adapter.render(outcome)
    sys.stdout.write(response.stdout)
    sys.stderr.write(response.stderr)
    return outcome


def _setup_include_failure_repo(tmp_path: Path) -> tuple[Path, bytes]:
    _init_git_repo(tmp_path, '# initial\n')
    pathspec = tmp_path / 'pathspec.py'
    staged = tmp_path / 'staged.py'
    pathspec.write_text('# initial\n', encoding='utf-8')
    staged.write_text('# initial\n', encoding='utf-8')
    run_setup(['git', '-C', str(tmp_path), 'add', 'pathspec.py', 'staged.py'])
    run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add fixtures'])
    pathspec.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    staged.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    run_setup(['git', '-C', str(tmp_path), 'add', 'staged.py'])
    staged_before = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
    return pathspec, staged_before


def _setup_incremental_precedence_repo(tmp_path: Path) -> None:
    _init_empty_git_repo(tmp_path)
    baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
    (tmp_path / 'mod.py').write_text(baseline, encoding='utf-8')
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    (tmp_path / 'mod.py').write_text(
        _INCREMENTAL_PRECEDENCE_CURRENT,
        encoding='utf-8',
    )
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)


def _setup_incremental_repo(tmp_path: Path) -> None:
    _init_empty_git_repo(tmp_path)
    baseline = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
    (tmp_path / 'mod.py').write_text(baseline)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    # Add a new def (aardvark) at the end -- should sort before alpha
    new_content = baseline + '\n\ndef aardvark():\n    pass\n'
    (tmp_path / 'mod.py').write_text(new_content)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)


def _stage_unsorted(repo: Path) -> None:
    _init_git_repo(repo, _SRC_NEEDS_SORT)
    _git_config(repo, 'defsort.hook', 'true')
    run_setup(['git', '-C', str(repo), 'add', 'test.py'])


def _post_tool(payload: dict[str, object], *, overrides: SortOverrides | None = None) -> int:
    host = 'codex' if payload.get('tool_name') == 'apply_patch' else 'claude'
    return run_hook(
        json.dumps({'hook_event_name': 'PostToolUse', **payload}),
        host=host,
        overrides=overrides,
    )


def _assert_denied_output(
    capsys: pytest.CaptureFixture[str],
    provider: str = 'claude',
    prefix: str = _REASON_PREFIX,
) -> str:
    captured = capsys.readouterr()
    return _deny_reason(captured.out, captured.err, provider, prefix)


def test_adapter_flow_requires_explicit_agent() -> None:
    status, _, stderr = invoke_cli(['hook'], stdin='{}')
    assert status == 2
    assert '--agent' in stderr


def test_adapter_flow_third_party_combines_two_syntax_errors(
    tmp_path: Path,
    distribution_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_distribution(
        distribution_root,
        'defsort-host-example',
        {'example': 'tests.hosts_example.adapter:ExampleAdapter'},
    )
    paths = [tmp_path / 'first.py', tmp_path / 'second.py']
    source = 'def broken(:\n'
    for path in paths:
        path.write_text(source, encoding='utf-8')
    payload = json.dumps(
        {
            'hook_event_name': 'PostToolUse',
            'cwd': str(tmp_path),
            'changed_files': [str(path) for path in paths],
        }
    )
    assert run_hook(payload, host='example') == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        'example': {
            'decision': 'allow',
            'reason': '\n'.join(
                f'Cannot sort {path.as_posix()}: invalid syntax (line 1).' for path in paths
            ),
        },
    }
    assert captured.err == ''
    assert [path.read_text(encoding='utf-8') for path in paths] == [source, source]


def test_existing_nested_repo_standalone_commit_keeps_processing(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path, '# outer baseline\n')
    child = tmp_path / 'child'
    child.mkdir()
    _init_git_repo(child, _SRC_NEEDS_SORT)
    run_setup(['git', '-C', str(child), 'add', 'test.py'])

    result_status, _, _ = invoke_cli(
        ['hook', '--agent', 'claude'],
        stdin=_pretooluse_payload('git -C child commit -m "next"', tmp_path),
        cwd=tmp_path,
    )

    assert result_status == 0
    staged = decode_text(run_setup(['git', '-C', str(child), 'show', ':test.py']).stdout)
    assert staged.index('alpha') < staged.index('bravo')


def test_hook_diagnostics_escape_toml_warning_before_splitting_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\n"bad\\u001b\\u0085\\n\\\\x1b" = true\n', encoding='utf-8'
    )
    path = tmp_path / 'ordered.py'
    path.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
    assert run_hook(_posttooluse_payload('claude', path, tmp_path), host='claude') == 0
    captured = capsys.readouterr()
    assert captured.err == (
        'Warning: Unknown [tool.defsort] key: bad\\x1b\\x85\\x0a\\x1b, ignoring\n'
    )
    assert json.loads(captured.out) == {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': captured.err.rstrip('\n'),
        }
    }


def _assert_denied(
    result: tuple[int, str, str],
    provider: str = 'claude',
    prefix: str = _REASON_PREFIX,
) -> str:
    status, stdout, stderr = result
    assert status == EXIT_OK
    return _deny_reason(stdout, stderr, provider, prefix)


def _staged_text(repo: Path, path: str = 'mod.py') -> str:
    return decode_text(run_setup(['git', 'show', f':{path}'], cwd=repo).stdout)


def _staged_bytes(repo: Path, path: str = 'test.py') -> bytes:
    return run_setup(['git', '-C', str(repo), 'show', f':{path}']).stdout


@pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
def test_nested_bootstrap_denies_and_does_not_touch_outer_repo(
    tmp_path: Path,
    provider: str,
) -> None:
    _init_git_repo(tmp_path, _SRC_NEEDS_SORT)
    child = tmp_path / 'child'
    child.mkdir()
    path = child / 'example.py'
    path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    command = 'git init child && git -C child add example.py && git -C child commit -m "initial"'

    payload = _provider_pretooluse_payload(provider, command, tmp_path)

    result_status, result_stdout, result_stderr = invoke_cli(
        [
            'hook',
            '--agent',
            ('agy' if provider == 'antigravity' else provider),
            *(['--event', 'PreToolUse'] if provider == 'antigravity' else []),
        ],
        stdin=payload,
        cwd=tmp_path,
    )

    reason = _assert_denied((result_status, result_stdout, result_stderr), provider)
    assert "segment 'git init child'" in reason
    staged = run_setup(['git', '-C', str(tmp_path), 'diff', '--cached', '--name-only'])
    assert decode_text(staged.stdout) == ''
    assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
    assert not (child / '.git').exists()


@pytest.mark.parametrize(
    'command',
    [
        'git init && git commit -m x',
        'git commit --future-option -m x',
        'git diff --check && git add test.py && git commit -m x',
        'git add test.py && git diff --cached --check && git commit -m x',
        'git status && git commit -m x',
        'git log --oneline && git commit -m x',
        r'echo `g\\it commit -m x`',
        r'echo `git com\\mit -m x`',
        r'echo "`g\\it commit -m x`"',
        r'echo "`git com\\mit -m x`"',
    ],
)
def test_adapter_flow_denies_unsupported_before_repository_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    def repository_work_forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unsupported classification reached repository work')

    for name in ('is_hook_enabled', 'discover_repo_root', 'run_pre_commit_adds'):
        monkeypatch.setattr(hook_mod, name, repository_work_forbidden)
    result = run_hook(_pretooluse_payload(command, tmp_path), host='claude', allow_on_failure=True)
    captured = capsys.readouterr()
    assert result == 0
    assert captured.out, 'unsupported commit must produce a native denial'
    decision = json.loads(captured.out)['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny'
    assert decision['permissionDecisionReason'].startswith('defsort: unsupported commit command')


@pytest.mark.parametrize(
    ('command', 'decision'),
    [
        ("$'git' commit", 'deny'),
        ('git commit -m "$(cat <<\'MESSAGE\'\nsubject\nMESSAGE\n)"', 'allow'),
        ("cat > notes.md <<'EOF'\ngit log then commit\nEOF\n", 'allow'),
        ("cat > notes.md <<'EOF' && git commit -m x\nbody\nEOF\n", 'deny'),
    ],
)
def test_adapter_flow_applies_vendored_classifier_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    decision: str,
) -> None:
    def hook_disabled(_root: str | None = None) -> bool:
        return False

    def repository_work_forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('classification reached repository work')

    monkeypatch.setattr(hook_mod, 'is_hook_enabled', hook_disabled)
    for name in ('discover_repo_root', 'run_pre_commit_adds'):
        monkeypatch.setattr(hook_mod, name, repository_work_forbidden)
    result = run_hook(
        _pretooluse_payload(command, tmp_path), host='claude', allow_on_failure=False
    )
    captured = capsys.readouterr()
    assert result == 0
    if decision == 'deny':
        output = json.loads(captured.out)['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        assert output['permissionDecisionReason'].startswith(
            'defsort: unsupported commit command: '
        )
    else:
        assert captured.out == ''


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_adapter_flow_classifier_exception_always_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allow_on_failure: bool,
) -> None:
    def classification_failed(_command: str, _cwd: str, *, reason_prefix: str) -> None:
        assert reason_prefix == _REASON_PREFIX
        raise RuntimeError('classifier exploded')

    def repository_work_forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('failed classification reached repository work')

    monkeypatch.setattr(front_mod, 'classify_command', classification_failed)
    for name in ('is_hook_enabled', 'discover_repo_root', 'run_pre_commit_adds'):
        monkeypatch.setattr(hook_mod, name, repository_work_forbidden)
    result = run_hook(
        _pretooluse_payload('git commit -m x', tmp_path),
        host='claude',
        allow_on_failure=allow_on_failure,
    )
    captured = capsys.readouterr()
    assert result == 0
    output = json.loads(captured.out)['hookSpecificOutput']
    assert output['permissionDecision'] == 'deny'
    assert output['permissionDecisionReason'] == (
        'defsort: unexpected error during classification'
    )
    assert 'classifier exploded' in captured.err
    assert 'WARNING:' not in captured.err


@pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
def test_adapter_flow_builtin_preserves_syntax_feedback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], provider: str
) -> None:
    path = tmp_path / 'broken.py'
    source = 'def broken(:\n'
    path.write_text(source, encoding='utf-8')
    assert (
        _run_in_process(
            _posttooluse_payload(provider, path, tmp_path), provider, event='PostToolUse'
        )
        == 0
    )
    captured = capsys.readouterr()
    message = f'Cannot sort {path.as_posix()}: invalid syntax (line 1).'
    expected = (
        {'systemMessage': message}
        if provider == 'antigravity'
        else {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': message,
            }
        }
    )
    assert json.loads(captured.out) == expected
    assert captured.err == ''
    assert path.read_text(encoding='utf-8') == source


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain control characters')
def test_hook_diagnostics_escape_blocker_paths_and_keep_line_breaks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_empty_git_repo(tmp_path)
    run_setup(['git', '-C', str(tmp_path), 'config', 'core.precomposeunicode', 'false'])
    names = ['first\x1b\x85\n.py', 'second\x1c\r.py']
    for name in names:
        (tmp_path / name).write_text(_SRC_INTERSTITIAL, encoding='utf-8')
    run_setup(['git', '-C', str(tmp_path), 'add', '.'])
    assert run_hook(_pretooluse_payload('git commit -m x', tmp_path), host='claude') == 0
    captured = capsys.readouterr()
    reason = json.loads(captured.out)['hookSpecificOutput']['permissionDecisionReason']
    expected = '\n'.join(
        _BLOCKER_MSGS['interstitial'].format(path=name)
        for name in ['first\\x1b\\x85\\x0a.py', 'second\\x1c\\x0d.py']
    )
    assert reason == expected
    assert captured.err == f'{expected}\n'


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain control characters')
def test_hook_diagnostics_escape_syntax_feedback_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / 'broken\x1b\x85\n.py'
    path.write_text('def broken(:\n', encoding='utf-8')
    assert run_hook(_posttooluse_payload('claude', path, tmp_path), host='claude') == 0
    captured = capsys.readouterr()
    reason = json.loads(captured.out)['hookSpecificOutput']['additionalContext']
    assert reason == f'Cannot sort {tmp_path}/broken\\x1b\\x85\\x0a.py: invalid syntax (line 1).'
    assert captured.err == ''


class TestUnknownInvocationDirectory:
    def test_absolute_route_replays_an_add_that_stages_new_content(self, tmp_path: Path) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        _stage_unsorted(session_repo)
        _init_git_repo(target_repo, _SRC_NEEDS_SORT)
        _git_config(target_repo, 'defsort.hook', 'true')
        not_staged = decode_text(
            run_setup(['git', '-C', str(target_repo), 'diff', '--cached', '--name-only']).stdout
        )
        assert not_staged == ''
        session_before = _repo_state(session_repo)

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(target_repo)} add test.py && '
                f'git -C {shell_path(target_repo)} commit -m "test"',
                session_repo,
            ),
            cwd=session_repo,
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = _staged_text(target_repo, 'test.py')
        assert staged.index('alpha') < staged.index('bravo')
        assert _repo_state(session_repo) == session_before

    @pytest.mark.parametrize('form', ['dash_c', 'cd'])
    def test_absolute_route_sorts_only_the_target_regardless_of_process_cwd(
        self, tmp_path: Path, form: str
    ) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        elsewhere = tmp_path / 'c'
        elsewhere.mkdir()
        _stage_unsorted(session_repo)
        _stage_unsorted(target_repo)
        command = (
            f'git -C {shell_path(target_repo)} commit -m "test"'
            if form == 'dash_c'
            else f'cd {shell_path(target_repo)} && git commit -m "test"'
        )
        session_before = _repo_state(session_repo)

        result_status, result_stdout, result_stderr = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(command, session_repo),
            cwd=elsewhere,
        )

        assert (result_status, result_stderr) == (0, '')
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = _staged_text(target_repo, 'test.py')
        assert staged.index('alpha') < staged.index('bravo')
        assert _repo_state(session_repo) == session_before

    def test_claude_trusts_its_absolute_cwd_over_the_process_directory(
        self, tmp_path: Path
    ) -> None:
        tracked_repo = tmp_path / 'b'
        other_repo = tmp_path / 'a'
        _stage_unsorted(tracked_repo)
        _stage_unsorted(other_repo)
        other_before = _repo_state(other_repo)
        payload = json.dumps(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m "test"'},
                'cwd': str(tracked_repo),
            }
        )

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'claude'], stdin=payload, cwd=other_repo
        )

        assert result_status == 0
        assert json.loads(result_stdout) == _RECEIPT_ONE
        staged = _staged_text(tracked_repo, 'test.py')
        assert staged.index('alpha') < staged.index('bravo')
        assert _repo_state(other_repo) == other_before

    def test_codex_bare_commit_is_denied_and_touches_neither_repository(
        self, tmp_path: Path
    ) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        _stage_unsorted(session_repo)
        _stage_unsorted(target_repo)
        before = (_repo_state(session_repo), _repo_state(target_repo))

        result = run_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload('git commit -m "test"', session_repo),
            cwd=target_repo,
            expect=0,
        )

        assert result.returncode == 0
        assert json_stdout(result) == {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': _UNKNOWN_CWD_REASON,
            }
        }
        assert result.stderr == f'{_UNKNOWN_CWD_REASON}\n'
        assert (_repo_state(session_repo), _repo_state(target_repo)) == before

    def test_denial_precedes_hook_disable_and_allow_on_failure(self, tmp_path: Path) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        _stage_unsorted(session_repo)
        _stage_unsorted(target_repo)
        _git_config(session_repo, 'defsort.hook', 'false')
        _git_config(target_repo, 'defsort.hook', 'false')
        before = (_repo_state(session_repo), _repo_state(target_repo))

        _, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex', '--allow-on-failure'],
            stdin=_codex_pretooluse_payload(
                'git add test.py && git commit -m "test"', session_repo
            ),
            cwd=target_repo,
        )

        assert json.loads(result_stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert (_repo_state(session_repo), _repo_state(target_repo)) == before

    def test_relative_cd_route_is_denied_with_unknown_directory(self, tmp_path: Path) -> None:
        session_repo = tmp_path / 'a'
        _stage_unsorted(session_repo)
        (session_repo / 'sub').mkdir()
        before = _repo_state(session_repo)

        _, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload('cd ./sub && git commit -m "test"', session_repo),
            cwd=session_repo,
        )

        assert json.loads(result_stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert _repo_state(session_repo) == before

    @pytest.mark.parametrize(
        'command',
        [
            'git -C {repo} commit -a -m "test"',
            'git -C {repo} commit -m "test" -- test.py',
            'git -C {repo} commit -i test.py -m "test"',
        ],
    )
    def test_routed_modes_commit_sorted_blobs_with_unknown_directory(
        self, tmp_path: Path, command: str
    ) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        _stage_unsorted(session_repo)
        _init_git_repo(target_repo, _SRC_NEEDS_SORT)
        _git_config(target_repo, 'defsort.hook', 'true')
        rendered = command.format(repo=shell_path(target_repo))
        session_before = _repo_state(session_repo)

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(rendered, session_repo),
            cwd=session_repo,
        )
        assert (result_status, result_stdout) == (0, '')
        run_setup(shlex.split(rendered), cwd=session_repo)

        committed = decode_text(
            run_setup(['git', '-C', str(target_repo), 'show', 'HEAD:test.py']).stdout
        )
        assert committed.index('alpha') < committed.index('bravo')
        assert _repo_state(session_repo) == session_before

    def test_unknown_directory_denial_performs_no_repository_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError('repository access is forbidden before a target exists')

        for name in ('discover_repo_root', 'is_hook_enabled', 'run_pre_commit_adds'):
            monkeypatch.setattr(hook_mod, name, explode)
        monkeypatch.setattr(hook_mod, 'resolve_config_at_root', explode)

        outcome = front_mod._run_pre_tool_use('git commit -m x', None, allow_on_failure=True)

        assert outcome == Outcome('deny', _UNKNOWN_CWD_REASON)
