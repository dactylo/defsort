"""Detect-mode output formatting."""

from collections.abc import Iterable, Mapping

from defsort._display._width import pad_cells
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import DetectResult, OrderKind

_DETECT_SUMMARY_ORDER: tuple[OrderKind, ...] = (
    'dependency',
    'alphabetical',
    'both',
    'unordered',
)


def _detect_score_str(order: OrderKind, dep_score: float, alpha_score: float) -> str:
    """Format tau score string for --detect verbose output."""
    if order == 'unordered':
        return f'(dep tau: {dep_score:.2f}, alpha tau: {alpha_score:.2f})'
    if order == 'alphabetical':
        return f'(tau: {alpha_score:.2f})'
    # DEPENDENCY or BOTH
    return f'(tau: {dep_score:.2f})'


def print_detect(
    entries: Iterable[tuple[str, DetectResult | None]],
    totals: tuple[Mapping[OrderKind, int], int],
    *,
    verbose: bool,
) -> None:
    """Print detect-mode results and summary.

    Args:
        entries: List of (path, detection_or_None) tuples in file iteration
            order. None means "too few definitions" (skipped). Preserves
            interleaved ordering from _run_detect.
        totals: (counts_dict, total_int) pair.
        verbose: Whether to show tau scores and skipped files.
    """
    counts, total = totals
    lines: list[str] = []

    for path, result in entries:
        safe_path = escape_terminal_text(path_text(path))
        if result is None:
            # Skipped file (too few definitions) -- verbose only
            if verbose:
                lines.append(f'{pad_cells(safe_path, 40)} (too few definitions)')
        elif verbose:
            score_str = _detect_score_str(result.order, result.dep_score, result.alpha_score)
            lines.append(f'{pad_cells(safe_path, 40)} {result.order:<13} {score_str}')
        else:
            lines.append(f'{pad_cells(safe_path, 40)} {result.order}')

    for line in lines:
        print(line)

    if total:
        parts = [f'{counts[kind]} {kind}' for kind in _DETECT_SUMMARY_ORDER if kind in counts]
        print(f'\n{total} files: {", ".join(parts)}')
