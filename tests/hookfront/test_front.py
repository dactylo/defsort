import importlib.metadata
import io
import sys
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from importlib.metadata import EntryPoint
from pathlib import Path, PureWindowsPath
from typing import cast, override

import pytest

from defsort._vendor.hookfront import (
    _front,
    _loader,
)
from defsort._vendor.hookfront._claude import (
    ClaudeAdapter,
)
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
from defsort._vendor.hookfront._front import (
    Engine,
    HookOptions,
    Outcome,
    _commit_hint,
    _documentation_url,
    allow,
    describe_error,
    fail,
    resolve_edited_paths,
    run_hook,
)
from defsort._vendor.hookfront._tool import (
    ToolIdentity,
)
from defsort._vendor.hookshell import (
    _shell,
)
from defsort._vendor.hookshell._grammar import (
    CommitPlan,
)
from defsort._vendor.srcfiles import (
    FileChangedError,
    RestoreError,
)

_TOOL = ToolIdentity('acme', 'sort')


def _invoke(
    stdin_data: str = '{}',
    *,
    host: str = 'probe',
    event: str | None = None,
    allow_on_failure: bool = False,
    load_engine: Callable[[], Engine] | None = None,
) -> int:
    return run_hook(
        stdin_data,
        tool=_TOOL,
        options=HookOptions(host, event, allow_on_failure),
        load_engine=_forbidden_engine if load_engine is None else load_engine,
    )


class _AdapterFields:
    def __init__(self, fields: dict[str, object]) -> None:
        self._fields = fields

    def __getattr__(self, name: str) -> object:
        if name not in self._fields:
            raise AttributeError(name)
        value = self._fields[name]
        if isinstance(value, BaseException):
            raise value
        return value


class _AdapterProbe:
    requires_event = False

    def __init__(self, parsed: object, response: object) -> None:
        self._parsed = parsed
        self._response = response

    def parse(self, payload: Mapping[str, object], event: str | None) -> object:
        del payload, event
        if isinstance(self._parsed, BaseException):
            raise self._parsed
        return self._parsed

    def render(self, result: HookResult) -> object:
        del result
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    def render_failure(self, reason: str) -> object:
        del reason
        return self._response


def _forbidden_engine() -> Engine:
    pytest.fail('repository or source work was called')


def _import_failure() -> Engine:
    raise ModuleNotFoundError('engine unavailable')


def _install_adapter_probe(monkeypatch: pytest.MonkeyPatch, adapter: object) -> None:
    def load_probe(tool: ToolIdentity, _host: str) -> HookAdapter:
        assert tool == _TOOL
        return cast('HookAdapter', adapter)

    monkeypatch.setattr(_loader, 'load_adapter', load_probe)


@pytest.mark.parametrize('exception_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('boundary', ['metadata', 'parse', 'input', 'render', 'response'])
def test_adapter_process_control_exceptions_propagate(
    boundary: str,
    exception_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = exception_type('stop')
    parsed: object = HostInput('pre_tool', None, '.', None)
    response: object = HostResponse('', '', 0)
    if boundary == 'parse':
        parsed = failure
    elif boundary == 'input':
        parsed = _AdapterFields({'command_cwd': failure})
    elif boundary == 'render':
        response = failure
    elif boundary == 'response':
        response = _AdapterFields({'stdout': 'partial', 'stderr': failure, 'exit_code': 0})
    adapter = _AdapterProbe(parsed, response)
    _install_adapter_probe(
        monkeypatch,
        _AdapterFields({'requires_event': failure}) if boundary == 'metadata' else adapter,
    )

    with pytest.raises(exception_type, match='stop'):
        _invoke('{}', host='probe')
    assert capsys.readouterr() == ('', '')


def _set_documentation_url(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    def documentation_url(distribution: str) -> str | None:
        assert distribution == _TOOL.name
        return url

    monkeypatch.setattr(_front, '_documentation_url', documentation_url)


@pytest.mark.parametrize('exception_type', [KeyboardInterrupt, SystemExit])
def test_failure_renderer_process_control_exceptions_propagate(
    exception_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class InterruptedFailureAdapter(_AdapterProbe):
        def render_failure(self, reason: str) -> object:
            del reason
            raise exception_type('stop')

    adapter = InterruptedFailureAdapter(
        HostInput('pre_tool', None, '.', None), RuntimeError('render failed')
    )
    _install_adapter_probe(monkeypatch, adapter)
    with pytest.raises(exception_type, match='stop'):
        _invoke('{}', host='probe')
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('raises', [False, True])
@pytest.mark.parametrize(
    'field', ['requires_event', 'command_cwd', 'event', 'command', 'cwd', 'edited_paths']
)
def test_malformed_adapter_fields_fail_closed(
    field: str,
    *,
    raises: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fields: dict[str, object] = {
        'event': 'post_tool' if field in {'cwd', 'edited_paths'} else 'pre_tool',
        'command': 'ls',
        'command_cwd': None,
        'cwd': '.',
        'edited_paths': (),
    }
    if field != 'requires_event':
        fields.pop(field)
        if raises:
            fields[field] = RuntimeError('first\nsecond')
    adapter = _AdapterProbe(_AdapterFields(fields), HostResponse('', '', 0))
    if field == 'requires_event':
        metadata: dict[str, object] = {'parse': adapter.parse}
        if raises:
            metadata[field] = RuntimeError('first\nsecond')
        _install_adapter_probe(monkeypatch, _AdapterFields(metadata))
    else:
        _install_adapter_probe(monkeypatch, adapter)

    assert _invoke('{}', host='probe\nname') == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("acme: host 'probe\\x0aname'")
    assert 'Traceback' not in captured.err


def test_commit_denies_when_engine_import_fails(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    assert _invoke(load_engine=_import_failure) == 0
    result = adapter.results[0]
    assert result.decision == 'deny'
    assert result.reason == (
        'acme: unexpected error during sort: ModuleNotFoundError: engine unavailable'
    )
    assert 'Traceback (most recent call last):' in capsys.readouterr().err


def test_commit_warns_and_allows_with_allow_on_failure(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    assert _invoke(load_engine=_import_failure, allow_on_failure=True) == 0
    assert adapter.results[0].decision == 'allow'
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'WARNING: acme: unexpected error during sort' in captured.err


@pytest.mark.parametrize(
    ('field', 'value'),
    [('event', 'PostToolUse'), ('event', 42), ('command', 42)],
)
def test_invalid_adapter_values_fail_closed(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fields: dict[str, object] = {
        'event': 'pre_tool',
        'command': 'ls',
        'command_cwd': None,
        'cwd': '.',
        'edited_paths': (),
    }
    fields[field] = value
    _install_adapter_probe(
        monkeypatch, _AdapterProbe(_AdapterFields(fields), HostResponse('', '', 0))
    )

    assert _invoke('{}', host='probe') == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err.startswith("acme: host 'probe' failed to parse the payload:")


@pytest.mark.parametrize(
    'case',
    [
        ('cwd', 42, 'post-tool cwd must be a string'),
        ('edited_paths', (42,), 'post-tool edited paths must be strings'),
        (
            'edited_paths',
            'mod.py',
            'post-tool edited paths must be a sequence of strings, not a string',
        ),
    ],
)
def test_malformed_post_tool_paths_fail_before_repository_work(
    case: tuple[str, object, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    field, value, detail = case
    source = tmp_path / 'mod.py'
    source.write_bytes(b'def zulu():\n    pass\n')
    fields: dict[str, object] = {
        'event': 'post_tool',
        'command': None,
        'command_cwd': None,
        'cwd': str(tmp_path),
        'edited_paths': ('mod.py',),
    }
    fields[field] = value
    adapter = _AdapterProbe(_AdapterFields(fields), HostResponse('partial', '', 0))
    _install_adapter_probe(monkeypatch, adapter)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        pytest.fail('repository or source work was called')

    monkeypatch.setattr(_front, '_run_event', fail_if_called)

    assert _invoke('{}', host='probe\nname') == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("acme: host 'probe\\x0aname'")
    assert detail in captured.err
    assert 'Traceback' not in captured.err
    assert source.read_bytes() == b'def zulu():\n    pass\n'


@pytest.mark.parametrize('field', ['stdout', 'stderr', 'exit_code'])
@pytest.mark.parametrize('failure', ['missing', 'raising', 'wrong_type'])
def test_malformed_response_emits_no_partial_output(
    field: str,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fields: dict[str, object] = {
        'stdout': '{"decision":"allow"}\n',
        'stderr': 'note\n',
        'exit_code': 0,
    }
    fields.pop(field)
    if failure == 'raising':
        fields[field] = RuntimeError('first\nsecond')
    elif failure == 'wrong_type':
        fields[field] = 'zero' if field == 'exit_code' else 7
    adapter = _AdapterProbe(HostInput('pre_tool', None, '.', None), _AdapterFields(fields))
    _install_adapter_probe(monkeypatch, adapter)

    assert _invoke('{}', host='probe\nname') == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("acme: host 'probe\\x0aname'")
    assert 'Traceback' not in captured.err


@pytest.mark.parametrize('failure', ['missing', 'raising'])
def test_unusable_renderers_fail_closed(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _AdapterProbe(HostInput('pre_tool', None, '.', None), HostResponse('', '', 0))
    fields: dict[str, object] = {'requires_event': False, 'parse': adapter.parse}
    if failure == 'raising':
        fields.update(
            render=RuntimeError('render failed'), render_failure=RuntimeError('failure failed')
        )
    _install_adapter_probe(monkeypatch, _AdapterFields(fields))
    assert _invoke('{}', host='probe') == 2
    assert capsys.readouterr() == (
        '',
        "acme: host 'probe' failed to render the failure response\n",
    )


class RecordingAdapter:
    name = 'probe'
    api_version = API_VERSION
    requires_event = False

    def __init__(self) -> None:
        self.parsed: object = HostInput(
            'pre_tool', 'git commit -m message', str(Path.cwd()), str(Path.cwd())
        )
        self.results: list[HookResult] = []

    def parse(self, payload: Mapping[str, object], event: str | None) -> object:
        del event
        return None if payload.get('inapplicable') else self.parsed

    def render(self, result: HookResult) -> HostResponse:
        self.results.append(result)
        return HostResponse(result.reason or '', ''.join(f'{line}\n' for line in result.notes), 0)

    def render_failure(self, reason: str) -> HostResponse:
        return HostResponse('denied', reason, 2)


def test_engine_notes_follow_captured_stderr(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    def run_commit(plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        del plan, allow_on_failure
        print('printed note', file=sys.stderr)
        return Outcome('allow', 'commit receipt', ('returned note',))

    def run_post_tool(edited_paths: Sequence[str], cwd: str) -> Outcome:
        del edited_paths, cwd
        pytest.fail('wrong engine runner')

    assert _invoke(load_engine=lambda: Engine(run_commit, run_post_tool)) == 0
    assert adapter.results == [
        Outcome('allow', 'commit receipt', ('printed note', 'returned note'))
    ]
    assert capsys.readouterr() == ('commit receipt', 'printed note\nreturned note\n')


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> 'RecordingAdapter':
    recording = RecordingAdapter()
    _install_adapter_probe(monkeypatch, recording)
    return recording


@pytest.mark.parametrize('event', ['pre_tool', 'post_tool'])
def test_engine_call_import_error_escapes(event: str, adapter: 'RecordingAdapter') -> None:
    def fail_commit(plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        del plan, allow_on_failure
        raise ImportError('engine call failed')

    def fail_post(edited_paths: Sequence[str], cwd: str) -> Outcome:
        del edited_paths, cwd
        raise ImportError('engine call failed')

    if event == 'post_tool':
        adapter.parsed = HostInput('post_tool', None, '/', None, ('mod.py',))
    with pytest.raises(ImportError, match='engine call failed'):
        _invoke(load_engine=lambda: Engine(fail_commit, fail_post))
    assert adapter.results == []


@pytest.mark.parametrize('reason', [None, ''])
def test_deny_without_reason_renders_the_fallback(
    reason: str | None, adapter: 'RecordingAdapter'
) -> None:
    def deny(plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        del plan, allow_on_failure
        return Outcome('deny', reason)

    def unused_post(edited_paths: Sequence[str], cwd: str) -> Outcome:
        del edited_paths, cwd
        pytest.fail('post-tool engine was called')

    assert _invoke(load_engine=lambda: Engine(deny, unused_post)) == 0
    assert adapter.results[0].decision == 'deny'
    assert adapter.results[0].reason == 'acme denied the operation'


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_classifier_exception_forwards_hint_and_always_denies(
    adapter: 'RecordingAdapter',
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_on_failure: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forwarded: list[tuple[str, str, str]] = []

    def classification_failed(
        _command: str,
        _cwd: str,
        *,
        reason_prefix: str,
        possible_reason_prefix: str,
        commit_hint: Callable[[], str],
    ) -> None:
        forwarded.append((reason_prefix, possible_reason_prefix, commit_hint()))
        raise RuntimeError('classifier exploded')

    monkeypatch.setattr(_front, 'classify_command', classification_failed)
    _set_documentation_url(monkeypatch, None)
    assert _invoke(allow_on_failure=allow_on_failure) == 0
    result = adapter.results[0]
    assert result.decision == 'deny'
    assert result.reason == (
        'acme: unexpected error during classification: RuntimeError: classifier exploded'
    )
    assert 'Traceback (most recent call last):' in result.notes
    assert forwarded == [
        (
            _TOOL.reason_prefix,
            _TOOL.possible_reason_prefix,
            'supported commit options are listed in the acme hook guide, hook.md',
        )
    ]
    assert 'WARNING:' not in capsys.readouterr().err


def test_invalid_json_and_inapplicable_payloads_are_silent(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    for payload in ('not json', '[1]', '{"inapplicable": true}'):
        assert _invoke(payload) == 0
    assert adapter.results == []
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize(
    ('project_urls', 'expected'),
    [
        (['documentation ,https://example.invalid/docs'], 'https://example.invalid/docs'),
        (['Documentation, '], None),
        (None, None),
    ],
)
def test_documentation_url_label_variants(
    monkeypatch: pytest.MonkeyPatch, project_urls: list[str] | None, expected: str | None
) -> None:
    def distribution_metadata(name: str) -> Message:
        assert name == _TOOL.name
        if project_urls is None:
            raise importlib.metadata.PackageNotFoundError(name)
        metadata = Message()
        for url in project_urls:
            metadata.add_header('Project-URL', url)
        return metadata

    monkeypatch.setattr(importlib.metadata, 'metadata', distribution_metadata)
    _documentation_url.cache_clear()
    try:
        assert _documentation_url(_TOOL.name) == expected
    finally:
        _documentation_url.cache_clear()


@pytest.mark.parametrize(
    ('project_url', 'expected'),
    [
        (
            'https://example.invalid/docs',
            'supported commit options are listed in hook.md at https://example.invalid/docs',
        ),
        (None, 'supported commit options are listed in the acme hook guide, hook.md'),
    ],
)
def test_commit_hint_uses_documentation_url(
    monkeypatch: pytest.MonkeyPatch, project_url: str | None, expected: str
) -> None:
    _set_documentation_url(monkeypatch, project_url)
    assert _commit_hint(_TOOL) == expected


@pytest.mark.parametrize('value', ['', 'relative/path', 42])
def test_invalid_command_cwd_is_a_last_resort_failure(
    value: object, adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    adapter.parsed = _AdapterFields({'command_cwd': value})
    assert _invoke() == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err.startswith("acme: host 'probe' supplied an invalid command_cwd")


def test_malformed_response_recovers_with_valid_denial(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class RecoveringAdapter(_AdapterProbe):
        def render_failure(self, reason: str) -> HostResponse:
            return HostResponse('denied', reason, 2)

    adapter = RecoveringAdapter(
        HostInput('pre_tool', None, '.', None),
        _AdapterFields({'stdout': 'partial allow', 'stderr': ''}),
    )
    _install_adapter_probe(monkeypatch, adapter)
    assert _invoke('{}', host='probe') == 2
    assert capsys.readouterr() == (
        'denied',
        "acme: host 'probe' failed to render the response",
    )


def test_missing_and_unexpected_event(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    adapter.requires_event = True
    assert _invoke() == 2
    assert capsys.readouterr() == ('', "acme: host 'probe' requires --event\n")
    adapter.requires_event = False
    assert _invoke(event='PreToolUse') == 2
    assert capsys.readouterr() == ('', "acme: host 'probe' does not accept --event\n")


def test_post_tool_allows_with_diagnostic_when_engine_import_fails(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    adapter.parsed = HostInput('post_tool', None, '/', None, ('mod.py',))
    assert _invoke(load_engine=_import_failure) == 0
    assert adapter.results[0].decision == 'allow'
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'acme: unexpected error during PostToolUse' in captured.err
    assert 'Traceback (most recent call last):' in captured.err


def test_post_tool_dispatch_forwards_paths_and_cwd(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[tuple[str, ...], str]] = []
    adapter.parsed = HostInput(
        'post_tool', None, 'event-directory', None, ('first.py', 'second.py')
    )

    def run_commit(plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        del plan, allow_on_failure
        pytest.fail('wrong engine runner')

    def run_post_tool(edited_paths: Sequence[str], cwd: str) -> Outcome:
        calls.append((tuple(edited_paths), cwd))
        return Outcome('allow', 'edit receipt')

    assert _invoke(load_engine=lambda: Engine(run_commit, run_post_tool)) == 0
    assert calls == [(('first.py', 'second.py'), 'event-directory')]
    assert adapter.results == [Outcome('allow', 'edit receipt')]
    assert capsys.readouterr() == ('edit receipt', '')


def test_raising_adapter_version_is_contained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_version(_self: object) -> int:
        raise RuntimeError('first\nsecond')

    monkeypatch.setattr(RecordingAdapter, 'api_version', property(fail_version))
    entry = EntryPoint(
        'probe', f'{RecordingAdapter.__module__}:RecordingAdapter', _TOOL.entry_point_group
    )

    def entries(*, group: str) -> tuple[EntryPoint, ...]:
        assert group == _TOOL.entry_point_group
        return (entry,)

    monkeypatch.setattr(_loader, 'entry_points', entries)
    assert _invoke() == 2
    assert capsys.readouterr() == ('', "acme: host 'probe' failed to load: first\\x0asecond\n")


def test_read_payload_returns_stdin_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"payload": true}\n'))
    assert _front.read_payload(_TOOL) == '{"payload": true}\n'


@pytest.mark.parametrize('message', ['parse exploded', 'first\nsecond'])
def test_adapter_parse_failure_is_one_diagnostic_line(
    message: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_adapter_probe(monkeypatch, _AdapterProbe(RuntimeError(message), None))
    assert _invoke() == 2
    expected = message.replace('\n', '\\x0a')
    assert capsys.readouterr() == (
        '',
        f"acme: host 'probe' failed to parse the payload: {expected}\n",
    )


@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_relative_post_tool_path_uses_msys_cwd(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(_shell.sys, 'platform', 'win32')
    monkeypatch.setattr(_front, 'Path', PureWindowsPath)
    adapter = ClaudeAdapter() if host == 'claude' else CodexAdapter()
    tool_input = (
        {'file_path': 'src/mod.py'}
        if host == 'claude'
        else {'command': '*** Begin Patch\n*** Update File: src/mod.py\n*** End Patch'}
    )
    payload = {
        'hook_event_name': 'PostToolUse',
        'tool_name': 'Edit' if host == 'claude' else 'apply_patch',
        'tool_input': tool_input,
        'cwd': '/c/Users/dev/project',
    }
    parsed = adapter.parse(payload, None)
    assert parsed is not None
    assert [
        PureWindowsPath(path) for path in resolve_edited_paths(parsed.edited_paths, parsed.cwd)
    ] == [PureWindowsPath('C:/Users/dev/project/src/mod.py')]


def test_resolve_edited_paths(tmp_path: Path) -> None:
    absolute = str(tmp_path / 'absolute.py')
    assert resolve_edited_paths(
        ['relative.py', absolute, 'ignore.txt', 'relative.py', absolute], str(tmp_path)
    ) == [str(tmp_path / 'relative.py'), absolute]


def test_resolve_edited_paths_normalizes_msys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_shell.sys, 'platform', 'win32')
    monkeypatch.setattr(_front, 'Path', PureWindowsPath)
    result = resolve_edited_paths(['/c/project/m.py'], 'C:/project')
    assert [PureWindowsPath(path) for path in result] == [PureWindowsPath('C:/project/m.py')]


@pytest.mark.parametrize('failure_fails', [False, True])
def test_render_failure_falls_back_or_uses_last_resort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    failure_fails: bool,
) -> None:
    class BrokenRenderAdapter(_AdapterProbe):
        @override
        def render_failure(self, reason: str) -> HostResponse:
            if failure_fails:
                raise RuntimeError('failure exploded')
            return HostResponse('denied', reason, 2)

    _install_adapter_probe(
        monkeypatch,
        BrokenRenderAdapter(
            HostInput('pre_tool', None, '.', None), RuntimeError('render exploded')
        ),
    )
    assert _invoke() == 2
    assert capsys.readouterr() == (
        ('', "acme: host 'probe' failed to render the failure response\n")
        if failure_fails
        else ('denied', "acme: host 'probe' failed to render the response")
    )


@pytest.mark.parametrize(
    ('error', 'expected'),
    [(ValueError('bad\nmore'), 'ValueError: bad'), (RuntimeError(), 'RuntimeError')],
)
def test_unexpected_error_summary_names_cause(error: Exception, expected: str) -> None:
    assert _front.error_summary(error) == expected


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (FileChangedError(Path('mod.py')), 'changed since it was read; not written'),
        (
            RestoreError(5, 'restore failed\noriginal saved at backup', Path('backup')),
            'restore failed\noriginal saved at backup',
        ),
        (UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid byte'), 'invalid byte'),
        (SyntaxError('bad syntax'), 'bad syntax'),
        (OSError(5, 'unavailable', 'mod.py'), 'unavailable'),
        (OSError('details'), 'details'),
        (ValueError('bad\nmore'), 'ValueError: bad'),
    ],
)
def test_describe_error_preserves_recovery_details(error: Exception, expected: str) -> None:
    assert describe_error(error) == expected


@pytest.mark.parametrize('url', [None, 'https://example.invalid/docs'])
def test_read_payload_guides_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str | None
) -> None:
    class Terminal(io.StringIO):
        @override
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, 'stdin', Terminal())
    _set_documentation_url(monkeypatch, url)
    assert _front.read_payload(_TOOL) is None
    second = (
        f'See hook.md at {url} for configuration.'
        if url
        else 'See the acme hook guide, hook.md, for configuration.'
    )
    assert capsys.readouterr() == (
        '',
        "acme hook: reads the coding agent's hook payload as JSON from stdin\n" + second + '\n',
    )


@pytest.mark.parametrize('error', [ValueError('closed'), OSError('unavailable')])
def test_read_payload_returns_none_when_stdin_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    class FailedInput(io.StringIO):
        @override
        def read(self, size: int | None = -1) -> str:
            del size
            raise error

    monkeypatch.setattr(sys, 'stdin', FailedInput())
    assert _front.read_payload(_TOOL) is None


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_fail_escapes_diagnostics(
    *, allow_on_failure: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    result = fail('bad\nline\x1b[31m', allow_on_failure=allow_on_failure)
    if allow_on_failure:
        assert result == allow()
        assert capsys.readouterr() == ('', 'WARNING: bad\\x0aline\\x1b[31m\n')
    else:
        assert result == Outcome('deny', 'bad\\x0aline\\x1b[31m')
        assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_commit_dispatch_forwards_plan_and_policy(
    adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str], *, allow_on_failure: bool
) -> None:
    calls: list[tuple[str, bool]] = []

    def run_commit(plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        calls.append((plan.cwd, allow_on_failure))
        print('commit note', file=sys.stderr)
        return Outcome('allow', 'commit receipt')

    def run_post_tool(edited_paths: Sequence[str], cwd: str) -> Outcome:
        del edited_paths, cwd
        pytest.fail('wrong engine runner')

    assert (
        _invoke(
            allow_on_failure=allow_on_failure,
            load_engine=lambda: Engine(run_commit, run_post_tool),
        )
        == 0
    )
    assert calls == [(str(Path.cwd()), allow_on_failure)]
    assert adapter.results == [Outcome('allow', 'commit receipt', ('commit note',))]
    assert capsys.readouterr() == ('commit receipt', 'commit note\n')


@pytest.mark.parametrize('command', [None, 'git diff --check', 'git commit --dry-run'])
def test_noncommitting_events_do_not_load_engine(
    command: str | None, adapter: 'RecordingAdapter', capsys: pytest.CaptureFixture[str]
) -> None:
    adapter.parsed = HostInput('pre_tool', command, '', str(Path.cwd()))
    assert _invoke() == 0
    assert adapter.results == [allow()]
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_unsupported_commit_denies_before_engine_loading(
    adapter: 'RecordingAdapter', *, allow_on_failure: bool
) -> None:
    adapter.parsed = HostInput('pre_tool', 'git commit --future-option -m x', '', str(Path.cwd()))
    assert _invoke(allow_on_failure=allow_on_failure) == 0
    assert adapter.results[0].decision == 'deny'
    assert adapter.results[0].reason is not None
    assert adapter.results[0].reason.startswith(_TOOL.reason_prefix)
