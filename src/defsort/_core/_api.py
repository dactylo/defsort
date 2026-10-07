"""High-level public API: sort_file, sort_file_incremental, detect_order."""

import ast
from collections.abc import Sequence
from dataclasses import replace

from defsort._core._analysis import (
    MIN_DEFS_FOR_DETECTION,
    aggregate_score,
    compute_ranks,
    has_future_annotations_import,
    kendall_tau,
)
from defsort._core._banners import compile_banner_pattern
from defsort._core._full import (
    count_top_level_methods,
    make_unchanged_result,
    sort_file,
    with_banner_constraints,
)
from defsort._core._future import (
    ensure_future_annotations,
    remove_unused_future_annotations,
)
from defsort._core._incremental import sort_incremental_impl
from defsort._core._layout import LayoutPreservationError
from defsort._core._ordering import order_alphabetical, order_dependency
from defsort._core._parse import parse_definitions, parse_postamble_names
from defsort._core._reassemble import reassemble_source
from defsort._core._safety import (
    has_anchored_main_annotation_hazard,
    has_annotation_order_hazard,
)
from defsort._types import (
    BannerMode,
    Definition,
    DetectResult,
    EngineOptions,
    MethodSortInfo,
    OrderKind,
    SortPolicy,
    SortResult,
    blocker_message,
    is_soft_blocker,
    validate_banner_pattern,
)


def _apply_hoist_and_future(
    source: str,
    tree: ast.Module,
    definitions: Sequence[Definition],
    *,
    options: EngineOptions,
) -> str:
    """Apply hoist and future-import transformations without a full sort."""
    result_source = source
    if options.hoist:
        parsed = parse_definitions(
            source,
            banners=options.policy.banners,
            banner_pattern=options.policy.banner_pattern,
            hoist=True,
            tree=tree,
        )
        hoisted = reassemble_source(parsed.layout, parsed.blocks, parsed.definitions)
        if hoisted != source:
            result_source = hoisted
    if options.add_future and has_anchored_main_annotation_hazard(
        definitions,
        policy=options.policy,
    ):
        add_tree = tree if result_source is source else ast.parse(result_source)
        if not has_future_annotations_import(result_source, _tree=add_tree):
            return ensure_future_annotations(result_source)
    if options.remove_future:
        remove_tree = tree if result_source is source else ast.parse(result_source)
        if has_future_annotations_import(result_source, _tree=remove_tree):
            result_source = remove_unused_future_annotations(result_source, _tree=remove_tree)
    return result_source


def _classify_order(dep_match: bool, alpha_match: bool) -> OrderKind:
    """Classify exact dependency and alphabetical target matches."""
    if dep_match and alpha_match:
        return 'both'
    if dep_match:
        return 'dependency'
    if alpha_match:
        return 'alphabetical'
    return 'unordered'


def _post_sort_future_management(
    new_source: str,
    original_definitions: Sequence[Definition],
    candidate_definitions: Sequence[Definition],
    method_sorts: Sequence[MethodSortInfo],
    options: EngineOptions,
) -> str:
    """Apply future-import transforms to accepted incremental output."""
    if not options.add_future and not options.remove_future:
        return new_source

    new_tree = ast.parse(new_source)
    parsed = parse_definitions(new_source, tree=new_tree)
    new_postamble_names = parse_postamble_names(parsed.postamble)
    has_future = has_future_annotations_import(new_source, _tree=new_tree)
    annotation_hazard = has_annotation_order_hazard(
        original_definitions,
        candidate_definitions,
        method_sorts,
        new_postamble_names,
    ) or has_anchored_main_annotation_hazard(parsed.definitions, policy=options.policy)

    if options.add_future and annotation_hazard and not has_future:
        return ensure_future_annotations(new_source)
    if options.remove_future and has_future:
        return remove_unused_future_annotations(new_source, _tree=new_tree)
    return new_source


def _target_ids(definitions: list[Definition]) -> tuple[list[int], list[int], list[int]]:
    """Return actual, dependency, and alphabetical definition IDs."""
    actual_ids = [definition.index for definition in definitions]
    policy = SortPolicy()
    dep_ids = [definition.index for definition in order_dependency(definitions, policy=policy)]
    alpha_ids = [definition.index for definition in order_alphabetical(definitions, policy=policy)]
    return actual_ids, dep_ids, alpha_ids


def _detect_global(definitions: list[Definition]) -> tuple[OrderKind, float, float]:
    """Detect ordering and scores across the full definition sequence."""
    actual_ids, dep_ids, alpha_ids = _target_ids(definitions)
    return (
        _classify_order(actual_ids == dep_ids, actual_ids == alpha_ids),
        kendall_tau(compute_ranks(actual_ids, dep_ids)),
        kendall_tau(compute_ranks(actual_ids, alpha_ids)),
    )


def _detect_sections(
    definitions: list[Definition],
) -> tuple[OrderKind, float, float] | None:
    """Detect ordering from comparable pairs inside each section."""
    sections: dict[int, list[Definition]] = {}
    for definition in definitions:
        sections.setdefault(definition.section, []).append(definition)
    comparable_sections = [
        section for section in sections.values() if len(section) >= MIN_DEFS_FOR_DETECTION
    ]
    if not comparable_sections:
        return None

    dep_matches: list[bool] = []
    alpha_matches: list[bool] = []
    dep_ranks: list[list[int]] = []
    alpha_ranks: list[list[int]] = []
    for section in comparable_sections:
        actual_ids, dep_ids, alpha_ids = _target_ids(section)
        dep_matches.append(actual_ids == dep_ids)
        alpha_matches.append(actual_ids == alpha_ids)
        dep_ranks.append(compute_ranks(actual_ids, dep_ids))
        alpha_ranks.append(compute_ranks(actual_ids, alpha_ids))

    dep_score = aggregate_score(dep_ranks)
    alpha_score = aggregate_score(alpha_ranks)
    if dep_score is None or alpha_score is None:
        return None
    return _classify_order(all(dep_matches), all(alpha_matches)), dep_score, alpha_score


def detect_definition_order(
    definitions: list[Definition],
    *,
    banners: BannerMode,
) -> DetectResult | None:
    """Detect ordering from already parsed definitions.

    Args:
        definitions: Parsed top-level definitions in source order.
        banners: Treatment of recognized section banners.

    Returns:
        Detection result, or `None` when there are too few comparable
        definitions. Barrier mode compares within sections and weights each
        section by its number of comparable pairs.
    """
    if len(definitions) < MIN_DEFS_FOR_DETECTION:
        return None
    result = (
        _detect_sections(definitions) if banners == 'barriers' else _detect_global(definitions)
    )
    if result is None:
        return None
    order, dep_score, alpha_score = result
    return DetectResult(order=order, dep_score=dep_score, alpha_score=alpha_score)


def detect_order(
    source: str,
    *,
    banners: BannerMode = 'barriers',
    banner_pattern: str | None = None,
    _tree: ast.Module | None = None,
) -> DetectResult | None:
    """Detect what ordering a file's definitions follow.

    Args:
        source: Python source code.
        banners: Treatment of recognized section banners.
        banner_pattern: Optional custom banner regular expression from the
            supported bounded subset.
        _tree: Optional pre-parsed AST that must correspond to `source`.

    Returns:
        Detection result, or `None` when no comparable section contains at
        least two definitions or source parsing raises `SyntaxError`.

    Raises:
        TypeError: If `banner_pattern` is neither a string nor `None`.
        ValueError: If `banner_pattern` does not compile or uses unsupported
            syntax. Compilation failures chain the underlying `re.error`.
    """
    compile_banner_pattern(banner_pattern)
    parsed = parse_definitions(
        source,
        banners=banners,
        banner_pattern=banner_pattern,
        tree=_tree,
    )
    return detect_definition_order(parsed.definitions, banners=banners)


def sort_file_incremental(
    source: str,
    baseline: str,
    *,
    options: EngineOptions,
    _tree: ast.Module | None = None,
) -> SortResult:
    """Incrementally sort source with full sorting and display metadata.

    Handles hoisting and future-annotations management according to `options`.
    An empty or whitespace-only baseline selects full-file sorting.

    Args:
        source: The Python source code.
        baseline: The baseline source, such as content from Git HEAD.
        options: Bundled sort options controlling ordering, safety, hoisting,
            and future-annotations management.
        _tree: Optional pre-parsed AST that must correspond to `source`.

    Returns:
        Sorting metadata and the resulting source. Source parsing or
        compilation failures reported as `SyntaxError` return unchanged source
        with empty definition metadata. Hard blockers, including layout
        refusal, preserve the original source; soft incremental blockers may
        coexist with independent method changes.

    Raises:
        TypeError: If the configured banner pattern is neither a string nor
            `None`.
        ValueError: If the configured banner pattern does not compile or uses
            unsupported syntax. Compilation failures chain `re.error`.
    """
    validate_banner_pattern(options.policy.banner_pattern)

    # Guard against invalid Python -- same pattern as sort_file().
    # sort_incremental_impl() calls ast.parse() and raises SyntaxError;
    # the CLI must not crash on invalid input.
    try:
        tree = _tree if _tree is not None else ast.parse(source)
    except SyntaxError:
        return make_unchanged_result(source)

    if not baseline.strip():
        return sort_file(source, options=options, _tree=tree)

    # Call internal impl to get method_sorts and original_defs (public
    # wrapper drops both). Pass the pre-parsed tree to avoid re-parsing.
    # Hoist flag propagates to the internal parse_definitions calls within
    # sort_incremental_impl, which also snapshots original_defs for
    # movement tracking.
    # sort_incremental_impl calls compile() which can raise SyntaxError
    # for constructs that ast.parse accepts (e.g. bare return at module
    # scope). Catch it here to match sort_file_incremental's non-crashing
    # contract.
    try:
        result = sort_incremental_impl(
            source,
            baseline,
            options=options,
            _tree=tree,
        )
    except SyntaxError:
        return make_unchanged_result(source)

    total_methods = count_top_level_methods(tree)

    if result.new_source is None:
        # Convert the blocker value to its human-readable message for
        # SortResult.warning (matching sort_file()'s convention).
        result_source = source
        if result.blocker is None and not result.class_blockers:
            # No new definitions and no blocker. Hoisting and future-import
            # removal still apply independently of incremental sorting.
            # IMPORTANT: Do NOT delegate to sort_file() here -- the user
            # may have modified function bodies (adding new dependency
            # edges) without adding new definitions, which would cause
            # sort_file() to reorder existing definitions. Apply hoist
            # and remove_future directly without a full sort.
            try:
                result_source = _apply_hoist_and_future(
                    source,
                    tree,
                    result.original_defs,
                    options=options,
                )
            except LayoutPreservationError:
                result = replace(result, blocker='layout')
        warning = blocker_message(result.blocker) if result.blocker else None
        if warning is None and result.class_blockers:
            warning = result.class_blockers[0].message
        unchanged_result = SortResult(
            original_source=source,
            new_source=result_source,
            original_order=result.original_defs,
            new_order=result.original_defs,
            warning=warning,
            blocking_code=[],
            blocker=result.blocker,
            skipped=result.blocker is not None or bool(result.class_blockers),
            total_methods=total_methods,
            class_blockers=result.class_blockers,
        )
        return with_banner_constraints(unchanged_result, result.banner_constraints)

    # A hard blocker on the success path means one scope changed while another
    # was blocked. Discard those changes and skip the whole file.
    if result.blocker is not None and not is_soft_blocker(result.blocker):
        skipped_result = SortResult(
            original_source=source,
            new_source=source,
            original_order=result.original_defs,
            new_order=result.original_defs,
            warning=blocker_message(result.blocker),
            blocking_code=[],
            blocker=result.blocker,
            skipped=True,
            total_methods=total_methods,
            class_blockers=result.class_blockers,
        )
        return with_banner_constraints(skipped_result, result.banner_constraints)

    # Post-processing: future annotations management (matching sort_file).
    new_source = _post_sort_future_management(
        result.new_source,
        result.original_defs,
        result.sorted_defs or tuple(result.original_defs),
        result.method_sorts,
        options,
    )

    sorted_result = SortResult(
        original_source=source,
        new_source=new_source,
        original_order=result.original_defs,
        new_order=list(result.sorted_defs) if result.sorted_defs else list(result.original_defs),
        blocker=result.blocker,
        warning=blocker_message(result.blocker) if result.blocker else None,
        blocking_code=[],
        method_sorts=result.method_sorts,
        total_methods=total_methods,
        class_blockers=result.class_blockers,
    )
    return with_banner_constraints(sorted_result, result.banner_constraints)
