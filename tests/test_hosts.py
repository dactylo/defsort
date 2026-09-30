import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from defsort._hook import run_hook
from defsort._hosts import AdapterLoadError, _registered_hosts, load_adapter
from defsort.hooks import HookAdapter, HookInput, HookResponse, HookResult
from tests._helpers import BUILTIN_ENTRY_POINTS, register_distribution
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

EXAMPLE_ENTRY_POINTS = {
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
PAYLOAD = json.dumps(
    {
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {'command': 'ls'},
        'cwd': '.',
    }
)


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
    register_distribution(registered_hosts, 'defsort-host-example', EXAMPLE_ENTRY_POINTS)
    return registered_hosts


@dataclass(frozen=True)
class _Result:
    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


def _result(decision: str, reason: str | None = None, notes: tuple[str, ...] = ()) -> HookResult:
    return _Result('deny' if decision == 'deny' else 'allow', reason, notes)


class TestDiscovery:
    def test_bundled_plugins_are_registered_through_the_entry_point_group(self) -> None:
        assert _registered_hosts() == sorted(BUILTIN_ENTRY_POINTS)
        for name in BUILTIN_ENTRY_POINTS:
            assert load_adapter(name).name == name

    def test_duplicate_name_is_refused(self, example_hosts: Path) -> None:
        register_distribution(
            example_hosts,
            'defsort-host-duplicate',
            {'example': EXAMPLE_ENTRY_POINTS['example']},
        )
        with pytest.raises(AdapterLoadError, match="host 'example' is registered 2 times"):
            load_adapter('example')

    @pytest.mark.usefixtures('example_hosts')
    def test_third_party_adapter_is_discovered(self) -> None:
        adapter = load_adapter('example')
        assert adapter.name == 'example'
        assert 'example' in _registered_hosts()

    @pytest.mark.usefixtures('example_hosts')
    def test_version_mismatch_is_refused(self) -> None:
        with pytest.raises(
            AdapterLoadError,
            match='hook adapter API',
        ):
            load_adapter('stale')

    def test_unknown_host_names_the_registered_hosts(self) -> None:
        names = ', '.join(sorted(BUILTIN_ENTRY_POINTS))
        with pytest.raises(
            AdapterLoadError,
            match=rf"unknown host 'nope'; registered hosts: {names}",
        ):
            load_adapter('nope')

    @pytest.mark.usefixtures('example_hosts')
    def test_unimportable_adapter_is_refused(self) -> None:
        with pytest.raises(AdapterLoadError, match="host 'missing' failed to load"):
            load_adapter('missing')


class TestLastResort:
    @pytest.mark.usefixtures('example_hosts')
    @pytest.mark.parametrize('host', ['empty-cwd', 'relative-cwd', 'typed-cwd'])
    def test_invalid_command_cwd_is_a_last_resort_failure(
        self,
        host: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError('repository code must not run')

        monkeypatch.setattr('defsort._hook._run_event', explode)
        assert run_hook(PAYLOAD, host=host) == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert 'invalid command_cwd' in captured.err

    @pytest.mark.usefixtures('example_hosts')
    def test_adapter_parse_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_hook(PAYLOAD, host='broken-parse') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == (
            "defsort: host 'broken-parse' failed to parse the payload: parse exploded\n"
        )

    @pytest.mark.usefixtures('example_hosts')
    def test_multiline_parse_failure_is_one_diagnostic_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail_parse(_self: object, _payload: object, _event: object) -> None:
            raise RuntimeError('first\nsecond')

        monkeypatch.setattr(BrokenParseAdapter, 'parse', fail_parse)
        assert run_hook(PAYLOAD, host='broken-parse') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == (
            "defsort: host 'broken-parse' failed to parse the payload: first\\x0asecond\n"
        )
        assert len(captured.err.splitlines()) == 1

    @pytest.mark.usefixtures('example_hosts')
    def test_invalid_json_and_inapplicable_payloads_are_silent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_hook('not json', host='example') == 0
        assert run_hook('[1]', host='example') == 0
        assert run_hook(json.dumps({'hook_event_name': 'Notification'}), host='example') == 0
        assert capsys.readouterr() == ('', '')

    def test_missing_and_unexpected_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        required = next(name for name in BUILTIN_ENTRY_POINTS if load_adapter(name).requires_event)
        embedded = next(
            name for name in BUILTIN_ENTRY_POINTS if not load_adapter(name).requires_event
        )
        assert run_hook('{}', host=required) == 2
        assert capsys.readouterr().err == f"defsort: host '{required}' requires --event\n"
        assert run_hook('{}', host=embedded, event='PreToolUse') == 2
        assert capsys.readouterr().err == (f"defsort: host '{embedded}' does not accept --event\n")

    @pytest.mark.usefixtures('example_hosts')
    def test_raising_adapter_version_is_contained(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail_version(_self: object) -> int:
            raise RuntimeError('first\nsecond')

        monkeypatch.setattr(ExampleAdapter, 'api_version', property(fail_version))
        assert run_hook(PAYLOAD, host='example') == 2
        assert capsys.readouterr() == (
            '',
            "defsort: host 'example' failed to load: first\\x0asecond\n",
        )

    @pytest.mark.usefixtures('example_hosts')
    def test_render_failure_falls_back_to_the_failure_response(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_hook(PAYLOAD, host='broken-render') == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {
            'example': {
                'decision': 'deny',
                'reason': "defsort: host 'broken-render' failed to render the response",
            }
        }

    @pytest.mark.usefixtures('example_hosts')
    def test_both_render_paths_failing_is_the_last_resort(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_hook(PAYLOAD, host='broken-both') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == (
            "defsort: host 'broken-both' failed to render the failure response\n"
        )

    def test_unknown_host(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_hook(PAYLOAD, host='nope') == 2
        captured = capsys.readouterr()
        assert captured.out == ''
        names = ', '.join(sorted(BUILTIN_ENTRY_POINTS))
        assert captured.err == f"defsort: unknown host 'nope'; registered hosts: {names}\n"
