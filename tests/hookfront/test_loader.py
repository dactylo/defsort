import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from defsort._vendor.hookfront import (
    _loader,
)
from defsort._vendor.hookfront._contract import (
    API_VERSION,
)
from defsort._vendor.hookfront._hints import (
    closest,
)
from defsort._vendor.hookfront._loader import (
    AdapterLoadError,
    _shadowing_module,
    load_adapter,
    registered_host_names,
)
from defsort._vendor.hookfront._tool import (
    ToolIdentity,
)

_TOOL = ToolIdentity('acme', 'sort')


class Adapter:
    name = 'claude'
    api_version = API_VERSION


class OldAdapter:
    api_version = API_VERSION + 1


def _entry(name: str, adapter: type[object]) -> EntryPoint:
    value = f'{adapter.__module__}:{adapter.__name__}'
    return EntryPoint(name, value, _TOOL.entry_point_group)


@contextmanager
def _isolated_plugins(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    names = [name for name in sys.modules if name == 'plugins' or name.startswith('plugins.')]
    with monkeypatch.context() as patch:
        for name in names:
            patch.delitem(sys.modules, name)
        try:
            yield
        finally:
            for name in tuple(sys.modules):
                if (name == 'plugins' or name.startswith('plugins.')) and name not in names:
                    sys.modules.pop(name, None)


def _missing_entry() -> EntryPoint:
    module = Adapter.__module__.rpartition('.')[0]
    adapter = type('_MissingAdapter', (), {'__module__': f'{module}.missing'})
    return _entry('missing', adapter)


def _register(monkeypatch: pytest.MonkeyPatch, *entries: EntryPoint) -> None:
    def entries_for_group(*, group: str) -> tuple[EntryPoint, ...]:
        assert group == _TOOL.entry_point_group
        return entries

    monkeypatch.setattr(_loader, 'entry_points', entries_for_group)


def _shadow_entry() -> EntryPoint:
    adapter = type('_MissingAdapter', (), {'__module__': 'plugins.claude._acme._adapter'})
    return _entry('claude', adapter)


def test_closest() -> None:
    assert closest('claud', ['agy', 'claude', 'codex']) == 'claude'
    assert closest('nope', ['agy', 'claude', 'codex']) is None
    assert closest('x', []) is None


def test_duplicate_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, _entry('claude', Adapter), _entry('claude', Adapter))
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'claude')
    assert str(error.value) == "host 'claude' is registered 2 times"


def test_registered_adapter_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, _entry('claude', Adapter))
    adapter = load_adapter(_TOOL, 'claude')
    assert isinstance(adapter, Adapter)


@pytest.mark.parametrize('shadow_path', ['plugins.py', 'plugins/__init__.py', 'plugins/claude.py'])
def test_shadowing_modules_are_named(
    shadow_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shadow = tmp_path / shadow_path
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.touch()
    monkeypatch.setattr(sys, 'path', [str(tmp_path), *sys.path])
    _register(monkeypatch, _shadow_entry())
    original_plugins = {
        name: module
        for name, module in sys.modules.items()
        if name == 'plugins' or name.startswith('plugins.')
    }
    with _isolated_plugins(monkeypatch), pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'claude')
    assert {
        name: module
        for name, module in sys.modules.items()
        if name == 'plugins' or name.startswith('plugins.')
    } == original_plugins
    module = 'plugins.claude' if shadow_path == 'plugins/claude.py' else 'plugins'
    assert f"'{module}' resolves to {shadow}" in str(error.value)
    assert str(error.value).endswith("which shadows acme's bundled adapters")


def test_shadowing_probe_finds_an_unimported_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shadow = tmp_path / 'plugins.py'
    shadow.touch()
    monkeypatch.setattr(sys, 'path', [str(tmp_path), *sys.path])
    with _isolated_plugins(monkeypatch):
        assert _shadowing_module(_shadow_entry().value) == ('plugins', str(shadow))
        assert 'plugins' not in sys.modules


def test_third_party_load_error_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = tmp_path / 'plugins.py'
    shadow.touch()
    monkeypatch.setattr(sys, 'path', [str(tmp_path), *sys.path])
    missing_module = f'{Adapter.__module__.rpartition(".")[0]}.missing'
    _register(monkeypatch, _missing_entry())
    with _isolated_plugins(monkeypatch), pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'missing')
    assert str(error.value) == f"host 'missing' failed to load: No module named '{missing_module}'"


def test_unimportable_adapter_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, _missing_entry())
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'missing')
    assert str(error.value).startswith("host 'missing' failed to load: No module named ")


def test_unknown_host_lists_registry_without_a_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch, *(_entry(name, Adapter) for name in ('agy', 'claude', 'codex')))
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'nope')
    assert str(error.value) == "unknown host 'nope'; registered hosts: agy, claude, codex"

    _register(monkeypatch)
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'claude')
    assert str(error.value) == "unknown host 'claude'; registered hosts: none"


def test_unknown_host_names_the_registered_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, *(_entry(name, Adapter) for name in ('codex', 'agy', 'claude')))
    assert registered_host_names(_TOOL) == ['agy', 'claude', 'codex']


def test_unknown_host_suggests_a_close_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, *(_entry(name, Adapter) for name in ('agy', 'claude', 'codex')))
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'claud')
    assert str(error.value) == (
        "unknown host 'claud' (did you mean 'claude'?); registered hosts: agy, claude, codex"
    )


def test_version_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, _entry('old', OldAdapter))
    with pytest.raises(AdapterLoadError) as error:
        load_adapter(_TOOL, 'old')
    assert str(error.value) == "host 'old' implements hook adapter API 2; acme requires 1"
