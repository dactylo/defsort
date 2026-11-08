import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._vendor.hookfront._agy import (
    AntigravityAdapter,
)
from defsort._vendor.hookfront._contract import (
    API_VERSION,
    HookAdapter,
    HookResult,
    HostInput,
    HostResponse,
)


@dataclass(frozen=True)
class _Result:
    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


def _payload(tool: str, args: dict[str, object]) -> dict[str, object]:
    return {'toolCall': {'name': tool, 'args': args}}


def _result(
    decision: Literal['allow', 'deny'],
    reason: str | None = None,
    notes: tuple[str, ...] = (),
) -> HookResult:
    return _Result(decision, reason, notes)


def test_contract() -> None:
    adapter: HookAdapter = AntigravityAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'agy',
        API_VERSION,
        True,
    )


def test_parse_null_tool_call_is_inert() -> None:
    assert AntigravityAdapter().parse({'toolCall': None}, 'PostToolUse') == HostInput(
        'post_tool', None, str(Path.cwd()), None
    )


@pytest.mark.parametrize(
    ('tool', 'field'),
    [
        ('write_to_file', 'TargetFile'),
        ('replace_file_content', 'TargetFile'),
        ('write_file', 'Path'),
        ('edit_file', 'Path'),
    ],
)
def test_parse_post_tool_file_writers(tool: str, field: str) -> None:
    payload = _payload(tool, {field: '/t/a.py', 'Cwd': '/t'})
    assert AntigravityAdapter().parse(payload, 'PostToolUse') == HostInput(
        'post_tool', None, '/t', None, ('/t/a.py',)
    )


@pytest.mark.parametrize('name', [None, [], {}, 1, True], ids=repr)
def test_parse_non_string_tool_name_has_no_paths(name: object) -> None:
    payload = {'toolCall': {'name': name, 'args': {'TargetFile': '/t/a.py'}}}
    assert AntigravityAdapter().parse(payload, 'PostToolUse') == HostInput(
        'post_tool', None, str(Path.cwd()), None
    )


def test_parse_post_tool_other_tool_has_no_paths() -> None:
    payload = _payload('read_file', {'TargetFile': '/t/a.py', 'Cwd': '/t'})
    assert AntigravityAdapter().parse(payload, 'PostToolUse') == HostInput(
        'post_tool', None, '/t', None
    )


def test_parse_pre_tool_other_tool_has_no_command() -> None:
    payload = _payload('write_to_file', {'TargetFile': 'a.py', 'Cwd': '/t'})
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', None, '/t', None
    )


def test_parse_rejects_unknown_event() -> None:
    with pytest.raises(ValueError, match='unknown Antigravity event'):
        AntigravityAdapter().parse({}, 'Stop')


@pytest.mark.parametrize('cwd', ['rel', 7, None])
def test_parse_run_command_non_absolute_cwd_is_unknown(cwd: object) -> None:
    payload = _payload('run_command', {'CommandLine': 'git commit -m x', 'Cwd': cwd})
    payload['workspacePaths'] = ['/ws']
    parsed = AntigravityAdapter().parse(payload, 'PreToolUse')
    assert parsed is not None
    assert parsed.command_cwd is None


@pytest.mark.parametrize('args', [{}, {'Cwd': ''}])
def test_parse_run_command_missing_cwd_uses_first_workspace(
    args: dict[str, object], tmp_path: Path
) -> None:
    workspace = str(tmp_path / 'ws')
    payload = _payload('run_command', {'CommandLine': 'git commit -m x', **args})
    payload['workspacePaths'] = [workspace, str(tmp_path / 'other')]
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', 'git commit -m x', workspace, workspace
    )


def test_parse_run_command_prefers_cwd_over_workspace(tmp_path: Path) -> None:
    cwd = str(tmp_path / 'command')
    payload = _payload('run_command', {'CommandLine': 'git commit -m x', 'Cwd': cwd})
    payload['workspacePaths'] = [str(tmp_path / 'workspace')]
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', 'git commit -m x', cwd, cwd
    )


@pytest.mark.parametrize(
    'workspace_fields',
    [{}, *({'workspacePaths': value} for value in [None, [], '', ['', '/later'], ['rel'], [7]])],
)
def test_parse_run_command_requires_absolute_first_workspace(
    workspace_fields: dict[str, object],
) -> None:
    payload = _payload('run_command', {'CommandLine': 'git commit -m x'})
    payload.update(workspace_fields)
    payload['cwd'] = '/session'
    parsed = AntigravityAdapter().parse(payload, 'PreToolUse')
    assert parsed is not None
    assert parsed.command_cwd is None


def test_render_allow_and_failure() -> None:
    assert AntigravityAdapter().render(_result('allow')) == HostResponse('', '', 0)
    failure = AntigravityAdapter().render_failure('tool: reason')
    assert json.loads(failure.stdout) == {'decision': 'deny', 'reason': 'tool: reason'}
    assert (failure.stderr, failure.exit_code) == ('tool: reason\n', 0)


def test_render_allow_feedback_has_decision_and_reason() -> None:
    response = AntigravityAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'decision': 'allow', 'reason': 'first\nsecond'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_render_deny_is_top_level_json() -> None:
    response = AntigravityAdapter().render(_result('deny', 'tool: reason', ('note',)))
    assert json.loads(response.stdout) == {'decision': 'deny', 'reason': 'tool: reason'}
    assert (response.stderr, response.exit_code) == ('note\ntool: reason\n', 0)


def test_render_deny_without_reason_uses_empty_reason() -> None:
    response = AntigravityAdapter().render(_result('deny'))
    assert json.loads(response.stdout) == {'decision': 'deny', 'reason': ''}
    assert response.stderr == '\n'


def test_render_post_tool_failure_is_stderr_only() -> None:
    adapter = AntigravityAdapter()
    adapter.parse({}, 'PostToolUse')
    assert adapter.render_failure('tool: reason') == HostResponse('', 'tool: reason\n', 0)


@pytest.mark.parametrize('decision', ['allow', 'deny'])
def test_render_post_tool_is_silent_with_notes_on_stderr(
    decision: Literal['allow', 'deny'],
) -> None:
    adapter = AntigravityAdapter()
    adapter.parse(_payload('write_file', {'Path': '/t/a.py'}), 'PostToolUse')
    assert adapter.render(_result(decision, 'receipt', ('diagnostic',))) == HostResponse(
        '', 'diagnostic\nreceipt\n', 0
    )
