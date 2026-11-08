import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._hook_front import _TOOL
from defsort._hook_front import _run_hook as run_hook
from defsort._vendor.hookfront._loader import load_adapter, registered_host_names
from defsort.hooks import HookAdapter, HookInput, HookResponse, HookResult
from tests._helpers import BUILTIN_ENTRY_POINTS, invoke_cli, register_distribution
from tests.hosts_example.adapter import (
    BrokenBothAdapter,
    BrokenParseAdapter,
    BrokenRenderAdapter,
    EmptyCwdAdapter,
    ExampleAdapter,
    ExampleInput,
    ExampleResponse,
    RelativeCwdAdapter,
    StaleAdapter,
    TypedCwdAdapter,
)

pytestmark = pytest.mark.usefixtures('registered_hosts')

_EXAMPLE_ENTRY_POINTS = {
    **{
        adapter.name: f'{adapter.__module__}:{adapter.__qualname__}'
        for adapter in (
            ExampleAdapter,
            StaleAdapter,
            EmptyCwdAdapter,
            RelativeCwdAdapter,
            TypedCwdAdapter,
            BrokenParseAdapter,
            BrokenRenderAdapter,
            BrokenBothAdapter,
        )
    },
    'missing': 'tests.hosts_example.missing:Adapter',
}
_PAYLOAD = json.dumps(
    {
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {'command': 'ls'},
        'cwd': '.',
    }
)


class TestDiscovery:
    def test_bundled_plugins_are_registered_through_the_entry_point_group(self) -> None:
        assert registered_host_names(_TOOL) == sorted(BUILTIN_ENTRY_POINTS)
        for name in BUILTIN_ENTRY_POINTS:
            assert load_adapter(_TOOL, name).name == name

    def test_third_party_adapter_is_discovered(self, example_hosts: Path) -> None:
        del example_hosts
        adapter = load_adapter(_TOOL, 'example')
        assert adapter.name == 'example'
        assert 'example' in registered_host_names(_TOOL)


class TestLastResort:
    def test_unknown_host(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_hook(_PAYLOAD, host='nope') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        names = ', '.join(sorted(BUILTIN_ENTRY_POINTS))
        assert captured.err == f"defsort: unknown host 'nope'; registered hosts: {names}\n"


class TestThirdPartyContract:
    def test_example_classes_satisfy_the_public_protocols(self) -> None:
        adapter: HookAdapter = ExampleAdapter()
        parsed: HookInput = ExampleInput('pre_tool', 'ls', '.', None)
        response: HookResponse = ExampleResponse('', '', 0)
        payload = {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls'},
            'cwd': '.',
        }
        assert adapter.parse(payload, None) == parsed
        assert adapter.render(_result('allow')) == response


@pytest.fixture
def example_hosts(registered_hosts: Path) -> Path:
    register_distribution(registered_hosts, 'defsort-host-example', _EXAMPLE_ENTRY_POINTS)
    return registered_hosts


@dataclass(frozen=True)
class _Result:
    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


def _result(decision: str, reason: str | None = None, notes: tuple[str, ...] = ()) -> HookResult:
    return _Result('deny' if decision == 'deny' else 'allow', reason, notes)


def test_unknown_host_suggests_the_close_name() -> None:
    status, stdout, stderr = invoke_cli(['hook', '--agent', 'claud'], stdin=_PAYLOAD)
    assert status == 2
    assert stdout == ''
    assert stderr == (
        "defsort: unknown host 'claud' (did you mean 'claude'?); "
        'registered hosts: agy, claude, codex\n'
    )
