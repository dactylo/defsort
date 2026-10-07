# pyright: reportPrivateUsage=false

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._hook_front import _build_hook_parser
from defsort.hooks import (
    API_VERSION,
    HookAdapter,
    HookResponse,
    HookResult,
    HostInput,
    HostResponse,
)
from plugins.claude._defsort._adapter import ClaudeAdapter

DENY_JSON = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'defsort: reason',
    }
}
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ROOT = PLUGIN_ROOT.parents[1]


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


def test_contract() -> None:
    adapter: HookAdapter = ClaudeAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'claude',
        API_VERSION,
        False,
    )


def test_manifest_registers_only_pre_tool_use() -> None:
    manifest = PLUGIN_ROOT / 'hooks' / 'hooks.json'
    assert json.loads(manifest.read_text(encoding='utf-8')) == {
        'hooks': {
            'PreToolUse': [
                {
                    'matcher': '^Bash$',
                    'hooks': [{'type': 'command', 'command': 'defsort hook --agent claude'}],
                }
            ]
        }
    }
    argv = shlex.split('defsort hook --agent claude')
    args = _build_hook_parser().parse_args(argv[2:])
    assert args.agent == 'claude'
    assert (args.event is not None) == ClaudeAdapter.requires_event


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


def test_plugin_manifest_and_marketplace_use_defsort_identity() -> None:
    manifest = json.loads(
        (PLUGIN_ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8')
    )
    marketplace = json.loads(
        (ROOT / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8')
    )
    assert manifest['name'] == 'defsort'
    assert marketplace['plugins'][0]['source'] == './plugins/claude'


def test_render_allow_feedback_is_one_system_message() -> None:
    response = ClaudeAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'systemMessage': 'first\nsecond'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_render_allow_is_silent_with_notes_on_stderr() -> None:
    response: HookResponse = ClaudeAdapter().render(
        _result('allow', notes=('defsort: a.py:1: f skipped',))
    )
    assert response == HostResponse('', 'defsort: a.py:1: f skipped\n', 0)


def test_render_deny_is_json_and_stderr_reason() -> None:
    response = ClaudeAdapter().render(_result('deny', 'defsort: reason', ('note',)))
    assert json.loads(response.stdout) == DENY_JSON
    assert response.stderr == 'note\ndefsort: reason\n'
    assert response.exit_code == 0


def test_render_failure() -> None:
    response = ClaudeAdapter().render_failure('defsort: reason')
    assert json.loads(response.stdout) == DENY_JSON
    assert response.stderr == 'defsort: reason\n'
    assert response.exit_code == 0


@pytest.mark.parametrize(
    ('reason', 'notes', 'context'),
    [
        (None, ('Cannot preserve layout.',), 'Cannot preserve layout.'),
        ('Cannot sort a.py: invalid syntax.', (), 'Cannot sort a.py: invalid syntax.'),
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
    response = adapter.render(_result('deny', 'defsort: reason'))
    assert json.loads(response.stdout) == DENY_JSON
    receipt = adapter.render(_result('allow', 'staged rewrite receipt'))
    assert json.loads(receipt.stdout) == {'systemMessage': 'staged rewrite receipt'}
