"""Discover and load hook host adapters through entry points."""

import sys
from contextlib import suppress
from importlib.machinery import PathFinder
from importlib.metadata import entry_points

from ._contract import API_VERSION, HookAdapter
from ._hints import closest
from ._tool import ToolIdentity


class AdapterLoadError(Exception):
    """Report that no functioning adapter exists for the selected host."""


def _shadowing_module(entry_point_value: str) -> tuple[str, str] | None:
    """Find a regular module that blocks a bundled adapter import."""
    module = entry_point_value.partition(':')[0]
    root, _, remainder = module.partition('.')
    agent, _, _ = remainder.partition('.')
    if root != 'plugins' or not agent:
        return None
    search_path = None
    with suppress(Exception):
        for name in ('plugins', f'plugins.{agent}'):
            loaded = sys.modules.get(name)
            if loaded is not None:
                origin = getattr(loaded, '__file__', None)
                if origin is not None:
                    return name, str(origin)
            spec = PathFinder.find_spec(name, search_path)
            if spec is None:
                if loaded is None:
                    return None
                search_path = getattr(loaded, '__path__', None)
                continue
            if spec.origin is not None:
                return name, spec.origin
            search_path = spec.submodule_search_locations
    return None


def registered_host_names(tool: ToolIdentity) -> list[str]:
    """Return the sorted names of every registered hook adapter.

    Args:
        tool: Tool whose adapter group is searched.
    """
    return sorted({entry.name for entry in entry_points(group=tool.entry_point_group)})


def load_adapter(tool: ToolIdentity, name: str) -> HookAdapter:
    """Load the adapter registered under a host name.

    Args:
        tool: Tool whose adapter group is searched and whose name appears in
            diagnostics.
        name: Host name selected by `--agent`.

    Returns:
        A new adapter instance.

    Raises:
        AdapterLoadError: If the name is unregistered or registered more than
            once, the entry point cannot be imported or instantiated, or the
            adapter's `api_version` differs from `API_VERSION`.
        Exception: If entry-point discovery or reading the adapter's
            `api_version` raises. These failures retain their original types.
    """
    matches = [entry for entry in entry_points(group=tool.entry_point_group) if entry.name == name]
    if not matches:
        hosts = registered_host_names(tool)
        listing = ', '.join(hosts) or 'none'
        match = closest(name, hosts)
        if match is not None:
            raise AdapterLoadError(
                f"unknown host '{name}' (did you mean '{match}'?); registered hosts: {listing}"
            )
        raise AdapterLoadError(f"unknown host '{name}'; registered hosts: {listing}")
    if len(matches) > 1:
        raise AdapterLoadError(f"host '{name}' is registered {len(matches)} times")
    try:
        factory = matches[0].load()
        adapter = factory()
    except Exception as error:
        reason = f"host '{name}' failed to load: {error}"
        shadow = _shadowing_module(matches[0].value)
        if shadow is not None:
            module, origin = shadow
            reason += (
                f"; '{module}' resolves to {origin}, which shadows {tool.name}'s bundled adapters"
            )
        raise AdapterLoadError(reason) from error
    version = getattr(adapter, 'api_version', None)
    if version != API_VERSION:
        raise AdapterLoadError(
            f"host '{name}' implements hook adapter API {version!r}; "
            f'{tool.name} requires {API_VERSION}'
        )
    return adapter
