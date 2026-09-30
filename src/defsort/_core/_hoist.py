"""Constant hoisting: move literal-only UPPER_SNAKE_CASE assignments to preamble."""

import ast
from collections import Counter
from collections.abc import Mapping
from itertools import takewhile

from defsort._core._analysis import (
    collect_const_def_lines,
    collect_name_references,
    get_bound_names,
    is_constant_name,
    iter_child_nodes,
)
from defsort._core._safety import is_safe_trailing
from defsort._source import (
    is_layout_blank,
    join_physical,
    join_physical_fragments,
    split_physical_lines,
)


def _count_constant_bindings(tree: ast.AST, candidates: set[str]) -> Counter[str]:
    """Count candidate bindings in the entire AST."""
    counts: Counter[str] = Counter()
    stack: list[ast.AST] = [tree]
    while stack:
        current = stack.pop()
        counts.update(name for name in get_bound_names(current) if name in candidates)
        stack.extend(iter_child_nodes(current))
    return counts


def _is_literal_only(node: ast.AST) -> bool:
    """Return whether an AST node contains only hoist-safe literals."""
    allowed_types = (
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Set,
        ast.Dict,
        ast.Load,
        ast.UnaryOp,
        ast.UAdd,
        ast.USub,
        ast.Invert,
        ast.Not,
    )
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, allowed_types):
            return False
        stack.extend(iter_child_nodes(cur))
    return True


def _is_hoistable_assign(node: ast.stmt, safe_names: set[str]) -> bool:
    """Return whether an AST statement is a hoistable constant assignment."""
    if not isinstance(node, ast.Assign):
        return False
    if len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Name):
        return False
    return is_constant_name(target.id) and target.id in safe_names and _is_literal_only(node.value)


def _overlapping_statements(statements: list[ast.stmt]) -> set[ast.stmt]:
    """Collect top-level statements whose physical line spans overlap."""
    line_owners: dict[int, ast.stmt] = {}
    overlapping: set[ast.stmt] = set()
    for statement in statements:
        end = statement.end_lineno or statement.lineno
        for line_number in range(statement.lineno, end + 1):
            owner = line_owners.get(line_number)
            if owner is None:
                line_owners[line_number] = statement
                continue
            overlapping.add(owner)
            overlapping.add(statement)
    return overlapping


def get_safe_to_hoist_by_gap(
    tree: ast.Module,
    candidates_by_gap: Mapping[int, set[str]],
    *,
    candidate_positions: Mapping[int, int] | None,
) -> dict[int, set[str]]:
    """Determine safe constants for every candidate gap in one analysis pass.

    Args:
        tree: The full source AST.
        candidates_by_gap: Candidate names keyed by their source gap position.
        candidate_positions: Proposed final positions by source occurrence, or
            None to use source positions.

    Returns:
        Safe constant names keyed by source gap position.
    """
    if not candidates_by_gap:
        return {}

    all_candidates = set[str]().union(*candidates_by_gap.values())
    if not all_candidates:
        return {}

    binding_counts = _count_constant_bindings(tree, all_candidates)
    const_def_lines = collect_const_def_lines(tree, all_candidates)
    reference_names = set(const_def_lines)
    def_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    ]
    references_by_position: dict[int, set[str]] = {}
    for source_position, node in enumerate(def_nodes):
        position = (
            candidate_positions.get(source_position, source_position)
            if candidate_positions is not None
            else source_position
        )
        references_by_position.setdefault(position, set()).update(
            collect_name_references(node, reference_names, '')
        )

    early: set[str] = set()
    reference_positions = iter(sorted(references_by_position.items()))
    current = next(reference_positions, None)
    safe_by_gap: dict[int, set[str]] = {}
    for gap_position in sorted(candidates_by_gap):
        while current is not None and current[0] <= gap_position:
            early.update(current[1])
            current = next(reference_positions, None)
        safe_by_gap[gap_position] = {
            name for name in candidates_by_gap[gap_position] - early if binding_counts[name] == 1
        }
    return safe_by_gap


def remove_hoisted_lines(trailing: str, hoisted_lines: set[int]) -> str:
    """Remove hoisted constant lines from trailing content.

    Line numbers are 1-based physical line numbers in the exact trailing text.

    Args:
        trailing: Exact trailing source text.
        hoisted_lines: Physical line numbers to remove.

    Returns:
        Trailing text without the selected lines.
    """
    lines = split_physical_lines(trailing, keepends=True)
    return join_physical_fragments(
        line for number, line in enumerate(lines, start=1) if number not in hoisted_lines
    )


def extract_hoistable_constants(
    trailing: str,
    safe_names: set[str],
    *,
    _trail_tree: ast.Module | None = None,
) -> tuple[list[str], set[int]]:
    """Extract hoistable constant assignments from trailing content.

    A constant is extractable when it's a simple assignment with a single
    UPPER_SNAKE_CASE target in safe_names, a pure literal value, and the
    remaining content after extraction is safe (blank lines/comments only).

    Args:
        trailing: The trailing content between definitions.
        safe_names: Names that passed the hoist safety analysis.
        _trail_tree: Pre-parsed AST of the exact trailing text, to avoid
            re-parsing.

    Returns:
        (hoisted_sources, hoisted_line_numbers): Exact source spans, including
        their relocated blank-line boundaries, for hoistable constants and
        their 1-based physical line numbers in the exact trailing text. Empty
        if unsafe.
    """
    if not trailing.strip():
        return [], set()

    if _trail_tree is not None:
        trail_tree = _trail_tree
    else:
        try:
            trail_tree = ast.parse(trailing)
        except SyntaxError:
            return [], set()

    trailing_lines = split_physical_lines(trailing, keepends=True)
    overlapping = _overlapping_statements(trail_tree.body)
    hoistable: list[str] = []
    hoisted_lines: set[int] = set()

    for node in trail_tree.body:
        end = node.end_lineno or node.lineno
        stmt_lines = trailing_lines[node.lineno - 1 : end]
        if not _is_hoistable_assign(node, safe_names) or node in overlapping or not stmt_lines:
            continue
        carried = list(
            takewhile(
                lambda number: is_layout_blank(trailing_lines[number - 1]),
                range(end + 1, len(trailing_lines) + 1),
            ),
        )
        if not carried:
            carried = list(
                takewhile(
                    lambda number: (
                        number not in hoisted_lines and is_layout_blank(trailing_lines[number - 1])
                    ),
                    range(node.lineno - 1, 0, -1),
                ),
            )
            carried.reverse()
        source = ''.join(stmt_lines)
        for number in carried:
            source = join_physical(source, trailing_lines[number - 1])
        hoistable.append(source)
        hoisted_lines.update(range(node.lineno, end + 1))
        hoisted_lines.update(carried)

    if not hoistable:
        return [], set()

    # Check remaining content is safe
    remaining = remove_hoisted_lines(trailing, hoisted_lines)
    if remaining.strip() and not is_safe_trailing(remaining):
        return [], set()

    return hoistable, hoisted_lines
