# pyright: reportPrivateUsage=false

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._hook_front import _build_hook_parser
from defsort.hooks import API_VERSION, HookAdapter, HookResult, HostInput, HostResponse
from plugins.agy._defsort._adapter import AntigravityAdapter

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


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


def test_manifest_registers_only_pre_tool_use() -> None:
    manifest = PLUGIN_ROOT / 'hooks.json'
    assert json.loads(manifest.read_text(encoding='utf-8')) == {
        'defsort': {
            'PreToolUse': [
                {
                    'matcher': 'run_command',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'defsort hook --agent agy --event PreToolUse',
                        }
                    ],
                }
            ]
        }
    }
    argv = shlex.split('defsort hook --agent agy --event PreToolUse')
    args = _build_hook_parser().parse_args(argv[2:])
    assert args.agent == 'agy'
    assert (args.event is not None) == AntigravityAdapter.requires_event


@pytest.mark.parametrize('tool', ['write_to_file', 'replace_file_content'])
def test_parse_post_tool_file_writers(tool: str) -> None:
    payload = _payload(tool, {'TargetFile': '/t/a.py', 'Cwd': '/t'})
    assert AntigravityAdapter().parse(payload, 'PostToolUse') == HostInput(
        'post_tool', None, '/t', None, ('/t/a.py',)
    )


def test_parse_pre_tool_other_tool_has_no_command() -> None:
    payload = _payload('write_to_file', {'TargetFile': 'a.py', 'Cwd': '/t'})
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', None, '/t', None
    )


def test_parse_rejects_unknown_event() -> None:
    with pytest.raises(ValueError, match='unknown Antigravity event'):
        AntigravityAdapter().parse({}, 'Stop')


@pytest.mark.parametrize('cwd', ['rel', '', 7])
def test_parse_run_command_non_absolute_cwd_is_unknown(cwd: object) -> None:
    payload = _payload('run_command', {'CommandLine': 'git commit -m x', 'Cwd': cwd})
    payload['workspacePaths'] = ['/ws']
    parsed = AntigravityAdapter().parse(payload, 'PreToolUse')
    assert parsed is not None
    assert parsed.command_cwd is None


def test_parse_run_command_prefers_cwd_over_workspace(tmp_path: Path) -> None:
    cwd = str(tmp_path / 'command')
    payload = _payload('run_command', {'CommandLine': 'git commit -m x', 'Cwd': cwd})
    payload['workspacePaths'] = [str(tmp_path / 'workspace')]
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', 'git commit -m x', cwd, cwd
    )


def test_parse_run_command_without_cwd_is_unknown() -> None:
    payload = _payload('run_command', {'CommandLine': 'git commit -m x'})
    payload['workspacePaths'] = ['/ws']
    assert AntigravityAdapter().parse(payload, 'PreToolUse') == HostInput(
        'pre_tool', 'git commit -m x', '/ws', None
    )


def test_plugin_manifest_uses_defsort_identity() -> None:
    manifest = json.loads((PLUGIN_ROOT / 'plugin.json').read_text(encoding='utf-8'))
    assert manifest == {
        '$schema': 'https://antigravity.google/schemas/v1/plugin.json',
        'name': 'defsort-agy',
        'description': 'Install the defsort commit-time definition sorting hook for Antigravity.',
    }


def test_render_allow_and_failure() -> None:
    assert AntigravityAdapter().render(_result('allow')) == HostResponse('', '', 0)
    failure = AntigravityAdapter().render_failure('defsort: reason')
    assert json.loads(failure.stdout) == {'decision': 'deny', 'reason': 'defsort: reason'}


def test_render_allow_feedback_is_one_system_message() -> None:
    response = AntigravityAdapter().render(_result('allow', 'first\nsecond', ('diagnostic',)))
    assert json.loads(response.stdout) == {'systemMessage': 'first\nsecond'}
    assert response.stderr == 'diagnostic\n'
    assert response.exit_code == 0


def test_render_deny_is_top_level_json() -> None:
    response = AntigravityAdapter().render(_result('deny', 'defsort: reason', ('note',)))
    assert json.loads(response.stdout) == {'decision': 'deny', 'reason': 'defsort: reason'}
    assert (response.stderr, response.exit_code) == ('note\ndefsort: reason\n', 0)
