"""Configuration reading for defsort."""

import contextlib
import functools
import os
import sys
import tomllib
import warnings
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from defsort._types import (
    BANNER_MODE_VALUES,
    COLOR_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    FALLBACK_MODE_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
    BannerMode,
    ColorMode,
    DefsortConfig,
    EvaluationOrder,
    FallbackMode,
    MainPlacement,
    MethodBodies,
    MethodDunders,
    MethodVisibility,
    SortOverrides,
    SortScopeOrder,
    TieBreak,
    validate_banner_pattern,
)
from defsort._vendor.hookfront._terminal import escape_terminal_text, path_text

_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        'allow-on-failure',
        'banner-pattern',
        'banners',
        'color',
        'definition-order',
        'evaluation-order',
        'incremental',
        'incremental-fallback',
        'main',
        'max-reorder-count',
        'max-reorder-percent',
        'method-bodies',
        'method-dunders',
        'method-order',
        'method-visibility',
        'order',
        'show-skips',
        'tie-break',
    }
)
_MAX_PERCENT = 100
_RENDER_CONFIG_WARNINGS = ContextVar('render_config_warnings', default=False)


class DefsortConfigWarning(UserWarning):
    """Category for defsort configuration diagnostics.

    Emitted for invalid, unreadable, or unknown configuration input that
    defsort resolves by falling back to defaults, and for configured policy
    values that have no effect. Filter or promote it with the standard
    `warnings` machinery.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedConfig:
    """Resolved transformation and runtime configuration.

    Attributes:
        transform: Source-transformation configuration.
        allow_on_failure: Whether hook failures allow the triggering operation.
        color: Requested diagnostic color mode, or `None` for automatic
            behavior.
        incremental: Whether incremental sorting is enabled, or `None` when
            unset.
        incremental_fallback: Behavior after a `non_conforming` or
            `target_incompatible` result. Missing baselines use full sorting;
            `user_reordered` remains warning-only.
        max_reorder_count: Maximum permitted moved definitions, or `None` when
            disabled.
        max_reorder_percent: Maximum permitted moved percentage, or `None` when
            disabled.
        show_skips: Whether compact CLI output includes skipped-file
            diagnostics.
    """

    transform: DefsortConfig = field(default_factory=DefsortConfig)
    allow_on_failure: bool = False
    color: ColorMode | None = None
    incremental: bool | None = None
    incremental_fallback: FallbackMode = 'warn'
    max_reorder_count: int | None = None
    max_reorder_percent: int | None = None
    show_skips: bool = True

    @property
    def guard_enabled(self) -> bool:
        """Return whether at least one reorder guard threshold is configured.

        Returns:
            `True` when a reorder guard threshold is configured.
        """
        return self.max_reorder_count is not None or self.max_reorder_percent is not None


def _nearest_pyproject(directory: Path) -> Path | None:
    """Return the nearest pyproject.toml, stopping at the git root."""
    for candidate_dir in (directory, *directory.parents):
        candidate = candidate_dir / 'pyproject.toml'
        if candidate.is_file():
            return candidate.resolve()
        if (candidate_dir / '.git').exists():
            break
    return None


def _warn_config(message: str) -> None:
    """Emit one configuration diagnostic with the public category."""
    if _RENDER_CONFIG_WARNINGS.get():
        print(f'warning: {escape_terminal_text(message)}', file=sys.stderr)
    else:
        warnings.warn(message, DefsortConfigWarning, stacklevel=2)


def _defsort_section(data: dict[str, Any] | None, path: Path) -> dict[str, object] | None:
    """Return the `[tool.defsort]` table from loaded TOML data, if present."""
    if data is None:
        return None
    tool = data.get('tool')
    if tool is None:
        return None
    if not isinstance(tool, dict):
        _warn_config(f'Invalid [tool] table in {path_text(path)}: not a table, using defaults')
        return None
    section = cast('dict[str, Any]', tool).get('defsort')
    if section is None:
        return None
    if not isinstance(section, dict):
        _warn_config(
            f'Invalid [tool.defsort] section in {path_text(path)}: not a table, using defaults'
        )
        return None
    return cast('dict[str, object]', section)


def _load_toml(path: Path) -> dict[str, Any] | None:
    """Load and return TOML data, warning and returning None on any error."""
    try:
        with path.open('rb') as f:
            return tomllib.load(f)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _warn_config(f'Cannot read configuration from {path_text(path)}: {exc}, using defaults')
        return None


def _parse_boolean(section: dict[str, object], key: str, path: Path, *, default: bool) -> bool:
    """Parse a boolean config key, warning on invalid values."""
    raw = section.get(key)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        _warn_config(
            f'{path_text(path)}: [tool.defsort].{key}: invalid value {raw!r}; '
            f'expected true or false; using {str(default).lower()}'
        )
        return default
    return raw


def _parse_bounded_int(
    section: dict[str, object],
    key: str,
    path: Path,
    *,
    default: int | None,
    max_val: int | None = None,
) -> int | None:
    """Parse an integer config key with range validation."""
    raw = section.get(key)
    if raw is None:
        return default
    if type(raw) is int and raw >= 0 and (max_val is None or raw <= max_val):
        return raw
    label = f'an integer from 0 to {max_val}' if max_val is not None else 'a non-negative integer'
    _warn_config(
        f'{path_text(path)}: [tool.defsort].{key}: invalid value {raw!r}; '
        f'expected {label}; using {"no limit" if default is None else default}'
    )
    return default


def _parse_string_literal(
    section: dict[str, object],
    key: str,
    allowed: tuple[str, ...],
    default: str,
    path: Path,
) -> str | None:
    """Parse a string-literal config key, warning on invalid values."""
    raw = section.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in allowed:
        _warn_config(
            f'{path_text(path)}: [tool.defsort].{key}: invalid value {raw!r}; '
            f'expected {", ".join(allowed)}; using {default}'
        )
        return None
    return raw


def _parse_vocabulary[T: str](
    section: dict[str, object],
    key: str,
    allowed: tuple[T, ...],
    default: T,
    path: Path,
) -> T:
    """Parse one closed-vocabulary key, warning and defaulting on bad input."""
    raw = _parse_string_literal(section, key, allowed, default, path)
    for value in allowed:
        if raw == value:
            return value
    return default


def _parse_section(section: dict[str, object], path: Path) -> ResolvedConfig:
    """Parse a [tool.defsort] section dict into ResolvedConfig."""
    defaults = ResolvedConfig()
    transform_defaults = defaults.transform
    for key in sorted(set(section) - _KNOWN_KEYS):
        _warn_config(f'{path_text(path)}: [tool.defsort]: unknown key {key}; ignoring')

    allow_on_failure = _parse_boolean(
        section, 'allow-on-failure', path, default=defaults.allow_on_failure
    )

    banners: BannerMode = _parse_vocabulary(
        section, 'banners', BANNER_MODE_VALUES, transform_defaults.banners, path
    )

    banner_pattern = transform_defaults.banner_pattern
    pattern_raw = section.get('banner-pattern')
    if pattern_raw is not None:
        if not isinstance(pattern_raw, str):
            _warn_config(
                f'{path_text(path)}: [tool.defsort].banner-pattern: '
                f'invalid value {pattern_raw!r}; expected a string; using built-in detection'
            )
        else:
            try:
                validate_banner_pattern(pattern_raw)
            except ValueError as exc:
                _warn_config(
                    f'{path_text(path)}: [tool.defsort].banner-pattern: '
                    f'invalid value {pattern_raw!r}; expected a valid banner pattern ({exc}); '
                    'using built-in detection'
                )
            else:
                banner_pattern = pattern_raw

    order: SortScopeOrder = _parse_vocabulary(
        section, 'order', SORT_SCOPE_ORDER_VALUES, transform_defaults.definition_order, path
    )
    definition_order: SortScopeOrder = _parse_vocabulary(
        section, 'definition-order', SORT_SCOPE_ORDER_VALUES, order, path
    )
    method_order: SortScopeOrder = _parse_vocabulary(
        section, 'method-order', SORT_SCOPE_ORDER_VALUES, order, path
    )
    evaluation_order: EvaluationOrder = _parse_vocabulary(
        section,
        'evaluation-order',
        EVALUATION_ORDER_VALUES,
        transform_defaults.evaluation_order,
        path,
    )
    main: MainPlacement = _parse_vocabulary(
        section, 'main', MAIN_PLACEMENT_VALUES, transform_defaults.main, path
    )
    method_bodies: MethodBodies = _parse_vocabulary(
        section,
        'method-bodies',
        METHOD_BODIES_VALUES,
        transform_defaults.method_bodies,
        path,
    )
    method_dunders: MethodDunders = _parse_vocabulary(
        section,
        'method-dunders',
        METHOD_DUNDERS_VALUES,
        transform_defaults.method_dunders,
        path,
    )
    method_visibility: MethodVisibility = _parse_vocabulary(
        section,
        'method-visibility',
        METHOD_VISIBILITY_VALUES,
        transform_defaults.method_visibility,
        path,
    )
    tie_break: TieBreak = _parse_vocabulary(
        section,
        'tie-break',
        TIE_BREAK_VALUES,
        transform_defaults.tie_break,
        path,
    )

    incremental = section.get('incremental')
    if incremental is not None and not isinstance(incremental, bool):
        _warn_config(
            f'{path_text(path)}: [tool.defsort].incremental: invalid value {incremental!r}; '
            'expected true or false; using full mode'
        )
        incremental = defaults.incremental

    incremental_fallback: FallbackMode = _parse_vocabulary(
        section, 'incremental-fallback', FALLBACK_MODE_VALUES, defaults.incremental_fallback, path
    )

    color_raw = _parse_string_literal(
        section, 'color', COLOR_MODE_VALUES, defaults.color or 'auto', path
    )
    color: ColorMode | None = next(
        (value for value in COLOR_MODE_VALUES if value == color_raw), None
    )

    max_reorder_count = _parse_bounded_int(
        section, 'max-reorder-count', path, default=defaults.max_reorder_count
    )
    max_reorder_percent = _parse_bounded_int(
        section,
        'max-reorder-percent',
        path,
        default=defaults.max_reorder_percent,
        max_val=_MAX_PERCENT,
    )
    show_skips = _parse_boolean(section, 'show-skips', path, default=defaults.show_skips)

    return ResolvedConfig(
        allow_on_failure=allow_on_failure,
        color=color,
        incremental=incremental,
        incremental_fallback=incremental_fallback,
        max_reorder_count=max_reorder_count,
        max_reorder_percent=max_reorder_percent,
        show_skips=show_skips,
        transform=DefsortConfig(
            banners=banners,
            banner_pattern=banner_pattern,
            definition_order=definition_order,
            evaluation_order=evaluation_order,
            main=main,
            method_bodies=method_bodies,
            method_dunders=method_dunders,
            method_order=method_order,
            method_visibility=method_visibility,
            tie_break=tie_break,
        ),
    )


@functools.cache
def _resolve_config_source(candidate: Path) -> tuple[ResolvedConfig, Path | None]:
    """Resolve configuration and provenance from one pyproject.toml."""
    section = _defsort_section(_load_toml(candidate), candidate)
    if section is None:
        return ResolvedConfig(), None
    return _parse_section(section, candidate), candidate


@functools.cache
def _resolve_config_sourced(directory: Path) -> tuple[ResolvedConfig, Path | None]:
    """Resolve configuration plus its source file for policy display."""
    candidate = _nearest_pyproject(directory)
    if candidate is None:
        return ResolvedConfig(), None
    return _resolve_config_source(candidate)


def clear_config_cache() -> None:
    """Clear cached configuration so later reads observe edited files.

    Discovery results are cached per resolved directory, and parsed and
    validated settings are cached per resolved configuration file. Directories
    sharing a file also share its warnings. Both caches last for the process,
    which suits a CLI invocation but not a long-running caller. Call this after
    changing a `pyproject.toml` in the same process. This also drops this
    module's warning registry, so a rereading caller sees the configuration
    diagnostics again instead of having them suppressed as duplicates by the
    default warning filter.
    """
    _resolve_config_source.cache_clear()
    _resolve_config_sourced.cache_clear()
    globals().pop('__warningregistry__', None)


def effective_config(cfg: ResolvedConfig, overrides: SortOverrides) -> ResolvedConfig:
    """Apply CLI overrides on top of a resolved configuration.

    Nullable override fields leave the resolved value unchanged when `None`.
    `-o/--order` sets both scoped orders before scoped overrides apply.
    `clear_banner_pattern` clears the configured pattern; a non-None
    `banner_pattern` then supplies its replacement.

    Args:
        cfg: Resolved configuration for the file being processed.
        overrides: CLI flag overrides.

    Returns:
        The effective configuration.
    """
    transform = cfg.transform
    definition_order = transform.definition_order
    method_order = transform.method_order
    if overrides.order is not None:
        definition_order = overrides.order
        method_order = overrides.order
    if overrides.definition_order is not None:
        definition_order = overrides.definition_order
    if overrides.method_order is not None:
        method_order = overrides.method_order
    banner_pattern = None if overrides.clear_banner_pattern else transform.banner_pattern
    if overrides.banner_pattern is not None:
        banner_pattern = overrides.banner_pattern
    new_transform = replace(
        transform,
        banners=overrides.banners if overrides.banners is not None else transform.banners,
        banner_pattern=banner_pattern,
        definition_order=definition_order,
        evaluation_order=overrides.evaluation_order
        if overrides.evaluation_order is not None
        else transform.evaluation_order,
        main=overrides.main if overrides.main is not None else transform.main,
        method_bodies=overrides.method_bodies
        if overrides.method_bodies is not None
        else transform.method_bodies,
        method_dunders=overrides.method_dunders
        if overrides.method_dunders is not None
        else transform.method_dunders,
        method_order=method_order,
        method_visibility=overrides.method_visibility
        if overrides.method_visibility is not None
        else transform.method_visibility,
        tie_break=overrides.tie_break if overrides.tie_break is not None else transform.tie_break,
    )
    return replace(
        cfg,
        transform=new_transform,
        incremental=overrides.incremental
        if overrides.incremental is not None
        else cfg.incremental,
        incremental_fallback=overrides.incremental_fallback
        if overrides.incremental_fallback is not None
        else cfg.incremental_fallback,
        show_skips=overrides.show_skips if overrides.show_skips is not None else cfg.show_skips,
    )


def ineffective_policy_message(key: str) -> str:
    """Describe one configured policy value that cannot affect the sort."""
    return f'[tool.defsort] {key} has no effect under the resolved strategies; ignoring'


def ineffective_policy_values(policy: DefsortConfig) -> list[str]:
    """Return nondefault policy keys that have no effect.

    Args:
        policy: The effective transformation configuration.

    Returns:
        The ineffective policy key names.
    """
    defaults = DefsortConfig()
    checks = (
        (
            'method-bodies',
            policy.method_bodies != defaults.method_bodies,
            policy.method_order != 'dependency',
        ),
        (
            'tie-break',
            policy.tie_break != defaults.tie_break,
            policy.definition_order != 'dependency' and policy.method_order != 'dependency',
        ),
        (
            'method-visibility',
            policy.method_visibility != defaults.method_visibility,
            policy.method_order == 'none',
        ),
        (
            'method-dunders',
            policy.method_dunders != defaults.method_dunders,
            policy.method_order == 'none',
        ),
        (
            'main',
            policy.main != defaults.main,
            policy.definition_order == 'none',
        ),
    )
    return [key for key, non_default, ineffective in checks if non_default and ineffective]


@contextlib.contextmanager
def render_config_warnings() -> Generator[None]:
    """Render configuration warnings to stderr in the CLI diagnostic format.

    Yields:
        None.
    """
    token = _RENDER_CONFIG_WARNINGS.set(True)
    try:
        yield
    finally:
        _RENDER_CONFIG_WARNINGS.reset(token)


def resolve_config_at_root(git_root: str) -> ResolvedConfig:
    """Load config from pyproject.toml at git root (hook run-level path).

    Args:
        git_root: Absolute path to git repository root.

    Returns:
        Configuration from that pyproject.toml, or defaults.
    """
    candidate = (Path(git_root) / 'pyproject.toml').resolve()
    if candidate.is_file():
        return _resolve_config_source(candidate)[0]
    return ResolvedConfig()


def resolve_config_sourced(file_path: Path) -> tuple[ResolvedConfig, Path | None]:
    """Load config and provenance from the nearest pyproject.toml.

    Args:
        file_path: File or directory whose effective policy should be resolved.

    Returns:
        Configuration plus its source path, or `None` when built-in defaults
        apply.
    """
    start = file_path if file_path.is_dir() else file_path.parent
    return _resolve_config_sourced(start.resolve())


def resolve_runtime_config(path: Path) -> ResolvedConfig:
    """Resolve internal runtime configuration for one path.

    Args:
        path: File or directory whose effective configuration is wanted.

    Returns:
        The cached resolved configuration for the discovery start directory.
    """
    start = path if path.is_dir() else path.parent
    config, _source = _resolve_config_sourced(start.resolve())
    return config


def resolve_config(path: str | os.PathLike[str]) -> DefsortConfig:
    """Load `[tool.defsort]` transformation configuration for `path`.

    Discovery starts at `path` when it is an existing directory and at `path`'s
    parent otherwise, including when `path` names a file that does not exist
    yet, then searches upward for the nearest `pyproject.toml`, stopping
    inclusively at the git repository root. Discovery results are cached per
    resolved directory, while parsed and validated settings are cached per
    resolved configuration file. Directories sharing a file also share its
    warnings. Call `clear_config_cache()` after editing a `pyproject.toml` in
    the same process.

    Invalid, unreadable, or unknown configuration emits `DefsortConfigWarning`
    and uses the applicable defaults. Standard warning filters can promote
    these diagnostics to exceptions.

    Args:
        path: File or directory path, as a string or path-like object.

    Returns:
        The transformation configuration from the nearest `pyproject.toml`, or
        defaults.
    """
    return resolve_runtime_config(Path(path)).transform


def warn_ineffective_policy(cfg: DefsortConfig) -> None:
    """Warn once per policy value that differs from its default but has no effect.

    Args:
        cfg: The effective configuration after override merging.
    """
    for key in ineffective_policy_values(cfg):
        print(
            f'warning: {escape_terminal_text(ineffective_policy_message(key))}',
            file=sys.stderr,
        )
