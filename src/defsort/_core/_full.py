"""Full-file sorting shared by CLI, library, and hook entry points."""

import ast
from collections.abc import Iterable

from defsort._core._analysis import (
    has_future_annotations_import,
)
from defsort._core._banners import (
    apply_lossless_strip,
    order_within_sections,
)
from defsort._core._future import (
    can_remove_future_annotations,
    ensure_future_annotations,
    remove_unused_future_annotations,
)
from defsort._core._layout import LayoutPreservationError
from defsort._core._methods import sort_file_methods
from defsort._core._ordering import (
    order_definitions,
    top_level_banner_constraints,
)
from defsort._core._parse import (
    parse_definitions,
    parse_postamble_names,
    postamble_has_dynamic_all,
)
from defsort._core._reassemble import reassemble_source
from defsort._core._safety import (
    check_sort_safety,
    has_anchored_main_annotation_hazard,
    has_annotation_order_hazard,
    has_sectioned_duplicates,
)
from defsort._types import (
    BannerConstraint,
    Blocker,
    BlockingCode,
    Definition,
    EngineOptions,
    MethodSortInfo,
    SortPolicy,
    SortResult,
    blocker_message,
    validate_banner_pattern,
)


def _apply_future_transforms(source: str, *, add: bool, remove: bool) -> str:
    """Apply requested future-annotations transformations after assembly."""
    if add:
        source = ensure_future_annotations(source)
    if remove:
        source = remove_unused_future_annotations(source)
    return source


def _check_full_safety(  # noqa: PLR0913
    definitions: list[Definition],
    new_order: list[Definition],
    method_sorts: list[MethodSortInfo],
    postamble_names: set[str],
    has_future: bool,
    *,
    dynamic_all: bool,
    is_noop: bool,
    options: EngineOptions,
    policy: SortPolicy,
) -> tuple[bool, Blocker | None, list[BlockingCode]]:
    """Run full-sort safety checks, including barrier-local grouping."""
    result = check_sort_safety(
        definitions,
        new_order,
        method_sorts,
        postamble_names,
        has_future,
        # Force skips only the interstitial check; all other checks still run.
        force=options.force or is_noop,
        add_future=options.add_future,
        check_top_level=policy.definition_order != 'none',
        dynamic_all=dynamic_all,
        evaluation_order=policy.evaluation_order,
    )
    skipped, _, _ = result
    if (
        not skipped
        and policy.definition_order != 'none'
        and policy.banners == 'barriers'
        and has_sectioned_duplicates(definitions)
    ):
        return True, 'duplicates', []
    return result


def _definition_orders(
    definitions: list[Definition],
    policy: SortPolicy,
) -> tuple[list[Definition], list[Definition]]:
    """Return unconstrained and effective top-level targets."""
    if policy.definition_order == 'none':
        unchanged = list(definitions)
        return unchanged, unchanged
    unconstrained = order_definitions(
        definitions,
        order=policy.definition_order,
        policy=policy,
    )
    if policy.banners != 'barriers':
        return unconstrained, unconstrained
    constrained = order_within_sections(
        definitions,
        lambda section: order_definitions(
            section,
            order=policy.definition_order,
            policy=policy,
        ),
    )
    return unconstrained, constrained


def count_top_level_methods(tree: ast.Module) -> int:
    """Count methods in top-level classes.

    Args:
        tree: Parsed module whose direct classes should be inspected.

    Returns:
        Number of direct methods in top-level classes.
    """
    return sum(
        sum(
            1
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    )


def make_unchanged_result(
    source: str,
    definitions: list[Definition] | None = None,
    method_sorts: list[MethodSortInfo] | None = None,
    total_methods: int = 0,
    banner_constraints: Iterable[BannerConstraint] = (),
) -> SortResult:
    """Build a no-change sort result.

    Args:
        source: Original source text.
        definitions: Parsed top-level definitions, when available.
        method_sorts: Method ordering metadata, when available.
        total_methods: Number of methods in top-level classes.
        banner_constraints: Constraints detected during parsing.

    Returns:
        A result whose original and new source are identical.
    """
    return SortResult(
        original_source=source,
        new_source=source,
        original_order=definitions if definitions is not None else [],
        new_order=definitions if definitions is not None else [],
        warning=None,
        blocking_code=[],
        method_sorts=method_sorts if method_sorts is not None else [],
        total_methods=total_methods,
        banner_constraints=list(banner_constraints),
    )


def with_banner_constraints(
    result: SortResult,
    constraints: Iterable[BannerConstraint],
) -> SortResult:
    """Attach a defensive copy of structured banner constraints.

    Args:
        result: Sort result to update.
        constraints: Constraints to copy onto the result.

    Returns:
        The updated result.
    """
    result.banner_constraints = list(constraints)
    return result


def _sort_file(source: str, tree: ast.Module, *, options: EngineOptions) -> SortResult:
    """Build a whole-file candidate, allowing layout refusals to reach the boundary."""
    policy = options.policy
    method_result = sort_file_methods(source, tree, policy=policy)
    candidate_source = method_result.candidate_source
    candidate_tree = tree if candidate_source == source else ast.parse(candidate_source)
    parsed = parse_definitions(
        candidate_source,
        banners=policy.banners,
        banner_pattern=policy.banner_pattern,
        hoist=False,
        tree=candidate_tree,
    )
    if options.hoist and parsed.definitions:
        _, planned_order = _definition_orders(parsed.definitions, policy)
        candidate_positions = {
            definition.index: position for position, definition in enumerate(planned_order)
        }
        parsed = parse_definitions(
            candidate_source,
            banners=policy.banners,
            banner_pattern=policy.banner_pattern,
            hoist=True,
            tree=candidate_tree,
            candidate_positions=candidate_positions,
        )
    definitions = parsed.definitions
    postamble = parsed.postamble
    hoistable = parsed.hoistable
    method_sorts = list(method_result.method_sorts)
    class_blockers = method_result.class_blockers
    method_constraints = list(method_result.banner_constraints)

    total_methods = count_top_level_methods(tree)

    if not definitions:
        return make_unchanged_result(
            source,
            [],
            method_sorts,
            total_methods,
            method_constraints,
        )

    unconstrained_order, new_order = _definition_orders(definitions, policy)
    top_constraints = top_level_banner_constraints(
        definitions,
        parsed.boundaries,
        policy=policy,
        unconstrained_order=unconstrained_order,
    )
    constraints = [*top_constraints, *method_constraints]

    postamble_names = parse_postamble_names(postamble)
    has_future = has_future_annotations_import(source, _tree=tree)

    old_ids = [definition.index for definition in definitions]
    new_ids = [definition.index for definition in new_order]
    order_changed = old_ids != new_ids
    boundaries_removed = policy.banners == 'strip' and bool(parsed.boundaries)
    method_source_changed = candidate_source != source

    annotation_hazard = has_annotation_order_hazard(
        definitions,
        new_order,
        method_sorts,
        postamble_names,
    )
    annotation_hazard = annotation_hazard or has_anchored_main_annotation_hazard(
        new_order,
        policy=policy,
    )
    should_add_future = options.add_future and annotation_hazard and not has_future
    should_remove_future = (
        options.remove_future and has_future and can_remove_future_annotations(tree)
    )

    no_source_change = (
        not order_changed
        and not method_sorts
        and not should_add_future
        and not should_remove_future
        and not hoistable
        and not boundaries_removed
        and not method_source_changed
    )

    skipped, blocker, blocking_code = _check_full_safety(
        definitions,
        new_order,
        method_sorts,
        postamble_names,
        has_future,
        dynamic_all=postamble_has_dynamic_all(postamble),
        is_noop=no_source_change and not class_blockers,
        options=options,
        policy=policy,
    )
    if skipped:
        skipped_result = SortResult(
            original_source=source,
            new_source=source,
            original_order=definitions,
            new_order=new_order,
            warning=blocker_message(blocker) if blocker else None,
            blocking_code=blocking_code,
            blocker=blocker,
            skipped=True,
            method_sorts=method_sorts,
            total_methods=total_methods,
            class_blockers=class_blockers,
        )
        return with_banner_constraints(skipped_result, constraints)

    if no_source_change:
        class_blocker = class_blockers[0].blocker if class_blockers else None
        unchanged_result = SortResult(
            original_source=source,
            new_source=source,
            original_order=definitions,
            new_order=new_order,
            warning=blocker_message(class_blocker) if class_blocker is not None else None,
            blocking_code=[],
            skipped=bool(class_blockers),
            method_sorts=method_sorts,
            total_methods=total_methods,
            class_blockers=class_blockers,
        )
        return with_banner_constraints(unchanged_result, constraints)

    strip_is_only_change = (
        policy.banners == 'strip'
        and policy.definition_order == 'none'
        and policy.method_order == 'none'
        and not hoistable
        and not should_add_future
        and not should_remove_future
    )
    new_source = (
        apply_lossless_strip(candidate_source, definitions, parsed.boundaries)
        if strip_is_only_change
        else reassemble_source(parsed.layout, parsed.blocks, new_order)
    )

    new_source = _apply_future_transforms(
        new_source,
        add=should_add_future,
        remove=should_remove_future,
    )

    sorted_result = SortResult(
        original_source=source,
        new_source=new_source,
        original_order=definitions,
        new_order=new_order,
        warning=None,
        blocking_code=[],
        method_sorts=method_sorts,
        total_methods=total_methods,
        class_blockers=class_blockers,
    )
    return with_banner_constraints(sorted_result, constraints)


def sort_file(
    source: str,
    *,
    options: EngineOptions,
    _tree: ast.Module | None = None,
) -> SortResult:
    """Sort definitions in source code with full option support.

    Args:
        source: The Python source code.
        options: Bundled sort options controlling ordering, safety, hoisting,
            and future-annotations management.
        _tree: Optional pre-parsed AST that must correspond to `source`.

    Returns:
        Full sorting result and display metadata. A source-parsing
        `SyntaxError` returns unchanged source with empty definition metadata.
        A layout refusal returns the original source with `skipped=True` and
        `blocker='layout'`.

    Raises:
        TypeError: If the configured banner pattern is neither a string nor
            `None`.
        ValueError: If the configured banner pattern does not compile or uses
            unsupported syntax. Compilation failures chain `re.error`.
    """
    policy = options.policy
    validate_banner_pattern(policy.banner_pattern)

    if _tree is not None:
        tree = _tree
    else:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return make_unchanged_result(source)

    try:
        return _sort_file(source, tree, options=options)
    except LayoutPreservationError:
        parsed = parse_definitions(
            source,
            banners=policy.banners,
            banner_pattern=policy.banner_pattern,
            tree=tree,
        )
        result = make_unchanged_result(
            source, parsed.definitions, total_methods=count_top_level_methods(tree)
        )
        result.blocker = 'layout'
        result.warning = blocker_message('layout')
        result.skipped = True
        return result
