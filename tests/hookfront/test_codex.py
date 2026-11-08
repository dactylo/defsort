import json
from dataclasses import dataclass
from typing import Literal

import pytest

from defsort._vendor.hookfront._codex import (
    CodexAdapter,
)
from defsort._vendor.hookfront._contract import (
    API_VERSION,
    HookAdapter,
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
    response = CodexAdapter().render(
        _result('allow', notes=('warning: a', 'warning: a', 'WARNING: b'))
    )
    assert response == HostResponse(
        '{"systemMessage": "warning: a\\nWARNING: b"}\n',
        'warning: a\nwarning: a\nWARNING: b\n',
        0,
    )


def test_allow_receipt_precedes_notes() -> None:
    response = CodexAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'systemMessage': 'first\nsecond\ndiagnostic'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_allow_system_message_is_capped() -> None:
    limit = 10_000
    response = CodexAdapter().render(_result('allow', notes=('a' * 10_001,)))
    message = json.loads(response.stdout)['systemMessage']
    assert len(message) <= limit
    assert message.endswith('... (more on stderr)')
    assert response.stderr == f'{"a" * 10_001}\n'


def test_contract() -> None:
    adapter: HookAdapter = CodexAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'codex',
        API_VERSION,
        False,
    )


def test_parse_post_tool_apply_patch_paths() -> None:
    command = (
        '*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b/c.py\n'
        '*** Move to: d.py\n*** Delete File: e.txt\n*** Update File: a.py\n*** End Patch\n'
    )
    payload = _payload('PostToolUse', 'apply_patch', {'command': command})
    assert CodexAdapter().parse(payload, None) == HostInput(
        'post_tool', None, '/r', None, ('a.py', 'b/c.py', 'd.py', 'e.txt')
    )


@pytest.mark.parametrize('name', [None, [], {}, 1, True], ids=repr)
def test_parse_post_tool_non_string_name_has_no_paths(name: object) -> None:
    payload = _payload('PostToolUse', 'apply_patch', {'command': '*** Update File: a.py'})
    payload['tool_name'] = name
    assert CodexAdapter().parse(payload, None) == HostInput('post_tool', None, '/r', None)


def test_parse_pre_tool_bash_has_no_invocation_directory() -> None:
    payload = _payload('PreToolUse', 'Bash', {'command': 'ls'})
    assert CodexAdapter().parse(payload, None) == HostInput('pre_tool', 'ls', '/r', None)


def test_parse_pre_tool_ignores_directory_lookalikes() -> None:
    payload = _payload(
        'PreToolUse',
        'Bash',
        {'command': 'git commit -m x', 'workdir': '/b', 'cwd': '/b'},
    )
    payload['workdir'] = '/b'
    parsed = CodexAdapter().parse(payload, None)
    assert parsed == HostInput('pre_tool', 'git commit -m x', '/r', None)


def test_receipt_at_the_limit_is_not_cut() -> None:
    receipt = 'r' * 10_000
    response = CodexAdapter().render(_result('allow', receipt))
    assert json.loads(response.stdout) == {'systemMessage': receipt}
    assert response.stderr == ''


def test_render_deny_matches_claude_shape() -> None:
    response = CodexAdapter().render(_result('deny', 'tool: reason'))
    assert json.loads(response.stdout) == _DENY_JSON
    assert (response.stderr, response.exit_code) == ('tool: reason\n', 0)


def test_render_deny_without_reason_uses_empty_reason() -> None:
    response = CodexAdapter().render(_result('deny'))
    assert json.loads(response.stdout)['hookSpecificOutput']['permissionDecisionReason'] == ''
    assert response.stderr == '\n'


def test_render_failure() -> None:
    response = CodexAdapter().render_failure('tool: reason')
    assert json.loads(response.stdout) == _DENY_JSON
    assert (response.stderr, response.exit_code) == ('tool: reason\n', 0)


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
    adapter = CodexAdapter()
    payload = _payload('PostToolUse', 'apply_patch', {'command': '*** Update File: a.py\n'})
    assert adapter.parse(payload, None)
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
    adapter = CodexAdapter()
    adapter.parse(_payload('PostToolUse', 'apply_patch', {}), None)
    assert adapter.render(_result('allow')) == HostResponse('', '', 0)
    adapter.parse({'hook_event_name': event}, None)
    response = adapter.render(_result('deny', 'tool: reason'))
    assert json.loads(response.stdout) == _DENY_JSON
    receipt = adapter.render(_result('allow', 'staged rewrite receipt'))
    assert json.loads(receipt.stdout) == {'systemMessage': 'staged rewrite receipt'}


def test_silent_allow_stays_empty() -> None:
    assert CodexAdapter().render(_result('allow')) == HostResponse('', '', 0)


def test_truncated_receipt_is_complete_on_stderr() -> None:
    receipt = 'r' * 10_000 + '\nrepeat\nrepeat\n\nlast receipt line'
    notes = ('same note', 'same note', '')
    response = CodexAdapter().render(_result('allow', receipt, notes))
    message = json.loads(response.stdout)['systemMessage']
    assert len(message) <= 10_000
    assert message.endswith('... (more on stderr)')
    assert response.stderr == f'{receipt}\nsame note\nsame note\n\n'
