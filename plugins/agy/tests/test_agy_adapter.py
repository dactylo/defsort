# pyright: reportPrivateUsage=false

import json
import shlex
from pathlib import Path

from defsort._hook_front import _build_hook_parser
from defsort.hooks import API_VERSION, HookAdapter
from plugins.agy._defsort._adapter import AntigravityAdapter

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_contract() -> None:
    adapter: HookAdapter = AntigravityAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'agy',
        API_VERSION,
        True,
    )


def test_manifest_registers_only_pre_tool_use() -> None:
    manifest = _PLUGIN_ROOT / 'hooks.json'
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


def test_plugin_manifest_uses_defsort_identity() -> None:
    manifest = json.loads((_PLUGIN_ROOT / 'plugin.json').read_text(encoding='utf-8'))
    assert manifest == {
        '$schema': 'https://antigravity.google/schemas/v1/plugin.json',
        'name': 'defsort-agy',
        'description': 'Install the defsort commit-time definition sorting hook for Antigravity.',
    }
