"""Incremental sort logic: merge new definitions/methods into existing order."""

import ast
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from defsort._core._analysis import (
    get_node_end_line,
    has_future_annotations_import,
)
from defsort._core._banners import (
    compile_banner_pattern,
    order_within_sections,
)
from defsort._core._full import sort_file
from defsort._core._future import remove_unused_future_annotations
from defsort._core._layout import LayoutPreservationError, SourceLayout
from defsort._core._merge import find_new_items, incremental_merge
from defsort._core._methods import (
    MethodParseMapping,
    apply_class_replacements,
    collect_method_banner_constraints,
    collect_method_coordinates,
    match_classes,
    parse_class_methods,
    per_class_incremental_sort,
)
from defsort._core._ordering import (
    order_alphabetical,
    order_dependency,
    top_level_banner_constraints,
)
from defsort._core._parse import (
    parse_definitions,
    parse_postamble_names,
    postamble_has_dynamic_all,
)
from defsort._core._reassemble import reassemble_source
from defsort._core._safety import (
    check_reorder_hazards,
    has_interstitial_code,
    has_nonconsecutive_duplicates,
    has_sectioned_duplicates,
)
from defsort._core._syntax import NESTING_LIMIT_MESSAGE, parse_source
from defsort._source import split_physical_lines
from defsort._types import (
    BannerConstraint,
    Blocker,
    BoundaryLayoutPart,
    ClassBlocker,
    ClassSlice,
    Definition,
    DefinitionKind,
    EngineOptions,
    MethodSortInfo,
    SectionBoundary,
    SortPolicy,
    blocker_message,
    is_soft_blocker,
)


@dataclass(frozen=True, slots=True)
class _HookSortResult:
    """Result of a hook sort with exposed definition metadata."""

    new_source: str | None
    blocker: Blocker | None
    sorted_defs: tuple[Definition, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()
    class_blockers: tuple[ClassBlocker, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _IncrementalMethodResult:
    """Result of incremental method sorting across classes."""

    new_source: str | None
    method_sorts: list[MethodSortInfo]
    banner_constraints: list[BannerConstraint]
    class_blockers: list[ClassBlocker]


@dataclass(frozen=True, slots=True)
class _IncrementalSortResult:
    """Internal incremental sort result with CLI metadata."""

    new_source: str | None
    blocker: Blocker | None
    method_sorts: list[MethodSortInfo]
    original_defs: list[Definition]
    sorted_defs: tuple[Definition, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()
    class_blockers: tuple[ClassBlocker, ...] = ()
    safety_gate: Blocker | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _TopLevelMergeResult:
    """Result of the top-level incremental merge pipeline."""

    new_source: str
    blocker: Blocker | None
    sorted_defs: tuple[Definition, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()


def _def_index(d: Definition) -> int:
    """Return a definition's stable source index."""
    return d.index


def _def_is_overload(d: Definition) -> bool:
    """Return whether a definition is an overload declaration."""
    return d.is_overload


def _def_key(d: Definition) -> tuple[str, DefinitionKind]:
    """Return the name and kind that identify a definition group."""
    return (d.name, d.kind)


def _incremental_method_sort(
    source: str,
    baseline: str,
    *,
    policy: SortPolicy,
    baseline_tree: ast.Module,
    tree_or_original_coordinates: ast.Module | MethodParseMapping,
) -> _IncrementalMethodResult:
    """Incrementally sort methods within classes."""
    if policy.method_order == 'none' and policy.banners != 'strip':
        return _IncrementalMethodResult(
            new_source=None,
            method_sorts=[],
            banner_constraints=[],
            class_blockers=[],
        )
    if isinstance(tree_or_original_coordinates, ast.Module):
        cur_tree = tree_or_original_coordinates
        original_coordinates = None
    else:
        cur_tree = parse_source(source)
        original_coordinates = tree_or_original_coordinates

    source_lines = split_physical_lines(source, keepends=True)
    baseline_lines = split_physical_lines(baseline, keepends=True)

    cur_classes = [n for n in cur_tree.body if isinstance(n, ast.ClassDef)]
    bl_classes = [n for n in baseline_tree.body if isinstance(n, ast.ClassDef)]
    matched = match_classes(cur_classes, bl_classes)

    class_blockers: list[ClassBlocker] = []
    method_sorts: list[MethodSortInfo] = []
    constraints: list[BannerConstraint] = []
    # Track replacements: list of (start_line, end_line, new_class_source)
    replacements: list[tuple[int, int, str]] = []
    current_seen: Counter[str] = Counter()
    collect_bodies = policy.method_order == 'dependency' and policy.method_bodies != 'ignore'
    pattern = compile_banner_pattern(policy.banner_pattern) if policy.banners != 'ignore' else None

    for cur_cls, bl_cls in matched:
        current_seen[cur_cls.name] += 1
        cur_start = cur_cls.lineno
        cur_end = get_node_end_line(cur_cls)
        cur_class_lines = source_lines[cur_start - 1 : cur_end]
        cur_cs = ClassSlice(lines=cur_class_lines, start_line=cur_start)

        # Parse current boundaries for reassembly. Baseline boundaries are
        # parsed independently so conformance uses their original sections.
        cur_parsed = parse_class_methods(
            cur_cls,
            cur_cs,
            banners=policy.banners,
            banner_pattern=pattern,
            collect_bodies=collect_bodies,
        )
        original_parse = (
            cur_parsed
            if original_coordinates is None
            else original_coordinates[(cur_cls.name, current_seen[cur_cls.name])]
        )
        bl_parsed = cur_parsed
        if bl_cls is not None:
            bl_start = bl_cls.lineno
            bl_end = get_node_end_line(bl_cls)
            bl_class_lines = baseline_lines[bl_start - 1 : bl_end]
            bl_cs = ClassSlice(lines=bl_class_lines, start_line=bl_start)
            bl_parsed = parse_class_methods(
                bl_cls,
                bl_cs,
                banners=policy.banners,
                banner_pattern=pattern,
                collect_bodies=collect_bodies,
            )

        result = per_class_incremental_sort(
            cur_cls,
            bl_cls,
            cur_parsed,
            bl_parsed,
            cur_cs,
            policy=policy,
            original_parse=original_parse,
            banner_pattern=pattern,
        )

        if result.blocker is not None:
            class_blockers.append(
                ClassBlocker(
                    class_name=cur_cls.name,
                    occurrence=current_seen[cur_cls.name],
                    blocker=result.blocker,
                    message=blocker_message(result.blocker),
                )
            )
        if result.new_source is not None:
            replacements.append((cur_start, cur_end, result.new_source))
        if result.method_sort is not None:
            method_sorts.append(result.method_sort)
        constraints.extend(result.banner_constraints)

    if original_coordinates is not None:
        # Coordinates were collected in original AST order, before top-level moves.
        original_order = {identity: index for index, identity in enumerate(original_coordinates)}
        class_blockers.sort(key=lambda item: original_order[(item.class_name, item.occurrence)])

    new_source = apply_class_replacements(source_lines, replacements)
    if new_source is None or new_source == source:
        return _IncrementalMethodResult(
            new_source=None,
            method_sorts=[],
            banner_constraints=constraints,
            class_blockers=class_blockers,
        )
    return _IncrementalMethodResult(
        new_source=new_source,
        method_sorts=method_sorts,
        banner_constraints=constraints,
        class_blockers=class_blockers,
    )


def _incremental_safety_blocker(
    current_defs: Sequence[Definition],
    *,
    policy: SortPolicy,
    force: bool,
) -> Blocker | None:
    """Return the top-level safety refusal to apply when a candidate changes."""
    definitions = list(current_defs)
    if not force and has_interstitial_code(definitions):
        return 'interstitial'
    if has_nonconsecutive_duplicates(definitions) or (
        policy.banners == 'barriers' and has_sectioned_duplicates(definitions)
    ):
        return 'duplicates'
    return None


def _order_incremental_definitions(
    items: list[Definition],
    *,
    policy: SortPolicy,
) -> list[Definition]:
    """Order one independently parsed definition list for incremental merge."""
    global_order_fn = partial(
        (order_alphabetical if policy.definition_order == 'alphabetical' else order_dependency),
        policy=policy,
    )
    if policy.banners == 'barriers':
        return order_within_sections(items, global_order_fn)
    return global_order_fn(items)


def _sort_incremental_full(
    source: str,
    tree: ast.Module,
    *,
    options: EngineOptions,
) -> _IncrementalSortResult:
    """Treat an empty baseline as the shared full-file sort path."""
    result = sort_file(source, options=options, _tree=tree)
    new_source = result.new_source if result.new_source != source else None
    if result.blocker is not None:
        new_source = None
    return _IncrementalSortResult(
        new_source=new_source,
        blocker=result.blocker,
        message=result.warning,
        method_sorts=result.method_sorts if result.blocker is None else [],
        original_defs=list(result.original_order),
        sorted_defs=(
            tuple(result.new_order) if new_source is not None and result.blocker is None else ()
        ),
        banner_constraints=tuple(result.banner_constraints),
        class_blockers=result.class_blockers,
        safety_gate=(
            _incremental_safety_blocker(
                result.original_order,
                policy=options.policy,
                force=options.force,
            )
            if options.policy.definition_order != 'none'
            else None
        ),
    )


def _top_level_incremental_merge(  # noqa: PLR0913
    source: str,
    current_defs: list[Definition],
    baseline_defs: list[Definition],
    layout: SourceLayout,
    blocks: Mapping[int, str],
    *,
    boundary_layout: list[BoundaryLayoutPart],
    policy: SortPolicy,
) -> _TopLevelMergeResult:
    """Run the top-level incremental merge pipeline."""
    if policy.definition_order == 'none':
        raise ValueError('definition_order must not be none')
    boundaries = [part for part in boundary_layout if isinstance(part, SectionBoundary)]
    constraints = top_level_banner_constraints(
        current_defs,
        boundaries,
        policy=policy,
    )
    order_fn = partial(
        _order_incremental_definitions,
        policy=policy,
    )
    new_defs = find_new_items(baseline_defs, current_defs, _def_key, _def_is_overload)

    merged, blocker = incremental_merge(
        current_defs,
        baseline_defs,
        new_defs,
        order_fn,
        _def_key,
        is_overload_fn=_def_is_overload,
        index_fn=_def_index,
    )
    if blocker is not None:
        return _TopLevelMergeResult(
            new_source=source,
            blocker=blocker,
            banner_constraints=tuple(constraints),
        )

    new_source = reassemble_source(layout, blocks, merged)
    # Return the original object, not the equal copy: callers reuse a cached
    # parse tree when the returned source is identical by identity.
    if new_source == source:
        return _TopLevelMergeResult(
            new_source=source,
            blocker=None,
            sorted_defs=tuple(merged),
            banner_constraints=tuple(constraints),
        )

    return _TopLevelMergeResult(
        new_source=new_source,
        blocker=None,
        sorted_defs=tuple(merged),
        banner_constraints=tuple(constraints),
    )


def _sort_incremental(
    source: str,
    baseline: str,
    tree: ast.Module,
    *,
    options: EngineOptions,
) -> _IncrementalSortResult:
    """Build an incremental candidate while preserving a whole-file refusal boundary."""
    original_source = source
    policy = options.policy
    try:
        baseline_tree = parse_source(baseline, filename='<defsort.baseline>', check=True)
    except SyntaxError:
        # Invalid baseline -> non-conforming, skip (spec: do not raise,
        # avoid trapping users who need to commit a fix for a syntax error).
        # Still populate original_defs so SortResult metadata is preserved.
        parsed = parse_definitions(
            source,
            banners=policy.banners,
            banner_pattern=policy.banner_pattern,
            tree=tree,
        )
        defs = parsed.definitions
        top_constraints = top_level_banner_constraints(
            defs,
            parsed.boundaries,
            policy=policy,
        )
        method_constraints = collect_method_banner_constraints(
            source,
            tree,
            policy=policy,
        )
        return _IncrementalSortResult(
            new_source=None,
            blocker='non_conforming',
            method_sorts=[],
            original_defs=defs,
            banner_constraints=(*top_constraints, *method_constraints),
        )

    # Parse without method reordering (capture preamble/postamble for reuse).
    # Pass hoist= so safe constants move to preamble (matching sort_file).
    current_parsed = parse_definitions(
        source,
        banners=policy.banners,
        banner_pattern=policy.banner_pattern,
        hoist=options.hoist,
        tree=tree,
    )
    current_defs = current_parsed.definitions
    safety_gate = (
        _incremental_safety_blocker(current_defs, policy=policy, force=options.force)
        if policy.definition_order != 'none'
        else None
    )
    postamble = current_parsed.postamble
    original_defs = list(current_defs)  # snapshot before any merge
    baseline_parsed = parse_definitions(
        baseline,
        banners=policy.banners,
        banner_pattern=policy.banner_pattern,
        tree=baseline_tree,
    )
    baseline_defs = baseline_parsed.definitions

    if not current_defs:
        return _IncrementalSortResult(
            new_source=None,
            blocker=None,
            method_sorts=[],
            original_defs=[],
            banner_constraints=(),
        )

    # --- Top-level scope ---
    # Per-scope: top-level and each class are checked independently.
    # A non-conforming top-level does NOT prevent method-level sorting.
    if policy.definition_order == 'none':
        top_source = reassemble_source(
            current_parsed.layout,
            current_parsed.blocks,
            current_defs,
        )
        # Restore the original object when the text is unchanged: the identity
        # check at the _incremental_method_sort call below reuses `tree` only
        # when `source is original_source`, avoiding a full re-parse.
        if top_source == source:
            top_source = source
        top_result = _TopLevelMergeResult(
            new_source=top_source,
            blocker=None,
            sorted_defs=tuple(current_defs),
            banner_constraints=(),
        )
    else:
        top_result = _top_level_incremental_merge(
            source,
            current_defs,
            baseline_defs,
            current_parsed.layout,
            current_parsed.blocks,
            boundary_layout=current_parsed.boundary_layout,
            policy=policy,
        )
    source = top_result.new_source

    # --- Method-level scope (per-class, independent of top-level) ---
    # Even if top-level was skipped (non-conforming, user reordered, etc.),
    # individual classes with conforming methods can still be sorted.
    tree_or_original_coordinates: ast.Module | MethodParseMapping = tree
    if source is not original_source:
        tree_or_original_coordinates = collect_method_coordinates(
            original_source,
            tree,
            banners=policy.banners,
            banner_pattern=policy.banner_pattern,
            collect_bodies=policy.method_order == 'dependency'
            and policy.method_bodies != 'ignore',
        )
    method_result = _incremental_method_sort(
        source,
        baseline,
        policy=policy,
        baseline_tree=baseline_tree,
        tree_or_original_coordinates=tree_or_original_coordinates,
    )
    if method_result.new_source is not None:
        source = method_result.new_source
    constraints = (*top_result.banner_constraints, *method_result.banner_constraints)
    top_candidate_defs = top_result.sorted_defs or tuple(original_defs)
    structural_blocker = top_result.blocker
    if safety_gate is not None and source != original_source:
        return _IncrementalSortResult(
            new_source=None,
            blocker=safety_gate,
            method_sorts=[],
            original_defs=original_defs,
            banner_constraints=constraints,
            class_blockers=tuple(method_result.class_blockers),
            safety_gate=safety_gate,
        )
    semantic_blocker = check_reorder_hazards(
        original_defs,
        top_candidate_defs,
        method_result.method_sorts,
        parse_postamble_names(postamble),
        has_future=has_future_annotations_import(original_source, _tree=tree),
        add_future=options.add_future,
        dynamic_all=postamble_has_dynamic_all(postamble),
        evaluation_order=policy.evaluation_order,
    )
    if semantic_blocker is not None:
        blocker, message = semantic_blocker
        return _IncrementalSortResult(
            new_source=None,
            blocker=structural_blocker or blocker,
            message=blocker_message(structural_blocker) if structural_blocker else message,
            method_sorts=[],
            original_defs=original_defs,
            banner_constraints=constraints,
            class_blockers=tuple(method_result.class_blockers),
            safety_gate=safety_gate,
        )

    if source != original_source:
        sorted_defs = top_candidate_defs if top_result.blocker is None else ()
        return _IncrementalSortResult(
            new_source=source,
            blocker=structural_blocker,
            method_sorts=method_result.method_sorts,
            original_defs=original_defs,
            sorted_defs=sorted_defs,
            banner_constraints=constraints,
            class_blockers=tuple(method_result.class_blockers),
            safety_gate=safety_gate,
        )

    return _IncrementalSortResult(
        new_source=None,
        blocker=structural_blocker,
        method_sorts=[],
        original_defs=original_defs,
        banner_constraints=constraints,
        class_blockers=tuple(method_result.class_blockers),
        safety_gate=safety_gate,
    )


def sort_for_hook_detailed(
    source: str,
    *,
    policy: SortPolicy,
) -> _HookSortResult:
    """Sort source for hook usage, exposing sorted definitions.

    Returns a `_HookSortResult` with the sorted definitions list, so downstream
    consumers (e.g. reorder guard) can inspect what moved without re-parsing.

    Args:
        source: The Python source code.
        policy: Concrete ordering policy.

    Returns:
        `_HookSortResult` with `new_source`, `blocker`, and `sorted_defs`.
        `sorted_defs` is non-empty only when sorting succeeded (`new_source` is
        not `None` and `blocker` is `None`).

    Raises:
        SyntaxError: If source is not valid Python (includes compile-level
            checks like bare return/break at module scope).
        TypeError: If the configured banner pattern is neither a string nor
            `None`.
        ValueError: If source contains a lone Unicode surrogate, or the
            configured banner pattern does not compile or uses unsupported
            syntax. Pattern compilation failures chain `re.error`.
    """
    tree = parse_source(source, filename='<sort_for_hook_detailed>', check=True)
    result = sort_file(
        source,
        options=EngineOptions(
            policy=policy,
            force=False,
            add_future=False,
            remove_future=True,
        ),
        _tree=tree,
    )
    new_source = result.new_source if result.new_source != source else None
    blocker: Blocker | None = result.blocker
    if blocker is None and new_source is None:
        blocker = next(
            (cb.blocker for cb in result.class_blockers if is_soft_blocker(cb.blocker)), None
        )
    if blocker is not None:
        new_source = None
    return _HookSortResult(
        new_source=new_source,
        blocker=blocker,
        message=result.warning,
        sorted_defs=(
            tuple(result.new_order) if new_source is not None and result.blocker is None else ()
        ),
        banner_constraints=tuple(result.banner_constraints),
        class_blockers=tuple(result.class_blockers),
    )


def sort_incremental_impl(
    source: str,
    baseline: str,
    *,
    options: EngineOptions,
    _tree: ast.Module | None = None,
) -> _IncrementalSortResult:
    """Sort incrementally and retain internal method metadata.

    Args:
        source: Current Python source.
        baseline: Baseline Python source used to identify new definitions.
            Empty or whitespace-only content selects full sorting.
        options: Concrete engine options.
        _tree: Optional pre-parsed AST that must correspond to `source`.

    Returns:
        The incremental result, including method and original-definition
        metadata. A baseline parsing or compilation `SyntaxError` reports
        `non_conforming`. A layout refusal reports `layout` with no candidate
        source. Other blockers may accompany candidate source and must be
        honored by the caller.

    Raises:
        SyntaxError: If current source cannot be parsed or compiled as a
            module, including constructs such as a top-level return.
        TypeError: If a validated banner pattern is neither a string nor
            `None`.
        ValueError: If parsing source or baseline encounters a lone Unicode
            surrogate, or a validated banner pattern does not compile or uses
            unsupported syntax. Pattern compilation failures chain `re.error`.
    """
    # Validate source is compilable (matching sort_for_hook_detailed).
    # ast.parse accepts some constructs that compile()
    # rejects (e.g., bare return/break at module scope). Raise
    # SyntaxError early so callers handle it uniformly.
    if _tree is None:
        tree = parse_source(source, filename='<sort_incremental_impl>', check=True)
    else:
        tree = _tree
        try:
            compile(tree, '<sort_incremental_impl>', 'exec')
        except (RecursionError, MemoryError) as error:
            raise SyntaxError(NESTING_LIMIT_MESSAGE) from error

    try:
        if not baseline.strip():
            return _sort_incremental_full(source, tree, options=options)
        return _sort_incremental(source, baseline, tree, options=options)
    except LayoutPreservationError:
        parsed = parse_definitions(
            source,
            banners=options.policy.banners,
            banner_pattern=options.policy.banner_pattern,
            tree=tree,
        )
        return _IncrementalSortResult(
            new_source=None,
            blocker='layout',
            method_sorts=[],
            original_defs=parsed.definitions,
        )


def sort_incremental_detailed(
    source: str,
    baseline: str,
    *,
    policy: SortPolicy,
) -> _HookSortResult:
    """Run detailed incremental sorting for the hook boundary.

    This hook-only orchestration boundary exposes merged definition and banner
    constraint metadata. It never adds the future import. After a blocker-free
    result, it removes every eligible `from __future__ import annotations`
    import only when the source has no syntactic annotations or `annotations`
    name occurrences. The shared cleanup reaches a fixed point while limiting
    each deletion to the import node's exclusively owned physical-line span.

    Args:
        source: The current Python source code.
        baseline: The baseline Python source code.
        policy: Concrete ordering policy.

    Returns:
        A hook result with candidate source, blocker, merged definitions, and
        banner constraints. Source is `None` when no rewrite is proposed;
        callers must honor a blocker before applying candidate source.
        `sorted_defs` contains the final merged definitions after a changed,
        blocker-free result; otherwise, it is empty.

    Raises:
        SyntaxError: If current source cannot be parsed or compiled as a
            module.
        TypeError: If a validated banner pattern is neither a string nor
            `None`.
        ValueError: If parsing source or baseline encounters a lone Unicode
            surrogate, or a validated banner pattern does not compile or uses
            unsupported syntax. Pattern compilation failures chain `re.error`.
    """
    tree = parse_source(source)
    result = sort_incremental_impl(
        source,
        baseline,
        options=EngineOptions(
            policy=policy,
            force=False,
            add_future=False,
            remove_future=False,
        ),
        _tree=tree,
    )
    blocker: Blocker | None = result.blocker
    if blocker is not None:
        return _HookSortResult(
            new_source=result.new_source,
            blocker=blocker,
            message=result.message,
            sorted_defs=(),
            banner_constraints=result.banner_constraints,
            class_blockers=result.class_blockers,
        )

    managed_source = result.new_source or source
    managed_tree = tree if managed_source is source else parse_source(managed_source)
    if has_future_annotations_import(managed_source, _tree=managed_tree):
        managed_source = remove_unused_future_annotations(managed_source, _tree=managed_tree)

    new_source = managed_source if managed_source != source else None
    if new_source is not None and result.safety_gate is not None:
        new_source = None
        blocker = result.safety_gate
    elif new_source is None:
        blocker = next(
            (cb.blocker for cb in result.class_blockers if is_soft_blocker(cb.blocker)), None
        )
    return _HookSortResult(
        new_source=new_source,
        blocker=blocker,
        sorted_defs=(
            result.sorted_defs or tuple(result.original_defs) if new_source is not None else ()
        ),
        banner_constraints=result.banner_constraints,
        class_blockers=result.class_blockers,
    )
