import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._vendor.hookfront._claude import (
    ClaudeAdapter,
)
from defsort._vendor.hookfront._contract import (
    API_VERSION,
    HookAdapter,
    HookResponse,
    HookResult,
    HostInput,
    HostResponse,
)

_DENY_JSON = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'tool: reason',
    }
}


@dataclass(frozen=True)
class _Result:
    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


def _payload(event: str, tool: str, tool_input: dict[str, object]) -> dict[str, object]:
    return {'hook_event_name': event, 'tool_name': tool, 'tool_input': tool_input, 'cwd': '/r'}


def _result(
    decision: Literal['allow', 'deny'],
    reason: str | None = None,
    notes: tuple[str, ...] = (),
) -> HookResult:
    return _Result(decision, reason, notes)


def test_allow_notes_become_system_message() -> None:
    response: HookResponse = ClaudeAdapter().render(
        _result('allow', notes=('warning: a', 'warning: a', 'WARNING: b'))
    )
    assert response == HostResponse(
        '{"systemMessage": "warning: a\\nWARNING: b"}\n',
        'warning: a\nwarning: a\nWARNING: b\n',
        0,
    )


def test_allow_receipt_precedes_notes() -> None:
    response = ClaudeAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'systemMessage': 'first\nsecond\ndiagnostic'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_allow_system_message_is_capped() -> None:
    limit = 10_000
    response = ClaudeAdapter().render(_result('allow', notes=('a' * 10_001,)))
    message = json.loads(response.stdout)['systemMessage']
    assert len(message) <= limit
    assert message.endswith('... (more on stderr)')
    assert response.stderr == f'{"a" * 10_001}\n'


def test_contract() -> None:
    adapter: HookAdapter = ClaudeAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'claude',
        API_VERSION,
        False,
    )


def test_parse_missing_cwd_uses_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    payload: dict[str, object] = {
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {},
    }
    parsed = ClaudeAdapter().parse(payload, None)
    assert parsed is not None
    assert parsed.cwd == str(tmp_path)


def test_parse_other_event_is_inapplicable() -> None:
    assert ClaudeAdapter().parse({'hook_event_name': 'Notification'}, None) is None


@pytest.mark.parametrize('tool', ['Edit', 'Write'])
def test_parse_post_tool_edit(tool: str) -> None:
    payload = _payload('PostToolUse', tool, {'file_path': 'a.py'})
    assert ClaudeAdapter().parse(payload, None) == HostInput(
        'post_tool', None, '/r', None, ('a.py',)
    )


def test_parse_post_tool_has_no_invocation_directory() -> None:
    payload = _payload('PostToolUse', 'Edit', {'file_path': '/r/a.py'})
    assert ClaudeAdapter().parse(payload, None) == HostInput(
        'post_tool', None, '/r', None, ('/r/a.py',)
    )


@pytest.mark.parametrize('name', [None, [], {}, 1, True], ids=repr)
def test_parse_post_tool_non_string_name_has_no_paths(name: object) -> None:
    payload = _payload('PostToolUse', 'Edit', {'file_path': 'a.py'})
    payload['tool_name'] = name
    assert ClaudeAdapter().parse(payload, None) == HostInput('post_tool', None, '/r', None)


def test_parse_pre_tool_bash_trusts_absolute_cwd(tmp_path: Path) -> None:
    payload = _payload('PreToolUse', 'Bash', {'command': 'ls'})
    payload['cwd'] = str(tmp_path)
    assert ClaudeAdapter().parse(payload, None) == HostInput(
        'pre_tool', 'ls', str(tmp_path), str(tmp_path)
    )


@pytest.mark.parametrize('cwd', ['rel', '', 123, None])
def test_parse_pre_tool_non_absolute_cwd_is_unknown(
    cwd: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = _payload('PreToolUse', 'Bash', {'command': 'ls'})
    payload['cwd'] = cwd
    parsed = ClaudeAdapter().parse(payload, None)
    assert parsed is not None
    assert parsed.command_cwd is None
    assert parsed.cwd == (cwd if isinstance(cwd, str) and cwd else str(tmp_path))


def test_parse_pre_tool_other_tool_has_no_command() -> None:
    payload = _payload('PreToolUse', 'Read', {'file_path': 'a.py'})
    assert ClaudeAdapter().parse(payload, None) == HostInput('pre_tool', None, '/r', None)


def test_receipt_at_the_limit_is_not_cut() -> None:
    receipt = 'r' * 10_000
    response = ClaudeAdapter().render(_result('allow', receipt))
    assert json.loads(response.stdout) == {'systemMessage': receipt}
    assert response.stderr == ''


def test_render_deny_is_json_and_stderr_reason() -> None:
    response = ClaudeAdapter().render(_result('deny', 'tool: reason', ('note',)))
    assert json.loads(response.stdout) == _DENY_JSON
    assert response.stderr == 'note\ntool: reason\n'
    assert response.exit_code == 0


def test_render_deny_without_reason_uses_empty_reason() -> None:
    response = ClaudeAdapter().render(_result('deny'))
    assert json.loads(response.stdout)['hookSpecificOutput']['permissionDecisionReason'] == ''
    assert response.stderr == '\n'


def test_render_failure() -> None:
    response = ClaudeAdapter().render_failure('tool: reason')
    assert json.loads(response.stdout) == _DENY_JSON
    assert response.stderr == 'tool: reason\n'
    assert response.exit_code == 0


@pytest.mark.parametrize(
    ('reason', 'notes', 'context'),
    [
        (None, ('Cannot preserve layout.',), 'Cannot preserve layout.'),
        ('Cannot process a.py: invalid syntax.', (), 'Cannot process a.py: invalid syntax.'),
        ('first\nsecond', ('first', 'diagnostic'), 'first\ndiagnostic\nsecond'),
    ],
)
def test_render_post_tool_context(
    reason: str | None, notes: tuple[str, ...], context: str
) -> None:
    adapter = ClaudeAdapter()
    assert adapter.parse(_payload('PostToolUse', 'Edit', {'file_path': 'a.py'}), None)
    response = adapter.render(_result('allow', reason, notes))
    assert json.loads(response.stdout) == {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': context,
        }
    }
    assert response.stderr == ''.join(f'{note}\n' for note in notes)
    assert response.exit_code == 0


@pytest.mark.parametrize('event', ['PreToolUse', 'Notification', None])
def test_render_resets_post_tool_context(event: str | None) -> None:
    adapter = ClaudeAdapter()
    adapter.parse(_payload('PostToolUse', 'Edit', {'file_path': 'a.py'}), None)
    assert adapter.render(_result('allow')) == HostResponse('', '', 0)
    adapter.parse({'hook_event_name': event}, None)
    response = adapter.render(_result('deny', 'tool: reason'))
    assert json.loads(response.stdout) == _DENY_JSON
    receipt = adapter.render(_result('allow', 'staged rewrite receipt'))
    assert json.loads(receipt.stdout) == {'systemMessage': 'staged rewrite receipt'}


def test_silent_allow_stays_empty() -> None:
    assert ClaudeAdapter().render(_result('allow')) == HostResponse('', '', 0)


def test_truncated_receipt_is_complete_on_stderr() -> None:
    receipt = 'r' * 10_000 + '\nrepeat\nrepeat\n\nlast receipt line'
    notes = ('same note', 'same note', '')
    response = ClaudeAdapter().render(_result('allow', receipt, notes))
    message = json.loads(response.stdout)['systemMessage']
    assert len(message) <= 10_000
    assert message.endswith('... (more on stderr)')
    assert response.stderr == f'{receipt}\nsame note\nsame note\n\n'
