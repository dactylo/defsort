"""Sort Python source strings by dependency or alphabetically.

Use `sort()`, `check()`, and `detect()` for source-string operations. `sort()`
and `check()` accept an explicit `DefsortConfig`; call `resolve_config()` when
a file's `[tool.defsort]` configuration should apply. Configuration discovery
is cached for the process lifetime, so long-running callers must call
`clear_config_cache()` after relevant `pyproject.toml` changes.
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from defsort._library import (
        BannerMode,
        Blocker,
        ClassBlocker,
        DefsortConfig,
        DefsortConfigWarning,
        DetectResult,
        EvaluationOrder,
        MainPlacement,
        MethodBodies,
        MethodDunders,
        MethodVisibility,
        OrderKind,
        SortCheckResult,
        SortScopeOrder,
        SortStatus,
        TieBreak,
        check,
        clear_config_cache,
        detect,
        resolve_config,
        sort,
    )

__all__ = [
    'BannerMode',
    'Blocker',
    'ClassBlocker',
    'DefsortConfig',
    'DefsortConfigWarning',
    'DetectResult',
    'EvaluationOrder',
    'MainPlacement',
    'MethodBodies',
    'MethodDunders',
    'MethodVisibility',
    'OrderKind',
    'SortCheckResult',
    'SortScopeOrder',
    'SortStatus',
    'TieBreak',
    'check',
    'clear_config_cache',
    'detect',
    'resolve_config',
    'sort',
]
_EXPORTS = frozenset(__all__)


def __dir__() -> list[str]:
    """List package attributes, including exports not loaded yet."""
    return sorted({*globals(), *__all__})


def __getattr__(name: str) -> object:
    """Load an exported library object when first requested.

    Args:
        name: Attribute requested from the package.

    Returns:
        The exported object, cached on the package for later lookups.

    Raises:
        AttributeError: If `name` is not an exported name.
    """
    if name not in _EXPORTS:
        msg = f'module {__name__!r} has no attribute {name!r}'
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f'{__name__}._library'), name)
    globals()[name] = value
    return value
