import errno
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from types import FunctionType
from typing import cast, get_args
from unittest.mock import patch

import pytest

import defsort._hook as hook_mod
from defsort._config import ResolvedConfig
from defsort._core._guard import ReorderGuardParams
from defsort._core._parse import parse_definitions
from defsort._files import _ReadResult, write_source
from defsort._git import (
    prepare_index_entry,
)
from defsort._hook import (
    _BLOCKER_MSGS,
    _Disposition,
    _file_config,
    _is_fallback_blocker,
    _plan_file_sort,
    _resolve_and_sort,
    _resolve_hook_context,
    _sort_worktree_files,
    _try_sort,
)
from defsort._hook_front import _TOOL
from defsort._hook_front import _run_hook as run_hook
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
from defsort._vendor.gitfiles._blobs import UnsupportedGitVersionError
from defsort._vendor.gitfiles._listings import StagedFileInfo
from defsort._vendor.gitfiles._repo import RepoDiscoveryResult, RepoDiscoveryStatus
from defsort._vendor.gitfiles._run import GitCommandError
from defsort._vendor.hookfront._front import Outcome
from defsort._vendor.hookfront._loader import load_adapter
from defsort._vendor.hookshell._grammar import (
    _ELEMENT_UNKNOWN_CWD,
    _HINT_ROUTE,
    CommitMode,
    PreCommitAdd,
)
from defsort._vendor.srcfiles import RestoreError
from defsort.hooks import HookAdapter, HookResponse, HookResult
from tests._deep_input import DEEP_DEFINITIONS, TOO_DEEP, deep_source_case
from tests._helpers import (
    DETECTOR_REGRESSION_TIMEOUT_SECONDS,
    DYNAMIC_ALL_BASELINE,
    DYNAMIC_ALL_SOURCE,
    DYNAMIC_ALL_WARNING,
    assert_renamed_result,
    decode_text,
    execute_source,
    fail_writes_to,
    incremental_gate_case,
    init_empty_git_repo,
    init_git_repo,
    install_stand_in_git,
    invoke_cli,
    isolate_git_config,
    json_stdout,
    prepare_renamed_file,
    process_transcript,
    redirect_recovery,
    register_distribution,
    run_cli,
    run_setup,
    shell_path,
)

pytestmark = pytest.mark.usefixtures('registered_hosts')


_CLASS_SKIP_BASELINE = 'class C:\n    def z(self): pass\n    x = 1\n'
_CLASS_SKIP_NOTE = 'defsort: m.py: class C: skipped (code between methods)'
_CLASS_SKIP_SOURCE = _CLASS_SKIP_BASELINE + '    def a(self): pass\n'
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
_POSSIBLE_REASON_PREFIX = _TOOL.possible_reason_prefix
_REASON_PREFIX = _TOOL.reason_prefix
_RECEIPT_ONE = {'systemMessage': 'defsort: sorted 1 staged Python file for this commit'}
_SKIP_BASELINE = {'systemMessage': 'defsort: mod.py: skipped (baseline not in target order)'}
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        reason = _assert_denied(
            (result_status, result_stdout, result_stderr),
            prefix=_POSSIBLE_REASON_PREFIX,
        )
        assert 'GIT_INDEX_FILE=alt.index' in reason
        assert 'to commit, use the supported form:' in reason
        after = run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout
        assert after == before


class TestAntigravityPostToolUse:
    @pytest.mark.parametrize(
        ('tool', 'field'), [('replace_file_content', 'TargetFile'), ('edit_file', 'Path')]
    )
    def test_antigravity_edit_resolves_relative_target(
        self, tmp_path: Path, tool: str, field: str
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                tool,
                {field: 'test.py', 'ReplacementContent': f.read_text(encoding='utf-8')},
                tmp_path,
            ),
            cwd=Path('/'),
        )

        assert result_status == 0
        assert result_stdout == ''
        assert f.read_text(encoding='utf-8').index('alpha') < f.read_text(encoding='utf-8').index(
            'bravo'
        )

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

    @pytest.mark.parametrize(
        ('tool', 'field'), [('write_to_file', 'TargetFile'), ('write_file', 'Path')]
    )
    def test_antigravity_write_sorts_python_file(
        self, tmp_path: Path, tool: str, field: str
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PostToolUse'],
            stdin=_antigravity_payload(
                tool,
                {field: str(f), 'CodeContent': f.read_text(encoding='utf-8')},
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
        init_git_repo(workspace_repo, '# baseline\n')
        init_git_repo(target_repo, _SRC_NEEDS_SORT)
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
        init_git_repo(workspace_repo, '# baseline\n')
        init_git_repo(target_repo, _SRC_NEEDS_SORT)
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
    def test_antigravity_commit_sorts_staged_file_with_a_receipt(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# baseline\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'],
            stdin=_antigravity_pretooluse_payload('git commit -m "test"', tmp_path),
            expect=0,
        )

        assert result.returncode == 0
        assert json_stdout(result) == {
            'decision': 'allow',
            'reason': _RECEIPT_ONE['systemMessage'],
        }
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_antigravity_commit_uses_target_cwd_not_process_cwd(self, tmp_path: Path) -> None:
        process_repo = tmp_path / 'process'
        target_repo = tmp_path / 'target'
        process_repo.mkdir()
        target_repo.mkdir()
        init_git_repo(process_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        assert json.loads(result_stdout) == {
            'decision': 'allow',
            'reason': _RECEIPT_ONE['systemMessage'],
        }
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_antigravity_commit_uses_workspace_fallback(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# baseline\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        payload = _antigravity_payload(
            'run_command', {'CommandLine': 'git commit -m "test"'}, tmp_path
        )

        result = run_cli(
            ['hook', '--agent', 'agy', '--event', 'PreToolUse'], stdin=payload, expect=0
        )

        assert result.returncode == 0
        assert json_stdout(result) == {
            'decision': 'allow',
            'reason': _RECEIPT_ONE['systemMessage'],
        }
        assert result.stderr == ''
        staged = decode_text(run_setup(['git', '-C', str(tmp_path), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_antigravity_infrastructure_denial_uses_top_level_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_git_repo(tmp_path, '# baseline\n')

        def no_staged_py_info(_cwd: str) -> list[StagedFileInfo] | None:
            raise GitCommandError('fatal: example')

        monkeypatch.setattr('defsort._hook.get_staged_py_info', no_staged_py_info)

        result = _run_in_process(
            provider='antigravity',
            stdin_data=_antigravity_pretooluse_payload('git commit -m "test"', tmp_path),
        )

        assert result == 0
        output = json.loads(capsys.readouterr().out)
        assert output['decision'] == 'deny'
        assert output['reason'] == 'defsort: failed to list staged files: fatal: example'

    def test_antigravity_safety_denial_uses_top_level_json(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# baseline\n')
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
        init_git_repo(workspace_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        assert json.loads(result_stdout) == {
            'decision': 'allow',
            'reason': _RECEIPT_ONE['systemMessage'],
        }
        staged = decode_text(run_setup(['git', '-C', str(target_repo), 'show', ':test.py']).stdout)
        assert staged.index('alpha') < staged.index('bravo')

    def test_target_repo_gate_workspace_enabled_skips_disabled_target(
        self, tmp_path: Path
    ) -> None:
        workspace_repo = tmp_path / 'workspace'
        target_repo = tmp_path / 'target'
        init_git_repo(workspace_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        init_git_repo(process_repo, '# baseline\n')
        init_git_repo(enabled_repo, _SRC_NEEDS_SORT)
        init_git_repo(disabled_repo, _SRC_NEEDS_SORT)
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
        init_git_repo(process_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        init_git_repo(process_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        init_git_repo(process_repo, '# baseline\n')
        init_git_repo(target_repo, '# baseline\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / 'clean.py').write_text('def alpha():\n    pass\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'clean.py'])
        result_status, result_stdout, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=_codex_pretooluse_payload(
                f'git -C {shell_path(tmp_path)} commit -m "test"', tmp_path
            ),
        )
        assert (result_status, result_stdout) == (0, '')


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_shift_heredoc_hook_preserves_classification(
    tmp_path: Path, *, enabled: bool, allow_on_failure: bool
) -> None:
    init_empty_git_repo(tmp_path)
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
            reason = _assert_denied(
                (result.returncode, result.stdout, result.stderr),
                prefix=_POSSIBLE_REASON_PREFIX,
            )
            assert 'heredoc body line 1000' in reason
            assert 'no eligible file target' in reason
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
        init_git_repo(tmp_path, prefix + zeta)
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
    def test_session_start_preserves_source(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        payload = json.dumps(
            {
                'hook_event_name': 'SessionStart',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result_status, _, _ = invoke_cli(['hook', '--agent', 'claude'], stdin=payload)
        assert result_status == 0
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestIncrementalMode:
    def test_incremental_non_conforming_reports_skip(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
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
        # Non-conforming is a skip condition, not a denial.
        assert result_status == 0
        assert json.loads(result_stdout) == _SKIP_BASELINE
        assert _staged_text(tmp_path) == new_content

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
        init_empty_git_repo(tmp_path)
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
        # Incremental policy skips the non-conforming baseline.
        assert result_status == 0
        assert json.loads(result_stdout) == _SKIP_BASELINE
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
        assert json.loads(result_stdout) == _SKIP_BASELINE
        assert _staged_text(tmp_path) == _INCREMENTAL_PRECEDENCE_CURRENT


class TestInvalidUtf8TomlHook:
    def test_hook_survives_invalid_utf8_pyproject(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)

        def inaccessible(*_args: object) -> None:
            raise PermissionError(13, 'Permission denied')

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
            assert 'WARNING: defsort: cannot read pathspec file paths' in captured.err
        else:
            assert 'cannot read pathspec file paths' in (result.reason or '')
        assert (tmp_path / '.git' / 'index').read_bytes() == index
        assert (tmp_path / 'test.py').read_bytes() == source


class TestPerFileHookConfig:
    def test_banner_override_uses_nested_pattern(self, tmp_path: Path) -> None:
        repo = tmp_path
        init_empty_git_repo(repo)
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
        init_empty_git_repo(repo)
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
        init_empty_git_repo(repo)
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

    def test_nested_guard_blocks_staged_sort(self, tmp_path: Path) -> None:
        repo = tmp_path
        init_empty_git_repo(repo)
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
        init_empty_git_repo(repo)
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
        init_empty_git_repo(repo)
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
        assert (
            f'{repo.as_posix()}/pyproject.toml: [tool.defsort].banner-pattern: invalid value'
            in result_stderr
        )
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
        current = 'def a():\n    pass\n\nx = 1\n\ndef b():\n    pass\n\n\ndef aa():\n    pass\n'
        result = _plan_file_sort(
            current,
            policy=SortPolicy(),
            incremental=True,
            baseline=baseline,
            display_path='test.py',
        )
        assert result.blocker == 'interstitial'
        assert result.new_source is None

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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_empty_git_repo(first_repo)
        init_empty_git_repo(second_repo)
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
            paths: list[Path],
        ) -> dict[Path, BlobReadResult]:
            baseline_calls.append((cwd, tuple(path.relative_to(cwd).as_posix() for path in paths)))
            return {path: BlobReadResult(status=BlobReadStatus.MISSING) for path in paths}

        def record_read(path: Path) -> object:
            processed.append(path)
            return original_read_file(path)

        monkeypatch.setattr(hook_mod, 'read_head_baselines', record_baselines)
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
        init_empty_git_repo(tmp_path)
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
            paths: list[Path],
        ) -> dict[Path, BlobReadResult]:
            baseline_calls.append((cwd, tuple(path.relative_to(cwd).as_posix() for path in paths)))
            return {path: BlobReadResult(status=BlobReadStatus.MISSING) for path in paths}

        monkeypatch.setattr(hook_mod, 'read_head_baselines', record_baselines)
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
                    f'defsort: PostToolUse skipped {payload_path.as_posix()}: '
                    "syntax error: '(' was never closed (line 1)"
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

    def test_non_python_post_tool_file_is_untouched(self, tmp_path: Path) -> None:
        target = tmp_path / 'readme.txt'
        target.write_text('hello', encoding='utf-8')
        original = target.read_bytes()

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        assert result == EXIT_OK
        assert target.read_bytes() == original

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


class TestHookSurfaceContracts:
    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    def test_dynamic_all_hook_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, incremental: bool
    ) -> None:
        isolate_git_config(monkeypatch)
        init_git_repo(tmp_path, DYNAMIC_ALL_BASELINE)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'baseline'])
        path = tmp_path / 'test.py'
        path.write_text(DYNAMIC_ALL_SOURCE, encoding='utf-8')
        status, _, stderr = invoke_cli(
            [
                'hook',
                '--agent',
                'claude',
                *self._hook_args('alphabetical', 'none', incremental=incremental),
            ],
            stdin=self._posttooluse_payload('claude', path, tmp_path),
            cwd=tmp_path,
        )
        assert status == EXIT_OK
        assert stderr == (
            f'ERROR: {path.as_posix()} {DYNAMIC_ALL_WARNING.removeprefix("sort ")}.'
            ' Write __all__ as one literal list to enable sorting.\n'
        )
        assert path.read_text(encoding='utf-8') == DYNAMIC_ALL_SOURCE

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
        init_git_repo(tmp_path, source)
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
        init_git_repo(tmp_path, source)
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
        init_git_repo(tmp_path, source)
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
        init_git_repo(tmp_path, '# placeholder\n')
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


class TestPostToolUseHardening:
    @pytest.mark.skipif(os.name == 'nt', reason='POSIX backslash is a filename character')
    def test_backslash_name_uses_its_own_configuration(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        config_dir = tmp_path / 'a'
        config_dir.mkdir()
        (config_dir / 'pyproject.toml').write_text(
            '[tool.defsort]\norder = "none"\n', encoding='utf-8'
        )
        path = tmp_path / 'a\\b.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        assert _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}}) == EXIT_OK
        assert path.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED

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
        captured = capsys.readouterr()
        expected = (
            f'defsort: PostToolUse skipped {f.as_posix()}: '
            'cannot read it: invalid or missing encoding declaration'
        )
        assert captured.err == expected + '\n'
        assert json.loads(captured.out)['hookSpecificOutput']['additionalContext'] == expected

    def test_repo_root_launch_failure_skips_without_guessing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'mod.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        calls: list[list[str]] = []

        def fail_launch(command: list[str], *_args: object, **_kwargs: object) -> None:
            calls.append(command)
            assert command[1:] == ['rev-parse', '--show-toplevel']
            raise PermissionError(13, 'Permission denied')

        monkeypatch.setattr(subprocess, 'run', fail_launch)

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}})

        captured = capsys.readouterr()
        assert result == EXIT_OK
        assert captured.err == (
            f'defsort: PostToolUse skipped {path.as_posix()}: '
            'failed to discover git work tree: git could not be run: Permission denied\n'
        )
        feedback = json.loads(captured.out)['hookSpecificOutput']
        assert feedback['additionalContext'] == captured.err.strip()
        assert [command[1:] for command in calls] == [['rev-parse', '--show-toplevel']]
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
        assert 'unexpected error: RuntimeError: boom' in captured.err


class TestPostToolUseSnapshotFailures:
    def test_concurrent_edit_is_kept_and_reported_as_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / 'test.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        def edit_then_write(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
            path.write_bytes(b'edited = True\n')
            return write_source(path, content, encoding=encoding, raw=raw)

        monkeypatch.setattr('defsort._hook.write_source', edit_then_write)

        assert (
            _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}}) == EXIT_OK
        )
        captured = capsys.readouterr()
        assert target.read_bytes() == b'edited = True\n'
        feedback = json.loads(captured.out)['hookSpecificOutput']
        assert feedback['hookEventName'] == 'PostToolUse'
        assert feedback['additionalContext'] == captured.err.strip()
        assert 'PostToolUse skipped' in captured.err
        assert 'changed since it was read' in captured.err

    def test_failed_restore_warns_with_saved_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recovery = redirect_recovery(monkeypatch, tmp_path / 'home')
        target = tmp_path / 'test.py'
        original = _SRC_NEEDS_SORT.encode('utf-8')
        target.write_bytes(original)
        fail_writes_to(monkeypatch, target, restore_fails=True)

        assert (
            _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}}) == EXIT_OK
        )
        captured = capsys.readouterr()
        saved = list(recovery.glob('*/*.orig'))
        assert len(saved) == 1
        assert saved[0].read_bytes() == original
        assert str(saved[0]) in captured.err
        feedback = json.loads(captured.out)['hookSpecificOutput']
        assert feedback['hookEventName'] == 'PostToolUse'
        assert feedback['additionalContext'] == captured.err.strip()


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

    def test_valid_sort(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        source = '# caf\u00e9 \u2603\n\ndef a():\n    return b()\n\n\ndef b():\n    pass\n'
        expected = '# caf\u00e9 \u2603\n\ndef b():\n    pass\n\n\ndef a():\n    return b()\n'
        f.write_bytes(source.encode('utf-8'))
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(f)},
            }
        )
        result = run_cli(['hook', '--agent', 'claude'], stdin=payload, expect=0)
        assert result.returncode == 0
        assert f.read_bytes() == expected.encode('utf-8')


class TestPostToolUseIncremental:
    @pytest.mark.parametrize(
        ('config_text', 'expected_message'),
        [
            ('[tool.defsort]\nincremental = true\n', 'incremental mode cannot use Git'),
            (
                '[tool.defsort]\nincremental = true\nmax-reorder-count = 0\n',
                'incremental mode and the reorder guard cannot use Git',
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
        init_empty_git_repo(tmp_path)
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
            raise GitCommandError('fatal: example')

        def record_write(*args: object, **_kwargs: object) -> None:
            write_calls.append(args)

        monkeypatch.setattr('defsort._hook.read_head_baselines', fail_fetch)
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

    def test_old_git_message_prints_once_per_run(
        self,
        tmp_path: Path,
        distribution_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        isolate_git_config(monkeypatch)
        register_distribution(
            distribution_root,
            'defsort-host-example',
            {'example': 'tests.hosts_example.adapter:ExampleAdapter'},
        )
        targets: list[Path] = []
        for name in ('first', 'second'):
            repo = tmp_path / name
            repo.mkdir()
            init_empty_git_repo(repo)
            (repo / 'pyproject.toml').write_text(
                '[tool.defsort]\nincremental = true\n', encoding='utf-8'
            )
            target = repo / 'mod.py'
            target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
            targets.append(target)

        def reject_git(_cwd: str, _paths: list[Path]) -> None:
            raise UnsupportedGitVersionError(
                '2.41.7',
                tool='defsort',
                purpose='incremental mode and the reorder guard on working-tree files',
            )

        monkeypatch.setattr('defsort._hook.read_head_baselines', reject_git)
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'cwd': str(tmp_path),
                'changed_files': [str(path) for path in targets],
            }
        )

        result = run_hook(payload, host='example')

        assert result == EXIT_OK
        assert capsys.readouterr().err == (
            'defsort: Git 2.42 or newer is required for incremental mode and the reorder guard '
            'on working-tree files (found Git 2.41.7)\n'
        )
        assert all(path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT for path in targets)

    def test_post_tool_use_baseline_missing_full_sort(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
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
        init_empty_git_repo(tmp_path)
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
        init_empty_git_repo(tmp_path)
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

    def test_post_tool_use_full_sort_default(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
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
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
        init_empty_git_repo(tmp_path)
        (tmp_path / 'placeholder.py').write_text('# p\n')
        run_setup(['git', 'add', '.'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'init'], cwd=tmp_path)
        f = tmp_path / 'pkg' / 'mod.py'
        f.parent.mkdir()
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
            == 'defsort: PostToolUse skipped pkg/mod.py: cannot write it: Permission denied\n'
        )

    def test_unsupported_git_warns_without_writing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_empty_git_repo(tmp_path)
        target = tmp_path / 'mod.py'
        target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )
        source = target.read_text(encoding='utf-8')

        def reject_git(_cwd: str, _paths: list[Path]) -> None:
            raise UnsupportedGitVersionError(
                '2.41.7',
                tool='defsort',
                purpose='incremental mode and the reorder guard on working-tree files',
            )

        monkeypatch.setattr('defsort._hook.read_head_baselines', reject_git)

        result = _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}})

        captured = capsys.readouterr()
        assert result == EXIT_OK
        assert captured.err == (
            'defsort: Git 2.42 or newer is required for incremental mode and the reorder guard '
            'on working-tree files (found Git 2.41.7)\n'
        )
        assert json.loads(captured.out) == {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': captured.err.rstrip('\n'),
            }
        }
        assert target.read_text(encoding='utf-8') == source


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
        )
        assert outcome.disposition is _Disposition.BLOCK
        assert outcome.blocker_message is not None
        assert 'code between definitions' in outcome.blocker_message

    @pytest.mark.parametrize('fail_on_unreadable', [False, True])
    def test_decode_failed_with_guard_skips(
        self, capsys: pytest.CaptureFixture[str], fail_on_unreadable: bool
    ) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        bl_entry = BlobReadResult(status=BlobReadStatus.DECODE_FAILED, detail='unknown codec')
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(max_reorder_count=5),
            display_path='test.py',
            baseline_entry=bl_entry,
            is_new=False,
            fail_on_unreadable=fail_on_unreadable,
        )
        assert outcome.disposition is _Disposition.SKIP
        assert outcome.skip_message is not None
        expected = (
            'baseline cannot be decoded: unknown codec'
            if fail_on_unreadable
            else 'reorder guard: unable to read baseline'
        )
        assert expected in outcome.skip_message
        assert expected in capsys.readouterr().err

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
        )
        assert outcome.disposition is _Disposition.SKIP
        assert 'would move' in capsys.readouterr().err

    @pytest.mark.parametrize('is_new', [False, True])
    def test_infra_error_with_incremental_returns_failure(self, is_new: bool) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=True)
        bl_entry = BlobReadResult(status=BlobReadStatus.INFRA_ERROR, detail='invalid batch')
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=bl_entry,
            is_new=is_new,
            fail_on_unreadable=True,
        )
        assert outcome.failure == 'defsort: failed to read baseline for test.py: invalid batch'

    def test_new_file_full_sort(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_NEEDS_SORT,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
        )
        assert outcome.disposition is _Disposition.CHANGE
        assert outcome.new_source is not None
        assert outcome.new_source.index('alpha') < outcome.new_source.index('bravo')

    def test_renamed_incremental_sorts_against_source(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=True)
        source = _SRC_ALREADY_ORDERED.replace('    pass', '    return helper()')
        source += '\n\ndef helper():\n    pass\n'
        outcome = _resolve_and_sort(
            ctx,
            source,
            file_cfg=ResolvedConfig(),
            display_path='new.py',
            baseline_entry=BlobReadResult(
                status=BlobReadStatus.CONTENT, content=_SRC_ALREADY_ORDERED
            ),
            is_new=False,
        )
        assert outcome.disposition is _Disposition.CHANGE
        assert outcome.new_source is not None
        assert (
            outcome.new_source.index('def helper')
            < outcome.new_source.index('def alpha')
            < outcome.new_source.index('def bravo')
        )

    def test_syntax_error_skips(self) -> None:
        ctx = _hook_ctx(Path('repo'), incremental=False)
        outcome = _resolve_and_sort(
            ctx,
            _SRC_SYNTAX_ERROR,
            file_cfg=ResolvedConfig(),
            display_path='test.py',
            baseline_entry=None,
            is_new=True,
        )
        assert outcome.disposition is _Disposition.SKIP


class TestPreToolUse:
    def test_already_ordered_no_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, source)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        captured = capsys.readouterr()
        assert captured.err == (
            'defsort: did not update test.py in the working tree: it has unstaged changes; '
            'the commit has the sorted version\n'
        )
        assert json.loads(captured.out) == {
            'systemMessage': _RECEIPT_ONE['systemMessage'] + '\n' + captured.err.rstrip('\n')
        }
        # Working tree must be left alone (non-UTF-8, not overwritten)
        assert f.read_bytes().startswith(b'\x80\x81\x82')

    def test_bare_commit_syncs_clean_working_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        assert json.loads(captured.out) == {
            'systemMessage': _RECEIPT_ONE['systemMessage'] + '\n' + captured.err.rstrip('\n')
        }
        assert (
            captured.err
            == 'defsort: did not update test.py in the working tree: cannot read it: denied; '
            'the commit has the sorted version\n'
        )
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert staged.index('alpha') < staged.index('bravo')

    def test_bare_commit_unwritable_working_tree_sorts_with_a_receipt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
        init_git_repo(tmp_path, '# placeholder\n')
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
        assert json.loads(captured.out) == {
            'systemMessage': _RECEIPT_ONE['systemMessage'] + '\n' + captured.err.rstrip('\n')
        }
        assert (
            captured.err == 'defsort: did not update test.py in the working tree: '
            'cannot write it: Permission denied; '
            'the commit has the sorted version\n'
        )

    def test_bare_heredoc_denies_without_sorting_index(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        reason = _assert_classification_denied(decision, prefix=_POSSIBLE_REASON_PREFIX)
        assert 'heredoc body line 1' in reason
        assert 'no eligible file target' in reason
        assert staged == _SRC_NEEDS_SORT

    def test_blocks_interstitial(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)

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
        init_git_repo(tmp_path, '# placeholder\n')
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
        captured = capsys.readouterr()
        note = (
            'defsort: did not update file_a.py in the working tree: it has unstaged changes; '
            'the commit has the sorted version'
        )
        assert captured.err == note + '\n'
        assert json.loads(captured.out) == {
            'systemMessage': 'defsort: sorted 2 staged Python files for this commit\n' + note
        }
        # file_a.py's working tree must preserve unstaged changes
        assert '# unstaged extra' in fa.read_text(encoding='utf-8')

    def test_compound_add_commit_sorts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        captured = capsys.readouterr()
        note = (
            'defsort: did not update test.py in the working tree: it has unstaged changes; '
            'the commit has the sorted version'
        )
        assert captured.err == note + '\n'
        assert json.loads(captured.out) == {
            'systemMessage': 'defsort: sorted 1 staged Python file for this commit\n' + note
        }
        # Working tree must still have the unstaged comment
        assert '# unstaged extra' in f.read_text(encoding='utf-8')

    def test_compound_add_with_arbitrary_prefix_denies(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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

    def test_failed_index_update_preserves_working_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch apply_index_entries to simulate failure
        def fail_apply_index_entries(
            _cwd: str, _entries: Collection[tuple[str, str, str]]
        ) -> bool:
            raise GitCommandError('fatal: example')

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

    def test_global_git_option_segment_before_commit_denies(
        self,
        tmp_path: Path,
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'

        def fail_discovery(_cwd: str) -> list[str] | None:
            raise GitCommandError('fatal: example')

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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'

        def fail_discovery(_cwd: str) -> list[str] | None:
            raise GitCommandError('fatal: example')

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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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

    def test_raw_different_sync_resolves_file_config_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        def fail_read_staged_blobs(
            _cwd: str, _info: Iterable[StagedFileInfo]
        ) -> dict[str, BlobReadResult] | None:
            raise GitCommandError('fatal: example')

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
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert 'WARNING' in captured.err
        assert 'failed to read' in captured.err
        assert rc == Outcome('allow')  # allow, not deny (C1 fix)

    def test_read_failure_denies_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch read_staged_blobs to return None (simulates git failure)
        def fail_read_staged_blobs(
            _cwd: str, _info: Iterable[StagedFileInfo]
        ) -> dict[str, BlobReadResult] | None:
            raise GitCommandError('fatal: example')

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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        original = target.read_bytes()

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('baseline or write operation was called')

        def fail_rename_discovery(_cwd: str) -> None:
            raise GitCommandError('fatal: example')

        monkeypatch.setattr(hook_mod, 'head_rename_sources', fail_rename_discovery)
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
        assert captured.err == 'defsort: failed to pair renamed files: fatal: example\n'
        output = json.loads(captured.out)['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        assert output['permissionDecisionReason'] == (
            'defsort: failed to pair renamed files: fatal: example'
        )
        assert target.read_bytes() == original

    def test_rename_discovery_failure_warns_and_allows_with_failure_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        original = target.read_bytes()

        def fail_if_called(*_args: object, **_kwargs: object) -> None:
            pytest.fail('baseline or write operation was called')

        def fail_rename_discovery(_cwd: str) -> None:
            raise GitCommandError('fatal: example')

        monkeypatch.setattr(hook_mod, 'head_rename_sources', fail_rename_discovery)
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
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err == 'WARNING: defsort: failed to pair renamed files: fatal: example\n'
        assert target.read_bytes() == original

    def test_sorts_staged_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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

    @pytest.mark.skipif(os.name == 'nt', reason='the Git stand-in is a POSIX shell script')
    def test_staged_commit_works_with_git_2_41(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'm.py'
        path.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', 'add', 'm.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\nmax-reorder-count = 1\n',
            encoding='utf-8',
        )
        source = _SRC_ALREADY_ORDERED + '\n\ndef aardvark():\n    pass\n'
        path.write_text(source, encoding='utf-8')
        run_setup(['git', 'add', 'm.py'], cwd=tmp_path)
        install_stand_in_git(monkeypatch, tmp_path, version='2.41.0')

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m test'},
                'cwd': str(tmp_path),
            }
        )

        assert decision.decision == 'allow'
        assert decision.reason == _RECEIPT_ONE['systemMessage']
        assert json.loads(capsys.readouterr().out) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout)
        assert staged.index('aardvark') < staged.index('alpha') < staged.index('bravo')
        run_setup(['git', 'commit', '-m', 'sorted'], cwd=tmp_path)
        assert decode_text(run_setup(['git', 'show', 'HEAD:m.py'], cwd=tmp_path).stdout) == staged

    @pytest.mark.skipif(os.name == 'nt', reason='the Git stand-in is a POSIX shell script')
    @pytest.mark.parametrize(
        'allow_on_failure', [False, True], ids=['default', 'allow-on-failure']
    )
    def test_staged_commit_with_old_git_succeeds_under_failure_policies(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        allow_on_failure: bool,
    ) -> None:
        init_git_repo(tmp_path, '# initial\n')
        path = tmp_path / 'test.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        install_stand_in_git(monkeypatch, tmp_path, version='2.41.0')

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m test'},
                'cwd': str(tmp_path),
            },
            allow_on_failure=allow_on_failure,
        )

        captured = capsys.readouterr()
        assert decision.decision == 'allow'
        assert decision.reason == _RECEIPT_ONE['systemMessage']
        assert captured.err == ''
        assert json.loads(captured.out) == _RECEIPT_ONE
        staged = decode_text(run_setup(['git', 'show', ':test.py'], cwd=tmp_path).stdout)
        assert staged.index('alpha') < staged.index('bravo')
        run_setup(['git', 'commit', '-m', 'sorted'], cwd=tmp_path)
        assert (
            decode_text(run_setup(['git', 'show', 'HEAD:test.py'], cwd=tmp_path).stdout) == staged
        )

    def test_syntax_error_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        # Monkey-patch get_staged_py_info to raise UnicodeDecodeError
        def _raise(_cwd: str) -> list[StagedFileInfo]:
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

        reason = _assert_classification_denied(decision, prefix=_POSSIBLE_REASON_PREFIX)
        assert '--definitely-not-real' in reason
        assert 'to commit, use the supported form:' in reason
        assert _assert_denied_output(capsys, prefix=_POSSIBLE_REASON_PREFIX) == reason

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

    @pytest.mark.skipif(os.name == 'nt', reason='the Git stand-in is a POSIX shell script')
    def test_working_tree_commit_names_the_by_name_purpose(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_empty_git_repo(tmp_path)
        path = tmp_path / 'm.py'
        path.write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', 'add', 'm.py'], cwd=tmp_path)
        run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\nmax-reorder-count = 1\n',
            encoding='utf-8',
        )
        source = _SRC_ALREADY_ORDERED + '\n\ndef aardvark():\n    pass\n'
        path.write_text(source, encoding='utf-8')
        install_stand_in_git(monkeypatch, tmp_path, version='2.41.0')

        decision = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -a -m test'},
                'cwd': str(tmp_path),
            }
        )

        assert decision.decision == 'deny'
        assert decision.reason == (
            'defsort: Git 2.42 or newer is required for incremental mode and the reorder guard '
            'on working-tree files (found Git 2.41.0)'
        )
        assert capsys.readouterr().err == f'{decision.reason}\n'
        assert path.read_text(encoding='utf-8') == source

    @pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain control characters')
    def test_bare_commit_sync_note_escapes_filename(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        init_empty_git_repo(tmp_path)
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
        assert json.loads(captured.out) == {
            'systemMessage': _RECEIPT_ONE['systemMessage'] + '\n' + captured.err.rstrip('\n')
        }
        assert captured.err == (
            'defsort: did not update escape\\x1b\\x0a.py in the working tree: '
            'cannot read it: denied; the commit has the sorted version\n'
        )
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize('policy_source', ['cli', 'config'])
    def test_discovery_error_honors_failure_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        policy_source: str,
    ) -> None:

        def fail_discovery(_cwd: str) -> RepoDiscoveryResult:
            return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

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
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
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

        def record_target(cwd: str) -> RepoDiscoveryResult:
            observed.append(cwd)
            return RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)

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
        init_git_repo(tmp_path, '# initial\n')
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
        reason = (
            'defsort: failed to prepare index entry for test.py: '
            'git could not be run: Git executable not found on PATH'
        )
        if allow_on_failure:
            assert result == 0
            if provider == 'antigravity':
                assert captured.out == ''
            else:
                assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
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

    @pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity'])
    def test_provider_discovery_error_denial_shape(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_discovery(_cwd: str) -> RepoDiscoveryResult:
            return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(hook_mod, 'is_hook_enabled', _hook_enabled)
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
    def test_provider_discovery_error_warns_with_failure_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        provider: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_discovery(_cwd: str) -> RepoDiscoveryResult:
            return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)

        monkeypatch.setattr(hook_mod, 'is_hook_enabled', _hook_enabled)
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
        assert captured.err == 'WARNING: defsort: failed to discover git work tree\n'
        if provider == 'antigravity':
            assert captured.out == ''
        else:
            assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}

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
        reason = _assert_denied_output(capsys, provider, prefix=_POSSIBLE_REASON_PREFIX)
        assert '--definitely-not-real' in reason
        assert 'to commit, use the supported form:' in reason

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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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


class TestReadOnlyCommitModes:
    def _staged_repo(self, tmp_path: Path) -> bytes:
        init_git_repo(tmp_path, '# placeholder\n')
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


class TestPreToolUseCommitModes:
    def test_commit_a_blocker_denies(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, _SRC_INTERSTITIAL)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_ALREADY_ORDERED)
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

        def fail_read(_cwd: str, _info: list[StagedFileInfo]) -> None:
            raise GitCommandError('fatal: example')

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
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err == 'WARNING: defsort: failed to read staged contents: fatal: example\n'
        assert pathspec.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
        assert staged_after == staged_before

    def test_commit_include_overlap(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )

        def reject_git(
            _cwd: str, _paths: list[str], *, sources: Mapping[str, str] | None = None
        ) -> None:
            assert sources == {}
            raise UnsupportedGitVersionError(
                '2.41.7',
                tool='defsort',
                purpose='incremental mode and the reorder guard on working-tree files',
            )

        monkeypatch.setattr('defsort._hook.batch_fetch_worktree_baselines', reject_git)

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
                    'defsort: Git 2.42 or newer is required for incremental mode and the '
                    'reorder guard on working-tree files (found Git 2.41.7)'
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
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )

        def reject_git(
            _cwd: str, _paths: list[str], *, sources: Mapping[str, str] | None = None
        ) -> None:
            assert sources == {}
            raise UnsupportedGitVersionError(
                '2.41.7',
                tool='defsort',
                purpose='incremental mode and the reorder guard on working-tree files',
            )

        monkeypatch.setattr('defsort._hook.batch_fetch_worktree_baselines', reject_git)

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
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err == (
            'WARNING: defsort: Git 2.42 or newer is required for incremental mode and the '
            'reorder guard on working-tree files (found Git 2.41.7)\n'
        )
        assert pathspec.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        staged_after = run_setup(['git', '-C', str(tmp_path), 'show', ':staged.py']).stdout
        assert staged_after == staged_before

    def test_commit_only_with_empty_pathspec_file_preserves_staged_changes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, staged_source)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_empty_git_repo(tmp_path)
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
        init_git_repo(tmp_path, source)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_INTERSTITIAL)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)

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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        # A sorted in index and synced because its text matches the staged copy
        content_a = fa.read_text(encoding='utf-8')
        assert content_a.index('alpha') < content_a.index('bravo')

    def test_compound_add_commit_patch_skips_sorting(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
        init_git_repo(tmp_path, case)

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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(sub, '# placeholder\n')
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

    def test_crlf_blob_with_lf_worktree_syncs_the_sorted_worktree_text(
        self, tmp_path: Path
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        ga = tmp_path / '.gitattributes'
        ga.write_text('*.py -text\n', encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.gitattributes'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'add gitattributes'])
        f = tmp_path / 'test.py'
        crlf_content = b'def bravo():\r\n    pass\r\n\r\n\r\ndef alpha():\r\n    pass\r\n'
        f.write_bytes(crlf_content)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        # The worktree differs from the staged blob only in line endings.
        f.write_bytes(crlf_content.replace(b'\r\n', b'\n'))
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
        assert f.read_bytes() == b'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        assert _staged_bytes(tmp_path) == (
            b'def alpha():\r\n    pass\r\n\r\n\r\ndef bravo():\r\n    pass\r\n'
        )

    def test_git_clean_raw_different_worktree_sorted_independently(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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

    def test_partial_replay_keeps_staged_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolate_git_config(monkeypatch)
        init_git_repo(tmp_path, '# placeholder\n')
        path = tmp_path / 'a.py'
        path.write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))
        (tmp_path / '.gitignore').write_text('ignored.py\n', encoding='utf-8')
        (tmp_path / 'ignored.py').write_text('x = 1\n', encoding='utf-8')

        result = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git add a.py ignored.py && git commit -m x', tmp_path),
        )

        reason = _assert_denied(result, prefix='defsort: failed to replay git add: ')
        assert reason.endswith(' Any files staged by the preceding git add remain staged.')
        assert run_setup(['git', 'show', ':a.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_replay_failure_denies_with_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolate_git_config(monkeypatch)
        init_git_repo(tmp_path, '# placeholder\n')

        result = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git add missing.py && git commit -m x', tmp_path),
        )

        prefix = 'defsort: failed to replay git add: '
        suffix = ' Any files staged by the preceding git add remain staged.'
        reason = _assert_denied(result, prefix=prefix)
        assert reason.endswith(suffix)
        assert reason.removeprefix(prefix).removesuffix(suffix).strip()

    def test_replay_failure_warns_under_allow_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolate_git_config(monkeypatch)
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)

        status, stdout, stderr = invoke_cli(
            ['hook', '--agent', 'claude', '--allow-on-failure'],
            stdin=_pretooluse_payload('git add missing.py && git commit -m x', tmp_path),
        )

        assert status == 0
        assert stderr.startswith('WARNING: defsort: failed to replay git add: ')
        assert stderr.removeprefix('WARNING: defsort: failed to replay git add: ').strip()
        assert json.loads(stdout) == {'systemMessage': stderr.rstrip('\n')}
        assert (
            run_setup(['git', 'show', ':test.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()
        )
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_sync_only_resort_does_not_duplicate_banner_diagnostic(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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
        init_git_repo(tmp_path, '# placeholder\n')
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
    @pytest.mark.parametrize('provider', ['codex', 'antigravity'])
    def test_banner_mode_survives_posttooluse_provider_dispatch(
        self,
        provider: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_banner_modes(monkeypatch)

        init_empty_git_repo(tmp_path)
        (tmp_path / 'test.py').write_text(_SRC_ALREADY_ORDERED, encoding='utf-8')
        monkeypatch.setattr('defsort._hook.is_hook_enabled', _hook_enabled)
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
        captured = _capture_banner_modes(monkeypatch)

        init_empty_git_repo(tmp_path)
        monkeypatch.setattr('defsort._hook.is_hook_enabled', _hook_enabled)
        payload = (
            _codex_pretooluse_payload(f'git -C {shell_path(tmp_path)} commit -m test', tmp_path)
            if provider == 'codex'
            else _antigravity_pretooluse_payload('git commit -m test', tmp_path)
        )

        result = _run_in_process(payload, provider, overrides=SortOverrides(banners='strip'))

        assert result == EXIT_OK
        assert captured == ['strip']

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
        init_empty_git_repo(tmp_path)
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
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        target = tmp_path / 'test.py'
        mtime_before = target.stat().st_mtime_ns

        def fail_baselines(
            _repo_root: str, _paths: list[str], *, sources: Mapping[str, str] | None = None
        ) -> None:
            assert sources == {}
            raise GitCommandError('fatal: example')

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
            assert captured.err == 'WARNING: defsort: failed to read baselines: fatal: example\n'
        else:
            assert captured.err == ''
            assert result.reason == 'defsort: failed to read baselines: fatal: example'

    def test_blocker_denies_without_modifying(self, tmp_path: Path) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_INTERSTITIAL, encoding='utf-8')
        original = f.read_text(encoding='utf-8')
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['test.py'])
        assert result.decision == 'deny'
        assert f.read_text(encoding='utf-8') == original

    def test_crlf_preserved(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
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

    @pytest.mark.parametrize('allow_on_failure', [False, True], ids=['deny', 'allow'])
    def test_file_changed_before_its_write_is_not_written(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        allow_on_failure: bool,
    ) -> None:
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        def edit_then_write(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
            path.write_text('edited = True\n', encoding='utf-8')
            return write_source(path, content, encoding=encoding, raw=raw)

        monkeypatch.setattr('defsort._hook.write_source', edit_then_write)
        result = _sort_worktree_files(
            _hook_ctx(tmp_path, allow_on_failure=allow_on_failure), ['a.py']
        )

        assert a.read_text(encoding='utf-8') == 'edited = True\n'
        if allow_on_failure:
            assert result.decision == 'allow'
            assert 'changed since it was read' in capsys.readouterr().err
        else:
            assert result.decision == 'deny'
            assert result.reason is not None
            assert 'changed since it was read' in result.reason

    def test_multi_file_write_failure_best_effort(self, tmp_path: Path) -> None:
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
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
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
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

    def test_rollback_leaves_a_file_changed_after_this_run_wrote_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        def write_a_then_edit_it(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
            if path.name == 'b.py':
                raise OSError('injected write failure')
            written = write_source(path, content, encoding=encoding, raw=raw)
            path.write_text('edited = True\n', encoding='utf-8')
            return written

        monkeypatch.setattr('defsort._hook.write_source', write_a_then_edit_it)
        result = _sort_worktree_files(_hook_ctx(tmp_path), ['a.py', 'b.py'])

        assert result.decision == 'deny'
        assert a.read_text(encoding='utf-8') == 'edited = True\n'
        assert result.reason is not None
        assert 'not restored: a.py (changed since it was read' in result.reason

    def test_rollback_restore_failure_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
        a = tmp_path / 'a.py'
        b = tmp_path / 'b.py'
        c = tmp_path / 'c.py'
        original = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        a.write_bytes(original.encode('utf-8'))
        b.write_bytes(original.encode('utf-8'))
        c.write_bytes(original.encode('utf-8'))

        def selective_write(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
            if path.name == 'c.py':
                raise OSError('injected write failure')
            written = write_source(path, content, encoding=encoding, raw=raw)
            if path.name == 'a.py':
                # The rollback can then not reopen a.py for writing.
                path.chmod(0o444)
            return written

        monkeypatch.setattr('defsort._hook.write_source', selective_write)

        try:
            result = _sort_worktree_files(
                _hook_ctx(
                    tmp_path,
                    overrides=SortOverrides(definition_order='alphabetical'),
                ),
                ['a.py', 'b.py', 'c.py'],
            )
        finally:
            a.chmod(0o644)

        assert result.decision == 'deny'
        assert a.read_text(encoding='utf-8') != original
        assert b.read_text(encoding='utf-8') == original
        assert c.read_text(encoding='utf-8') == original
        assert capsys.readouterr().out == ''
        assert result.reason is not None
        assert 'cannot write c.py' in result.reason
        assert 'not restored: a.py (' in result.reason

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
        assert result == Outcome('deny', 'defsort: cannot read test.py: read denied')
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unreadable_file_skips_with_failure_override(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / 'test.py'
        f.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('read denied')):
            result = _sort_worktree_files(_hook_ctx(tmp_path, allow_on_failure=True), ['test.py'])
        assert result == Outcome('allow')
        assert capsys.readouterr().err == 'WARNING: defsort: cannot read test.py: read denied\n'
        assert f.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_unwritable_file_denies_on_write(self, tmp_path: Path) -> None:
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
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
        if sys.platform != 'win32' and os.geteuid() == 0:
            pytest.skip('root can write read-only files')
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
        fail_writes_to(monkeypatch, b)

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
        assert result.syntax_detail is None

    def test_duplicates_blocker(self) -> None:
        result = _try_sort(
            _SRC_DUPLICATES,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'duplicates'
        assert result.syntax_detail is None

    def test_forward_ref_blocker(self) -> None:
        result = _try_sort(
            _SRC_FORWARD_REF,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'forward_refs'
        assert result.syntax_detail is None

    def test_interstitial_blocker(self) -> None:
        result = _try_sort(
            _SRC_INTERSTITIAL,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker == 'interstitial'
        assert result.syntax_detail is None

    def test_returns_sorted_content(self) -> None:
        result = _try_sort(
            _SRC_NEEDS_SORT,
            policy=SortPolicy(),
        )
        assert result.new_source is not None
        assert result.blocker is None
        assert result.syntax_detail is None

    def test_syntax_error_returns_skip(self) -> None:
        result = _try_sort(
            _SRC_SYNTAX_ERROR,
            policy=SortPolicy(),
        )
        assert result.new_source is None
        assert result.blocker is None
        assert result.syntax_detail is not None


class TestWorktreeSync:
    def test_file_changed_after_planning_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        f = tmp_path / 'test.py'
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])

        def edit_then_write(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
            path.write_text('edited = True\n', encoding='utf-8')
            return write_source(path, content, encoding=encoding, raw=raw)

        monkeypatch.setattr('defsort._hook.write_source', edit_then_write)
        status, _, err = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert status == 0
        assert _staged_text(tmp_path, 'test.py') == _SRC_ALREADY_ORDERED
        assert f.read_text(encoding='utf-8') == 'edited = True\n'
        assert (
            'did not update test.py in the working tree: '
            'cannot write it: changed since it was read' in err
        )

    @pytest.mark.parametrize(
        'command', ['git commit -m test', 'git add filtered.py && git commit -m test']
    )
    def test_filter_divergence_leaves_worktree_untouched(
        self, tmp_path: Path, command: str
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        (tmp_path / '.gitattributes').write_text('filtered.py ident\n', encoding='utf-8')
        filtered = tmp_path / 'filtered.py'
        filtered.write_text('# $Id$\n' + _SRC_ALREADY_ORDERED, encoding='utf-8')
        run_setup(['git', '-C', str(tmp_path), 'add', '.'])
        run_setup(['git', '-C', str(tmp_path), 'commit', '-m', 'track filtered file'])
        filtered.unlink()
        run_setup(['git', '-C', str(tmp_path), 'checkout', '--', 'filtered.py'])
        expanded = filtered.read_text(encoding='utf-8')
        assert expanded.startswith('# $Id: ')
        filtered.write_text(
            expanded.replace(_SRC_ALREADY_ORDERED, _SRC_NEEDS_SORT), encoding='utf-8'
        )
        run_setup(['git', '-C', str(tmp_path), 'add', 'filtered.py'])
        worktree_before = filtered.read_bytes()

        status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'], stdin=_pretooluse_payload(command, tmp_path)
        )

        assert status == 0
        staged = _staged_text(tmp_path, 'filtered.py')
        assert staged.index('def alpha') < staged.index('def bravo')
        assert filtered.read_bytes() == worktree_before


def _alter_sync_copy(path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> str:
    reason = 'it has unstaged changes'
    if case == 'edited':
        path.write_text('# unstaged\n' + _SRC_NEEDS_SORT, encoding='utf-8')
    elif case == 'missing':
        path.unlink()
    elif case == 'symlink':
        path.rename(path.parent / 'target.txt')
        path.symlink_to('target.txt')
    elif case == 'undecodable':
        path.write_bytes(b'# coding: utf-8\nvalue = "\xff"\n')
    elif case == 'unreadable':
        original_read = Path.read_bytes

        def read(target: Path) -> bytes:
            if target == path:
                raise PermissionError(errno.EACCES, 'Permission denied')
            return original_read(target)

        monkeypatch.setattr(Path, 'read_bytes', read)
        reason = 'cannot read it: Permission denied'
    else:
        errors = {
            'write': OSError(errno.EIO, 'I/O error'),
            'restore': RestoreError(errno.EIO, 'restore failed; original saved', None),
            'unexpected': RuntimeError('boom'),
        }

        def write(*_args: object, **_kwargs: object) -> None:
            raise errors[case]

        monkeypatch.setattr(hook_mod, 'write_source', write)
        details = {
            'write': 'I/O error',
            'restore': 'restore failed; original saved',
            'unexpected': 'RuntimeError: boom',
        }
        reason = f'cannot write it: {details[case]}'
    return reason


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


def _assert_classification_denied(decision: Outcome, *, prefix: str = _REASON_PREFIX) -> str:
    assert decision.decision == 'deny'
    assert decision.reason is not None
    assert decision.reason.startswith(prefix)
    return decision.reason


def _capture_banner_modes(monkeypatch: pytest.MonkeyPatch) -> list[BannerMode | None]:
    captured: list[BannerMode | None] = []

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
        return _resolve_hook_context(
            cwd,
            repo_root,
            overrides=overrides,
            allow_on_failure=allow_on_failure,
        )

    monkeypatch.setattr('defsort._hook._resolve_hook_context', capture_resolver)
    return captured


def _codex_patch_event(repo: Path, paths: list[Path]) -> dict[str, object]:
    sections = ''.join(
        f'*** Update File: {path.relative_to(repo).as_posix()}\n@@\n-x\n+x\n' for path in paths
    )
    return {
        'hook_event_name': 'PostToolUse',
        'tool_name': 'apply_patch',
        'tool_input': {'command': f'*** Begin Patch\n{sections}*** End Patch\n'},
        'cwd': str(repo),
        'model': 'gpt-5.5',
        'turn_id': 'turn_test',
    }


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
        init_git_repo(tmp_path, '# placeholder\n')
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


def _failed_private_paths(_root: str, _paths: list[str]) -> tuple[frozenset[str], str]:
    return frozenset(), 'fatal: check-attr failed'


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
    init_git_repo(tmp_path, baseline)
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


def _names_under_two_orders(root: Path) -> tuple[Path, Path]:
    sorted_dir = root / 'sorted'
    kept_dir = root / 'kept'
    sorted_dir.mkdir()
    kept_dir.mkdir()
    (kept_dir / 'pyproject.toml').write_text(
        '[tool.defsort]\ndefinition-order = "none"\n', encoding='utf-8'
    )
    a = sorted_dir / 'test.py'
    a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    b = kept_dir / 'test.py'
    os.link(a, b)
    return a, b


def _pathspec_repo(repo: Path) -> None:
    run_setup(['git', 'init'], cwd=repo)
    path = repo / 'a.py'
    path.write_text('value = 1\n', encoding='utf-8')
    run_setup(['git', 'add', 'a.py'], cwd=repo)
    run_setup(
        [
            'git',
            '-c',
            'user.name=Test',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-m',
            'baseline',
        ],
        cwd=repo,
    )
    path.write_text('value = 2\n', encoding='utf-8')


def _prepare_class_skip(
    repo: Path, source: str, *, incremental: bool, baseline: str = _CLASS_SKIP_BASELINE
) -> Path:
    init_empty_git_repo(repo)
    path = repo / 'm.py'
    path.write_text(baseline, encoding='utf-8')
    run_setup(['git', 'add', 'm.py'], cwd=repo)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=repo)
    (repo / 'pyproject.toml').write_text(
        '[tool.defsort]\nmethod-order = "alphabetical"\n'
        f'incremental = {str(incremental).lower()}\n',
        encoding='utf-8',
    )
    path.write_text(source, encoding='utf-8')
    run_setup(['git', 'add', 'm.py'], cwd=repo)
    return path


def _post_tool_names_disagree(_needed: object) -> bool:
    return False


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
    init_git_repo(tmp_path, baseline)
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


def _assert_hook_incremental_removes_unused_future_beside_class_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: str,
    source: str,
    note: str | None,
) -> None:
    isolate_git_config(monkeypatch)
    path = _prepare_class_skip(tmp_path, source, incremental=True, baseline=baseline)

    result, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload('git commit -m test', tmp_path)
    )

    assert result == EXIT_OK
    output = json.loads(stdout)
    assert 'hookSpecificOutput' not in output
    receipt = 'defsort: sorted 1 staged Python file for this commit'
    assert output['systemMessage'] == (f'{receipt}\n{note}' if note else receipt)
    assert stderr == (f'{note}\n' if note else '')
    staged = decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout)
    expected = source.removeprefix('from __future__ import annotations\n')
    assert staged == expected
    assert path.read_text(encoding='utf-8') == expected


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
    adapter = load_adapter(_TOOL, 'claude')
    render = adapter.render
    outcomes: list[Outcome] = []

    def record_outcome(_adapter: HookAdapter, result: HookResult) -> HookResponse:
        outcomes.append(Outcome(result.decision, result.reason))
        return render(result)

    with patch.object(type(adapter), 'render', record_outcome):
        assert (
            run_hook(
                json.dumps(payload),
                host='claude',
                overrides=overrides,
                allow_on_failure=allow_on_failure,
            )
            == 0
        )
    assert len(outcomes) == 1
    return outcomes[0]


def _run_sync_commit(repo: Path) -> Outcome:
    plan = hook_mod.CommitPlan(
        cwd=str(repo),
        mode=CommitMode(
            auto_stage=False,
            include=False,
            only=False,
            pathspecs=(),
            pathspec_from_file=None,
            pathspec_file_nul=False,
        ),
        pre_commit_adds=(),
    )
    return hook_mod.run_commit(
        plan, allow_on_failure=False, overrides=SortOverrides(incremental=False)
    )


def _setup_include_failure_repo(tmp_path: Path) -> tuple[Path, bytes]:
    init_git_repo(tmp_path, '# initial\n')
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
    init_empty_git_repo(tmp_path)
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
    init_empty_git_repo(tmp_path)
    baseline = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
    (tmp_path / 'mod.py').write_text(baseline)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    # Add a new def (aardvark) at the end -- should sort before alpha
    new_content = baseline + '\n\ndef aardvark():\n    pass\n'
    (tmp_path / 'mod.py').write_text(new_content)
    run_setup(['git', 'add', 'mod.py'], cwd=tmp_path)


def _stage_text(repo: Path, path: str, text: str) -> None:
    blob = run_setup(
        ['git', 'hash-object', '-w', '--stdin'], cwd=repo, stdin=text.encode('utf-8')
    ).stdout.strip()
    run_setup(
        ['git', 'update-index', '--add', '--cacheinfo', f'100644,{decode_text(blob)},{path}'],
        cwd=repo,
    )


def _stage_unsorted(repo: Path) -> None:
    init_git_repo(repo, _SRC_NEEDS_SORT)
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
                f'defsort: PostToolUse skipped {path.as_posix()}: '
                'syntax error: invalid syntax (line 1)'
                for path in paths
            ),
        },
    }
    assert captured.err == ''
    assert [path.read_text(encoding='utf-8') for path in paths] == [source, source]


def test_commit_denies_when_engine_import_fails_through_run_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, 'defsort._hook', None)
    assert run_hook(_pretooluse_payload('git commit -m x', tmp_path), host='claude') == 0
    output = json.loads(capsys.readouterr().out)['hookSpecificOutput']
    assert output['permissionDecision'] == 'deny'
    assert output['permissionDecisionReason'].startswith(
        'defsort: unexpected error during sort: ModuleNotFoundError'
    )


def test_existing_nested_repo_standalone_commit_keeps_processing(
    tmp_path: Path,
) -> None:
    init_git_repo(tmp_path, '# outer baseline\n')
    child = tmp_path / 'child'
    child.mkdir()
    init_git_repo(child, _SRC_NEEDS_SORT)
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
        f'warning: {tmp_path.as_posix()}/pyproject.toml: [tool.defsort]: '
        'unknown key bad\\x1b\\x85\\x0a\\x1b; ignoring\n'
    )
    assert json.loads(captured.out) == {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': captured.err.rstrip('\n'),
        }
    }


def test_hook_incremental_removes_unused_future_beside_class_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = 'from __future__ import annotations\n\n' + _CLASS_SKIP_SOURCE
    _assert_hook_incremental_removes_unused_future_beside_class_blocker(
        tmp_path,
        monkeypatch,
        baseline=_CLASS_SKIP_BASELINE,
        source=source,
        note=_CLASS_SKIP_NOTE,
    )


def test_hook_incremental_removes_unused_future_beside_soft_class_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = 'class C:\n    def z(self): pass\n    def a(self): pass\n'
    source = 'from __future__ import annotations\n\n' + baseline + '    def b(self): pass\n'
    _assert_hook_incremental_removes_unused_future_beside_class_blocker(
        tmp_path,
        monkeypatch,
        baseline=baseline,
        source=source,
        note=None,
    )


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
    init_git_repo(tmp_path, _SRC_NEEDS_SORT)
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
    prefix = _POSSIBLE_REASON_PREFIX if command.startswith('echo ') else _REASON_PREFIX
    reason = decision['permissionDecisionReason']
    assert reason.startswith(prefix)
    if command.startswith('echo '):
        assert 'command substitution' in reason
        assert 'to commit, use the supported form:' in reason


@pytest.mark.parametrize(
    ('command', 'decision'),
    [
        ("$'git' commit", 'deny'),
        ('git commit -m "$(cat <<\'MESSAGE\'\nsubject\nMESSAGE\n)"', 'allow'),
        ("cat > notes.md <<'EOF'\ngit log then commit\nEOF\n", 'allow'),
        ("cat > notes.md <<'EOF' && git commit -m x\nbody\nEOF\n", 'deny'),
    ],
)
def test_adapter_flow_classifies_quoted_commands_and_heredocs(
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
    message = (
        f'defsort: PostToolUse skipped {path.as_posix()}: syntax error: invalid syntax (line 1)'
    )
    if provider == 'antigravity':
        assert captured.out == ''
        assert captured.err == message + '\n'
    else:
        assert json.loads(captured.out) == {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': message,
            }
        }
        assert captured.err == ''
    assert path.read_text(encoding='utf-8') == source


@pytest.mark.skipif(os.name == 'nt', reason='Windows paths cannot contain control characters')
def test_hook_diagnostics_escape_blocker_paths_and_keep_line_breaks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_empty_git_repo(tmp_path)
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
    assert (
        reason == f'defsort: PostToolUse skipped {tmp_path}/broken\\x1b\\x85\\x0a.py: '
        'syntax error: invalid syntax (line 1)'
    )
    assert captured.err == ''


class TestUnknownInvocationDirectory:
    def test_absolute_route_replays_an_add_that_stages_new_content(self, tmp_path: Path) -> None:
        session_repo = tmp_path / 'a'
        target_repo = tmp_path / 'b'
        _stage_unsorted(session_repo)
        init_git_repo(target_repo, _SRC_NEEDS_SORT)
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
        init_git_repo(target_repo, _SRC_NEEDS_SORT)
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

        outcome = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': 'git commit -m x'},
            },
            allow_on_failure=True,
        )

        assert outcome == Outcome('deny', _UNKNOWN_CWD_REASON)


class TestHardLinkedNames:
    def test_commit_sync_leaves_names_needing_different_results(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        a, b = _names_under_two_orders(tmp_path)
        run_setup(['git', '-C', str(tmp_path), 'add', 'sorted', 'kept'])

        status, _, err = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert status == 0
        assert _staged_text(tmp_path, 'sorted/test.py') == _SRC_ALREADY_ORDERED
        assert _staged_text(tmp_path, 'kept/test.py') == _SRC_NEEDS_SORT
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert (
            'did not update kept/test.py, sorted/test.py in the working tree: names of one file'
            in err
        )
        assert b.stat().st_ino == a.stat().st_ino

    def test_commit_sync_name_whose_index_entry_fails_blocks_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)
        run_setup(['git', '-C', str(tmp_path), 'add', 'a.py', 'b.py'])
        real_prepare = hook_mod.prepare_index_entry

        def prepare(
            cwd: str, path: str, content: str, mode: str, *, encoding: str = 'utf-8'
        ) -> tuple[str, str, str] | None:
            if path == 'b.py':
                raise GitCommandError('fatal: example')
            return real_prepare(cwd, path, content, mode, encoding=encoding)

        monkeypatch.setattr(hook_mod, 'prepare_index_entry', prepare)
        status, _, err = invoke_cli(
            ['hook', '--agent', 'claude', '--allow-on-failure'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert status == 0
        assert _staged_text(tmp_path, 'a.py') == _SRC_ALREADY_ORDERED
        assert _staged_text(tmp_path, 'b.py') == _SRC_NEEDS_SORT
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert (
            'did not update a.py, b.py in the working tree: '
            'names of one file need different results' in err
        )

    def test_commit_sync_requires_every_staged_name_to_authorize_it(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)
        run_setup(['git', '-C', str(tmp_path), 'add', 'a.py'])
        _stage_text(tmp_path, 'b.py', _SRC_ALREADY_ORDERED)

        status, _, err = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert status == 0
        assert _staged_text(tmp_path, 'a.py') == _SRC_ALREADY_ORDERED
        assert _staged_text(tmp_path, 'b.py') == _SRC_ALREADY_ORDERED
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert 'did not update a.py, b.py in the working tree: b.py has unstaged changes' in err

    def test_commit_sync_writes_agreeing_names_once(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)
        run_setup(['git', '-C', str(tmp_path), 'add', 'a.py', 'b.py'])

        status, _, _ = invoke_cli(
            ['hook', '--agent', 'claude'],
            stdin=_pretooluse_payload('git commit -m test', tmp_path),
        )

        assert status == 0
        assert _staged_text(tmp_path, 'b.py') == _SRC_ALREADY_ORDERED
        assert b.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        assert a.stat().st_ino == b.stat().st_ino

    def test_post_tool_name_that_cannot_be_read_blocks_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_empty_git_repo(tmp_path)
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)
        real_read = hook_mod.read_file_result

        def deny_b(path: Path) -> object:
            if path.name == 'b.py':
                raise OSError('denied')
            return real_read(path)

        monkeypatch.setattr(hook_mod, 'read_file_result', deny_b)
        status, _, err = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=json.dumps(_codex_patch_event(tmp_path, [a, b])),
        )

        assert status == 0
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert 'names of one file need different results' in err

    def test_post_tool_name_whose_repository_is_unknown_blocks_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_empty_git_repo(tmp_path)
        known = tmp_path / 'known'
        unknown = tmp_path / 'unknown'
        known.mkdir()
        unknown.mkdir()
        a = known / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = unknown / 'b.py'
        os.link(a, b)
        real_discover = hook_mod.discover_repo_root

        def fail_for_unknown(cwd: str) -> RepoDiscoveryResult:
            if Path(cwd).name == 'unknown':
                return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)
            return real_discover(cwd)

        monkeypatch.setattr(hook_mod, 'discover_repo_root', fail_for_unknown)
        status, _, err = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=json.dumps(_codex_patch_event(tmp_path, [a, b])),
        )

        assert status == 0
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert 'names of one file need different results' in err

    def test_post_tool_skips_names_needing_different_results(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        a, b = _names_under_two_orders(tmp_path)

        status, _, err = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=json.dumps(_codex_patch_event(tmp_path, [a, b])),
        )

        assert status == 0
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        names = ', '.join(path.relative_to(tmp_path).as_posix() for path in (a, b))
        assert err == (
            f'defsort: PostToolUse skipped {names}: names of one file need different results\n'
        )

    def test_post_tool_writes_agreeing_names_once(self, tmp_path: Path) -> None:
        init_empty_git_repo(tmp_path)
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)

        status, _, _ = invoke_cli(
            ['hook', '--agent', 'codex'],
            stdin=json.dumps(_codex_patch_event(tmp_path, [a, b])),
        )

        assert status == 0
        assert b.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED

    def test_worktree_name_that_cannot_be_read_blocks_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)
        real_read = hook_mod.read_file_result

        def deny_b(path: Path) -> object:
            if path.name == 'b.py':
                raise OSError('denied')
            return real_read(path)

        monkeypatch.setattr(hook_mod, 'read_file_result', deny_b)
        result = _sort_worktree_files(_hook_ctx(tmp_path, allow_on_failure=True), ['a.py', 'b.py'])

        assert result.decision == 'allow'
        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert 'names of one file need different results' in capsys.readouterr().err

    @pytest.mark.parametrize('allow_on_failure', [False, True], ids=['deny', 'allow'])
    def test_worktree_paths_refuse_names_needing_different_results(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], allow_on_failure: bool
    ) -> None:
        a, _b = _names_under_two_orders(tmp_path)
        c = tmp_path / 'c.py'
        c.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        result = _sort_worktree_files(
            _hook_ctx(tmp_path, allow_on_failure=allow_on_failure),
            ['c.py', 'sorted/test.py', 'kept/test.py'],
        )

        assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        if allow_on_failure:
            assert result.decision == 'allow'
            assert c.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
            assert 'names of one file need different results' in capsys.readouterr().err
        else:
            assert result.decision == 'deny'
            assert c.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_worktree_paths_write_agreeing_names_once(self, tmp_path: Path) -> None:
        a = tmp_path / 'a.py'
        a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        b = tmp_path / 'b.py'
        os.link(a, b)

        result = _sort_worktree_files(_hook_ctx(tmp_path), ['a.py', 'b.py'])

        assert result.decision == 'allow'
        assert b.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        assert a.stat().st_ino == b.stat().st_ino


def test_post_tool_notes_keep_payload_order_across_read_and_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_setup(['git', 'init'], cwd=tmp_path)
    first, second = [tmp_path / name for name in ('first.py', 'second.py')]
    for path in (first, second):
        path.write_text(_SRC_NEEDS_SORT)
    read = hook_mod.read_file_result

    def read_or_fail(path: Path) -> _ReadResult:
        if path == second:
            raise PermissionError(13, 'Permission denied')
        return read(path)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(hook_mod, 'read_file_result', read_or_fail)
    monkeypatch.setattr(hook_mod, 'write_source', fail_write)
    outcome = hook_mod.run_post_tool([str(first), str(second)], str(tmp_path))
    assert outcome == Outcome('allow')
    assert capsys.readouterr().err.splitlines() == [
        'defsort: PostToolUse skipped first.py: cannot write it: disk full',
        'defsort: PostToolUse skipped second.py: cannot read it: Permission denied',
    ]
    assert all(path.read_text() == _SRC_NEEDS_SORT for path in (first, second))


def test_post_tool_other_capitalization_keeps_untouched_order(tmp_path: Path) -> None:
    init_empty_git_repo(tmp_path)
    path = tmp_path / 'Mod.py'
    baseline = 'def beta():\n    pass\n\n\ndef alpha():\n    pass\n'
    path.write_text(baseline, encoding='utf-8')
    run_setup(['git', 'add', 'Mod.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    source = (baseline + '\n\ndef gamma():\n    pass\n').encode()
    path.write_bytes(source)
    other = tmp_path / 'mod.py'
    if not other.exists():
        other.hardlink_to(path)
    results: list[subprocess.CompletedProcess[str]] = []
    for selected in (path, other):
        payload = json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(selected)},
            }
        )
        results.append(
            run_cli(
                ['hook', '--agent', 'claude', '-i', '--definition-order', 'alphabetical'],
                stdin=payload,
                cwd=tmp_path,
            )
        )
    assert path.read_bytes() == source
    exact, alternate = results
    assert alternate.returncode == exact.returncode
    assert alternate.stdout.replace('mod.py', 'Mod.py') == exact.stdout
    assert alternate.stderr.replace('mod.py', 'Mod.py') == exact.stderr


def test_post_tool_read_failure_reports_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_empty_git_repo(tmp_path)
    target = tmp_path / 'pkg' / 'mod.py'
    target.parent.mkdir()
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(hook_mod, 'read_file_result', fail_read)
    hook_mod.run_post_tool([str(target)], str(tmp_path))
    assert 'cannot read it: Permission denied' in capsys.readouterr().err
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


def test_post_tool_sort_error_names_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / 'mod.py'
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValueError('bad\nmore')

    monkeypatch.setattr(hook_mod, '_plan_file_sort', explode)
    assert hook_mod.run_post_tool([str(target)], str(tmp_path)) == Outcome('allow')
    captured = capsys.readouterr()
    assert (
        f'defsort: PostToolUse skipped {target.as_posix()}: unexpected error: ValueError: bad\n'
        in captured.err
    )
    assert 'Traceback' in captured.err
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


class TestPrivateLabelHookFailures:
    @pytest.mark.parametrize(
        ('command', 'stage'),
        [
            ('git commit -m labels', True),
            ('git commit -a -m labels', False),
            ('git commit -m labels -- test.py', False),
        ],
    )
    def test_label_failure_denies_the_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        *,
        stage: bool,
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        if stage:
            run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        monkeypatch.setattr(hook_mod, 'private_paths', _failed_private_paths)

        assert _run_in_process(_pretooluse_payload(command, tmp_path)) == 0
        captured = capsys.readouterr()
        assert _deny_reason(captured.out, captured.err, 'claude', 'defsort:') == (
            'defsort: failed to read private labels: fatal: check-attr failed'
        )
        assert (tmp_path / 'test.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize('allow_on_failure', [False, True], ids=['deny', 'allow'])
    def test_include_label_failure_on_the_second_list(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        *,
        allow_on_failure: bool,
    ) -> None:
        pathspec, staged_before = _setup_include_failure_repo(tmp_path)
        calls: list[list[str]] = []

        def fail_second(_root: str, paths: list[str]) -> tuple[frozenset[str], str | None]:
            calls.append(paths)
            if len(calls) == 1:
                return frozenset(), None
            return frozenset(), 'fatal: check-attr failed'

        monkeypatch.setattr(hook_mod, 'private_paths', fail_second)
        assert (
            _run_in_process(
                _pretooluse_payload('git commit -i -m labels -- pathspec.py', tmp_path),
                allow_on_failure=allow_on_failure,
            )
            == 0
        )
        captured = capsys.readouterr()
        assert calls == [['pathspec.py'], ['staged.py']]
        assert _staged_bytes(tmp_path, 'staged.py') == staged_before
        if allow_on_failure:
            assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
            assert captured.err == (
                'WARNING: defsort: failed to read private labels: fatal: check-attr failed\n'
            )
            assert pathspec.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        else:
            assert _deny_reason(captured.out, captured.err, 'claude', 'defsort:') == (
                'defsort: failed to read private labels: fatal: check-attr failed'
            )
            assert pathspec.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    def test_label_failure_warns_under_allow_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        run_setup(['git', '-C', str(tmp_path), 'add', 'test.py'])
        monkeypatch.setattr(hook_mod, 'private_paths', _failed_private_paths)

        assert (
            _run_in_process(
                _pretooluse_payload('git commit -m labels', tmp_path), allow_on_failure=True
            )
            == 0
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err == (
            'WARNING: defsort: failed to read private labels: fatal: check-attr failed\n'
        )

    def test_post_tool_use_formats_a_private_file(self, tmp_path: Path) -> None:
        init_git_repo(tmp_path, '# placeholder\n')
        drafts = tmp_path / 'drafts'
        drafts.mkdir()
        (drafts / '.gitattributes').write_text('* private\n', encoding='utf-8')
        path = drafts / 'p.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

        assert _post_tool(json.loads(_posttooluse_payload('claude', path, tmp_path))) == 0
        assert path.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED


@pytest.mark.parametrize(
    'case',
    [
        ('get_staged_py_info', 'git commit -m test', False, 'failed to list staged files'),
        ('read_staged_blobs', 'git commit -m test', False, 'failed to read staged contents'),
        ('read_baseline_blobs', 'git commit -m test', True, 'failed to read baselines'),
        ('head_rename_sources', 'git commit --all -m test', True, 'failed to pair renamed files'),
        (
            'batch_fetch_worktree_baselines',
            'git commit --all -m test',
            True,
            'failed to read baselines',
        ),
        (
            'match_tracked_py_files',
            'git commit test.py -m test',
            False,
            'cannot use pathspecs',
        ),
        (
            'get_modified_tracked_py_files',
            'git commit test.py -m test',
            False,
            'failed to discover modified files',
        ),
        (
            'get_modified_tracked_py_files',
            'git commit --all -m test',
            False,
            'failed to discover modified files',
        ),
        (
            'prepare_index_entry',
            'git commit -m test',
            False,
            'failed to prepare index entry for test.py',
        ),
        ('apply_index_entries', 'git commit -m test', False, 'failed to update git index'),
    ],
)
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_git_site_names_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str, bool, str],
    allow_on_failure: bool,
) -> None:
    site, command, incremental, message = case
    init_git_repo(tmp_path, '# baseline\n')
    target = tmp_path / 'test.py'
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    run_setup(['git', 'add', 'test.py'], cwd=tmp_path)

    def fail_git(*_args: object, **_kwargs: object) -> None:
        raise GitCommandError('index unavailable\x1b[31m')

    monkeypatch.setattr(hook_mod, site, fail_git)
    _run_in_process(
        _pretooluse_payload(command, tmp_path),
        overrides=SortOverrides(incremental=incremental),
        allow_on_failure=allow_on_failure,
    )
    captured = capsys.readouterr()
    expected = f'defsort: {message}: index unavailable\\x1b[31m'
    assert expected in captured.err
    assert '\x1b' not in captured.err
    if not allow_on_failure:
        assert (
            json.loads(captured.out)['hookSpecificOutput']['permissionDecisionReason'] == expected
        )
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


def test_sync_failure_names_cause_and_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / 'pkg' / 'mod.py'
    entry = hook_mod._StagedFileEntry(
        'pkg/mod.py', 'before', 'after', '100644', 'utf-8', None, is_new=True
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied', str(target))

    monkeypatch.setattr(hook_mod, 'read_file_result', refuse)
    assert hook_mod._plan_worktree_sync(_hook_ctx(tmp_path), entry, target, ResolvedConfig()) == (
        None,
        'cannot read it: Permission denied',
    )
    assert capsys.readouterr().err == ''


def test_sync_note_names_only_the_unstaged_alias(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_empty_git_repo(tmp_path)
    a = tmp_path / 'a.py'
    a.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    upper = tmp_path / 'A.py'
    if not upper.exists():
        os.link(a, upper)
    _stage_text(tmp_path, 'a.py', _SRC_NEEDS_SORT)
    _stage_text(tmp_path, 'A.py', _SRC_NEEDS_SORT.replace('pass', 'return 1'))
    identity = a.stat().st_ino
    assert _run_sync_commit(tmp_path) == Outcome(
        'allow', 'defsort: sorted 2 staged Python files for this commit'
    )
    assert capsys.readouterr().err == (
        'defsort: did not update A.py, a.py in the working tree: A.py has unstaged changes; '
        'the commit has the sorted version\n'
    )
    assert a.stat().st_ino == upper.stat().st_ino == identity
    assert a.read_text(encoding='utf-8') == _SRC_NEEDS_SORT
    assert _staged_text(tmp_path, 'a.py') == _SRC_ALREADY_ORDERED
    assert _staged_text(tmp_path, 'A.py') == _SRC_ALREADY_ORDERED.replace('pass', 'return 1')


def test_too_deep_source_post_tool_names_the_limit_without_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git_config(monkeypatch)
    init_git_repo(tmp_path, TOO_DEEP)
    path = tmp_path / 'test.py'
    payload = json.dumps(
        {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(path)},
            'cwd': str(tmp_path),
        }
    )
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude', '--no-incremental'], stdin=payload, cwd=tmp_path
    )
    assert status == EXIT_OK, stderr
    additional_context = json.loads(stdout)['hookSpecificOutput']['additionalContext']
    assert additional_context == (
        'defsort: PostToolUse skipped test.py: syntax error: '
        "source nests too deeply for Python's parser on this platform"
    )
    assert path.read_text(encoding='utf-8') == TOO_DEEP


@pytest.mark.parametrize('operation', ['read', 'write'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_worktree_failure_names_cause_and_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    allow_on_failure: bool,
) -> None:
    target = tmp_path / 'pkg' / 'mod.py'
    target.parent.mkdir()
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied', str(target))

    monkeypatch.setattr(
        hook_mod, 'read_file_result' if operation == 'read' else 'write_source', refuse
    )
    outcome = _sort_worktree_files(
        _hook_ctx(tmp_path, allow_on_failure=allow_on_failure), ['pkg/mod.py']
    )
    expected = f'defsort: cannot {operation} pkg/mod.py: Permission denied'
    assert outcome.decision == ('allow' if allow_on_failure else 'deny')
    if allow_on_failure:
        assert f'WARNING: {expected}' in capsys.readouterr().err
    else:
        assert outcome.reason == expected
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


@pytest.mark.parametrize(
    'status', [BlobReadStatus.MISSING, BlobReadStatus.INFRA_ERROR, BlobReadStatus.DECODE_FAILED]
)
def test_staged_blob_failure_names_cause(tmp_path: Path, status: BlobReadStatus) -> None:
    detail = None if status is BlobReadStatus.MISSING else 'invalid blob'
    prepared = hook_mod._PreparedStagedFile(
        'pkg/mod.py',
        '100644',
        BlobReadResult(status=status, detail=detail),
        ResolvedConfig(),
        None,
        is_new=True,
    )
    outcome = hook_mod._sort_staged_files(_hook_ctx(tmp_path), prepared_files=[prepared])
    expected = detail or 'blob missing from git cat-file output'
    assert outcome.reason == f'defsort: failed to read staged content for pkg/mod.py: {expected}'


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_discovery_failure_names_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allow_on_failure: bool,
) -> None:
    def fail_discovery(_cwd: str) -> RepoDiscoveryResult:
        return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail='repository unavailable')

    monkeypatch.setattr(hook_mod, 'discover_repo_root', fail_discovery)
    _run_in_process(
        _pretooluse_payload('git commit -m test', tmp_path), allow_on_failure=allow_on_failure
    )
    captured = capsys.readouterr()
    expected = 'defsort: failed to discover git work tree: repository unavailable'
    if allow_on_failure:
        assert captured.err == f'WARNING: {expected}\n'
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
    else:
        assert (
            json.loads(captured.out)['hookSpecificOutput']['permissionDecisionReason'] == expected
        )


@pytest.fixture
def hook_key_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'global.gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_ATTR_NOSYSTEM', '1')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.delenv('GIT_CONFIG_COUNT', raising=False)
    repo = tmp_path / 'repo'
    repo.mkdir()
    run_setup(['git', 'init', str(repo)])
    return repo


def test_false_hook_key_skips_discovery(
    hook_key_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_setup(['git', 'config', 'defsort.hook', 'false'], cwd=hook_key_repo)

    def discovery_forbidden(_cwd: str) -> None:
        pytest.fail('a disabled hook must not discover the repository')

    monkeypatch.setattr(hook_mod, 'discover_repo_root', discovery_forbidden)
    payload = json.dumps(
        {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': 'git commit -m test'},
            'cwd': str(hook_key_repo),
        }
    )
    assert _run_in_process(payload) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize('policy', ['deny', 'flag', 'root-config'])
@pytest.mark.parametrize('broken_config', [False, True])
def test_pre_tool_malformed_hook_key_preserves_index(
    hook_key_repo: Path, capsys: pytest.CaptureFixture[str], policy: str, *, broken_config: bool
) -> None:
    path = hook_key_repo / 'target.py'
    path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    run_setup(['git', 'add', 'target.py'], cwd=hook_key_repo)
    before = (hook_key_repo / '.git' / 'index').read_bytes()
    path.write_text(path.read_text() + '# unstaged\n', encoding='utf-8')
    worktree_before = path.read_bytes()
    if policy == 'root-config':
        (hook_key_repo / 'pyproject.toml').write_text(
            '[tool.defsort]\nallow-on-failure = true\n', encoding='utf-8'
        )
    cwd = hook_key_repo / 'subdir'
    cwd.mkdir()
    if broken_config:
        # The existing discovery-error policy resolves from the command directory.
        cwd = hook_key_repo
        config = hook_key_repo / '.git' / 'config'
        config.write_bytes(config.read_bytes() + b'\n[broken\n')
    else:
        run_setup(['git', 'config', 'defsort.hook', 'flase'], cwd=hook_key_repo)
    command = 'git add -A && git commit -m test'
    payload = json.dumps(
        {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': command},
            'cwd': str(cwd),
        }
    )

    assert _run_in_process(payload, allow_on_failure=policy == 'flag') == 0
    captured = capsys.readouterr()
    prefix = 'defsort: cannot read git config defsort.hook: '
    if policy == 'deny':
        output = json.loads(captured.out)['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        reason = output['permissionDecisionReason']
        assert reason.startswith(prefix)
        assert reason.removeprefix(prefix)
    else:
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err.startswith('WARNING: ' + prefix)
        assert captured.err.removeprefix('WARNING: ' + prefix).strip()
    assert (hook_key_repo / '.git' / 'index').read_bytes() == before
    assert path.read_bytes() == worktree_before


@pytest.mark.parametrize('policy', ['deny', 'flag', 'config'])
def test_pre_tool_global_malformed_hook_key_outside_worktree(
    hook_key_repo: Path, capsys: pytest.CaptureFixture[str], policy: str
) -> None:
    cwd = hook_key_repo.parent
    (cwd / 'global.gitconfig').write_text('[defsort]\nhook = flase\n', encoding='utf-8')
    if policy == 'config':
        (cwd / 'pyproject.toml').write_text('[tool.defsort]\nallow-on-failure = true\n')
    payload = json.dumps(
        {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': 'git commit -m test'},
            'cwd': str(cwd),
        }
    )
    assert _run_in_process(payload, allow_on_failure=policy == 'flag') == 0
    captured = capsys.readouterr()
    prefix = 'defsort: cannot read git config defsort.hook: '
    if policy == 'deny':
        output = json.loads(captured.out)['hookSpecificOutput']
        assert output['permissionDecision'] == 'deny'
        assert output['permissionDecisionReason'].startswith(prefix)
        assert output['permissionDecisionReason'].removeprefix(prefix)
    else:
        assert json.loads(captured.out) == {'systemMessage': captured.err.rstrip('\n')}
        assert captured.err.startswith('WARNING: ' + prefix)


@pytest.mark.parametrize('failure', ['malformed', 'launch'])
def test_post_tool_malformed_hook_key_skips_file(
    hook_key_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    cwd = hook_key_repo if failure == 'malformed' else hook_key_repo.parent
    path = cwd / 'target.py'
    path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    before = path.read_bytes()
    if failure == 'malformed':
        run_setup(['git', 'config', 'defsort.hook', 'flase'], cwd=cwd)
    else:

        def missing_git(_cwd: str | None = None) -> bool:
            raise GitCommandError('git could not be run: x')

        monkeypatch.setattr(hook_mod, 'is_hook_enabled', missing_git)
    payload = json.dumps(
        {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(path)},
            'cwd': str(cwd),
        }
    )
    assert _run_in_process(payload) == 0
    captured = capsys.readouterr()
    display = path.name if failure == 'malformed' else path.as_posix()
    prefix = f'defsort: PostToolUse skipped {display}: cannot read git config defsort.hook: '
    assert captured.err.startswith(prefix)
    assert captured.err.removeprefix(prefix).strip()
    context = json.loads(captured.out)['hookSpecificOutput']['additionalContext']
    assert captured.err.strip() in context
    assert path.read_bytes() == before


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize(
    'command',
    [
        'git commit -m test',
        'git commit -a -m test',
        'git commit m.py -m test',
        'git commit --include m.py -m test',
    ],
)
def test_class_only_skip_allows_commit(tmp_path: Path, incremental: bool, command: str) -> None:
    path = _prepare_class_skip(tmp_path, _CLASS_SKIP_SOURCE, incremental=incremental)
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload(command, tmp_path)
    )
    output = json.loads(stdout)
    assert status == 0
    assert 'hookSpecificOutput' not in output
    assert output['systemMessage'] == _CLASS_SKIP_NOTE
    assert stderr.count(_CLASS_SKIP_NOTE) == 1
    assert path.read_text(encoding='utf-8') == _CLASS_SKIP_SOURCE
    assert (
        decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout) == _CLASS_SKIP_SOURCE
    )


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_class_skip_with_other_changes_applies_them(
    tmp_path: Path, incremental: bool, partial: bool
) -> None:
    source = _CLASS_SKIP_SOURCE + '\n\ndef zeta(): return alpha()\n\ndef alpha(): pass\n'
    path = _prepare_class_skip(tmp_path, source, incremental=incremental)
    worktree = source + '\n# unstaged\n' if partial else source
    path.write_text(worktree, encoding='utf-8')
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload('git commit -m test', tmp_path)
    )
    output = json.loads(stdout)
    assert status == 0
    assert 'hookSpecificOutput' not in output
    assert output['systemMessage'].count(_CLASS_SKIP_NOTE) == 1
    assert stderr.count(_CLASS_SKIP_NOTE) == 1
    staged = decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout)
    assert staged.startswith(_CLASS_SKIP_SOURCE)
    assert staged.index('def alpha') < staged.index('def zeta')
    assert path.read_text(encoding='utf-8') == (worktree if partial else staged)


@pytest.mark.parametrize('incremental', [False, True])
def test_class_only_skip_preserves_partial_staging(tmp_path: Path, incremental: bool) -> None:
    path = _prepare_class_skip(tmp_path, _CLASS_SKIP_SOURCE, incremental=incremental)
    worktree = _CLASS_SKIP_SOURCE + '\n# unstaged\n'
    path.write_text(worktree, encoding='utf-8')
    status, stdout, _stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload('git commit -m test', tmp_path)
    )
    assert status == 0
    assert json.loads(stdout)['systemMessage'] == _CLASS_SKIP_NOTE
    assert path.read_text(encoding='utf-8') == worktree
    assert (
        decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout) == _CLASS_SKIP_SOURCE
    )


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('other_changes', [False, True])
def test_post_tool_class_only_skip_reports_context(
    tmp_path: Path, incremental: bool, other_changes: bool
) -> None:
    source = _CLASS_SKIP_SOURCE
    if other_changes:
        source += '\n\ndef zeta(): return alpha()\n\ndef alpha(): pass\n'
    path = _prepare_class_skip(tmp_path, source, incremental=incremental)
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'],
        stdin=json.dumps(
            {
                'hook_event_name': 'PostToolUse',
                'tool_name': 'Edit',
                'cwd': str(tmp_path),
                'tool_input': {'file_path': str(path)},
            }
        ),
    )
    assert status == 0
    assert json.loads(stdout)['hookSpecificOutput']['additionalContext'] == _CLASS_SKIP_NOTE
    assert stderr.count(_CLASS_SKIP_NOTE) == 1
    rewritten = path.read_text(encoding='utf-8')
    assert rewritten.startswith(_CLASS_SKIP_SOURCE)
    if other_changes:
        assert rewritten.index('def alpha') < rewritten.index('def zeta')
    else:
        assert rewritten == source


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('second_name', ['A', 'C', 'D'])
def test_two_blocked_classes_report_two_notes(
    tmp_path: Path, incremental: bool, second_name: str
) -> None:
    source = (
        _CLASS_SKIP_SOURCE + f'\n\nclass {second_name}:\n'
        '    def z(self): pass\n    def a(self): pass\n    def z(self): pass\n'
    )
    path = _prepare_class_skip(tmp_path, source, incremental=incremental)
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload('git commit -m test', tmp_path)
    )
    assert status == 0
    output = json.loads(stdout)
    assert 'hookSpecificOutput' not in output
    expected_notes = [
        _CLASS_SKIP_NOTE,
        f'defsort: m.py: class {second_name}: skipped (duplicate method names)',
    ]
    assert [line for line in output['systemMessage'].splitlines() if 'skipped (' in line] == (
        expected_notes
    )
    assert [line for line in stderr.splitlines() if 'skipped (' in line] == expected_notes
    staged = decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout)
    assert _CLASS_SKIP_SOURCE in staged
    assert source[len(_CLASS_SKIP_SOURCE) :].strip() in staged
    assert path.read_text(encoding='utf-8') == staged
    if second_name == 'A':
        assert staged.index('class A:') < staged.index('class C:')


@pytest.mark.parametrize('incremental', [False, True])
def test_duplicate_method_class_skip_allows(tmp_path: Path, incremental: bool) -> None:
    source = 'class C:\n    def z(self): pass\n    def a(self): pass\n    def z(self): pass\n'
    _prepare_class_skip(tmp_path, source, incremental=incremental)
    status, stdout, _stderr = invoke_cli(
        ['hook', '--agent', 'claude'], stdin=_pretooluse_payload('git commit -m test', tmp_path)
    )
    assert status == 0
    assert json.loads(stdout)['systemMessage'] == (
        'defsort: m.py: class C: skipped (duplicate method names)'
    )


@pytest.mark.parametrize(
    'case',
    [
        'method',
        'method_banner',
        'future',
        'duplicates_method',
        'sectioned_method',
        'top_level',
        'duplicates_top_level',
        'sectioned_top_level',
        'unchanged',
        'already_placed',
        'none',
        'non_conforming',
    ],
)
def test_incremental_gate_candidate(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, source, banners, definition_order, blocker = incremental_gate_case(case)
    isolate_git_config(monkeypatch)
    source += '# edited\n'
    path = _prepare_class_skip(tmp_path, source, incremental=True, baseline=baseline)
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\nincremental = true\nmethod-order = "alphabetical"\n'
        f'definition-order = "{definition_order}"\nbanners = "{banners}"\n',
        encoding='utf-8',
    )
    before = run_setup(['git', 'ls-files', '-s'], cwd=tmp_path).stdout
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude'],
        stdin=_pretooluse_payload('git commit -m test', tmp_path),
    )

    assert status == EXIT_OK, stderr
    output = json.loads(stdout or '{}')
    if blocker in {'interstitial', 'duplicates'}:
        specific = output['hookSpecificOutput']
        assert specific['permissionDecision'] == 'deny'
        reason = specific['permissionDecisionReason']
        assert _BLOCKER_MSGS[blocker].format(path='m.py') in reason
    else:
        assert 'hookSpecificOutput' not in output
        if blocker == 'non_conforming':
            assert 'baseline' in stderr
    staged = decode_text(run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout)
    if case == 'none':
        assert staged.index('def a(') < staged.index('def z(')
        assert path.read_text(encoding='utf-8') == staged
    else:
        assert staged == source
        assert path.read_text(encoding='utf-8') == source
        assert run_setup(['git', 'ls-files', '-s'], cwd=tmp_path).stdout == before


@pytest.mark.parametrize(
    'command',
    [
        'git commit -m test',
        'git commit -a -m test',
        'git commit -m test -- m.py other.py',
        'git commit --include m.py -m test',
        'git commit --include other.py -m test',
    ],
    ids=['staged', 'all', 'pathspec', 'include-worktree', 'include-staged'],
)
class TestPreToolBaselineFailures:
    def test_bad_baseline_cookie_skips_with_cause(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
    ) -> None:
        _setup_baseline_failure_repo(tmp_path, baseline='# -*- coding: nosuchcodec -*-\n')

        outcome = _pre_tool(
            json.loads(_pretooluse_payload(command, tmp_path)),
            overrides=SortOverrides(incremental=True),
        )

        assert outcome.decision == 'allow'
        stderr = capsys.readouterr().err
        assert 'defsort: m.py: skipped (baseline cannot be decoded: ' in stderr
        assert 'nosuchcodec' in stderr
        assert 'WARNING:' not in stderr
        assert (tmp_path / 'm.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert (tmp_path / 'other.py').read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        assert run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()

    @pytest.mark.parametrize('policy', ['incremental', 'guard', 'both'])
    def test_infra_baseline_denies_in_pre_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, policy: str
    ) -> None:
        _setup_baseline_failure_repo(tmp_path)
        _stub_failed_baseline(
            monkeypatch, BlobReadStatus.INFRA_ERROR, 'unexpected git cat-file output'
        )
        config = '[tool.defsort]\n'
        if policy != 'guard':
            config += 'incremental = true\n'
        if policy != 'incremental':
            config += 'max-reorder-count = 100\n'
        (tmp_path / 'pyproject.toml').write_text(config, encoding='utf-8')

        outcome = _pre_tool(json.loads(_pretooluse_payload(command, tmp_path)))

        assert outcome.decision == 'deny'
        assert outcome.reason == (
            'defsort: failed to read baseline for m.py: unexpected git cat-file output'
        )
        assert (tmp_path / 'm.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()

    def test_infra_baseline_warns_and_continues_under_allow_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _setup_baseline_failure_repo(tmp_path)
        _stub_failed_baseline(
            monkeypatch, BlobReadStatus.INFRA_ERROR, 'unexpected git cat-file output\x1b[31m'
        )

        outcome = _pre_tool(
            json.loads(_pretooluse_payload(command, tmp_path)),
            overrides=SortOverrides(incremental=True),
            allow_on_failure=True,
        )

        assert outcome.decision == 'allow'
        assert (
            'WARNING: defsort: failed to read baseline for m.py: '
            'unexpected git cat-file output\\x1b[31m'
        ) in capsys.readouterr().err
        assert (tmp_path / 'm.py').read_text(encoding='utf-8') == _SRC_NEEDS_SORT
        assert (tmp_path / 'other.py').read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        assert run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()


def _setup_baseline_failure_repo(tmp_path: Path, *, baseline: str = '# baseline\n') -> None:
    init_empty_git_repo(tmp_path)
    (tmp_path / 'm.py').write_bytes(baseline.encode('utf-8'))
    (tmp_path / 'other.py').write_bytes(b'# baseline\n')
    run_setup(['git', 'add', '.'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    for name in ('m.py', 'other.py'):
        (tmp_path / name).write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))
    run_setup(['git', 'add', '.'], cwd=tmp_path)


def _stub_failed_baseline(
    monkeypatch: pytest.MonkeyPatch, status: BlobReadStatus, detail: str | None = None
) -> None:
    staged_read = hook_mod.read_baseline_blobs
    worktree_read = hook_mod.batch_fetch_worktree_baselines
    head_read = hook_mod.read_head_baselines
    failure = BlobReadResult(status=status, detail=detail)

    def read_staged(cwd: str, infos: Iterable[StagedFileInfo]) -> dict[str, BlobReadResult]:
        results = staged_read(cwd, infos)
        results['m.py'] = failure
        return results

    def read_worktree(
        cwd: str, paths: Iterable[str], *, sources: Mapping[str, str] | None = None
    ) -> dict[str, BlobReadResult]:
        results = worktree_read(cwd, paths, sources=sources)
        results['m.py'] = failure
        return results

    def read_head(cwd: str, paths: Sequence[Path]) -> dict[Path, BlobReadResult]:
        results = head_read(cwd, paths)
        results[Path(cwd) / 'm.py'] = failure
        return results

    monkeypatch.setattr(hook_mod, 'read_head_baselines', read_head)
    monkeypatch.setattr(hook_mod, 'read_baseline_blobs', read_staged)
    monkeypatch.setattr(hook_mod, 'batch_fetch_worktree_baselines', read_worktree)


def test_missing_baseline_for_existing_file_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_baseline_failure_repo(tmp_path)
    _stub_failed_baseline(monkeypatch, BlobReadStatus.MISSING)

    outcome = _pre_tool(
        json.loads(_pretooluse_payload('git commit -m test', tmp_path)),
        overrides=SortOverrides(incremental=True),
    )

    assert outcome.decision == 'deny'
    assert outcome.reason == (
        'defsort: failed to read baseline for m.py: blob missing from git cat-file output'
    )
    assert run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout == _SRC_NEEDS_SORT.encode()


def test_missing_baseline_for_new_file_sorts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_empty_git_repo(tmp_path)
    target = tmp_path / 'm.py'
    target.write_bytes(_SRC_NEEDS_SORT.encode('utf-8'))
    run_setup(['git', 'add', '.'], cwd=tmp_path)
    _stub_failed_baseline(monkeypatch, BlobReadStatus.MISSING)

    outcome = _pre_tool(
        json.loads(_pretooluse_payload('git commit -m test', tmp_path)),
        overrides=SortOverrides(incremental=True),
    )

    assert outcome.decision == 'allow'
    assert target.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
    assert (
        run_setup(['git', 'show', ':m.py'], cwd=tmp_path).stdout == _SRC_ALREADY_ORDERED.encode()
    )


@pytest.mark.parametrize('status', [BlobReadStatus.INFRA_ERROR, BlobReadStatus.DECODE_FAILED])
def test_post_tool_baseline_failure_stays_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: BlobReadStatus,
) -> None:
    _setup_baseline_failure_repo(tmp_path)
    _stub_failed_baseline(monkeypatch, status, 'unreadable baseline')
    target = tmp_path / 'm.py'

    assert (
        _post_tool(
            {'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}},
            overrides=SortOverrides(incremental=True),
        )
        == 0
    )

    assert capsys.readouterr().err == ''
    assert target.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


@pytest.mark.parametrize('kind', DEEP_DEFINITIONS)
@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_deep_expression_in_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, event: str
) -> None:
    isolate_git_config(monkeypatch)
    source, expected = deep_source_case(kind)
    init_git_repo(tmp_path, source)
    path = tmp_path / 'test.py'
    if event == 'PreToolUse':
        run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
        payload = _pretooluse_payload('git commit -m test', tmp_path)
    else:
        payload = json.dumps(
            {
                'hook_event_name': event,
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(path)},
                'cwd': str(tmp_path),
            }
        )
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude', '--no-incremental'], stdin=payload, cwd=tmp_path
    )
    assert status == EXIT_OK, stderr
    assert 'Traceback' not in stdout + stderr
    assert path.read_text(encoding='utf-8') == expected
    if event == 'PreToolUse':
        assert _staged_text(tmp_path, 'test.py') == expected


@pytest.mark.parametrize('kind', [*DEEP_DEFINITIONS, 'too-deep'])
def test_deep_baseline_does_not_crash_post_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    isolate_git_config(monkeypatch)
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\nmax-reorder-count = 1\n', encoding='utf-8'
    )
    baseline = TOO_DEEP if kind == 'too-deep' else deep_source_case(kind)[1]
    init_git_repo(tmp_path, baseline)
    run_setup(['git', 'add', 'test.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'deep baseline'], cwd=tmp_path)
    path = tmp_path / 'test.py'
    path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    payload = json.dumps(
        {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(path)},
            'cwd': str(tmp_path),
        }
    )
    status, stdout, stderr = invoke_cli(
        ['hook', '--agent', 'claude', '--incremental'],
        stdin=payload,
        cwd=tmp_path,
    )
    assert status == EXIT_OK, stderr
    assert 'Traceback' not in stdout + stderr


def _pathspec_commit(
    repo: Path,
    *,
    nul: bool = False,
    name: str = 'paths',
    allow_on_failure: bool = False,
    replay: bool = True,
) -> Outcome:
    plan = hook_mod.CommitPlan(
        cwd=str(repo),
        mode=CommitMode(
            auto_stage=False,
            include=False,
            only=False,
            pathspecs=(),
            pathspec_from_file=name,
            pathspec_file_nul=nul,
        ),
        pre_commit_adds=(PreCommitAdd(('git', 'add', 'a.py'), str(repo), str(repo)),)
        if replay
        else (),
    )
    return hook_mod.run_commit(
        plan,
        allow_on_failure=allow_on_failure,
        overrides=SortOverrides(incremental=False),
    )


def test_pathspec_git_failure_names_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pathspec_repo(tmp_path)
    (tmp_path / 'paths').write_bytes(b'a.py\n')

    def fail_matching(*_args: object, **_kwargs: object) -> list[str]:
        raise GitCommandError('fatal: x')

    monkeypatch.setattr(hook_mod, 'match_tracked_py_files', fail_matching)
    result = _pathspec_commit(tmp_path, replay=False)
    assert result.decision == 'deny'
    assert result.reason == 'defsort: cannot use pathspecs: fatal: x'


@pytest.mark.parametrize(
    ('data', 'nul', 'detail'),
    [
        (b'a.py\n\n', False, 'empty pathspec record'),
        (b'""\n', False, 'empty pathspec record'),
        (b'\0', True, 'empty pathspec record'),
        (b'"a.py\n', False, 'malformed quoted pathspec record'),
    ],
)
def test_refused_pathspec_file_changes_nothing(
    tmp_path: Path, data: bytes, *, nul: bool, detail: str
) -> None:
    _pathspec_repo(tmp_path)
    path = tmp_path / 'paths'
    path.write_bytes(data)
    index = (tmp_path / '.git' / 'index').read_bytes()
    source = (tmp_path / 'a.py').read_bytes()
    result = _pathspec_commit(tmp_path, nul=nul)
    assert result.decision == 'deny'
    assert result.reason == f'defsort: cannot use pathspec file paths: {detail}'
    assert (tmp_path / '.git' / 'index').read_bytes() == index
    assert (tmp_path / 'a.py').read_bytes() == source
    assert path.read_bytes() == data
    path.write_bytes(b'a.py\0' if nul else b'a.py\n')
    assert _pathspec_commit(tmp_path, nul=nul).decision == 'allow'
    assert (tmp_path / '.git' / 'index').read_bytes() != index


def test_unreadable_pathspec_file_names_the_reason(tmp_path: Path) -> None:
    _pathspec_repo(tmp_path)
    index = (tmp_path / '.git' / 'index').read_bytes()
    with pytest.raises(FileNotFoundError) as caught:
        (tmp_path / 'missing').stat()
    result = _pathspec_commit(tmp_path, name='missing')
    assert result.reason == (
        f'defsort: cannot read pathspec file missing: {caught.value.strerror}'
    )
    assert result.decision == 'deny'
    assert (tmp_path / '.git' / 'index').read_bytes() == index


@pytest.mark.skipif(os.name != 'nt', reason='Windows pathspec transport')
def test_windows_refuses_an_undecodable_pathspec_record(tmp_path: Path) -> None:
    _pathspec_repo(tmp_path)
    (tmp_path / 'paths').write_bytes(b'bad-\xff.py\0')
    index = (tmp_path / '.git' / 'index').read_bytes()
    result = _pathspec_commit(tmp_path, nul=True)
    assert result.decision == 'deny'
    assert result.reason == (
        'defsort: cannot use pathspec file paths: '
        'pathspec-file entry bad-\\udcff.py is not valid UTF-8; '
        'Git on Windows cannot receive it'
    )
    assert (tmp_path / '.git' / 'index').read_bytes() == index


def _run_renamed_hook(repo: Path, route: str, *, incremental: bool = True) -> str:
    if route == 'post':
        assert (
            _post_tool(
                {
                    'tool_name': 'Edit',
                    'tool_input': {'file_path': str(repo / 'new.py')},
                    'cwd': str(repo),
                },
                overrides=SortOverrides(incremental=incremental),
            )
            == EXIT_OK
        )
    else:
        command = 'git commit -m rename' if route == 'staged' else 'git commit -a -m rename'
        outcome = _pre_tool(
            {
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Bash',
                'tool_input': {'command': command},
                'cwd': str(repo),
            },
            overrides=SortOverrides(incremental=incremental),
        )
        assert outcome.decision == 'allow'
    return (
        _staged_text(repo, 'new.py')
        if route == 'staged'
        else (repo / 'new.py').read_text(encoding='utf-8')
    )


@pytest.mark.parametrize('route', ['post', 'staged', 'worktree'])
class TestRenamedBaselines:
    def test_nonconforming_renamed_baseline_is_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], route: str
    ) -> None:
        case = 'nonconforming'
        target, source = prepare_renamed_file(tmp_path, case)
        result = _run_renamed_hook(tmp_path, route, incremental=True)
        assert target.read_text(encoding='utf-8') == result
        captured = capsys.readouterr()
        assert_renamed_result(result, source, case, captured.out + captured.err)

    @pytest.mark.parametrize('setting', ['false', 'copies'])
    @pytest.mark.parametrize('case', ['conforming', 'nonconforming', 'guard'])
    def test_rename_settings_do_not_change_pairing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        route: str,
        case: str,
        setting: str,
    ) -> None:
        target, source = prepare_renamed_file(tmp_path, case)
        run_setup(['git', 'config', 'diff.renames', setting], cwd=tmp_path)
        result = _run_renamed_hook(tmp_path, route, incremental=case != 'guard')
        assert target.read_text(encoding='utf-8') == result
        captured = capsys.readouterr()
        assert_renamed_result(result, source, case, captured.out + captured.err)

    def test_rename_sorts_only_new_definitions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], route: str
    ) -> None:
        case = 'conforming'
        target, source = prepare_renamed_file(tmp_path, case)
        result = _run_renamed_hook(tmp_path, route, incremental=True)
        assert target.read_text(encoding='utf-8') == result
        captured = capsys.readouterr()
        assert_renamed_result(result, source, case, captured.out + captured.err)

    def test_reorder_guard_measures_renamed_files_against_their_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], route: str
    ) -> None:
        case = 'guard'
        target, source = prepare_renamed_file(tmp_path, case)
        result = _run_renamed_hook(tmp_path, route, incremental=False)
        assert target.read_text(encoding='utf-8') == result
        captured = capsys.readouterr()
        assert_renamed_result(result, source, case, captured.out + captured.err)


@pytest.mark.parametrize('pairing', ['unstaged', 'below-threshold'])
def test_unpaired_rename_is_new(tmp_path: Path, pairing: str) -> None:
    init_empty_git_repo(tmp_path)
    old = tmp_path / 'old.py'
    baseline = _SRC_NEEDS_SORT if pairing == 'unstaged' else '# unrelated baseline\n' * 20
    old.write_text(baseline, encoding='utf-8')
    run_setup(['git', 'add', 'old.py'], cwd=tmp_path)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=tmp_path)
    target = old.rename(tmp_path / 'new.py')
    target.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
    if pairing == 'below-threshold':
        run_setup(['git', 'add', '-A'], cwd=tmp_path)
    paired = run_setup(['git', 'diff', '--name-status', '-M', 'HEAD'], cwd=tmp_path).stdout
    assert not paired.startswith(b'R')
    assert _run_renamed_hook(tmp_path, 'post') == _SRC_ALREADY_ORDERED


@pytest.mark.skipif(os.name == 'nt', reason='the Git stand-in is a POSIX shell script')
def test_renamed_staged_commit_works_with_git_2_41(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source = prepare_renamed_file(tmp_path, 'conforming')
    install_stand_in_git(monkeypatch, tmp_path, version='2.41.0')
    result = _run_renamed_hook(tmp_path, 'staged')
    assert_renamed_result(result, source, 'conforming', '')
    assert target.read_text(encoding='utf-8') == result
    run_setup(['git', 'commit', '-m', 'renamed'], cwd=tmp_path)
    assert decode_text(run_setup(['git', 'show', 'HEAD:new.py'], cwd=tmp_path).stdout) == result


class TestPostToolGitRequiredModes:
    @pytest.mark.skipif(os.name == 'nt', reason='POSIX stand-in executable')
    @pytest.mark.parametrize('mode', ['incremental', 'guard', 'both'])
    def test_post_tool_git_failure_leaves_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode: str,
    ) -> None:
        init_git_repo(tmp_path, _SRC_NEEDS_SORT)
        config = '[tool.defsort]\n'
        if mode != 'guard':
            config += 'incremental = true\n'
        if mode != 'incremental':
            config += 'max-reorder-count = 0\n'
        (tmp_path / 'pyproject.toml').write_text(config, encoding='utf-8')
        path = tmp_path / 'test.py'
        install_stand_in_git(monkeypatch, tmp_path, fail='cat-file')
        assert _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}}) == EXIT_OK
        names = {
            'incremental': 'incremental mode',
            'guard': 'the reorder guard',
            'both': 'incremental mode and the reorder guard',
        }
        assert capsys.readouterr().err == (
            f'defsort: PostToolUse skipped test.py: '
            f'{names[mode]} cannot use Git: fatal: simulated failure\n'
        )
        assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT

    @pytest.mark.parametrize('mode', ['incremental', 'guard', 'full'])
    def test_post_tool_outside_repository_leaves_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        mode: str,
    ) -> None:
        setting = {
            'incremental': 'incremental = true',
            'guard': 'max-reorder-count = 0',
            'full': '',
        }[mode]
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\n' + setting + '\n', encoding='utf-8'
        )
        path = tmp_path / 'm.py'
        path.write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        assert _post_tool({'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}}) == EXIT_OK
        stderr = capsys.readouterr().err
        if mode == 'full':
            assert 'not in a Git repository' not in stderr
            assert path.read_text(encoding='utf-8') == _SRC_ALREADY_ORDERED
        else:
            assert (
                stderr
                == f'defsort: PostToolUse skipped {path.as_posix()}: not in a Git repository\n'
            )
            assert path.read_text(encoding='utf-8') == _SRC_NEEDS_SORT


def _prepare_sync_repo(repo: Path, names: tuple[str, ...] = ('m.py',)) -> None:
    init_empty_git_repo(repo)
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(_SRC_NEEDS_SORT, encoding='utf-8')
        _stage_text(repo, name, _SRC_NEEDS_SORT)


def _assert_sync_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    _prepare_sync_repo(tmp_path)
    path = tmp_path / 'm.py'
    reason = _alter_sync_copy(path, monkeypatch, case)
    before = path.read_bytes() if case not in ('missing', 'unreadable') else None
    assert _run_sync_commit(tmp_path) == Outcome(
        'allow', 'defsort: sorted 1 staged Python file for this commit'
    )
    assert _staged_text(tmp_path, 'm.py') == _SRC_ALREADY_ORDERED
    assert capsys.readouterr().err == (
        f'defsort: did not update m.py in the working tree: {reason}; '
        'the commit has the sorted version\n'
    )
    if before is not None:
        assert path.read_bytes() == before
    if case == 'missing':
        assert not path.exists()
    if case == 'symlink':
        assert path.is_symlink()


def test_index_failure_prints_no_sync_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_sync_repo(tmp_path)
    path = tmp_path / 'm.py'
    path.write_text('# unstaged\n' + _SRC_NEEDS_SORT, encoding='utf-8')
    before = path.read_bytes()

    def fail_index(*_args: object, **_kwargs: object) -> None:
        raise GitCommandError('fatal: x')

    monkeypatch.setattr(hook_mod, 'apply_index_entries', fail_index)
    assert _run_sync_commit(tmp_path) == Outcome(
        'deny', 'defsort: failed to update git index: fatal: x'
    )
    assert capsys.readouterr().err == ''
    assert _staged_text(tmp_path, 'm.py') == _SRC_NEEDS_SORT
    assert path.read_bytes() == before


def test_unexpected_sync_error_allows_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_sync_note(tmp_path, monkeypatch, capsys, 'unexpected')


@pytest.mark.parametrize(
    'case', ['edited', 'missing', 'symlink', 'undecodable', 'unreadable', 'write', 'restore']
)
def test_worktree_sync_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    _assert_sync_note(tmp_path, monkeypatch, capsys, case)


@pytest.mark.parametrize('names', [('a.py', 'b.py'), ('a.py', 'a/z.py')])
def test_sync_notes_follow_staging_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    names: tuple[str, str],
) -> None:
    _prepare_sync_repo(tmp_path, names)
    (tmp_path / names[1]).write_text('# unstaged\n' + _SRC_NEEDS_SORT, encoding='utf-8')

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, 'I/O error')

    monkeypatch.setattr(hook_mod, 'write_source', fail_write)
    assert _run_sync_commit(tmp_path) == Outcome(
        'allow', 'defsort: sorted 2 staged Python files for this commit'
    )
    assert capsys.readouterr().err.splitlines() == [
        (
            'defsort: did not update a.py in the working tree: cannot write it: I/O error; '
            'the commit has the sorted version'
        ),
        (
            f'defsort: did not update {names[1]} in the working tree: it has unstaged changes; '
            'the commit has the sorted version'
        ),
    ]
    assert _staged_text(tmp_path, 'a.py') == _SRC_ALREADY_ORDERED
    assert _staged_text(tmp_path, names[1]) == _SRC_ALREADY_ORDERED


@pytest.mark.parametrize('disposition', ['error', 'skip', 'block'])
def test_sync_resort_failure_allows_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    disposition: str,
) -> None:
    _prepare_sync_repo(tmp_path)
    path = tmp_path / 'm.py'
    raw = _SRC_NEEDS_SORT.replace('\n', '\r\n').encode()
    path.write_bytes(raw)
    original_sort = hook_mod._resolve_and_sort

    def sort(ctx: HookContext, source: str, **kwargs: object) -> object:
        if '\r' in source:
            if disposition == 'error':
                raise RuntimeError('boom')
            return hook_mod._FileSortOutcome(disposition=hook_mod._Disposition(disposition))
        return original_sort(
            ctx,
            source,
            file_cfg=cast('ResolvedConfig', kwargs['file_cfg']),
            display_path=cast('str', kwargs['display_path']),
            baseline_entry=cast('BlobReadResult | None', kwargs['baseline_entry']),
            is_new=cast('bool', kwargs['is_new']),
            emit_diagnostics=cast('bool', kwargs.get('emit_diagnostics', True)),
        )

    monkeypatch.setattr(hook_mod, '_resolve_and_sort', sort)
    assert _run_sync_commit(tmp_path) == Outcome(
        'allow', 'defsort: sorted 1 staged Python file for this commit'
    )
    assert _staged_text(tmp_path, 'm.py') == _SRC_ALREADY_ORDERED
    assert path.read_bytes() == raw
    assert capsys.readouterr().err == (
        'defsort: did not update m.py in the working tree: cannot sort it: RuntimeError: boom; '
        'the commit has the sorted version\n'
        if disposition == 'error'
        else ''
    )


@pytest.mark.parametrize(
    'alias_state', ['unstaged', 'different-result', 'unprocessed', 'unchanged']
)
def test_sync_preserves_windows_alias_authorization(
    monkeypatch: pytest.MonkeyPatch, alias_state: str
) -> None:
    # Model Windows equality without claiming native filesystem acceptance.
    root = cast('Path', PureWindowsPath('C:/repo'))
    upper, lower = root / 'A.py', root / 'a.py'
    assert upper == lower
    examined: list[str] = []

    def grouped(paths: list[Path]) -> list[list[Path]]:
        assert [path.name for path in paths] == ['A.py', 'a.py']
        return [paths]

    entries = [
        hook_mod._StagedFileEntry(
            'A.py', 'upper', 'upper result', '100644', 'utf-8', None, is_new=True
        ),
        hook_mod._StagedFileEntry(
            'a.py', 'lower', 'lower result', '100644', 'utf-8', None, is_new=True
        ),
    ]

    def planned(
        _ctx: HookContext, entry: hook_mod._StagedFileEntry, path: Path, _config: ResolvedConfig
    ) -> tuple[hook_mod._WorktreeSync | None, str | None]:
        examined.append(entry.path)
        if entry.path == 'A.py' and alias_state == 'unstaged':
            return None, 'it has unstaged changes'
        return hook_mod._WorktreeSync(path, entry.new_source, 'utf-8', b'original'), None

    def unchanged(
        entry: hook_mod._StagedFileEntry, _path: Path
    ) -> tuple[bytes | None, str | None]:
        examined.append(entry.path)
        return b'unchanged upper', None

    monkeypatch.setattr(hook_mod, '_unchanged_worktree_bytes', unchanged)
    monkeypatch.setattr(hook_mod, 'Path', PureWindowsPath)
    monkeypatch.setattr(hook_mod, 'group_paths', grouped)
    monkeypatch.setattr(hook_mod, '_plan_worktree_sync', planned)
    syncs = hook_mod._plan_staged_syncs(
        _hook_ctx(root),
        entries[1:] if alias_state in {'unprocessed', 'unchanged'} else entries,
        [upper]
        if alias_state == 'unprocessed'
        else [entries[0]]
        if alias_state == 'unchanged'
        else [],
        {'A.py': ResolvedConfig(), 'a.py': ResolvedConfig()},
    )
    assert len(syncs) == 1
    assert syncs[0].names == ['A.py', 'a.py']
    assert syncs[0].write is None
    assert syncs[0].reason == (
        'A.py has unstaged changes'
        if alias_state == 'unstaged'
        else 'names of one file need different results'
    )
    expected_checks = {
        'unprocessed': ['a.py'],
        'unchanged': ['a.py', 'A.py'],
    }
    assert examined == expected_checks.get(alias_state, ['A.py', 'a.py'])


@pytest.mark.parametrize(
    ('failure', 'reason'),
    [
        ('read', 'cannot read it: Permission denied'),
        ('decode', 'cannot read it: unknown encoding: missing-codec'),
        ('changed', 'changed since it was read; not written'),
        ('deleted', 'no longer exists; not written'),
        ('write', 'cannot write it: Permission denied'),
        ('restore', 'cannot write it: '),
        ('alias', 'names of one file need different results'),
        ('transform', 'unexpected error: RuntimeError: boom'),
        ('unexpected-write', 'unexpected error: RuntimeError: boom'),
    ],
)
def test_post_tool_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    reason: str,
) -> None:
    run_setup(['git', 'init'], cwd=tmp_path)
    target = tmp_path / 'm.py'
    target.write_text(_SRC_NEEDS_SORT)
    original = target.read_bytes()
    write = hook_mod.write_source

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('boom')

    def changed_write(path: Path, text: str, *, encoding: str, raw: bytes) -> bytes:
        if failure == 'deleted':
            path.unlink()
        else:
            path.write_text('edited = True\n')
        return write(path, text, encoding=encoding, raw=raw)

    injected = {
        'read': ('read_file_result', fail_read),
        'write': ('write_source', fail_read),
        'transform': ('_plan_file_sort', explode),
        'unexpected-write': ('write_source', explode),
    }
    if failure in injected:
        name, replacement = injected[failure]
        monkeypatch.setattr(hook_mod, name, replacement)
    elif failure == 'decode':
        target.write_bytes(b'# coding: missing-codec\npass\n')
    elif failure in {'changed', 'deleted'}:
        monkeypatch.setattr(hook_mod, 'write_source', changed_write)
    elif failure == 'restore':
        redirect_recovery(monkeypatch, tmp_path / 'recovery')
        fail_writes_to(monkeypatch, target, restore_fails=True)
    else:
        monkeypatch.setattr(hook_mod, 'names_agree', _post_tool_names_disagree)
    payload = json.dumps(
        {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(target)},
            'cwd': str(tmp_path),
        }
    )
    assert run_hook(payload, host='claude') == 0
    captured = capsys.readouterr()
    expected = f'defsort: PostToolUse skipped m.py: {reason}'
    notes = [line for line in captured.err.splitlines() if line.startswith('defsort:')]
    assert len(notes) == 1
    assert notes[0].startswith(expected)
    context = json.loads(captured.out)['hookSpecificOutput']['additionalContext']
    assert context.splitlines()[0] == notes[0]
    assert context.count(notes[0]) == 1
    assert captured.err.splitlines()[1:2] == (
        ['Traceback (most recent call last):']
        if failure in {'transform', 'unexpected-write'}
        else []
    )
    assert ('Traceback' in captured.err) == (failure in {'transform', 'unexpected-write'})
    if failure not in {'decode', 'changed', 'deleted', 'restore'}:
        assert target.read_bytes() == original


@pytest.mark.parametrize('host', ['claude', 'codex', 'antigravity'])
def test_post_tool_messages_follow_host_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    run_setup(['git', 'init'], cwd=tmp_path)
    broken, writable = [tmp_path / name for name in ('broken.py', 'write.py')]
    broken.write_text('def broken(:\n')
    writable.write_text(_SRC_NEEDS_SORT)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(hook_mod, 'write_source', fail_write)

    def two_paths(_paths: Sequence[str], _cwd: str) -> list[str]:
        return [str(broken), str(writable)]

    monkeypatch.setattr(hook_mod, 'resolve_edited_paths', two_paths)
    payload = (
        {
            'toolCall': {'name': 'write_to_file', 'args': {'TargetFile': str(broken)}},
            'cwd': str(tmp_path),
        }
        if host == 'antigravity'
        else {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Edit',
            'tool_input': {'file_path': str(broken)},
            'cwd': str(tmp_path),
        }
    )
    if host == 'codex':
        payload = {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'apply_patch',
            'tool_input': {
                'command': f'*** Begin Patch\n*** Update File: {broken}\n*** End Patch\n'
            },
            'cwd': str(tmp_path),
        }
    assert (
        run_hook(
            json.dumps(payload),
            host='agy' if host == 'antigravity' else host,
            event='PostToolUse' if host == 'antigravity' else None,
        )
        == 0
    )
    captured = capsys.readouterr()
    note = 'defsort: PostToolUse skipped write.py: cannot write it: disk full'
    reason = 'defsort: PostToolUse skipped broken.py: syntax error: invalid syntax (line 1)'
    if host == 'antigravity':
        assert captured.out == ''
        assert captured.err == note + '\n' + reason + '\n'
    else:
        assert captured.err == note + '\n'
        assert json.loads(captured.out) == {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': note + '\n' + reason,
            }
        }


@pytest.mark.parametrize('line', [None, 1])
def test_syntax_error_is_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: int | None
) -> None:
    run_setup(['git', 'init'], cwd=tmp_path)
    target = tmp_path / 'broken.py'
    source = 'f = ' + 'lambda: ' * 3000 + '0\n' if line is None else 'def broken(:\n'
    target.write_text(source)
    outcome = hook_mod.run_post_tool([str(target)], str(tmp_path))
    reason = (
        "source nests too deeply for Python's parser on this platform"
        if line is None
        else 'invalid syntax (line 1)'
    )
    assert outcome == Outcome(
        'allow', f'defsort: PostToolUse skipped broken.py: syntax error: {reason}'
    )
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('in_repository', [False, True])
def test_post_tool_note_uses_repository_name_or_payload_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    in_repository: bool,
) -> None:
    if in_repository:
        run_setup(['git', 'init'], cwd=tmp_path)
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'm.py').write_text('pass\n')

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(hook_mod, 'read_file_result', fail_read)
    assert hook_mod.run_post_tool(['pkg/../m.py'], str(tmp_path)) == Outcome('allow')
    display = 'm.py' if in_repository else 'pkg/../m.py'
    assert (
        capsys.readouterr().err
        == f'defsort: PostToolUse skipped {display}: cannot read it: Permission denied\n'
    )


@pytest.mark.parametrize('failure', ['blocker', 'guard'])
def test_post_tool_blocker_and_guard_diagnostics_keep_absolute_lexical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    init_empty_git_repo(tmp_path)
    (tmp_path / 'pkg').mkdir()
    target = tmp_path / 'test.py'
    source = _SRC_INTERSTITIAL if failure == 'blocker' else _SRC_NEEDS_SORT
    target.write_text(source)
    payload_path = 'pkg/../test.py'
    display = (tmp_path / payload_path).as_posix()
    expected = _BLOCKER_MSGS['interstitial'].format(path=display)
    if failure == 'guard':
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nmax-reorder-count = 1\n')

        def unreadable_baselines(_root: str, paths: Sequence[Path]) -> dict[Path, BlobReadResult]:
            return {
                path: BlobReadResult(status=BlobReadStatus.DECODE_FAILED, detail='unknown codec')
                for path in paths
            }

        monkeypatch.setattr(hook_mod, 'read_head_baselines', unreadable_baselines)
        expected = f'defsort: {display}: skipped (reorder guard: unable to read baseline)'

    assert (
        _post_tool(
            {
                'cwd': str(tmp_path),
                'tool_name': 'Edit',
                'tool_input': {'file_path': payload_path},
            }
        )
        == EXIT_OK
    )
    captured = capsys.readouterr()
    assert captured.err == expected + '\n'
    assert json.loads(captured.out)['hookSpecificOutput']['additionalContext'] == expected
    assert target.read_text() == source
