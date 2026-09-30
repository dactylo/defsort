"""Top-level dependency and alphabetical definition ordering."""

# pyright: reportPrivateUsage=false

import heapq
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from defsort._core._analysis import find_cycles
from defsort._core._banners import collect_banner_constraints
from defsort._types import (
    BannerConstraint,
    Definition,
    SectionBoundary,
    SortPolicy,
    SortScopeOrder,
)


@dataclass
class _DefinitionGroup:
    """A group of consecutive same-name definitions (e.g., @overload stubs + impl)."""

    name: str
    definitions: list[Definition]
    dependencies: set[str]

    @property
    def _first_index(self) -> int:
        """Return the index of the group's first definition."""
        return self.definitions[0].index


class _DependencyMaps:
    """Data structures for Kahn's algorithm on definition groups."""

    def __init__(self, groups: list[_DefinitionGroup], policy: SortPolicy) -> None:
        """Initialize dependency indexes for the definition groups."""
        all_names = {g.name for g in groups}
        self.group_ids = {id(group): group_id for group_id, group in enumerate(groups)}
        self.groups = list(groups)

        dependencies = {
            group.name: (group.dependencies & all_names) - {group.name} for group in groups
        }
        if policy.evaluation_order == 'preserve':
            previous_eager: _DefinitionGroup | None = None
            for group in groups:
                if not _group_has_eager_evaluation(group):
                    continue
                if previous_eager is not None:
                    dependencies[group.name] = dependencies[group.name] | {previous_eager.name}
                previous_eager = group
        cycle_by_name = {name: cycle for cycle in find_cycles(dependencies) for name in cycle}

        # Effective deps: strip only edges internal to the same cycle.
        self.effective_deps: dict[int, set[str]] = {}
        for group_id, group in enumerate(groups):
            deps = dependencies[group.name] - cycle_by_name.get(group.name, frozenset())
            self.effective_deps[group_id] = deps

        # In-degree
        self.in_degree: dict[int, int] = {}
        for group_id, _group in enumerate(groups):
            self.in_degree[group_id] = len(self.effective_deps[group_id])

        # Reverse deps: name -> list of group indices that depend on it
        self.reverse_deps: dict[str, list[int]] = defaultdict(list)
        for group_id, _group in enumerate(groups):
            for dep_name in self.effective_deps[group_id]:
                self.reverse_deps[dep_name].append(group_id)

    def _group_id(self, group: _DefinitionGroup) -> int:
        """Return the stable algorithm identity for a definition group."""
        return self.group_ids[id(group)]


class _GroupQueue:
    """Heap queue using the configured top-level dependency tie-break."""

    def __init__(self, policy: SortPolicy) -> None:
        """Initialize a queue for the configured tie-break policy."""
        self._alphabetical: list[tuple[str, int, _DefinitionGroup]] = []
        self._policy = policy
        self._source: list[tuple[int, int, _DefinitionGroup]] = []

    def __bool__(self) -> bool:
        """Return whether the selected heap contains groups."""
        if self._policy.tie_break == 'source':
            return bool(self._source)
        return bool(self._alphabetical)

    def _pop(self) -> _DefinitionGroup:
        """Pop the next group under the configured tie-break."""
        if self._policy.tie_break == 'source':
            _, _order, group = heapq.heappop(self._source)
            return group
        _, _index, group = heapq.heappop(self._alphabetical)
        return group

    def _push(self, group: _DefinitionGroup, group_order: int) -> None:
        """Push a group using the configured tie-break key."""
        if self._policy.tie_break == 'source':
            heapq.heappush(self._source, (group._first_index, group_order, group))
        else:
            heapq.heappush(
                self._alphabetical,
                (group.name, group._first_index, group),
            )


def _group_has_eager_evaluation(group: _DefinitionGroup) -> bool:
    """Return whether a group has import-time definition evaluation."""
    return any(definition.has_eager_evaluation for definition in group.definitions)


def _is_main_function_group(group: _DefinitionGroup) -> bool:
    """Return whether a group is exclusively function definitions named main."""
    return group.name == 'main' and all(
        definition.kind == 'function' for definition in group.definitions
    )


def _partition_main_groups(
    groups: list[_DefinitionGroup],
    *,
    anchored: bool,
) -> tuple[list[_DefinitionGroup], list[_DefinitionGroup]]:
    """Partition ordinary and anchored main groups without splitting groups."""
    if not anchored:
        return groups, []
    main_groups = [group for group in groups if _is_main_function_group(group)]
    other_groups = [group for group in groups if not _is_main_function_group(group)]
    return other_groups, main_groups


def _pin_eager_groups(
    groups: list[_DefinitionGroup], *, policy: SortPolicy
) -> list[_DefinitionGroup]:
    """Restore eager groups to relative source order when policy requires it."""
    if policy.evaluation_order == 'allow-reordering':
        return groups
    positions = [index for index, group in enumerate(groups) if _group_has_eager_evaluation(group)]
    if len(positions) <= 1:
        return groups
    ordered = sorted((groups[index] for index in positions), key=lambda g: g._first_index)
    pinned = list(groups)
    for slot, group in zip(positions, ordered, strict=True):
        pinned[slot] = group
    return pinned


def _should_anchor_main(definitions: Sequence[Definition], policy: SortPolicy) -> bool:
    """Return whether policy can safely keep main after other definitions."""
    return policy.main == 'last' and not any(
        'main' in definition.eager_refs for definition in definitions if definition.name != 'main'
    )


def _topological_sort_groups(
    groups: list[_DefinitionGroup],
    maps: _DependencyMaps,
    *,
    policy: SortPolicy,
) -> list[_DefinitionGroup]:
    """Run Kahn's algorithm with the configured tie-breaking on groups."""
    queue = _GroupQueue(policy)
    for group in groups:
        group_id = maps._group_id(group)
        if maps.in_degree[group_id] == 0:
            queue._push(group, group_id)

    result: list[_DefinitionGroup] = []
    processed: set[int] = set()

    while queue:
        group = queue._pop()
        group_id = maps._group_id(group)
        if group_id in processed:
            continue
        processed.add(group_id)
        result.append(group)

        for dependent_id in maps.reverse_deps[group.name]:
            if dependent_id not in processed:
                maps.in_degree[dependent_id] -= 1
                if maps.in_degree[dependent_id] == 0:
                    queue._push(maps.groups[dependent_id], dependent_id)

    result.extend(group for group in groups if maps._group_id(group) not in processed)
    return result


def group_consecutive_same_name(
    definitions: Sequence[Definition],
    *,
    respect_sections: bool = False,
) -> list[_DefinitionGroup]:
    """Group consecutive definitions with the same name.

    Only groups if the previous definition had no interstitial code (checked
    via had_interstitial_code, which reflects original state).
    Single definitions become groups of size 1. Group dependencies are the
    union of all member dependencies. Annotation deps are not aggregated here
    -- safety checks iterate group members directly.

    Args:
        definitions: Definitions in source order.
        respect_sections: End groups when the section changes.

    Returns:
        Consecutive same-name definition groups.
    """
    if not definitions:
        return []

    def build_group(members: list[Definition]) -> _DefinitionGroup:
        dependencies = {dep for definition in members for dep in definition.dependencies}
        return _DefinitionGroup(members[0].name, list(members), dependencies)

    groups: list[_DefinitionGroup] = []
    current_group: list[Definition] = [definitions[0]]

    for i in range(1, len(definitions)):
        prev_def = definitions[i - 1]
        curr_def = definitions[i]
        same_section = not respect_sections or curr_def.section == prev_def.section
        if curr_def.name == prev_def.name and same_section and not prev_def.had_interstitial_code:
            current_group.append(curr_def)
        else:
            groups.append(build_group(current_group))
            current_group = [curr_def]

    groups.append(build_group(current_group))
    return groups


def has_duplicate_group_names(groups: Iterable[_DefinitionGroup]) -> bool:
    """Check if any group name appears more than once.

    Args:
        groups: Consecutive same-name definition groups.

    Returns:
        `True` when non-consecutive groups share a name.
    """
    seen: set[str] = set()
    for g in groups:
        if g.name in seen:
            return True
        seen.add(g.name)
    return False


def order_alphabetical(
    definitions: Sequence[Definition],
    *,
    policy: SortPolicy,
) -> list[Definition]:
    """Order definitions: classes alphabetically, then functions alphabetically.

    When `policy.main == 'last'`, `main()` is placed last unless another
    definition needs it at definition time. Groups with observable
    definition-time evaluation retain source-relative order under the default
    evaluation policy, including when that keeps an eager `main()` earlier.
    Overload groups sort by group name and remain together.

    Args:
        definitions: Definitions in source order.
        policy: Concrete ordering policy.

    Returns:
        Definitions in alphabetical category order.
    """
    anchored = _should_anchor_main(definitions, policy)

    groups = group_consecutive_same_name(definitions)
    other_groups, main_groups = _partition_main_groups(groups, anchored=anchored)

    class_groups = [g for g in other_groups if g.definitions[0].kind == 'class']
    func_groups = [g for g in other_groups if g.definitions[0].kind == 'function']

    class_groups.sort(key=lambda g: (g.name, g._first_index))
    func_groups.sort(key=lambda g: (g.name, g._first_index))

    ordered_groups = _pin_eager_groups(class_groups + func_groups + main_groups, policy=policy)
    result: list[Definition] = []
    for g in ordered_groups:
        result.extend(g.definitions)
    return result


def order_dependency(
    definitions: Sequence[Definition],
    *,
    policy: SortPolicy,
) -> list[Definition]:
    """Order definitions by dependency with configured tie-breaking.

    When `policy.main == 'last'`, `main()` is placed last unless another
    definition needs it at definition time. Groups with observable
    definition-time evaluation retain source-relative order under the default
    evaluation policy, including when that keeps an eager `main()` earlier.
    Uses Kahn's algorithm with cycle-aware edge removal.

    Args:
        definitions: Definitions in source order.
        policy: Concrete ordering policy.

    Returns:
        Definitions in dependency order.
    """
    if not definitions:
        return []

    anchored = _should_anchor_main(definitions, policy)

    groups = group_consecutive_same_name(definitions)
    other_groups, main_groups = _partition_main_groups(groups, anchored=anchored)

    if not other_groups:
        return [definition for group in main_groups for definition in group.definitions]

    maps = _DependencyMaps(other_groups, policy)
    sorted_groups = _topological_sort_groups(other_groups, maps, policy=policy)

    ordered_groups = _pin_eager_groups(sorted_groups + main_groups, policy=policy)
    result: list[Definition] = []
    for group in ordered_groups:
        result.extend(group.definitions)
    return result


def order_definitions(
    definitions: Sequence[Definition],
    *,
    order: SortScopeOrder,
    policy: SortPolicy,
) -> list[Definition]:
    """Order definitions by the specified strategy.

    Args:
        definitions: Definitions to order.
        order: Sort strategy -- dependency, alphabetical, or none.
        policy: Concrete ordering policy.

    Returns:
        Definitions ordered by the selected strategy.
    """
    if order == 'none':
        return list(definitions)
    if order == 'alphabetical':
        return order_alphabetical(definitions, policy=policy)
    return order_dependency(definitions, policy=policy)


def top_level_banner_constraints(
    definitions: list[Definition],
    boundaries: list[SectionBoundary],
    *,
    policy: SortPolicy,
    unconstrained_order: list[Definition] | None = None,
) -> list[BannerConstraint]:
    """Return top-level banner constraints for an enabled barriers scope.

    Args:
        definitions: Definitions in current source order.
        boundaries: Section boundaries detected in the current source.
        policy: Concrete ordering policy.
        unconstrained_order: Already-computed order to compare against. Derived
            from `policy` when omitted, so callers that have no ordering in
            hand pay for one only when barriers are actually enabled.

    Returns:
        Banner constraints for the top-level scope, empty when barriers are off
        or the definition scope is unordered.
    """
    if policy.banners != 'barriers':
        return []
    if policy.definition_order == 'none':
        return []
    if unconstrained_order is None:
        unconstrained_order = order_definitions(
            definitions,
            order=policy.definition_order,
            policy=policy,
        )
    return collect_banner_constraints(
        definitions,
        unconstrained_order,
        boundaries,
        class_name=None,
    )
