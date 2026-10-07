"""Movement table rendering for preview mode."""

from collections.abc import Iterable, Sequence

from defsort._color import BOLD, colorize, spectral_map
from defsort._display._constraints import format_banner_constraint_details
from defsort._display._types import MovementPositions, MovementRow
from defsort._display._width import display_width, pad_cells
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import Definition, MethodDefinition, MethodSortInfo, SortResult


def _print_unchanged_preview(
    path: str,
    verbose: bool,
    ordering: str,
    *,
    color: bool,
    separate: bool,
) -> bool:
    """Print verbose unchanged status and return whether it was emitted."""
    if not verbose:
        return False
    if separate:
        print()
    safe_path = escape_terminal_text(path_text(path))
    label = colorize(safe_path, BOLD) if color else safe_path
    print(f'{label}: already in {ordering} order')
    return True


def _render_movement_table(
    rows: list[MovementRow],
    *,
    color: bool = False,
    moved_indices: list[int] | None = None,
    bold_header: bool = True,
    indent: int = 0,
) -> str:
    """Render movement rows as an aligned text table."""
    if not rows:
        return ''
    col1 = max(display_width('Current'), *(display_width(r.old_name) for r in rows))
    col2 = max(display_width('Proposed'), *(display_width(r.new_name) for r in rows))
    pad = ' ' * indent
    header = f'{pad}{"Current":<{col1}}  {"Proposed":<{col2}}  Move'
    if color and bold_header:
        header = colorize(header, BOLD)
    color_map: dict[int, str] = {}
    if color and moved_indices:
        color_map = spectral_map(moved_indices)
    lines = [header]
    for row in rows:
        movement = row.movement or '-'
        line = f'{pad}{pad_cells(row.old_name, col1)}  {pad_cells(row.new_name, col2)}  {movement}'
        if color and row.movement and color_map:
            new_color = color_map[row.new_idx]
            old_color = color_map[row.old_idx]
            colored_old = colorize(row.old_name, old_color) + ' ' * (
                col1 - display_width(row.old_name)
            )
            colored_new = colorize(row.new_name, new_color) + ' ' * (
                col2 - display_width(row.new_name)
            )
            colored_move = colorize(row.movement, new_color)
            line = f'{pad}{colored_old}  {colored_new}  {colored_move}'
        lines.append(line)
    return '\n'.join(lines)


def movement_positions[T: (Definition, MethodDefinition)](
    old_order: Iterable[T],
    new_order: Iterable[T],
) -> MovementPositions:
    """Map stable indices to old positions and identify moved indices.

    Args:
        old_order: Definitions in their original order.
        new_order: The same definitions in their proposed order.

    Returns:
        Original positions and moved stable indices in proposed order.
    """
    old_by_index = {definition.index: position for position, definition in enumerate(old_order)}
    moved_indices = [
        definition.index
        for position, definition in enumerate(new_order)
        if old_by_index[definition.index] != position
    ]
    return MovementPositions(old_by_index=old_by_index, moved_indices=moved_indices)


def _build_movement_rows[T: (Definition, MethodDefinition)](
    old_order: Sequence[T],
    new_order: Sequence[T],
    *,
    verbose: bool,
) -> list[MovementRow]:
    """Build movement rows that compare old and proposed positions."""
    positions = movement_positions(old_order, new_order)
    rows: list[MovementRow] = []
    for i, defn in enumerate(new_order):
        old_pos = positions.old_by_index[defn.index]
        if old_pos > i:
            movement = f'^{old_pos - i}'
        elif old_pos < i:
            movement = f'v{i - old_pos}'
        else:
            movement = ''
        if not verbose and not movement:
            continue
        old_name = old_order[i].name
        old_idx_at_row = old_order[i].index
        rows.append(
            MovementRow(
                old_name=old_name,
                new_name=defn.name,
                movement=movement,
                new_idx=defn.index,
                old_idx=old_idx_at_row,
            )
        )
    return rows


def _format_method_tables(
    method_sorts: list[MethodSortInfo],
    *,
    color: bool = False,
    verbose: bool = False,
) -> str:
    """Build movement tables for method sorting within classes."""
    sections: list[str] = []
    for info in method_sorts:
        total = len(info.new_order)
        positions = movement_positions(info.original_order, info.new_order)
        moved_indices = positions.moved_indices
        moved_count = len(moved_indices)
        rows = _build_movement_rows(info.original_order, info.new_order, verbose=verbose)
        if not rows:
            continue
        if moved_count == total:
            class_header = f'{info.class_name} ({total} methods):'
        else:
            class_header = f'{info.class_name} ({moved_count} of {total} methods):'
        if color:
            class_header = colorize(class_header, BOLD)
        body = _render_movement_table(
            rows,
            color=color,
            moved_indices=moved_indices if color else None,
            indent=2,
        )
        sections.append(f'{class_header}\n{body}')
    return '\n\n'.join(sections)


def count_moved_split(result: SortResult) -> tuple[int, int]:
    """Count definitions that changed position, split by category.

    Args:
        result: Sorting result whose movements should be counted.

    Returns:
        Tuple of (top_level_moved, methods_moved).
    """
    top = len(movement_positions(result.original_order, result.new_order).moved_indices)
    methods = sum(
        len(movement_positions(info.original_order, info.new_order).moved_indices)
        for info in result.method_sorts
    )
    return top, methods


def format_movement_table(
    result: SortResult,
    *,
    bold_header: bool = True,
    color: bool = False,
    indent: int = 0,
    verbose: bool = False,
) -> str:
    """Build a side-by-side comparison table with movement indicators.

    Args:
        result: Sorting result to display.
        bold_header: Whether to bold the table header.
        color: Whether to apply ANSI color.
        indent: Number of spaces to indent each row.
        verbose: Whether to include definitions that did not move.

    Returns:
        The rendered movement table.
    """
    positions = movement_positions(result.original_order, result.new_order)
    rows = _build_movement_rows(result.original_order, result.new_order, verbose=verbose)
    return _render_movement_table(
        rows,
        bold_header=bold_header,
        color=color,
        indent=indent,
        moved_indices=positions.moved_indices if color else None,
    )


def print_preview(  # noqa: PLR0913
    path: str,
    result: SortResult,
    *,
    verbose: bool,
    ordering: str,
    color: bool,
    multi: bool,
    first: bool,
) -> bool:
    """Print movement tables for one file (preview mode).

    Args:
        path: Display path for the source file.
        result: Sorting result to preview.
        verbose: Whether to include unchanged definitions and files.
        ordering: Effective ordering name for unchanged output.
        color: Whether to apply ANSI color.
        multi: Whether multiple files share the output stream.
        first: Whether this is the first file displayed.

    Returns:
        Whether output was emitted, for inter-file separator tracking.
    """
    constraint_details = format_banner_constraint_details(result.banner_constraints)
    safe_path = escape_terminal_text(path_text(path))

    # Unchanged file
    if result.original_source == result.new_source:
        emitted = _print_unchanged_preview(
            safe_path,
            verbose,
            ordering,
            color=color,
            separate=multi and not first,
        )
        if verbose and constraint_details:
            print(constraint_details)
        return emitted or (verbose and bool(constraint_details))

    # Build tables
    def_table = format_movement_table(result, color=color, verbose=verbose)
    method_table = (
        _format_method_tables(result.method_sorts, color=color, verbose=verbose)
        if result.method_sorts
        else ''
    )

    if def_table or method_table:
        if multi:
            sep = '' if first else '\n'
            label = colorize(safe_path, BOLD) if color else safe_path
            print(f'{sep}{label}:\n')
            indent = '  '
        else:
            indent = ''
        if def_table:
            print(indent + def_table.replace('\n', f'\n{indent}'))
        if method_table:
            if def_table:
                print()
            print(indent + method_table.replace('\n', f'\n{indent}'))
    else:
        # No movement tables (e.g. source-only change) -- inline X/Y format
        top, methods = count_moved_split(result)
        x = top + methods
        y = len(result.original_order) + result.total_methods
        changed = result.original_source != result.new_source
        p = colorize(safe_path, BOLD) if color and changed else safe_path
        if y == 0:
            print(f'{p}: 0/0')
        else:
            print(f'{p}: {x}/{y} ({int(x / y * 100)}%)')

    if constraint_details:
        print(constraint_details)
    return True
