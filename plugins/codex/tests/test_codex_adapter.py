# pyright: reportPrivateUsage=false

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._cli import _build_hook_parser
from defsort.hooks import API_VERSION, HookAdapter, HookResult, HostInput, HostResponse
from plugins.codex._defsort._adapter import CodexAdapter

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
    adapter: HookAdapter = CodexAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'codex',
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
                    'hooks': [{'type': 'command', 'command': 'defsort hook --agent codex'}],
                }
            ]
        }
    }
    argv = shlex.split('defsort hook --agent codex')
    args = _build_hook_parser().parse_args(argv[2:])
    assert args.agent == 'codex'
    assert (args.event is not None) == CodexAdapter.requires_event


def test_parse_post_tool_apply_patch_paths() -> None:
    command = (
        '*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b/c.py\n'
        '*** Move to: d.py\n*** Delete File: e.txt\n*** Update File: a.py\n*** End Patch\n'
    )
    payload = _payload('PostToolUse', 'apply_patch', {'command': command})
    assert CodexAdapter().parse(payload, None) == HostInput(
        'post_tool', None, '/r', None, ('a.py', 'b/c.py', 'd.py', 'e.txt')
    )


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


def test_plugin_manifest_and_marketplace_use_defsort_identity() -> None:
    manifest = json.loads(
        (PLUGIN_ROOT / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8')
    )
    marketplace = json.loads(
        (ROOT / '.agents' / 'plugins' / 'marketplace.json').read_text(encoding='utf-8')
    )
    assert manifest['name'] == 'defsort'
    assert 'hooks' not in manifest
    assert marketplace['plugins'][0]['source'] == {
        'source': 'local',
        'path': './plugins/codex',
    }


def test_render_allow_and_failure() -> None:
    assert CodexAdapter().render(_result('allow')) == HostResponse('', '', 0)
    assert json.loads(CodexAdapter().render_failure('defsort: reason').stdout) == DENY_JSON


def test_render_allow_feedback_is_one_system_message() -> None:
    response = CodexAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'systemMessage': 'first\nsecond'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_render_deny_matches_claude_shape() -> None:
    response = CodexAdapter().render(_result('deny', 'defsort: reason'))
    assert json.loads(response.stdout) == DENY_JSON
    assert (response.stderr, response.exit_code) == ('defsort: reason\n', 0)


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
    response = adapter.render(_result('deny', 'defsort: reason'))
    assert json.loads(response.stdout) == DENY_JSON
    receipt = adapter.render(_result('allow', 'staged rewrite receipt'))
    assert json.loads(receipt.stdout) == {'systemMessage': 'staged rewrite receipt'}
