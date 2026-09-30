"""Method ordering and class body reassembly."""

import ast
import heapq
import itertools
import re
import tokenize
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from defsort._core._analysis import (
    MIN_METHODS_FOR_SORT,
    analyze_annotation_refs,
    collect_body_refs,
    collect_name_references,
    decorated_start_line,
    find_cycles,
    get_node_end_line,
    is_overload_decorated,
)
from defsort._core._banners import (
    GapClassification,
    GapLocation,
    classify_gap,
    classify_initial_gap,
    collect_banner_constraints,
    compile_banner_pattern,
    order_within_sections,
    remove_boundary_lines,
    statement_owned_lines_by_gap,
)
from defsort._core._layout import (
    OwnershipBoundary,
    SourceLayout,
    make_ownership_boundary,
    permute_separators,
    render_layout,
    split_terminal_line_ending,
)
from defsort._core._merge import find_new_items, incremental_merge
from defsort._core._safety import is_safe_trailing
from defsort._source import scope_indent_body, split_physical_lines
from defsort._types import (
    BannerConstraint,
    BannerMode,
    Blocker,
    BoundaryLayoutPart,
    ClassBlocker,
    ClassSlice,
    IncrementalClassResult,
    MethodDefinition,
    MethodSortInfo,
    PropertyKind,
    SectionBoundary,
    SortPolicy,
    SortScopeOrder,
    blocker_message,
)

_CATEGORY_REGULAR = 3
_DEFAULT_SORT_POLICY = SortPolicy()

_GK = tuple[int, str, int]


_ClassIdentity = tuple[str, int]
MethodParseMapping = Mapping[_ClassIdentity, '_MethodParseResult']


@dataclass(frozen=True, slots=True)
class _FileMethodSortResult:
    """Full-file class rewrite candidate and method metadata."""

    candidate_source: str
    method_sorts: tuple[MethodSortInfo, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()
    class_blockers: tuple[ClassBlocker, ...] = ()


@dataclass(frozen=True, slots=True)
class _MethodGap:
    """Owned and fixed results of one inter-method gap."""

    trailing: str
    separator: str
    leading: str
    boundaries: tuple[SectionBoundary, ...]
    boundary_layout: tuple[BoundaryLayoutPart, ...]
    next_section: int


@dataclass(slots=True)
class _MethodLayoutState:
    """Mutable class-local layout metadata collected during parsing."""

    blocks: dict[int, str]
    fixed_following: list[str]
    fixed_ownership: list[OwnershipBoundary]
    guard_required: set[int]
    guard_required_before: set[int]


@dataclass(frozen=True, slots=True)
class _MethodParseResult:
    """Parsed methods and their fixed class-local boundary layout."""

    methods: list[MethodDefinition]
    boundaries: list[SectionBoundary]
    boundary_layout: list[BoundaryLayoutPart]
    layout: SourceLayout
    blocks: dict[int, str]


@dataclass(frozen=True, slots=True)
class _MethodProcessResult:
    """Result of processing methods in one class."""

    new_source: str | None
    sort_info: MethodSortInfo | None
    boundaries: list[SectionBoundary]
    boundary_layout: list[BoundaryLayoutPart]
    banner_constraints: list[BannerConstraint]
    blocker: Blocker | None = None


def _analyze_method_deps(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    method_names: set[str],
) -> set[str]:
    """Analyze method runtime dependencies from decorators and defaults."""
    nodes: list[ast.AST] = list(func.decorator_list)
    nodes.extend(func.args.defaults)
    nodes.extend(d for d in func.args.kw_defaults if d)
    deps: set[str] = set()
    for node in nodes:
        deps.update(collect_name_references(node, method_names, ''))
    return deps


def _class_header_end_line(cls: ast.ClassDef, cs: ClassSlice) -> int:
    """Return the absolute line containing the class suite colon."""
    start_offset = cls.lineno - cs.start_line
    physical_lines = iter(cs.lines[start_offset:])
    depth = 0
    for token in tokenize.generate_tokens(physical_lines.__next__):
        if token.type != tokenize.OP:
            continue
        if token.string in '([{':
            depth += 1
        elif token.string in ')]}':
            depth -= 1
        elif token.string == ':' and depth == 0:
            return cls.lineno + token.start[0] - 1
    return cls.lineno


def _class_structural_end_line(
    cls: ast.ClassDef,
    cs: ClassSlice,
    first_method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    """Return the last absolute line of the class structural prefix."""
    end = _class_header_end_line(cls, cs)
    for node in cls.body:
        if node is first_method:
            break
        end = max(end, get_node_end_line(node))
    return end


def _get_property_group_info(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, PropertyKind] | None:
    """Return the property name and kind for a setter or deleter."""
    for decorator in func.decorator_list:
        if (
            isinstance(decorator, ast.Attribute)
            and decorator.attr in ('setter', 'deleter')
            and isinstance(decorator.value, ast.Name)
        ):
            return decorator.value.id, decorator.attr
    return None


def _group_has_eager_evaluation(group: list[MethodDefinition]) -> bool:
    """Return whether a method group has definition-time evaluation."""
    return any(method.has_eager_evaluation for method in group)


def _has_duplicate_method_groups(groups: list[list[MethodDefinition]]) -> bool:
    """Check for repeated display names across distinct method groups."""
    names = [group[0].property_group or group[0].name for group in groups]
    return len(names) != len(set(names))


def _has_duplicate_property_binders(methods: list[MethodDefinition]) -> bool:
    """Check for repeated property binder kinds before regrouping."""
    seen: set[tuple[str, PropertyKind]] = set()
    for method in methods:
        if method.property_group is None or method.property_kind is None:
            continue
        key = (method.property_group, method.property_kind)
        if key in seen:
            return True
        seen.add(key)
    return False


def _has_unsafe_method_trailing(methods: list[MethodDefinition], cs: ClassSlice) -> bool:
    """Check if any inter-method content is unsafe to reorder."""
    for i, method in enumerate(methods[:-1]):
        next_method = methods[i + 1]
        trailing_start = method.end_line - cs.start_line + 1
        trailing_end = next_method.start_line - cs.start_line
        trailing = ''.join(cs.lines[trailing_start:trailing_end])
        if trailing.strip() and not is_safe_trailing(trailing):
            return True
    return False


def _incremental_method_no_merge(
    cur_parsed: _MethodParseResult,
    cur_cs: ClassSlice,
    original_parse: _MethodParseResult,
    constraints: list[BannerConstraint],
    *,
    banners: BannerMode,
) -> IncrementalClassResult:
    """Return an unchanged or strip-only incremental class result."""
    new_source: str | None = None
    if banners == 'strip' and cur_parsed.boundaries:
        new_source = remove_boundary_lines(
            cur_cs.source,
            cur_parsed.boundaries,
            source_start_line=cur_cs.start_line,
        )
    return IncrementalClassResult(
        new_source,
        None,
        None,
        had_new_methods=False,
        boundary_layout=tuple(original_parse.boundary_layout),
        banner_constraints=tuple(constraints),
    )


def _is_property_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if method has @property decorator."""
    return any(isinstance(d, ast.Name) and d.id == 'property' for d in func.decorator_list)


def _line_indent(physical: str) -> str:
    """Return the form-feed-aware indentation of a physical source line."""
    effective = scope_indent_body(physical, '') or ''
    return effective[: len(effective) - len(effective.lstrip(' \t'))]


def _class_scope_indent(cls: ast.ClassDef, cs: ClassSlice) -> str:
    """Return the exact indentation of direct class-suite statements."""
    first_statement = cls.body[0]
    if isinstance(
        first_statement,
        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
    ):
        first_line = decorated_start_line(first_statement)
    else:
        first_line = first_statement.lineno
    physical = cs.lines[first_line - cs.start_line]
    return _line_indent(physical)


def _class_initial_layout(
    cls: ast.ClassDef,
    cs: ClassSlice,
    first_method: ast.FunctionDef | ast.AsyncFunctionDef,
    first_method_line: int,
    banner_settings: tuple[BannerMode, str | None],
) -> tuple[GapClassification, str, re.Pattern[str] | None]:
    """Return the classified initial gap, exact prefix, and banner pattern."""
    banners, banner_pattern = banner_settings
    pattern = compile_banner_pattern(banner_pattern) if banners != 'ignore' else None
    structural_end = _class_structural_end_line(cls, cs, first_method)
    initial_raw = ''.join(
        cs.lines[structural_end - cs.start_line + 1 : first_method_line - cs.start_line]
    )
    initial_gap = classify_initial_gap(
        initial_raw,
        content_start_line=structural_end + 1,
        scope_indent=_class_scope_indent(cls, cs),
        banner_settings=(banners, pattern),
    )
    prefix = (
        ''.join(cs.lines[: structural_end - cs.start_line + 1])
        + initial_gap.trailing
        + initial_gap.separator
    )
    return initial_gap, prefix, pattern


def _method_group_key(name: str, policy: SortPolicy) -> tuple[int, str]:
    """Return (category, name) for method ordering under the resolved policy."""
    if name == '__new__':
        return (0, name)
    if name == '__init__':
        return (1, name)
    is_dunder = name.startswith('__') and name.endswith('__')
    if is_dunder:
        dunder_rank = 2 if policy.method_dunders == 'before-regular' else 5
        return (dunder_rank, name)
    rank = _CATEGORY_REGULAR
    if policy.method_visibility == 'public-first' and name.startswith('_'):
        rank += 1
    return (rank, name)


def _build_method_dep_graph(
    groups: list[list[MethodDefinition]],
    policy: SortPolicy,
) -> tuple[
    list[_GK],
    dict[_GK, list[MethodDefinition]],
    dict[_GK, int],
    dict[_GK, list[_GK]],
]:
    """Build the dependency graph for method groups."""
    group_keys: list[_GK] = []
    name_to_gkeys: dict[str, list[_GK]] = defaultdict(list)
    gkey_to_group: dict[_GK, list[MethodDefinition]] = {}
    gkey_to_semantic: dict[_GK, set[str]] = {}
    gkey_to_body_refs: dict[_GK, set[str]] = {}

    for gi, group in enumerate(groups):
        name = group[0].property_group or group[0].name
        pri, sname = _method_group_key(name, policy)
        gk = (pri, sname, gi)
        group_keys.append(gk)
        name_to_gkeys[name].append(gk)
        gkey_to_group[gk] = group
        member_names = {method.name for method in group}
        body_refs = {ref for method in group for ref in method.body_refs}
        semantic_deps = {dep for method in group for dep in method.semantic_deps}
        gkey_to_body_refs[gk] = body_refs - member_names
        gkey_to_semantic[gk] = semantic_deps - member_names

    def _edges_for(names: set[str], name_to_gkeys: dict[str, list[_GK]]) -> set[_GK]:
        return {gk for name in names for gk in name_to_gkeys.get(name, ())}

    semantic_adj = {
        gk: _edges_for(gkey_to_semantic[gk], name_to_gkeys) - {gk} for gk in group_keys
    }
    body_adj: dict[_GK, set[_GK]] = {}
    if policy.method_bodies == 'within-categories':
        body_adj = {
            gk: {
                target
                for target in _edges_for(gkey_to_body_refs[gk], name_to_gkeys)
                if target[0] == gk[0] and target != gk
            }
            for gk in group_keys
        }
    elif policy.method_bodies == 'across-categories':
        body_adj = {
            gk: _edges_for(gkey_to_body_refs[gk], name_to_gkeys) - {gk} for gk in group_keys
        }

    combined = {gk: semantic_adj.get(gk, set()) | body_adj.get(gk, set()) for gk in group_keys}
    for scc in find_cycles(combined):
        for gk in scc:
            if gk in body_adj:
                body_adj[gk] -= scc
    final = {gk: semantic_adj.get(gk, set()) | body_adj.get(gk, set()) for gk in group_keys}

    cycle_by_group = {group_key: cycle for cycle in find_cycles(final) for group_key in cycle}
    in_degree: dict[_GK, int] = {}
    reverse_deps: dict[_GK, list[_GK]] = defaultdict(list)

    for gk in group_keys:
        effective = final[gk] - cycle_by_group.get(gk, frozenset())
        in_degree[gk] = len(effective)
        for dep_gk in effective:
            reverse_deps[dep_gk].append(gk)

    return group_keys, gkey_to_group, in_degree, reverse_deps


def _method_index(m: MethodDefinition) -> int:
    """Return a method's stable source index."""
    return m.index


def _method_is_overload(m: MethodDefinition) -> bool:
    """Return whether a method is an overload declaration."""
    return m.is_overload


def _method_key(m: MethodDefinition) -> tuple[str, PropertyKind | None]:
    """Return the name and property kind that identify a method group."""
    return (m.name, m.property_kind)


def _pin_eager_groups(
    groups: list[list[MethodDefinition]],
    *,
    policy: SortPolicy,
) -> list[list[MethodDefinition]]:
    """Restore eager method groups to relative source order when required."""
    if policy.evaluation_order == 'allow-reordering':
        return groups
    positions = [index for index, group in enumerate(groups) if _group_has_eager_evaluation(group)]
    if len(positions) <= 1:
        return groups
    ordered = sorted(
        (groups[index] for index in positions),
        key=lambda group: min(method.index for method in group),
    )
    pinned = list(groups)
    for slot, group in zip(positions, ordered, strict=True):
        pinned[slot] = group
    return pinned


def _process_method_gap(
    raw_between: str,
    location: GapLocation,
    section: int,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
    *,
    statement_lines: frozenset[int],
) -> _MethodGap:
    """Classify one exact inter-method gap into owned and fixed source."""
    banners, banner_pattern = banner_settings
    gap = classify_gap(
        raw_between,
        location=location,
        section=section,
        banner_settings=(banners, banner_pattern),
        statement_lines=statement_lines,
    )
    return _MethodGap(
        gap.trailing,
        gap.separator,
        gap.leading,
        gap.boundaries,
        gap.boundary_layout,
        gap.next_section,
    )


def _property_sort_order(method: MethodDefinition) -> int:
    """Return sort priority within a property group: getter=0, setter=1, deleter=2."""
    if method.property_kind == 'getter':
        return 0
    if method.property_kind == 'setter':
        return 1
    return 2


def _group_property_methods(methods: list[MethodDefinition]) -> list[list[MethodDefinition]]:
    """Group related property methods and retain source tie-breaking."""
    property_groups: dict[str, list[MethodDefinition]] = defaultdict(list)
    non_property: list[MethodDefinition] = []

    for method in methods:
        if method.property_group:
            property_groups[method.property_group].append(method)
        else:
            non_property.append(method)

    result: list[list[MethodDefinition]] = []
    for group in property_groups.values():
        group.sort(key=lambda m: (_property_sort_order(m), m.index))
        result.append(group)
    result.extend([method] for method in non_property)
    return result


def _group_methods(methods: list[MethodDefinition]) -> list[list[MethodDefinition]]:
    """Group property triplets, then merge consecutive same-name groups."""
    groups = _group_property_methods(methods)
    groups.sort(key=lambda g: g[0].index)
    merged: list[list[MethodDefinition]] = []
    for group in groups:
        if merged:
            prev = merged[-1]
            same_name = (prev[0].property_group or prev[0].name) == (
                group[0].property_group or group[0].name
            )
            adjacent = group[0].index == prev[-1].index + 1
            if same_name and adjacent and prev[-1].safe_gap_after:
                prev.extend(group)
                continue
        merged.append(list(group))
    return merged


def _has_duplicate_method_names(
    methods: list[MethodDefinition],
    banners: BannerMode,
) -> bool:
    """Check for repeated method names across effective class-local groups."""
    if _has_duplicate_property_binders(methods):
        return True
    if banners == 'barriers':
        groups = [
            group
            for _, section_methods in itertools.groupby(methods, key=lambda m: m.section)
            for group in _group_methods(list(section_methods))
        ]
    else:
        groups = _group_methods(methods)
    return _has_duplicate_method_groups(groups)


def _order_methods_alphabetical(
    methods: list[MethodDefinition],
    *,
    policy: SortPolicy = _DEFAULT_SORT_POLICY,
) -> list[MethodDefinition]:
    """Order methods alphabetically within configured categories."""
    if not methods:
        return []

    groups = _group_methods(methods)

    def group_sort_key(group: list[MethodDefinition]) -> tuple[int, str]:
        name = group[0].property_group or group[0].name
        return _method_group_key(name, policy)

    groups.sort(key=group_sort_key)
    groups = _pin_eager_groups(groups, policy=policy)

    result: list[MethodDefinition] = []
    for group in groups:
        result.extend(group)
    return result


def _order_methods_by_dependency(
    methods: list[MethodDefinition],
    *,
    policy: SortPolicy = _DEFAULT_SORT_POLICY,
) -> list[MethodDefinition]:
    """Order methods by dependency with configured category tie-breaking."""
    if not methods:
        return []

    groups = _group_methods(methods)
    _, gkey_to_group, in_degree, reverse_deps = _build_method_dep_graph(groups, policy)

    def heap_key(gk: _GK) -> tuple[int, str, int] | tuple[int, int]:
        if policy.tie_break == 'source':
            return (gk[0], gkey_to_group[gk][0].index)
        return gk

    heap_to_gkey = {heap_key(gk): gk for gk in gkey_to_group}
    queue = [heap_key(gk) for gk, deg in in_degree.items() if deg == 0]
    heapq.heapify(queue)

    ordered_groups: list[list[MethodDefinition]] = []
    while queue:
        current = heap_to_gkey[heapq.heappop(queue)]
        ordered_groups.append(gkey_to_group[current])
        for dependent in reverse_deps.get(current, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                heapq.heappush(queue, heap_key(dependent))

    remaining = sorted(
        (gk for gk, deg in in_degree.items() if deg > 0),
        key=heap_key,
    )
    ordered_groups.extend(gkey_to_group[gk] for gk in remaining)

    result: list[MethodDefinition] = []
    for group in _pin_eager_groups(ordered_groups, policy=policy):
        result.extend(group)
    return result


def _order_methods(
    methods: list[MethodDefinition],
    *,
    order: SortScopeOrder,
    policy: SortPolicy,
) -> list[MethodDefinition]:
    """Order methods by the specified strategy, mirroring `order_definitions`."""
    if order == 'none':
        return list(methods)
    if order == 'alphabetical':
        return _order_methods_alphabetical(methods, policy=policy)
    return _order_methods_by_dependency(methods, policy=policy)


def _class_banner_constraints(
    parsed: _MethodParseResult,
    class_name: str,
    *,
    policy: SortPolicy,
) -> list[BannerConstraint]:
    """Collect class-local constraints under the resolved banner policy."""
    if policy.banners != 'barriers' or policy.method_order == 'none':
        return []
    unconstrained = _order_methods(
        parsed.methods,
        order=policy.method_order,
        policy=policy,
    )
    return collect_banner_constraints(
        parsed.methods,
        unconstrained,
        parsed.boundaries,
        class_name=class_name,
    )


def _order_methods_for_policy(
    methods: list[MethodDefinition],
    *,
    policy: SortPolicy,
) -> list[MethodDefinition]:
    """Order methods under the complete method and banner policy."""
    if policy.method_order == 'none':
        return list(methods)
    order_fn = partial(
        _order_methods,
        order=policy.method_order,
        policy=policy,
    )
    if policy.banners == 'barriers':
        return order_within_sections(methods, order_fn)
    return order_fn(methods)


def _reassemble_class(
    parsed: _MethodParseResult,
    new_order: Sequence[MethodDefinition],
) -> str:
    """Render methods in the requested positional class layout."""
    order = [method.index for method in new_order]
    pinned: dict[int, int] = {}
    position = 0
    for _, run_iter in itertools.groupby(
        new_order,
        key=lambda method: method.property_group or method.name,
    ):
        run = list(run_iter)
        source_order = sorted(run, key=_method_index)
        if len(run) > 1 and all(
            right.index == left.index + 1 and left.safe_gap_after
            for left, right in itertools.pairwise(source_order)
        ):
            for offset, left in enumerate(source_order[:-1]):
                pinned[position + offset] = left.index
        position += len(run)
    return render_layout(
        permute_separators(parsed.layout, order, pinned),
        parsed.blocks,
        order,
    )


def _record_method_layout(
    state: _MethodLayoutState,
    pending_leading: str,
    method: MethodDefinition,
    gap: _MethodGap,
    *,
    has_following: bool,
) -> None:
    """Record one method block and its ownership-boundary metadata."""
    block, ending = split_terminal_line_ending(pending_leading + method.source + gap.trailing)
    owns_trailing = bool(gap.trailing)
    separator = gap.separator
    if ending == '\r' and separator.startswith('\n'):
        ending, separator = '\n', '\r' + separator[1:]
    state.blocks[method.index] = block
    state.fixed_following.append(ending + separator)
    state.fixed_ownership.append(
        make_ownership_boundary(
            ending,
            separator,
            owns_trailing=owns_trailing,
        )
        if has_following
        else OwnershipBoundary(
            terminator_length=len(ending),
            has_candidate=False,
        )
    )
    if owns_trailing and has_following:
        state.guard_required.add(method.index)
    if pending_leading:
        state.guard_required_before.add(method.index)


def apply_class_replacements(
    source_lines: Iterable[str],
    replacements: Collection[tuple[int, int, str]],
) -> str | None:
    """Apply per-class rewrites to source lines, processed in reverse line order.

    Args:
        source_lines: Exact physical source lines.
        replacements: Inclusive class line ranges and replacement text.

    Returns:
        The rewritten source, or `None` when no replacements are supplied.
    """
    if not replacements:
        return None
    result_lines = list(source_lines)
    for start_line, end_line, new_class_source in sorted(
        replacements, key=lambda r: r[0], reverse=True
    ):
        new_class_lines = split_physical_lines(new_class_source, keepends=True)
        result_lines[start_line - 1 : end_line] = new_class_lines
    return ''.join(result_lines)


def match_classes(
    cur_classes: Iterable[ast.ClassDef],
    bl_classes: Iterable[ast.ClassDef],
) -> list[tuple[ast.ClassDef, ast.ClassDef | None]]:
    """Match classes from baseline and current by name (positional for duplicates).

    Args:
        cur_classes: Classes from the current source.
        bl_classes: Classes from the baseline source.

    Returns:
        Current classes paired with their positional baseline matches.
    """
    bl_by_name: dict[tuple[str, int], ast.ClassDef] = {}
    bl_name_seen: Counter[str] = Counter()
    for cls in bl_classes:
        bl_name_seen[cls.name] += 1
        bl_by_name[(cls.name, bl_name_seen[cls.name])] = cls

    cur_name_seen: Counter[str] = Counter()
    matched: list[tuple[ast.ClassDef, ast.ClassDef | None]] = []
    for cls in cur_classes:
        cur_name_seen[cls.name] += 1
        bl_cls = bl_by_name.get((cls.name, cur_name_seen[cls.name]))
        matched.append((cls, bl_cls))
    return matched


def parse_class_methods(
    cls: ast.ClassDef,
    cs: ClassSlice,
    *,
    banners: BannerMode = 'ignore',
    banner_pattern: str | None = None,
    collect_bodies: bool = True,
) -> _MethodParseResult:
    """Extract MethodDefinition objects from a class body.

    Args:
        cls: The class AST node.
        cs: The class source slice (lines, start_line, source).
        banners: Treatment of recognized class-local banners.
        banner_pattern: Optional custom banner regular expression.
        collect_bodies: Whether to collect method-body references. When false,
            each method's `body_refs` is empty; all other analysis is retained.

    Returns:
        Parsed methods, layout, and class-local banner metadata.
    """
    method_names = {
        m.name for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    methods: list[MethodDefinition] = []
    method_nodes = [m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

    bound_earlier: set[str] = set()
    for idx, func in enumerate(method_nodes):
        start = decorated_start_line(func)
        end = get_node_end_line(func)

        # Extract source lines (relative to class)
        source_lines = cs.lines[start - cs.start_line : end - cs.start_line + 1]
        source = ''.join(source_lines)

        # Detect property group and kind
        prop_group: str | None = None
        kind: PropertyKind | None = None
        if _is_property_decorator(func):
            prop_group = func.name
            kind = 'getter'
        else:
            info = _get_property_group_info(func)
            if info is not None:
                prop_group, kind = info

        body_refs = (
            collect_body_refs(func, method_names, cls.name) if collect_bodies else set[str]()
        )
        reads = _analyze_method_deps(func, method_names)
        annotation_refs = (analyze_annotation_refs(func) & method_names) - {func.name}

        methods.append(
            MethodDefinition(
                index=idx,
                name=func.name,
                start_line=start,
                end_line=end,
                source=source,
                semantic_deps=reads & bound_earlier,
                property_group=prop_group,
                property_kind=kind,
                is_overload=is_overload_decorated(func),
                annotation_refs=annotation_refs,
                body_refs=body_refs,
                eager_reads=reads,
                has_eager_evaluation=bool(
                    func.decorator_list
                    or func.args.defaults
                    or any(default is not None for default in func.args.kw_defaults)
                ),
            )
        )
        bound_earlier.add(func.name)

    if not methods:
        return _MethodParseResult(
            methods=[],
            boundaries=[],
            boundary_layout=[],
            layout=SourceLayout(cs.source, (), ''),
            blocks={},
        )

    initial_gap, prefix, pattern = _class_initial_layout(
        cls,
        cs,
        method_nodes[0],
        methods[0].start_line,
        (banners, banner_pattern),
    )
    boundaries: list[SectionBoundary] = list(initial_gap.boundaries)
    boundary_layout: list[BoundaryLayoutPart] = list(initial_gap.boundary_layout)
    layout_state = _MethodLayoutState({}, [], [], set(), set())
    pending_leading = initial_gap.leading
    section = initial_gap.next_section
    gap_ranges = tuple(
        (method.end_line + 1, methods[index + 1].start_line - 1)
        for index, method in enumerate(methods[:-1])
    )
    statement_lines_by_gap = statement_owned_lines_by_gap(
        cls.body,
        gap_ranges,
        movable_types=(ast.AsyncFunctionDef, ast.FunctionDef),
    )
    for index, method in enumerate(methods):
        method.section = section
        if index < len(methods) - 1:
            next_method = methods[index + 1]
            gap_start = method.end_line - cs.start_line + 1
            gap_end = next_method.start_line - cs.start_line
            raw_between = ''.join(cs.lines[gap_start:gap_end])
            method.safe_gap_after = is_safe_trailing(raw_between)
            method_line = cs.lines[method.start_line - cs.start_line]
            scope_indent = _line_indent(method_line)
            gap = _process_method_gap(
                raw_between,
                GapLocation(method.end_line + 1, scope_indent),
                section,
                (banners, pattern),
                statement_lines=statement_lines_by_gap[index],
            )
        else:
            gap = _MethodGap('', '', '', (), (), section)

        _record_method_layout(
            layout_state,
            pending_leading,
            method,
            gap,
            has_following=index < len(methods) - 1,
        )
        boundaries.extend(gap.boundaries)
        boundary_layout.extend(gap.boundary_layout)
        pending_leading = gap.leading
        section = gap.next_section

    layout = SourceLayout(
        prefix=prefix,
        separators=tuple(layout_state.fixed_following[:-1]),
        suffix=layout_state.fixed_following[-1]
        + ''.join(cs.lines[methods[-1].end_line - cs.start_line + 1 :]),
        ownership=tuple(layout_state.fixed_ownership[:-1]),
        guard_required=frozenset(layout_state.guard_required),
        guard_required_before=frozenset(layout_state.guard_required_before),
    )
    return _MethodParseResult(methods, boundaries, boundary_layout, layout, layout_state.blocks)


def _sort_class_methods(
    cls: ast.ClassDef,
    class_source: str,
    class_start: int,
    *,
    policy: SortPolicy,
) -> _MethodProcessResult:
    """Sort methods within a class and return new class source."""
    class_lines = split_physical_lines(class_source, keepends=True)
    cs = ClassSlice(lines=class_lines, start_line=class_start)
    parsed = parse_class_methods(
        cls,
        cs,
        banners=policy.banners,
        banner_pattern=policy.banner_pattern,
        collect_bodies=policy.method_order == 'dependency' and policy.method_bodies != 'ignore',
    )
    methods = parsed.methods

    if len(methods) < MIN_METHODS_FOR_SORT and not parsed.boundaries:
        return _MethodProcessResult(None, None, [], [], [])

    constraints = _class_banner_constraints(parsed, cls.name, policy=policy)
    if policy.method_order != 'none' and _has_duplicate_method_names(methods, policy.banners):
        return _MethodProcessResult(
            None,
            None,
            parsed.boundaries,
            parsed.boundary_layout,
            constraints,
            blocker='duplicates',
        )

    new_order = _order_methods_for_policy(methods, policy=policy)

    # Check if order actually changed
    old_names = [(m.name, m.index) for m in methods]
    new_names = [(m.name, m.index) for m in new_order]
    order_changed = old_names != new_names
    method_blocker: Blocker | None = None
    if order_changed and _has_unsafe_method_trailing(methods, cs):
        new_order = list(methods)
        order_changed = False
        method_blocker = 'interstitial'

    boundaries_removed = policy.banners == 'strip' and bool(parsed.boundaries)
    if not order_changed and not boundaries_removed:
        return _MethodProcessResult(
            None,
            None,
            parsed.boundaries,
            parsed.boundary_layout,
            constraints,
            blocker=method_blocker,
        )
    if not order_changed:
        return _MethodProcessResult(
            remove_boundary_lines(
                class_source,
                parsed.boundaries,
                source_start_line=class_start,
            ),
            None,
            parsed.boundaries,
            parsed.boundary_layout,
            constraints,
            blocker=method_blocker,
        )

    new_class_source = _reassemble_class(parsed, new_order)
    info = MethodSortInfo(
        class_name=cls.name,
        original_order=list(methods),
        new_order=list(new_order),
    )
    return _MethodProcessResult(
        new_class_source,
        info,
        parsed.boundaries,
        parsed.boundary_layout,
        constraints,
    )


def collect_method_coordinates(
    source: str,
    tree: ast.Module,
    *,
    banners: BannerMode,
    banner_pattern: str | None,
    collect_bodies: bool = True,
) -> dict[_ClassIdentity, _MethodParseResult]:
    """Snapshot method and boundary coordinates from the original source.

    Args:
        source: Exact Python source.
        tree: Parsed source module.
        banners: Treatment of recognized class-local banners.
        banner_pattern: Optional custom banner regular expression.
        collect_bodies: Whether to include method-body references in the
            snapshot. When false, `body_refs` is empty on each parsed method.

    Returns:
        Parsed method metadata keyed by class identity.
    """
    lines = split_physical_lines(source, keepends=True)
    seen: Counter[str] = Counter()
    result: dict[_ClassIdentity, _MethodParseResult] = {}
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        seen[class_node.name] += 1
        start = class_node.lineno
        end = get_node_end_line(class_node)
        class_slice = ClassSlice(lines=lines[start - 1 : end], start_line=start)
        result[(class_node.name, seen[class_node.name])] = parse_class_methods(
            class_node,
            class_slice,
            banners=banners,
            banner_pattern=banner_pattern,
            collect_bodies=collect_bodies,
        )
    return result


def collect_method_banner_constraints(
    source: str,
    tree: ast.Module,
    *,
    policy: SortPolicy,
) -> list[BannerConstraint]:
    """Collect class-local banner constraints from original source coordinates.

    Args:
        source: Original source text.
        tree: Parsed source tree corresponding to `source`.
        policy: Resolved ordering and banner policy.

    Returns:
        Method constraints in source class and boundary order.
    """
    if policy.banners != 'barriers' or policy.method_order == 'none':
        return []
    coordinates = collect_method_coordinates(
        source,
        tree,
        banners=policy.banners,
        banner_pattern=policy.banner_pattern,
        collect_bodies=policy.method_order == 'dependency' and policy.method_bodies != 'ignore',
    )
    constraints: list[BannerConstraint] = []
    for (class_name, _), parsed in coordinates.items():
        constraints.extend(_class_banner_constraints(parsed, class_name, policy=policy))
    return constraints


def per_class_incremental_sort(  # noqa: PLR0911, PLR0913
    cur_cls: ast.ClassDef,
    bl_cls: ast.ClassDef | None,
    cur_parsed: _MethodParseResult,
    bl_parsed: _MethodParseResult,
    cur_cs: ClassSlice,
    *,
    policy: SortPolicy,
    original_parse: _MethodParseResult,
) -> IncrementalClassResult:
    """Incrementally sort methods within one class.

    Args:
        cur_cls: Class node from the current source.
        bl_cls: Matched baseline class node, when present.
        cur_parsed: Parsed current-class methods and layout.
        bl_parsed: Parsed baseline-class methods and layout.
        cur_cs: Current class source slice.
        policy: Concrete ordering policy.
        original_parse: Original current-class method coordinates.

    Returns:
        The class source and method movement when sorting changes them, with a
        blocker when sorting cannot proceed. The result also retains the
        original method layout and banner constraints. `had_new_methods` is
        true only when eligible incremental processing finds new methods in an
        existing class; it stays false for a new class or an early exit.

    Raises:
        LayoutPreservationError: If the requested method order cannot preserve
            layout and comment ownership. The caller must refuse the whole
            file.
    """
    # New class (no baseline match): delegate to full method sort.
    # had_new_methods stays False -- new classes are not "incremental".
    if bl_cls is None:
        result = _sort_class_methods(
            cur_cls,
            cur_cs.source,
            cur_cs.start_line,
            policy=policy,
        )
        constraints = _class_banner_constraints(original_parse, cur_cls.name, policy=policy)
        return IncrementalClassResult(
            result.new_source,
            result.blocker,
            result.sort_info,
            had_new_methods=False,
            boundary_layout=tuple(original_parse.boundary_layout),
            banner_constraints=tuple(constraints),
        )

    cur_methods = cur_parsed.methods
    bl_methods = bl_parsed.methods

    if policy.method_order == 'none':
        return _incremental_method_no_merge(
            cur_parsed,
            cur_cs,
            original_parse,
            [],
            banners=policy.banners,
        )

    if len(cur_methods) < MIN_METHODS_FOR_SORT:
        if not cur_parsed.boundaries:
            return IncrementalClassResult(
                None,
                None,
                None,
                had_new_methods=False,
                boundary_layout=tuple(original_parse.boundary_layout),
            )
        return _incremental_method_no_merge(
            cur_parsed,
            cur_cs,
            original_parse,
            [],
            banners=policy.banners,
        )

    if _has_duplicate_method_names(cur_methods, policy.banners):
        return IncrementalClassResult(
            None,
            'duplicates',
            None,
            had_new_methods=False,
            boundary_layout=tuple(original_parse.boundary_layout),
        )

    order_fn = partial(
        _order_methods_for_policy,
        policy=policy,
    )
    constraints = _class_banner_constraints(original_parse, cur_cls.name, policy=policy)

    new_methods = find_new_items(bl_methods, cur_methods, _method_key, _method_is_overload)

    if new_methods and _has_unsafe_method_trailing(cur_methods, cur_cs):
        return IncrementalClassResult(
            None,
            'interstitial',
            None,
            had_new_methods=True,
            boundary_layout=tuple(original_parse.boundary_layout),
            banner_constraints=tuple(constraints),
        )

    merged, blocker = incremental_merge(
        cur_methods,
        bl_methods,
        new_methods,
        order_fn,
        _method_key,
        is_overload_fn=_method_is_overload,
        index_fn=_method_index,
        min_baseline=MIN_METHODS_FOR_SORT,
    )
    if blocker is not None:
        return IncrementalClassResult(
            None,
            blocker,
            None,
            had_new_methods=bool(new_methods),
            boundary_layout=tuple(original_parse.boundary_layout),
            banner_constraints=tuple(constraints),
        )

    if not new_methods:
        return _incremental_method_no_merge(
            cur_parsed,
            cur_cs,
            original_parse,
            constraints,
            banners=policy.banners,
        )

    # Check if order actually changed
    if [m.index for m in merged] == [m.index for m in cur_methods]:
        return _incremental_method_no_merge(
            cur_parsed,
            cur_cs,
            original_parse,
            constraints,
            banners=policy.banners,
        )._replace(
            had_new_methods=True,
        )

    new_cls_source = _reassemble_class(cur_parsed, merged)
    info = MethodSortInfo(
        class_name=cur_cls.name,
        original_order=list(cur_methods),
        new_order=list(merged),
    )
    return IncrementalClassResult(
        new_cls_source,
        None,
        info,
        had_new_methods=True,
        boundary_layout=tuple(original_parse.boundary_layout),
        banner_constraints=tuple(constraints),
    )


def sort_file_methods(
    source: str,
    tree: ast.Module,
    *,
    policy: SortPolicy,
) -> _FileMethodSortResult:
    """Sort methods in every top-level class.

    Args:
        source: Full Python source text.
        tree: Parsed source AST corresponding to `source`.
        policy: Method ordering and class-local banner treatment.

    Returns:
        Rewritten candidate source and method processing metadata.

    Raises:
        LayoutPreservationError: If a requested method order cannot preserve
            layout and comment ownership. The caller must refuse the whole
            file.
    """
    if policy.method_order == 'none' and policy.banners != 'strip':
        return _FileMethodSortResult(source, (), (), ())

    source_lines = split_physical_lines(source, keepends=True)
    replacements: list[tuple[int, int, str]] = []
    method_sorts: list[MethodSortInfo] = []
    constraints: list[BannerConstraint] = []
    class_blockers: list[ClassBlocker] = []
    class_occurrences: Counter[str] = Counter()

    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        class_occurrences[cls.name] += 1
        start = decorated_start_line(cls)
        end = get_node_end_line(cls)
        class_source = ''.join(source_lines[start - 1 : end])
        result = _sort_class_methods(
            cls,
            class_source,
            start,
            policy=policy,
        )
        if result.new_source is not None:
            replacements.append((start, end, result.new_source))
        if result.sort_info is not None:
            method_sorts.append(result.sort_info)
        constraints.extend(result.banner_constraints)
        if result.blocker is not None:
            class_blockers.append(
                ClassBlocker(
                    class_name=cls.name,
                    occurrence=class_occurrences[cls.name],
                    blocker=result.blocker,
                    message=blocker_message(result.blocker),
                )
            )

    candidate_source = apply_class_replacements(source_lines, replacements)
    return _FileMethodSortResult(
        source if candidate_source is None else candidate_source,
        tuple(method_sorts),
        tuple(constraints),
        tuple(class_blockers),
    )
