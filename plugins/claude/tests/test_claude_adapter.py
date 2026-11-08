# pyright: reportPrivateUsage=false

import json
import shlex
from pathlib import Path

from defsort._hook_front import _build_hook_parser
from defsort.hooks import API_VERSION, HookAdapter
from plugins.claude._defsort._adapter import ClaudeAdapter

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_ROOT = _PLUGIN_ROOT.parents[1]


def test_contract() -> None:
    adapter: HookAdapter = ClaudeAdapter()
    assert (adapter.name, adapter.api_version, adapter.requires_event) == (
        'claude',
        API_VERSION,
        False,
    )


def test_manifest_registers_only_pre_tool_use() -> None:
    manifest = _PLUGIN_ROOT / 'hooks' / 'hooks.json'
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


def test_plugin_manifest_and_marketplace_use_defsort_identity() -> None:
    manifest = json.loads(
        (_PLUGIN_ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8')
    )
    marketplace = json.loads(
        (_ROOT / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8')
    )
    assert manifest['name'] == 'defsort'
    assert marketplace['plugins'][0]['source'] == './plugins/claude'
