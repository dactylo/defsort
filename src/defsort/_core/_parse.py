"""Parse source into preamble, definitions, and postamble."""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple

from defsort._core._analysis import (
    ENCODING_LINE_LIMIT,
    ENCODING_PATTERN,
    analyze_annotation_refs,
    analyze_dependencies,
    analyze_eager_refs,
    decorated_start_line,
    extract_postamble_names,
    get_bound_names,
    get_node_end_line,
    is_constant_name,
    is_overload_decorated,
)
from defsort._core._banners import (
    GapLocation,
    classify_gap,
    classify_initial_gap,
    compile_banner_pattern,
    statement_owned_lines_by_gap,
)
from defsort._core._hoist import (
    extract_hoistable_constants,
    get_safe_to_hoist_by_gap,
    remove_hoisted_lines,
)
from defsort._core._layout import (
    OwnershipBoundary,
    SourceLayout,
    make_ownership_boundary,
    split_item_gap,
    split_terminal_line_ending,
    split_trailing_owned,
)
from defsort._core._safety import is_safe_trailing
from defsort._source import join_physical, split_physical_lines
from defsort._types import (
    BannerMode,
    BoundaryLayoutPart,
    Definition,
    DefinitionKind,
    SectionBoundary,
)


@dataclass(frozen=True, slots=True)
class _ParseContext:
    """Fixed inputs shared by one `parse_definitions` invocation."""

    definition_nodes: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]
    definition_starts: list[int]
    source_lines: list[str]
    all_definition_names: set[str]
    banner_settings: tuple[BannerMode, re.Pattern[str] | None]


@dataclass(frozen=True, slots=True)
class _ParseResult:
    """Top-level source structure and positional layout."""

    definitions: list[Definition]
    postamble: str
    hoistable: str
    boundaries: list[SectionBoundary]
    boundary_layout: list[BoundaryLayoutPart]
    layout: SourceLayout
    blocks: dict[int, str]


class _ProcessedDefinition(NamedTuple):
    """Result of processing a single definition in `parse_definitions`."""

    definition: Definition
    pending_leading: str
    hoisted_lines: list[str]
    boundary_layout: tuple[BoundaryLayoutPart, ...]
    next_section: int
    block: str
    following: str
    ownership: OwnershipBoundary
    requires_guard: bool


class _ProcessedGap(NamedTuple):
    """Owned and fixed results of one inter-definition gap."""

    trailing: str
    separator: str
    next_leading: str
    boundary_layout: tuple[BoundaryLayoutPart, ...]
    boundary_unsafe: bool
    next_section: int


@dataclass(slots=True)
class _ScopeParseState:
    """Invocation-local top-level binding state."""

    body_cursor: int
    earlier_definition_names: set[str]
    preamble_bound: set[str]
    visible_classes: dict[str, ast.ClassDef]


def _advance_scope_state(
    body: list[ast.stmt],
    current: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    state: _ScopeParseState,
) -> None:
    """Advance top-level binding state to the current definition."""
    while state.body_cursor < len(body) and body[state.body_cursor] is not current:
        preceding = body[state.body_cursor]
        if isinstance(preceding, ast.ClassDef):
            state.earlier_definition_names.add(preceding.name)
            state.visible_classes[preceding.name] = preceding
        elif isinstance(preceding, (ast.AsyncFunctionDef, ast.FunctionDef)):
            state.earlier_definition_names.add(preceding.name)
            state.visible_classes.pop(preceding.name, None)
        else:
            bound_names = get_bound_names(preceding)
            state.preamble_bound.update(bound_names)
            for name in bound_names:
                state.visible_classes.pop(name, None)
        state.body_cursor += 1


def _empty_parse_result(source: str) -> _ParseResult:
    """Return an empty parse result that preserves the original source."""
    return _ParseResult(
        definitions=[],
        postamble='',
        hoistable='',
        boundaries=[],
        boundary_layout=[],
        layout=SourceLayout(source, (), ''),
        blocks={},
    )


def _hoist_candidate_names(raw_between: str) -> set[str]:
    """Return constant names that may be hoist candidates in one gap."""
    try:
        trail_tree = ast.parse(raw_between)
    except SyntaxError:
        return set()

    candidates: set[str] = set()
    for node in trail_tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and is_constant_name(target.id):
                candidates.add(target.id)
    return candidates


def _hoist_gap(
    raw_between: str,
    *,
    safe_names: set[str],
) -> tuple[str, list[str], set[int]]:
    """Hoist safe constants from one exact raw inter-definition gap."""
    if not safe_names:
        return raw_between, [], set()

    try:
        trail_tree = ast.parse(raw_between)
    except SyntaxError:
        return raw_between, [], set()

    hoisted, hoisted_lines = extract_hoistable_constants(
        raw_between, safe_names, _trail_tree=trail_tree
    )

    if not hoisted:
        return raw_between, [], set()

    return remove_hoisted_lines(raw_between, hoisted_lines), hoisted, hoisted_lines


def _is_all_reference(node: ast.AST) -> bool:
    """Return whether a node directly references `__all__`."""
    return isinstance(node, ast.Name) and node.id == '__all__'


def _is_rooted_in_all(node: ast.AST) -> bool:
    """Return whether an attribute or subscription is rooted in `__all__`."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return _is_all_reference(node)


def _is_string_sequence(node: ast.AST) -> bool:
    """Return whether a node is a literal list or tuple of strings."""
    return isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
    )


def _process_gap(
    raw_between: str,
    content_start_line: int,
    section: int,
    *,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
    statement_lines: frozenset[int],
) -> _ProcessedGap:
    """Classify one exact gap into owned and fixed source."""
    gap = classify_gap(
        raw_between,
        location=GapLocation(content_start_line, ''),
        section=section,
        banner_settings=banner_settings,
        statement_lines=statement_lines,
    )
    return _ProcessedGap(
        trailing=gap.trailing,
        separator=gap.separator,
        next_leading=gap.leading,
        boundary_layout=gap.boundary_layout,
        boundary_unsafe=bool(gap.fixed_ordinary.strip())
        and not is_safe_trailing(gap.fixed_ordinary),
        next_section=gap.next_section,
    )


def _hoistable_gap_prefix(
    raw_between: str,
    content_start_line: int,
    section: int,
    *,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
    statement_lines: frozenset[int],
) -> tuple[str, int]:
    """Return the hoistable prefix and following section for one gap."""
    prefix = split_item_gap(raw_between).trailing
    if banner_settings[0] != 'barriers':
        return prefix, section

    original_gap = _process_gap(
        raw_between,
        content_start_line,
        section,
        banner_settings=banner_settings,
        statement_lines=statement_lines,
    )
    if original_gap.boundary_layout:
        prefix = original_gap.trailing
    return prefix, original_gap.next_section


def _collect_hoist_candidates_by_gap(
    def_nodes: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef],
    lines: list[str],
    statement_lines_by_gap: tuple[frozenset[int], ...],
    *,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
    section: int,
) -> dict[int, set[str]]:
    """Collect candidate names from each exact hoistable gap prefix."""
    candidates_by_gap: dict[int, set[str]] = {}
    candidate_section = section
    def_starts = [decorated_start_line(node) for node in def_nodes]
    for i, statement_lines in enumerate(statement_lines_by_gap):
        trailing_start = get_node_end_line(def_nodes[i])
        trailing_end = def_starts[i + 1] - 1
        raw_between = ''.join(lines[trailing_start:trailing_end])
        hoist_prefix, candidate_section = _hoistable_gap_prefix(
            raw_between,
            trailing_start + 1,
            candidate_section,
            banner_settings=banner_settings,
            statement_lines=statement_lines,
        )
        candidates = _hoist_candidate_names(hoist_prefix)
        if candidates:
            candidates_by_gap[i] = candidates
    return candidates_by_gap


def _process_trailing(trailing: str) -> bool:
    """Return whether owned trailing content contains interstitial code."""
    return bool(trailing.strip()) and not is_safe_trailing(trailing)


def _protected_header_end_line(lines: list[str]) -> int:
    """Return the last protected shebang or encoding line."""
    end = 1 if lines and lines[0].startswith('#!') else 0
    for index, line in enumerate(lines[:ENCODING_LINE_LIMIT], start=1):
        if ENCODING_PATTERN.match(line) is not None:
            end = max(end, index)
    return end


def _module_structural_end_line(
    tree: ast.Module,
    lines: list[str],
    first_definition: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    """Return the last physical line of the module structural prefix."""
    first_start = decorated_start_line(first_definition)
    end = min(_protected_header_end_line(lines), first_start - 1)
    for node in tree.body:
        if node is first_definition:
            break
        end = max(end, get_node_end_line(node))
    return end


def _restore_line(line: int, removed_sorted: list[int]) -> int:
    """Map a reduced gap-relative line back to its original coordinate."""
    for removed in removed_sorted:
        if removed > line:
            break
        line += 1
    return line


def _process_definition_gap(
    index: int,
    context: _ParseContext,
    *,
    statement_lines: frozenset[int],
    section: int,
    safe_hoist_names: set[str],
) -> tuple[_ProcessedGap, list[str]]:
    """Process the exact gap following one definition."""
    if index == len(context.definition_nodes) - 1:
        return (
            _ProcessedGap(
                trailing='',
                separator='',
                next_leading='',
                boundary_layout=(),
                boundary_unsafe=False,
                next_section=section,
            ),
            [],
        )

    trailing_start = get_node_end_line(context.definition_nodes[index])
    trailing_end = context.definition_starts[index + 1] - 1
    raw_between = ''.join(context.source_lines[trailing_start:trailing_end])
    hoist_prefix = split_item_gap(raw_between).trailing
    if safe_hoist_names:
        hoist_prefix, _next_section = _hoistable_gap_prefix(
            raw_between,
            trailing_start + 1,
            section,
            banner_settings=context.banner_settings,
            statement_lines=statement_lines,
        )
    hoist_prefix_end = len(hoist_prefix)
    reduced_prefix, hoisted_lines, hoist_removed = _hoist_gap(
        hoist_prefix,
        safe_names=safe_hoist_names,
    )
    raw_between = join_physical(
        reduced_prefix,
        raw_between[hoist_prefix_end:],
    )
    removed_sorted: list[int] = []
    if hoist_removed:
        removed_sorted = sorted(hoist_removed)
        statement_lines = frozenset(
            line - sum(removed <= line for removed in removed_sorted)
            for line in statement_lines
            if line + 1 not in hoist_removed
        )
    gap = _process_gap(
        raw_between,
        trailing_start + 1,
        section,
        banner_settings=context.banner_settings,
        statement_lines=statement_lines,
    )
    if not hoist_removed:
        return gap, hoisted_lines

    restored_layout: list[BoundaryLayoutPart] = []
    for part in gap.boundary_layout:
        if isinstance(part, str):
            restored_layout.append(part)
            continue
        restored_layout.append(
            SectionBoundary(
                section=part.section,
                start_line=trailing_start
                + _restore_line(part.start_line - trailing_start, removed_sorted),
                end_line=trailing_start
                + _restore_line(part.end_line - trailing_start, removed_sorted),
                raw=part.raw,
                text=part.text,
            )
        )
    return (
        _ProcessedGap(
            trailing=gap.trailing,
            separator=gap.separator,
            next_leading=gap.next_leading,
            boundary_layout=tuple(restored_layout),
            boundary_unsafe=gap.boundary_unsafe,
            next_section=gap.next_section,
        ),
        hoisted_lines,
    )


def _process_definition(  # noqa: PLR0913
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    i: int,
    context: _ParseContext,
    scope_state: _ScopeParseState,
    *,
    statement_lines: frozenset[int],
    pending_leading: str,
    section: int,
    safe_hoist_names: set[str],
) -> _ProcessedDefinition:
    """Process a single definition from a `parse_definitions` iteration."""
    start = context.definition_starts[i]
    end = get_node_end_line(node)

    kind: DefinitionKind = 'class' if isinstance(node, ast.ClassDef) else 'function'
    def_source = ''.join(context.source_lines[start - 1 : end])
    gap, hoisted_lines = _process_definition_gap(
        i,
        context,
        statement_lines=statement_lines,
        section=section,
        safe_hoist_names=safe_hoist_names,
    )

    trailing = gap.trailing
    had_interstitial = _process_trailing(trailing)
    owned_trailing, exposed_separator = split_trailing_owned(trailing)
    separator = exposed_separator + gap.separator

    had_interstitial = had_interstitial or gap.boundary_unsafe

    eager_refs = analyze_eager_refs(
        node,
        module_classes=scope_state.visible_classes,
        module_shadowed=frozenset(
            scope_state.preamble_bound | scope_state.earlier_definition_names
        ),
    )
    deps = analyze_dependencies(
        node,
        context.all_definition_names,
        eager_refs=eager_refs,
        preamble_shadowed=(scope_state.preamble_bound - scope_state.earlier_definition_names),
    )
    ann_deps = analyze_annotation_refs(node)
    has_eager_evaluation = bool(node.decorator_list or eager_refs)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        has_eager_evaluation = bool(
            has_eager_evaluation
            or node.args.defaults
            or any(default is not None for default in node.args.kw_defaults)
        )

    definition = Definition(
        index=i,
        name=node.name,
        start_line=start,
        end_line=end,
        kind=kind,
        source=def_source,
        trailing=owned_trailing,
        leading=pending_leading,
        dependencies=deps,
        annotation_refs=ann_deps,
        had_interstitial_code=had_interstitial,
        is_overload=is_overload_decorated(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        else False,
        section=section,
        eager_refs=eager_refs,
        has_eager_evaluation=has_eager_evaluation,
    )
    block, ending = split_terminal_line_ending(pending_leading + def_source + owned_trailing)
    owns_trailing = bool(owned_trailing)
    if ending == '\r' and separator.startswith('\n'):
        ending, separator = '\n', '\r' + separator[1:]
    following = ending + separator
    ownership = (
        make_ownership_boundary(
            ending,
            separator,
            owns_trailing=owns_trailing,
        )
        if i < len(context.definition_nodes) - 1
        else OwnershipBoundary(
            terminator_length=len(ending),
            has_candidate=False,
        )
    )
    return _ProcessedDefinition(
        definition=definition,
        pending_leading=gap.next_leading,
        hoisted_lines=list(hoisted_lines) if hoisted_lines else [],
        boundary_layout=gap.boundary_layout,
        next_section=gap.next_section,
        block=block,
        following=following,
        ownership=ownership,
        requires_guard=owns_trailing,
    )


def _target_contains_all(node: ast.AST) -> bool:
    """Return whether an assignment target contains `__all__`."""
    if _is_rooted_in_all(node):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_target_contains_all(item) for item in node.elts)
    if isinstance(node, ast.Starred):
        return _target_contains_all(node.value)
    return False


def _statement_mutates_all(statement: ast.stmt) -> bool:
    """Return whether a statement may mutate `__all__`."""
    for node in ast.walk(statement):
        if isinstance(node, (ast.AugAssign, ast.Delete)):
            targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
            if any(_target_contains_all(target) for target in targets):
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_rooted_in_all(node.func.value)
        ):
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_target_contains_all(target) for target in targets):
                return True
    return False


def parse_definitions(  # noqa: PLR0913
    source: str,
    *,
    banners: BannerMode = 'ignore',
    banner_pattern: str | None = None,
    hoist: bool = False,
    tree: ast.Module | None = None,
    candidate_positions: Mapping[int, int] | None = None,
) -> _ParseResult:
    """Parse source into preamble, definitions, and postamble.

    Args:
        source: The Python source code.
        banners: Treatment of recognized section banners.
        banner_pattern: Optional custom banner regular expression from the
            supported bounded subset.
        hoist: Enable constant hoisting from trailing content.
        tree: Optional pre-parsed AST that must correspond to `source`.
        candidate_positions: Proposed final positions by source occurrence, or
            `None` to use source positions for hoist eligibility.

    Returns:
        Parsed source, including fixed boundary metadata. When parsing raises
        `SyntaxError` or no top-level definitions exist, definitions are empty
        and the layout prefix preserves the entire original source.

    Raises:
        TypeError: If an active custom banner pattern is neither a string nor
            `None`.
        ValueError: If an active custom banner pattern does not compile or uses
            unsupported syntax.
    """
    if tree is None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return _empty_parse_result(source)

    # Find top-level definitions
    def_nodes: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    if not def_nodes:
        return _empty_parse_result(source)

    lines = split_physical_lines(source, keepends=True)
    all_names = {node.name for node in def_nodes}

    def_starts = [decorated_start_line(node) for node in def_nodes]
    pattern = compile_banner_pattern(banner_pattern) if banners != 'ignore' else None
    structural_end = _module_structural_end_line(tree, lines, def_nodes[0])
    preamble = ''.join(lines[:structural_end])
    initial_raw = ''.join(lines[structural_end : def_starts[0] - 1])
    initial_gap = classify_initial_gap(
        initial_raw,
        content_start_line=structural_end + 1,
        scope_indent='',
        banner_settings=(banners, pattern),
    )

    # Process each definition
    definitions: list[Definition] = []
    blocks: dict[int, str] = {}
    fixed_following: list[str] = []
    fixed_ownership: list[OwnershipBoundary] = []
    guard_required: set[int] = set()
    all_hoisted: list[str] = []
    boundaries: list[SectionBoundary] = list(initial_gap.boundaries)
    boundary_layout: list[BoundaryLayoutPart] = list(initial_gap.boundary_layout)
    pending_leading = initial_gap.leading
    section = initial_gap.next_section

    gap_ranges = tuple(
        (get_node_end_line(node) + 1, def_starts[i + 1] - 1)
        for i, node in enumerate(def_nodes[:-1])
    )
    statement_lines_by_gap = statement_owned_lines_by_gap(
        tree.body,
        gap_ranges,
        movable_types=(ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
    )

    candidates_by_gap = (
        _collect_hoist_candidates_by_gap(
            def_nodes,
            lines,
            statement_lines_by_gap,
            banner_settings=(banners, pattern),
            section=section,
        )
        if hoist
        else {}
    )
    safe_names_by_gap = get_safe_to_hoist_by_gap(
        tree,
        candidates_by_gap,
        candidate_positions=candidate_positions,
    )

    context = _ParseContext(
        definition_nodes=def_nodes,
        definition_starts=def_starts,
        source_lines=lines,
        all_definition_names=all_names,
        banner_settings=(banners, pattern),
    )
    scope_state = _ScopeParseState(0, set(), set(), {})
    for i, node in enumerate(def_nodes):
        _advance_scope_state(tree.body, node, scope_state)
        processed = _process_definition(
            node,
            i,
            context,
            scope_state,
            statement_lines=statement_lines_by_gap[i]
            if i < len(statement_lines_by_gap)
            else frozenset(),
            pending_leading=pending_leading,
            section=section,
            safe_hoist_names=safe_names_by_gap.get(i, set()),
        )
        definitions.append(processed.definition)
        blocks[processed.definition.index] = processed.block
        fixed_following.append(processed.following)
        fixed_ownership.append(processed.ownership)
        if processed.requires_guard and i < len(def_nodes) - 1:
            guard_required.add(processed.definition.index)
        if processed.hoisted_lines:
            all_hoisted.extend(processed.hoisted_lines)
        for part in processed.boundary_layout:
            boundary_layout.append(part)
            if isinstance(part, SectionBoundary):
                boundaries.append(part)
        pending_leading = processed.pending_leading
        section = processed.next_section

    # Postamble: everything after the last definition
    postamble_start = get_node_end_line(def_nodes[-1])
    postamble = ''.join(lines[postamble_start:])

    hoistable_str = ''.join(all_hoisted)
    layout = SourceLayout(
        prefix=(preamble + initial_gap.trailing + hoistable_str + initial_gap.separator),
        separators=tuple(fixed_following[:-1]),
        suffix=fixed_following[-1] + postamble,
        ownership=tuple(fixed_ownership[:-1]),
        guard_required=frozenset(guard_required),
        guard_required_before=frozenset(
            definition.index for definition in definitions if definition.leading
        ),
    )

    return _ParseResult(
        definitions=definitions,
        postamble=postamble,
        hoistable=hoistable_str,
        boundaries=boundaries,
        boundary_layout=boundary_layout,
        layout=layout,
        blocks=blocks,
    )


def parse_postamble_names(postamble: str) -> set[str]:
    """Parse postamble source and extract names it binds or deletes.

    Args:
        postamble: Python source after the final sortable definition.

    Returns:
        Names bound or deleted by postamble statements.
    """
    stmts = ast.parse(postamble).body if postamble.strip() else []
    return extract_postamble_names(stmts)


def postamble_has_dynamic_all(postamble: str) -> bool:
    """Return whether postamble `__all__` construction can observe order.

    Args:
        postamble: Python source after the final sortable definition.

    Returns:
        `True` when `__all__` is constructed or mutated dynamically.
    """
    statements = ast.parse(postamble).body if postamble.strip() else []
    static_assignment_seen = False
    for statement in statements:
        if isinstance(statement, ast.Assign) and any(
            _is_all_reference(target) for target in statement.targets
        ):
            if (
                static_assignment_seen
                or len(statement.targets) != 1
                or not _is_all_reference(statement.targets[0])
                or not _is_string_sequence(statement.value)
            ):
                return True
            static_assignment_seen = True
            continue
        if isinstance(statement, ast.AnnAssign) and _is_all_reference(statement.target):
            if (
                static_assignment_seen
                or statement.value is None
                or not _is_string_sequence(statement.value)
            ):
                return True
            static_assignment_seen = True
            continue
        if _statement_mutates_all(statement):
            return True
    return False
