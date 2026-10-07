"""Library API implementation behind the package facade."""

import ast
import warnings

from defsort._config import (
    DefsortConfigWarning,
    clear_config_cache,
    resolve_config,
)
from defsort._config import (
    ineffective_policy_values as _ineffective_policy_values,
)
from defsort._core import detect_order, sort_file
from defsort._core._incremental import sort_incremental_impl
from defsort._types import (
    BANNER_MODE_VALUES,
    BannerMode,
    Blocker,
    ClassBlocker,
    DefsortConfig,
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
    is_soft_blocker,
    sort_policy_from_config,
    validate_banner_pattern,
    validate_config,
    validate_vocabulary,
)
from defsort._types import (
    EngineOptions as _EngineOptions,
)
from defsort._types import (
    blocker_message as _blocker_message,
)


def _apply_hard_blocker(new_source: str | None, blocker: Blocker | None) -> str | None:
    """Suppress new_source when a hard blocker is present."""
    if new_source is not None and blocker is not None and not is_soft_blocker(blocker):
        return None
    return new_source


def _engine_options(config: DefsortConfig) -> _EngineOptions:
    """Convert public configuration to concrete non-transforming engine settings."""
    for key in _ineffective_policy_values(config):
        warnings.warn(
            f'[tool.defsort] {key} has no effect under the resolved strategies',
            DefsortConfigWarning,
            stacklevel=3,
        )
    return _EngineOptions(
        policy=sort_policy_from_config(config),
        force=config.force,
        hoist=False,
        add_future=False,
        remove_future=False,
    )


def _check_incremental(
    source: str,
    baseline: str,
    *,
    options: DefsortConfig,
) -> SortCheckResult:
    """Check whether incremental sorting would change the source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return SortCheckResult(status='invalid')

    # sort_incremental_impl -> compile() can raise
    # SyntaxError for constructs ast.parse accepts (bare return/break).
    try:
        result = sort_incremental_impl(
            source,
            baseline,
            options=_engine_options(options),
            _tree=tree,
        )
    except SyntaxError:
        return SortCheckResult(status='invalid')
    new_source = _apply_hard_blocker(result.new_source, result.blocker)
    banner_constrained = bool(result.banner_constraints)
    if new_source is not None:
        return SortCheckResult(
            status='changed',
            banner_constrained=banner_constrained,
            class_blockers=result.class_blockers,
        )
    if result.blocker is None:
        return SortCheckResult(
            status='skipped' if result.class_blockers else 'unchanged',
            banner_constrained=banner_constrained,
            class_blockers=result.class_blockers,
        )
    return SortCheckResult(
        status='skipped',
        blocker=result.blocker,
        blocker_message=_blocker_message(result.blocker),
        banner_constrained=banner_constrained,
        class_blockers=result.class_blockers,
    )


def _normalize_baseline(baseline: str | None) -> str | None:
    """Return a non-empty incremental baseline, if present."""
    if baseline is None or not baseline.strip():
        return None
    return baseline


def check(
    source: str,
    *,
    baseline: str | None = None,
    config: DefsortConfig | None = None,
) -> SortCheckResult:
    """Return the sort status of source without modifying it.

    Args:
        source: Python source code to analyze.
        baseline: When non-empty, only check new definitions added since
            baseline (incremental mode). Empty or whitespace-only baselines use
            full sorting.
        config: Transformation configuration. Defaults to `DefsortConfig()`.

    Returns:
        SortCheckResult with:
            'invalid' if source is not valid Python (SyntaxError or ValueError,
                including null bytes and lone Unicode surrogates).
            'skipped' when sorting is blocked. A file-level skip carries its
                classification in `blocker` and display text in
                `blocker_message`. A class-only skip can instead report
                `class_blockers` with both file-level fields `None`. A `layout`
                blocker refuses the whole file even without an order change.
            'changed' if sort() would change the source.
            'unchanged' if source is already in the correct order.

    Raises:
        TypeError: If `config.force` is not a `bool`, or
            `config.banner_pattern` is neither a string nor `None`.
        ValueError: If a `config` vocabulary field holds an unknown value, or
            `config.banner_pattern` does not compile or uses unsupported
            syntax.
            Compilation failures chain the `re.error`.
    """
    policy = config if config is not None else DefsortConfig()
    validate_config(policy)
    incremental_baseline = _normalize_baseline(baseline)
    if incremental_baseline is not None:
        return _check_incremental(source, incremental_baseline, options=policy)
    try:
        tree = ast.parse(source)
        compile(tree, '<defsort.check>', 'exec')
    except (SyntaxError, ValueError):
        return SortCheckResult(status='invalid')
    result = sort_file(
        source,
        options=_engine_options(policy),
        _tree=tree,
    )
    banner_constrained = bool(result.banner_constraints)
    if result.skipped:
        old_ids = [d.index for d in result.original_order]
        new_ids = [d.index for d in result.new_order]
        if (
            result.blocker != 'layout'
            and old_ids == new_ids
            and not result.method_sorts
            and not result.class_blockers
        ):
            return SortCheckResult(
                status='unchanged',
                banner_constrained=banner_constrained,
            )
        message = _blocker_message(result.blocker) if result.blocker is not None else None
        return SortCheckResult(
            status='skipped',
            blocker=result.blocker,
            blocker_message=message,
            banner_constrained=banner_constrained,
            class_blockers=result.class_blockers,
        )
    if result.new_source != source:
        return SortCheckResult(
            status='changed',
            banner_constrained=banner_constrained,
            class_blockers=result.class_blockers,
        )
    return SortCheckResult(
        status='unchanged',
        banner_constrained=banner_constrained,
        class_blockers=result.class_blockers,
    )


def detect(
    source: str,
    *,
    banners: BannerMode = 'barriers',
    banner_pattern: str | None = None,
) -> DetectResult | None:
    """Detect what ordering a file's definitions follow.

    Args:
        source: Python source code to analyze.
        banners: Treatment of recognized section banners.
        banner_pattern: Optional custom banner regular expression from the
            supported bounded subset.

    Returns:
        DetectResult with classification and scores, or None when no comparable
        section contains at least two definitions.

    Raises:
        TypeError: If `banner_pattern` is neither a string nor `None`.
        ValueError: If `banners` is outside the `BannerMode` vocabulary, or
            `banner_pattern` does not compile or uses unsupported syntax, or
            source contains lone Unicode surrogates (possibly raised as
            `UnicodeEncodeError`). Compilation failures chain the `re.error`.
        SyntaxError: If source is syntactically invalid.
    """
    validate_vocabulary('banners', banners, BANNER_MODE_VALUES)
    validate_banner_pattern(banner_pattern)
    tree = ast.parse(source)  # raises SyntaxError for invalid Python
    compile(tree, '<defsort.detect>', 'exec')
    return detect_order(
        source,
        banners=banners,
        banner_pattern=banner_pattern,
        _tree=tree,
    )


def sort(
    source: str,
    *,
    baseline: str | None = None,
    config: DefsortConfig | None = None,
) -> str:
    """Return source with definitions sorted by dependency or alphabetically.

    Args:
        source: Python source code to sort.
        baseline: When non-empty, only sort new definitions added since
            baseline (incremental mode). Empty or whitespace-only baselines use
            full sorting.
        config: Transformation configuration. Defaults to `DefsortConfig()`.

    Returns:
        Sorted source code. Hard file blockers preserve the exact input. Soft
        top-level incremental blockers may coexist with independent method
        changes. Does not add or remove future-annotations imports.

    Raises:
        TypeError: If `config.force` is not a `bool`, or
            `config.banner_pattern` is neither a string nor `None`.
        SyntaxError: If source is not valid Python, including null bytes.
        ValueError: If a `config` vocabulary field holds an unknown value,
            `config.banner_pattern` does not compile or uses unsupported
            syntax, or source contains lone Unicode surrogates. Compilation
            failures chain the `re.error`.
    """
    policy = config if config is not None else DefsortConfig()
    validate_config(policy)
    engine_options = _engine_options(policy)
    incremental_baseline = _normalize_baseline(baseline)
    if incremental_baseline is not None:
        tree = ast.parse(source)
        result = sort_incremental_impl(
            source,
            incremental_baseline,
            options=engine_options,
            _tree=tree,
        )
        new_source = _apply_hard_blocker(result.new_source, result.blocker)
        return new_source if new_source is not None else source
    tree = ast.parse(source)
    compile(tree, '<defsort.sort>', 'exec')
    result = sort_file(
        source,
        options=engine_options,
        _tree=tree,
    )
    return result.new_source


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
