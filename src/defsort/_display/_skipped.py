"""Skipped-file diagnostic output."""

import sys
import textwrap

from defsort._color import BOLD, colorize
from defsort._display._movement import format_movement_table
from defsort._display._types import CliMode
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import ClassBlocker, SortResult

_BLOCKER_LABELS = {
    'duplicates': 'duplicate method names',
    'interstitial': 'code between methods',
}
_PREVIEW_TEXT_WIDTH = 88


def _format_blocking_code(result: SortResult) -> str:
    """Format blocking-code snippets for a skipped file."""
    if not result.blocking_code:
        return ''
    lines = [
        '  Code between definitions:',
        *(
            f'    line {block.line}: {escape_terminal_text(block.text)}'
            for block in result.blocking_code
        ),
    ]
    return '\n'.join(lines)


def _format_compact_header(path: str, result: SortResult, *, color: bool) -> str:
    """Format a compact skipped-file line."""
    safe_path = escape_terminal_text(path_text(path))
    label = colorize(safe_path, BOLD) if color else safe_path
    reason = escape_terminal_text(result.warning or 'safety check')
    return f'{label}: skipped ({reason})'


def _format_detailed_header(path: str, *, color: bool) -> str:
    """Format a detailed preview skipped-file header."""
    safe_path = escape_terminal_text(path_text(path))
    label = colorize(safe_path, BOLD) if color else safe_path
    return f'{label}: skipped'


def _format_labeled_text(label: str, value: str) -> str:
    """Format wrapped preview prose with an aligned label."""
    prefix = f'  {label}: '
    return textwrap.fill(
        value,
        width=_PREVIEW_TEXT_WIDTH,
        initial_indent=prefix,
        subsequent_indent=' ' * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )


def _format_remedy(result: SortResult) -> str:
    """Format the existing applicable remedy for a skipped result."""
    if result.guarded:
        value = 'use --force to bypass the reorder guard'
    elif result.blocker == 'eager_order':
        value = 'keep each referenced definition before the code that uses it'
    elif result.blocker == 'interstitial':
        value = 'use --force to bypass safety checks'
    elif result.blocker == 'duplicates':
        value = 'remove the duplicate definition'
    else:
        return ''
    return _format_labeled_text('fix', value)


def format_class_blocker(cb: ClassBlocker) -> str:
    """Render one blocked class as a skipped-diagnostic line.

    Args:
        cb: The blocked-class record to render.

    Returns:
        The indented diagnostic line for text output.
    """
    label = escape_terminal_text(_BLOCKER_LABELS.get(cb.blocker, cb.message))
    return f'  class {cb.class_name}: skipped ({label})'


def print_skipped(
    path: str,
    result: SortResult,
    *,
    mode: CliMode,
    color: bool,
    verbose: bool = False,
) -> None:
    """Print a skipped-file diagnostic to stderr.

    Args:
        path: Display path for the skipped file.
        result: Sort result containing the blocker and proposed order.
        mode: Active CLI output mode.
        color: Whether ANSI color is enabled on stderr.
        verbose: Whether preview tables include unchanged rows.
    """
    if mode is not CliMode.PREVIEW:
        print(_format_compact_header(path, result, color=color), file=sys.stderr)
        return

    details = [
        _format_detailed_header(path, color=color),
        _format_labeled_text(
            'reason',
            escape_terminal_text(result.warning or 'safety check'),
        ),
    ]
    remedy = _format_remedy(result)
    if remedy:
        details.append(remedy)
    sections = ['\n'.join(details)]

    blocking_code = _format_blocking_code(result)
    if blocking_code:
        sections.append(blocking_code)

    table = format_movement_table(result, color=color, indent=2, verbose=verbose)
    if table:
        sections.append(table)

    print('\n\n'.join(sections), file=sys.stderr)
