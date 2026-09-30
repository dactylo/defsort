"""Summary table formatting."""

from collections.abc import Collection
from typing import NamedTuple

from defsort._color import BOLD, YELLOW, colorize
from defsort._display._movement import count_moved_split
from defsort._display._terminal import escape_terminal_text
from defsort._display._types import SummaryRow
from defsort._display._width import display_width, pad_cells
from defsort._types import SortResult

_MIN_FILES_FOR_TOTAL = 2
_SUMMARY_HEADERS = ('Name', 'Defs', 'Methods', 'Total', 'Move', 'Move%')


class _SummaryWidths(NamedTuple):
    """Column widths for the summary table."""

    path: int
    defs: int
    methods: int
    total: int
    moved: int
    pct: int


def _colorize_pct(row: str, pct: str, n_moved: int, total: int, *, name: str | None = None) -> str:
    """Apply severity color to Move% and optionally filename."""
    if not pct or total == 0:
        return row
    if n_moved == 0:
        return row
    if name:
        row = colorize(name, YELLOW) + row[len(name) :]
    return row[: -len(pct)] + colorize(pct, YELLOW)


def _compute_summary_totals(rows: list[SummaryRow]) -> SummaryRow:
    """Pass 2: totals row aggregated across changed files."""
    changed_rows = [row for row in rows if row.changed]
    return SummaryRow(
        path='TOTAL',
        defs=sum(row.defs for row in changed_rows),
        methods=sum(row.methods for row in changed_rows),
        moved=sum(row.moved for row in changed_rows),
        total=sum(row.total for row in changed_rows),
        changed=True,
    )


def _pct_str(n_moved: int, total: int) -> str:
    """Format moved percentage as 'NN%', or empty string when total is zero."""
    if total == 0:
        return ''
    return f'{int(n_moved / total * 100)}%'


def _compute_summary_widths(
    rows: list[SummaryRow],
    *,
    show_total: bool,
    totals: SummaryRow,
) -> _SummaryWidths:
    """Pass 3: column widths for aligned rendering."""
    headers = _SUMMARY_HEADERS
    name_w = max(display_width(headers[0]), *(display_width(r.path) for r in rows))
    if show_total:
        name_w = max(name_w, len('TOTAL'))
    defs_w = max(len(headers[1]), *(len(str(r.defs)) for r in rows))
    meth_w = max(len(headers[2]), *(len(str(r.methods)) for r in rows))
    tot_w = max(len(headers[3]), *(len(str(r.total)) for r in rows))
    moved_w = max(len(headers[4]), *(len(str(r.moved)) for r in rows))
    pct_w = max(
        len(headers[5]),
        *(len(_pct_str(r.moved, r.total)) for r in rows),
    )
    if show_total:
        defs_w = max(defs_w, len(str(totals.defs)))
        meth_w = max(meth_w, len(str(totals.methods)))
        tot_w = max(tot_w, len(str(totals.total)))
        moved_w = max(moved_w, len(str(totals.moved)))
        pct_w = max(pct_w, len(_pct_str(totals.moved, totals.total)))
    return _SummaryWidths(
        path=name_w,
        defs=defs_w,
        methods=meth_w,
        total=tot_w,
        moved=moved_w,
        pct=pct_w,
    )


def _render_summary_rows(
    rows: list[SummaryRow],
    widths: _SummaryWidths,
    *,
    color: bool,
    show_total: bool,
    totals: SummaryRow,
) -> str:
    """Pass 4: build aligned text (header + rows + optional total)."""
    headers = _SUMMARY_HEADERS

    def _fmt(  # noqa: PLR0913
        name: str,
        defs_s: str,
        meth_s: str,
        tot_s: str,
        moved_s: str,
        *,
        pct_s: str,
    ) -> str:
        return (
            f'{pad_cells(name, widths.path)}  {defs_s:>{widths.defs}}  {meth_s:>{widths.methods}}'
            f'  {tot_s:>{widths.total}}  {moved_s:>{widths.moved}}  {pct_s:>{widths.pct}}'
        )

    header_line = _fmt(*headers[:5], pct_s=headers[5])
    if color:
        header_line = colorize(header_line, BOLD)

    lines = [header_line]
    for row in rows:
        pct = _pct_str(row.moved, row.total)
        line = _fmt(
            row.path,
            str(row.defs),
            str(row.methods),
            str(row.total),
            str(row.moved),
            pct_s=pct,
        )
        if color:
            line = _colorize_pct(line, pct, row.moved, row.total, name=row.path)
        lines.append(line)

    if show_total:
        agg_pct = _pct_str(totals.moved, totals.total)
        line = _fmt(
            totals.path,
            str(totals.defs),
            str(totals.methods),
            str(totals.total),
            str(totals.moved),
            pct_s=agg_pct,
        )
        if color:
            line = _colorize_pct(line, agg_pct, totals.moved, totals.total)
            line = colorize(line, BOLD)
        lines.append(line)

    return '\n'.join(lines)


def build_summary_row(path: str, result: SortResult) -> SummaryRow:
    """Reduce one sort result to its summary fields.

    Args:
        path: Display path for the sorted file.
        result: Complete result from sorting the file.

    Returns:
        Summary data for the file.
    """
    changed = result.original_source != result.new_source
    defs = len(result.original_order)
    methods = result.total_methods
    moved_defs, moved_methods = count_moved_split(result)
    return SummaryRow(
        path=path,
        defs=defs,
        methods=methods,
        moved=moved_defs + moved_methods,
        total=defs + methods,
        changed=changed,
    )


def format_summary_rows(
    rows: Collection[SummaryRow],
    *,
    verbose: bool,
    color: bool,
) -> str:
    """Format retained summary rows as a columnar table.

    Args:
        rows: Summary data for non-skipped files.
        verbose: Whether to include unchanged files.
        color: Whether to apply ANSI color.

    Returns:
        The formatted summary table, or an empty string when no rows are
        visible.
    """
    visible_rows = rows if verbose else [row for row in rows if row.changed]
    if not visible_rows:
        return ''
    visible_rows = [row._replace(path=escape_terminal_text(row.path)) for row in visible_rows]
    n_changed = sum(1 for row in visible_rows if row.changed)
    show_total = n_changed >= _MIN_FILES_FOR_TOTAL
    totals = _compute_summary_totals(visible_rows)
    widths = _compute_summary_widths(visible_rows, show_total=show_total, totals=totals)
    return _render_summary_rows(
        visible_rows,
        widths,
        color=color,
        show_total=show_total,
        totals=totals,
    )
