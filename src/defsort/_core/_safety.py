"""Safety checks for structural and definition-time reorder hazards."""

from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import NamedTuple

from defsort._core._analysis import (
    BLOCKING_CODE_PREVIEW_LEN,
    DIRECTIVE_PATTERN,
    SAFE_TRAILING_PATTERN,
)
from defsort._core._ordering import group_consecutive_same_name, has_duplicate_group_names
from defsort._source import split_physical_lines
from defsort._types import (
    DYNAMIC_ALL_MESSAGE,
    Blocker,
    BlockingCode,
    Definition,
    EvaluationOrder,
    MethodDefinition,
    MethodSortInfo,
    SortPolicy,
    blocker_message,
)


class SafetyVerdict(NamedTuple):
    """Carry a safety blocker, its cause, and any blocking source previews.

    `blocker` and `message` are `None` when the candidate is safe.
    """

    blocker: Blocker | None
    message: str | None
    blocking_code: list[BlockingCode]


def _eager_definition_order_changed(new_order: Sequence[Definition]) -> bool:
    """Return whether eager definitions left their source relative order."""
    eager = [definition.index for definition in new_order if definition.has_eager_evaluation]
    return any(later <= earlier for earlier, later in pairwise(eager))


def _eager_method_order_changed(method_sorts: Sequence[MethodSortInfo]) -> bool:
    """Return whether eager methods left their source relative order."""
    for method_sort in method_sorts:
        source_eager = [
            method.index for method in method_sort.original_order if method.has_eager_evaluation
        ]
        candidate_eager = [
            method.index for method in method_sort.new_order if method.has_eager_evaluation
        ]
        if source_eager != candidate_eager:
            return True
    return False


def _has_forward_annotation_refs(
    definitions: list[Definition],
    postamble_names: set[str],
) -> bool:
    """Check whether the proposed order has forward annotation references."""
    # Map name -> first position in the proposed order
    name_positions: dict[str, int] = {}
    for i, d in enumerate(definitions):
        if d.name not in name_positions:
            name_positions[d.name] = i

    for i, d in enumerate(definitions):
        for ref in d.annotation_refs:
            # Self-referential
            if ref == d.name:
                return True
            # Postamble reference
            if ref in postamble_names:
                return True
            # Forward reference (same position or later)
            if ref in name_positions and name_positions[ref] >= i:
                return True
    return False


def _has_new_order_violation(
    original: Sequence[Definition | MethodDefinition],
    candidate: Sequence[Definition | MethodDefinition],
    reads_by_index: Mapping[int, set[str]],
) -> bool:
    """Return whether a candidate changes any read's exact binding identity."""
    original_readers = {unit.index for unit in original}

    def collect_identities(
        units: Sequence[Definition | MethodDefinition],
    ) -> dict[tuple[int, str], int | None]:
        visible: dict[str, int] = {}
        identities: dict[tuple[int, str], int | None] = {}
        for unit in units:
            if unit.index in original_readers:
                for name in reads_by_index.get(unit.index, set()):
                    identities[(unit.index, name)] = visible.get(name)
            visible[unit.name] = unit.index
        return identities

    original_identities = collect_identities(original)
    candidate_identities = collect_identities(candidate)
    return any(
        original_identities[key] != candidate_identities[key]
        for key in original_identities.keys() & candidate_identities.keys()
    )


def _is_safe_trailing_line(line: str) -> bool:
    """Return whether one trailing source line is safe to move."""
    return bool(SAFE_TRAILING_PATTERN.match(line)) and not DIRECTIVE_PATTERN.search(line)


def _physical_line_count(text: str) -> int:
    """Return the number of physical source lines represented by text."""
    return len(split_physical_lines(text))


def _pinned_unit_moved(
    original: Sequence[Definition | MethodDefinition],
    candidate: Sequence[Definition | MethodDefinition],
) -> bool:
    """Return whether any pinned unit has a different set of preceding units."""
    preceding_by_index: dict[int, set[int]] = {}
    preceding: set[int] = set()
    for unit in original:
        if unit.binds_other_names:
            preceding_by_index[unit.index] = preceding.copy()
        preceding.add(unit.index)
    preceding = set()
    for unit in candidate:
        if unit.index in preceding_by_index and preceding_by_index[unit.index] != preceding:
            return True
        preceding.add(unit.index)
    return False


def _has_eager_order_hazard(
    definitions: Sequence[Definition],
    new_order: Sequence[Definition],
    method_sorts: Sequence[MethodSortInfo],
) -> bool:
    """Return whether either scope has a new eager-order violation."""
    if _pinned_unit_moved(definitions, new_order) or _has_new_order_violation(
        definitions,
        new_order,
        {definition.index: definition.eager_refs for definition in definitions},
    ):
        return True
    return any(
        _pinned_unit_moved(method_sort.original_order, method_sort.new_order)
        or _has_new_order_violation(
            method_sort.original_order,
            method_sort.new_order,
            {method.index: method.eager_reads for method in method_sort.original_order},
        )
        for method_sort in method_sorts
    )


def _truncate_blocking_text(text: str) -> str:
    """Truncate blocking-code preview text to the display limit."""
    if len(text) > BLOCKING_CODE_PREVIEW_LEN:
        return text[:BLOCKING_CODE_PREVIEW_LEN] + '...'
    return text


def _first_blocking_line(segment: str, start_line: int) -> BlockingCode | None:
    """Return the first unsafe line in a source segment."""
    for offset, line in enumerate(split_physical_lines(segment)):
        if _is_safe_trailing_line(line):
            continue
        return BlockingCode(
            line=start_line + offset,
            text=_truncate_blocking_text(line.strip()),
        )
    return None


def _collect_blocking_code(definitions: list[Definition]) -> list[BlockingCode]:
    """Collect one blocking-code preview per inter-definition section."""
    previews: list[BlockingCode] = []
    for previous, current in pairwise(definitions):
        preview = _first_blocking_line(previous.trailing, previous.end_line + 1)
        if preview is None and current.leading:
            leading_start = current.start_line - _physical_line_count(current.leading)
            preview = _first_blocking_line(current.leading, leading_start)
        if preview is not None:
            previews.append(preview)
    return previews


def has_anchored_main_annotation_hazard(
    new_order: Sequence[Definition],
    *,
    policy: SortPolicy,
) -> bool:
    """Return whether an anchored `main` is annotated against from above.

    A `main` held last by policy cannot move up to satisfy an earlier
    annotation, so the hazard exists regardless of whether anything was
    reordered. Callers must therefore check it independently of
    `has_annotation_order_hazard`, which only fires when an order changed.

    Args:
        new_order: Definitions in their final order.
        policy: Concrete ordering policy.

    Returns:
        `True` when a definition above an anchored `main` annotates against it.
    """
    if policy.main != 'last':
        return False
    main_position = next(
        (
            position
            for position, definition in enumerate(new_order)
            if definition.name == 'main' and definition.kind == 'function'
        ),
        None,
    )
    if main_position is None:
        return False
    if any(
        'main' in definition.eager_refs for definition in new_order if definition.name != 'main'
    ):
        return False
    return any('main' in definition.annotation_refs for definition in new_order[:main_position])


def has_annotation_order_hazard(
    definitions: Sequence[Definition],
    new_order: Sequence[Definition],
    method_sorts: Sequence[MethodSortInfo],
    postamble_names: set[str],
) -> bool:
    """Return whether either scope has an annotation hazard from reordering.

    Args:
        definitions: Original top-level definitions.
        new_order: Proposed top-level order.
        method_sorts: Proposed method orders by class.
        postamble_names: Names bound after the sortable definitions.

    Returns:
        `True` when a changed scope introduces an annotation-order hazard.
    """
    top_changed = [unit.index for unit in definitions] != [unit.index for unit in new_order]
    if top_changed and _has_forward_annotation_refs(list(new_order), postamble_names):
        return True
    return any(
        _has_new_order_violation(
            method_sort.original_order,
            method_sort.new_order,
            {method.index: method.annotation_refs for method in method_sort.original_order},
        )
        for method_sort in method_sorts
    )


def check_reorder_hazards(  # noqa: PLR0913
    definitions: Sequence[Definition],
    new_order: Sequence[Definition],
    method_sorts: Sequence[MethodSortInfo],
    postamble_names: set[str],
    *,
    has_future: bool,
    add_future: bool,
    dynamic_all: bool = False,
    evaluation_order: EvaluationOrder = 'preserve',
) -> tuple[Blocker, str] | None:
    """Classify hard hazards across actual top-level and method candidates.

    Args:
        definitions: Original top-level definitions.
        new_order: Proposed top-level order.
        method_sorts: Proposed method orders by class.
        postamble_names: Names bound after the sortable definitions.
        has_future: Whether future annotation evaluation is enabled.
        add_future: Whether the future import may be added.
        dynamic_all: Whether postamble `__all__` can observe definition order.
        evaluation_order: Whether to preserve relative definition-time
            execution order. Binding and annotation checks always apply.

    Returns:
        The hard blocker and its cause, or `None` when there is no hard hazard.
    """
    top_changed = [unit.index for unit in definitions] != [unit.index for unit in new_order]
    if top_changed and dynamic_all:
        return 'eager_order', DYNAMIC_ALL_MESSAGE
    if evaluation_order == 'preserve':
        if _eager_definition_order_changed(new_order):
            return 'eager_order', blocker_message('eager_order')
        if _eager_method_order_changed(method_sorts):
            return 'eager_order', blocker_message('eager_order')
    if _has_eager_order_hazard(definitions, new_order, method_sorts):
        return 'eager_order', blocker_message('eager_order')
    if (
        has_annotation_order_hazard(
            definitions,
            new_order,
            method_sorts,
            postamble_names,
        )
        and not has_future
        and not add_future
    ):
        return 'forward_refs', blocker_message('forward_refs')
    return None


def has_interstitial_code(definitions: list[Definition]) -> bool:
    """Check whether definitions have unsafe interstitial content.

    Checks the full span between adjacent definitions, including content stored
    as trailing on the previous definition and leading on the next definition.
    The `had_interstitial_code` flag still blocks sorting for parser-detected
    unsafe method or hoisting cases that do not have a preview line.

    Args:
        definitions: Definitions to check.

    Returns:
        `True` when any inter-definition span is unsafe to move.
    """
    if any(d.had_interstitial_code for d in definitions):
        return True
    return bool(_collect_blocking_code(definitions))


def has_nonconsecutive_duplicates(definitions: list[Definition]) -> bool:
    """Check for non-consecutive duplicate definition names.

    All names are checked including main. Consecutive same-name defs (overload
    groups) are not considered duplicates.

    Args:
        definitions: Definitions to check.

    Returns:
        `True` when a name appears in non-consecutive definition groups.
    """
    groups = group_consecutive_same_name(definitions)
    return has_duplicate_group_names(groups)


def check_sort_safety(  # noqa: PLR0913
    definitions: list[Definition],
    new_order: list[Definition],
    method_sorts: Sequence[MethodSortInfo],
    postamble_names: set[str],
    has_future: bool,
    *,
    force: bool,
    add_future: bool,
    check_top_level: bool = True,
    dynamic_all: bool = False,
    evaluation_order: EvaluationOrder = 'preserve',
) -> SafetyVerdict:
    """Run safety checks and report the blocker, cause, and source previews.

    Args:
        definitions: Original definitions.
        new_order: Proposed new order.
        method_sorts: Proposed method orders by class.
        postamble_names: Names defined in the postamble.
        has_future: Whether source has `from __future__ import annotations`.
        force: Bypass interstitial checks.
        add_future: Add future annotations to resolve forward refs.
        check_top_level: Run structural checks for top-level ordering.
        dynamic_all: Whether postamble `__all__` construction can observe
            order.
        evaluation_order: Whether to preserve relative definition-time
            execution order. Other safety checks remain in force.

    Returns:
        The blocker and its cause when sorting is unsafe, with any blocking
        source previews. A safe candidate has no blocker or message.
    """
    # Interstitial code prevents moving top-level definitions.
    if check_top_level and not force:
        blocking = _collect_blocking_code(definitions)
        if any(definition.had_interstitial_code for definition in definitions) or blocking:
            return SafetyVerdict('interstitial', blocker_message('interstitial'), blocking)

    hazard = check_reorder_hazards(
        definitions,
        new_order,
        method_sorts,
        postamble_names,
        has_future=has_future,
        add_future=add_future,
        dynamic_all=dynamic_all,
        evaluation_order=evaluation_order,
    )
    if hazard is not None:
        return SafetyVerdict(*hazard, [])

    # Non-consecutive duplicate names cannot be grouped safely.
    if check_top_level and has_nonconsecutive_duplicates(definitions):
        return SafetyVerdict('duplicates', blocker_message('duplicates'), [])

    return SafetyVerdict(None, None, [])


def has_sectioned_duplicates(definitions: list[Definition]) -> bool:
    """Return whether definitions repeat across section-aware groups.

    Args:
        definitions: Definitions in source order.

    Returns:
        Whether a definition name occurs in more than one group.
    """
    groups = group_consecutive_same_name(definitions, respect_sections=True)
    return has_duplicate_group_names(groups)


def is_safe_trailing(trailing: str) -> bool:
    """Check if trailing content is safe to move.

    Safe: blank lines and regular comments.
    Unsafe: executable code or directive comments (noqa, type: ignore, etc.).

    Args:
        trailing: Exact trailing source text.

    Returns:
        `True` when the text can move with its definition.
    """
    return all(_is_safe_trailing_line(line) for line in split_physical_lines(trailing))
