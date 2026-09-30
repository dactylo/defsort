import tomllib
from pathlib import Path
from typing import cast

import pytest

from defsort._hosts import load_adapter

pytestmark = pytest.mark.usefixtures('registered_hosts')

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / 'plugins'
PLUGIN_ROOTS = sorted(path for path in PLUGINS.iterdir() if path.is_dir())


def _adapter_entry(plugin: Path) -> tuple[str, str]:
    with (plugin / 'adapter.toml').open('rb') as stream:
        data = tomllib.load(stream)
    groups = cast('dict[str, object]', data['entry-points'])
    entries = cast('dict[str, str]', groups['defsort.hook_adapters'])
    assert len(entries) == 1, plugin
    return next(iter(entries.items()))


def test_plugin_directories_are_implicit_namespaces_with_owned_runtime() -> None:
    assert not (PLUGINS / '__init__.py').exists()
    for plugin in PLUGIN_ROOTS:
        assert not (plugin / '__init__.py').exists()
        assert (plugin / 'adapter.toml').is_file()
        assert (plugin / '_defsort' / '__init__.py').is_file()
        assert (plugin / '_defsort' / '_adapter.py').is_file()


@pytest.mark.parametrize('plugin', PLUGIN_ROOTS, ids=lambda path: path.name)
def test_plugin_registration_names_its_owned_runtime(plugin: Path) -> None:
    agent, target = _adapter_entry(plugin)
    assert target.startswith(f'plugins.{plugin.name}._defsort._adapter:')
    assert load_adapter(agent).name == agent
