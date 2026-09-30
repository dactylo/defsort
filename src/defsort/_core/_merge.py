"""Generic incremental merge algorithm for definitions and methods."""

from collections import Counter
from collections.abc import Callable, Hashable, Iterable

from defsort._types import Blocker


def find_new_items[T](
    baseline: Iterable[T],
    current: Iterable[T],
    key_fn: Callable[[T], Hashable],
    is_overload_fn: Callable[[T], bool],
) -> list[T]:
    """Find items in current that are new (not in baseline).

    Matching is by identity key, positional for duplicates.
    Overload-decorated items are never considered new.

    Args:
        baseline: Items in the baseline scope.
        current: Items in the current scope.
        key_fn: Function returning each item's identity key.
        is_overload_fn: Function identifying overload-decorated items.

    Returns:
        Current items without matching baseline occurrences.
    """
    baseline_counts: Counter[Hashable] = Counter()
    for item in baseline:
        if not is_overload_fn(item):
            baseline_counts[key_fn(item)] += 1

    new: list[T] = []
    seen: Counter[Hashable] = Counter()
    for item in current:
        if is_overload_fn(item):
            continue
        key = key_fn(item)
        seen[key] += 1
        if seen[key] > baseline_counts[key]:
            new.append(item)
    return new


def incremental_merge[T](  # noqa: PLR0913
    current: list[T],
    baseline: list[T],
    new_items: Iterable[T],
    order_fn: Callable[[list[T]], list[T]],
    key_fn: Callable[[T], Hashable],
    *,
    is_overload_fn: Callable[[T], bool],
    index_fn: Callable[[T], int],
    min_baseline: int = 0,
) -> tuple[list[T], Blocker | None]:
    """Run the shared incremental merge algorithm.

    This merge checks baseline conformance, detects changes to the relative
    order of existing items, and requires the target order to retain that
    existing order. Callers handle safety checks, annotation hazards, and
    source reassembly.

    The conformance check uses key_fn (identity keys), not index_fn, because
    indices are not stable across independent parses. The method-level caller
    uses (name, property_kind) identity and the top-level caller uses
    (name, kind) identity.

    Args:
        current: Items in current source order.
        baseline: Items in baseline source order.
        new_items: Current items absent from the baseline.
        order_fn: Function computing the target order.
        key_fn: Function returning each item's stable identity key.
        is_overload_fn: Function identifying overload-decorated items.
        index_fn: Function returning each item's current occurrence identity.
        min_baseline: Minimum baseline size required for conformance checking.

    Returns:
        (merged_list, None) on success or ([], blocker) on failure.
    """
    new_indices = {index_fn(item) for item in new_items}

    # Compare stable identity keys across independently parsed baselines.
    if len(baseline) >= min_baseline:
        baseline_target = order_fn(baseline)
        if [key_fn(d) for d in baseline] != [key_fn(d) for d in baseline_target]:
            return [], 'non_conforming'

    # Detect a changed relative order among existing items.
    existing_current = [item for item in current if index_fn(item) not in new_indices]

    bl_pos: dict[tuple[Hashable, int], int] = {}
    bl_seen: Counter[Hashable] = Counter()
    for i, item in enumerate(baseline):
        if is_overload_fn(item):
            continue
        key = key_fn(item)
        bl_seen[key] += 1
        bl_pos[(key, bl_seen[key])] = i

    cur_seen: Counter[Hashable] = Counter()
    current_positions: list[int] = []
    for item in existing_current:
        if is_overload_fn(item):
            continue
        key = key_fn(item)
        cur_seen[key] += 1
        pos = bl_pos.get((key, cur_seen[key]), -1)
        current_positions.append(pos)

    valid_positions = [p for p in current_positions if p != -1]
    if any(valid_positions[i] >= valid_positions[i + 1] for i in range(len(valid_positions) - 1)):
        return [], 'user_reordered'

    # The target must keep existing items in their current relative order.
    all_target = order_fn(current)
    existing_in_target = [index_fn(d) for d in all_target if index_fn(d) not in new_indices]
    existing_current_ids = [index_fn(d) for d in existing_current]
    if existing_in_target != existing_current_ids:
        return [], 'target_incompatible'

    return all_target, None
