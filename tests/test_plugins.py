import json
import tomllib
from pathlib import Path
from typing import cast

import pytest

from defsort._hook_front import _TOOL
from defsort._vendor.hookfront._loader import load_adapter

pytestmark = pytest.mark.usefixtures('registered_hosts')

_ROOT = Path(__file__).resolve().parents[1]
_PLUGINS = _ROOT / 'plugins'
_MARKETPLACES = (
    _ROOT / '.agents' / 'plugins' / 'marketplace.json',
    _ROOT / '.claude-plugin' / 'marketplace.json',
)
_PLUGIN_ROOTS = sorted(path for path in _PLUGINS.iterdir() if path.is_dir())


def _adapter_entry(plugin: Path) -> tuple[str, str]:
    with (plugin / 'adapter.toml').open('rb') as stream:
        data = tomllib.load(stream)
    groups = cast('dict[str, object]', data['entry-points'])
    entries = cast('dict[str, str]', groups['defsort.hook_adapters'])
    assert len(entries) == 1, plugin
    return next(iter(entries.items()))


def _declared_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for manifest in sorted(_PLUGINS.rglob('plugin.json')):
        data = cast('dict[str, object]', json.loads(manifest.read_text(encoding='utf-8')))
        if 'version' in data:
            versions[manifest.relative_to(_ROOT).as_posix()] = cast('str', data['version'])
    for marketplace in _MARKETPLACES:
        data = cast('dict[str, object]', json.loads(marketplace.read_text(encoding='utf-8')))
        entries = cast('list[dict[str, object]]', data['plugins'])
        for index, entry in enumerate(entries):
            if 'version' in entry:
                key = f'{marketplace.relative_to(_ROOT).as_posix()}:plugins[{index}]'
                versions[key] = cast('str', entry['version'])
    return versions


def test_declared_versions_match_package_version() -> None:
    with (_ROOT / 'pyproject.toml').open('rb') as stream:
        package_version = cast('str', tomllib.load(stream)['project']['version'])
    versions = _declared_versions()
    assert versions
    assert versions == dict.fromkeys(versions, package_version)


def test_plugin_directories_are_implicit_namespaces_with_owned_runtime() -> None:
    assert not (_PLUGINS / '__init__.py').exists()
    for plugin in _PLUGIN_ROOTS:
        assert not (plugin / '__init__.py').exists()
        assert (plugin / 'adapter.toml').is_file()
        assert (plugin / '_defsort' / '__init__.py').is_file()
        assert (plugin / '_defsort' / '_adapter.py').is_file()


@pytest.mark.parametrize('plugin', _PLUGIN_ROOTS, ids=lambda path: path.name)
def test_plugin_registration_names_its_owned_runtime(plugin: Path) -> None:
    agent, target = _adapter_entry(plugin)
    assert target.startswith(f'plugins.{plugin.name}._defsort._adapter:')
    assert load_adapter(_TOOL, agent).name == agent
