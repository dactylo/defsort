"""Future annotations import management: insertion and removal."""

import ast

from defsort._core._analysis import (
    ENCODING_LINE_LIMIT,
    ENCODING_PATTERN,
    has_future_annotations_import,
    module_contains_annotations,
)
from defsort._source import (
    is_layout_blank,
    join_physical,
    split_physical_lines,
    terminal_line_ending,
)

_FUTURE_ANNOTATIONS_IMPORT = 'from __future__ import annotations'


def _future_import_insertion_index(
    lines: list[str],
    docstring_start: int,
    docstring_end: int,
) -> int:
    """Return the physical-line index after which to insert the import."""
    insert_after = 0
    i = 0
    while i < len(lines):
        if i + 1 == docstring_start:
            insert_after = docstring_end
            i = docstring_end
            continue
        body = lines[i].removesuffix(terminal_line_ending(lines[i])).strip(' \t\f')
        if i == 0 and body.startswith('#!'):
            insert_after = i + 1
            i += 1
            continue
        if i < ENCODING_LINE_LIMIT and ENCODING_PATTERN.match(body):
            insert_after = i + 1
            i += 1
            continue
        if is_layout_blank(lines[i]):
            i += 1
            continue
        if body.startswith('#'):
            insert_after = i + 1
            i += 1
            continue
        break
    return insert_after


def _insertion_line_ending(lines: list[str], insert_after: int) -> str:
    """Return the nearest available line ending at an insertion point."""
    if insert_after > 0:
        ending = terminal_line_ending(lines[insert_after - 1])
        if ending:
            return ending
    if insert_after < len(lines):
        ending = terminal_line_ending(lines[insert_after])
        if ending:
            return ending
    for line in reversed(lines[: max(insert_after - 1, 0)]):
        ending = terminal_line_ending(line)
        if ending:
            return ending
    for line in lines[insert_after + 1 :]:
        ending = terminal_line_ending(line)
        if ending:
            return ending
    return '\n'


def _insert_future_before_statement(lines: list[str], statement: ast.stmt) -> str:
    """Insert the future import before a same-line statement."""
    line_index = statement.lineno - 1
    line = lines[line_index]
    terminal_ending = terminal_line_ending(line)
    body = line.removesuffix(terminal_ending)
    byte_prefix = body.encode('utf-8')[: statement.col_offset]
    character_offset = len(byte_prefix.decode('utf-8'))
    insertion_ending = _insertion_line_ending(lines, line_index + 1)
    lines[line_index] = (
        body[:character_offset]
        + _FUTURE_ANNOTATIONS_IMPORT
        + insertion_ending
        + body[character_offset:]
        + terminal_ending
    )
    return ''.join(lines)


def _node_owns_span(lines: list[str], node: ast.ImportFrom) -> bool:
    """Return whether the import exclusively owns its physical-line span."""
    if node.end_col_offset is None:
        return False
    span = lines[node.lineno - 1 : node.end_lineno or node.lineno]
    if any('#' in line for line in span):
        # A future-import statement cannot contain a string literal, so any
        # `#` in the span is a comment the deletion would take with it.
        return False
    first = span[0]
    first_body = first.removesuffix(terminal_line_ending(first))
    prefix = first_body.encode('utf-8')[: node.col_offset].decode('utf-8')
    last = span[-1]
    last_body = last.removesuffix(terminal_line_ending(last))
    suffix = last_body.encode('utf-8')[node.end_col_offset :].decode('utf-8')
    return not prefix.strip() and not suffix.strip()


def _remove_future_annotations_import(source: str) -> str:
    """Remove the first exclusively owned future annotations import."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    try:
        compile(tree, '<unknown>', 'exec')
    except SyntaxError:
        return source

    lines = split_physical_lines(source, keepends=True)
    for node in tree.body:
        if not (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == '__future__'
            and len(node.names) == 1
            and node.names[0].name == 'annotations'
            and node.names[0].asname is None
        ):
            continue
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        if _node_owns_span(lines, node):
            return join_physical(''.join(lines[:start]), ''.join(lines[end:]))

    return source


def can_remove_future_annotations(tree: ast.Module) -> bool:
    """Return whether annotations and possible uses of the import binding are absent.

    Every `annotations` name occurrence prevents removal, including stores and
    deletions at any depth. This conservative check does not resolve scopes or
    reflective uses through string names.
    """
    return not module_contains_annotations(tree) and not any(
        isinstance(node, ast.Name) and node.id == 'annotations' for node in ast.walk(tree)
    )


def ensure_future_annotations(source: str) -> str:
    """Insert `from __future__ import annotations` at the correct position.

    Insertion respects (in order): shebang lines, PEP 263 encoding cookies, the
    AST-owned module docstring span, and regular comments. If the import
    already exists or the source cannot be parsed, the source is returned
    unchanged.

    Args:
        source: The Python source code.

    Returns:
        Source with the future import inserted, or the unchanged source when
        insertion is unnecessary or unsafe.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    if has_future_annotations_import(source, _tree=tree):
        return source

    lines = split_physical_lines(source, keepends=True)
    if not lines:
        return _FUTURE_ANNOTATIONS_IMPORT

    docstring_start = 0
    docstring_end = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        docstring_start = tree.body[0].lineno
        docstring_end = tree.body[0].end_lineno or tree.body[0].lineno
        if len(tree.body) > 1 and tree.body[1].lineno == docstring_end:
            return _insert_future_before_statement(lines, tree.body[1])
    insert_after = _future_import_insertion_index(
        lines,
        docstring_start,
        docstring_end,
    )
    ending = _insertion_line_ending(lines, insert_after)
    if insert_after == len(lines) and not terminal_line_ending(lines[-1]):
        lines.append(ending + _FUTURE_ANNOTATIONS_IMPORT)
    else:
        lines.insert(insert_after, _FUTURE_ANNOTATIONS_IMPORT + ending)
    return ''.join(lines)


def remove_unused_future_annotations(source: str, *, _tree: ast.Module | None = None) -> str:
    """Remove every eligible future annotations import when its binding is unused.

    Removal requires no syntactic annotations or `annotations` name
    occurrences.
    The first-only editor preserves ineligible spans and surrounding source;
    repeated removal reaches a fixed point in one automatic cleanup operation.
    Invalid source is returned unchanged.

    Args:
        source: The Python source code.
        _tree: An optional parsed tree matching `source` exactly.

    Returns:
        Source with all eligible unused import spans removed, or the unchanged
        source when cleanup is unnecessary or unsafe.
    """
    if _tree is None:
        try:
            _tree = ast.parse(source)
        except SyntaxError:
            return source
    if not can_remove_future_annotations(_tree):
        return source
    while True:
        updated = _remove_future_annotations_import(source)
        if updated == source:
            return source
        source = updated
