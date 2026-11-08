"""Skipped-file diagnostic output."""

import sys
import textwrap

from defsort._display._movement import format_movement_table
from defsort._display._types import CliMode
from defsort._types import (
    CLASS_BLOCKER_LABELS,
    DYNAMIC_ALL_MESSAGE,
    Blocker,
    ClassBlocker,
    SortResult,
)
from defsort._vendor.hookfront._color import BOLD, colorize
from defsort._vendor.hookfront._notes import OUTSIDE_REPOSITORY
from defsort._vendor.hookfront._terminal import escape_terminal_text, path_text

_BLOCKER_REMEDIES: dict[Blocker, str] = {
    'duplicates': 'remove the duplicate definition',
    'eager_order': 'keep each referenced definition before the code that uses it',
    'forward_refs': 'add from __future__ import annotations, or quote the annotation',
    'layout': 'separate attached comments from section banners with blank lines',
    'non_conforming': (
        'sort this file with --no-incremental and commit it to establish its baseline'
    ),
    'target_incompatible': (
        'sort this file with --no-incremental and commit it to adopt the new target order'
    ),
    'user_reordered': (
        'restore the baseline order of the existing definitions, or sort this file '
        'with --no-incremental and commit it'
    ),
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


def _format_remedy(result: SortResult, *, hoist_clears: bool) -> str:
    """Format the concrete remedy for a skipped result."""
    if result.warning == OUTSIDE_REPOSITORY:
        value = 'run inside a Git repository'
        if not result.guarded:
            value += ', or run again with --no-incremental'
    elif result.guarded:
        if result.warning and result.warning.startswith('reorder guard: unable to read baseline'):
            value = 'make HEAD readable for this file, or run again with --force'
        else:
            value = 'raise max-reorder-count or max-reorder-percent, or run again with --force'
    elif result.warning and result.warning.startswith('incremental: unable to read baseline'):
        value = 'make HEAD readable for this file, or run again with --no-incremental'
    elif result.blocker == 'eager_order' and result.warning == DYNAMIC_ALL_MESSAGE:
        value = 'write __all__ as one literal list'
    elif result.blocker == 'interstitial':
        value = (
            'run again with --hoist to move these constants to the top of the module'
            if hoist_clears
            else 'move this code above the first definition or below the last one'
        )
    elif result.blocker is not None:
        value = _BLOCKER_REMEDIES.get(result.blocker, '')
    else:
        return ''
    if not value:
        return ''
    return _format_labeled_text('fix', value)


def format_class_blocker(cb: ClassBlocker, *, path: str, preview: bool = False) -> str:
    """Render one blocked class as a skipped-diagnostic line.

    Args:
        cb: The blocked-class record to render.
        path: Display path for the blocked class's file.
        preview: Whether to include an actionable fix below the class line.

    Returns:
        The path-qualified diagnostic line for text output.
    """
    label = escape_terminal_text(CLASS_BLOCKER_LABELS.get(cb.blocker, cb.message))
    name = escape_terminal_text(cb.class_name)
    safe_path = escape_terminal_text(path_text(path))
    line = f'{safe_path}: class {name}: skipped ({label})'
    if not preview:
        return line
    if cb.blocker == 'interstitial':
        remedy = (
            f'move the code between methods of class {name} before its first method '
            'or after its last'
        )
    elif cb.blocker == 'duplicates':
        remedy = f'rename or remove the duplicate method in class {name}'
    else:
        remedy = _BLOCKER_REMEDIES.get(cb.blocker, '')
        if not remedy:
            return line
    return f'{line}\n{_format_labeled_text("fix", remedy)}'


def print_skipped(  # noqa: PLR0913
    path: str,
    result: SortResult,
    *,
    mode: CliMode,
    color: bool,
    verbose: bool = False,
    hoist_clears: bool = False,
) -> None:
    """Print a skipped-file diagnostic to stderr.

    Args:
        path: Display path for the skipped file.
        result: Sort result containing the blocker and proposed order.
        mode: Active CLI output mode.
        color: Whether ANSI color is enabled on stderr.
        verbose: Whether preview tables include unchanged rows.
        hoist_clears: Whether a preview-only hoist probe clears the
            interstitial skip.
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
    remedy = _format_remedy(result, hoist_clears=hoist_clears)
    if remedy:
        details.append(remedy)
    if result.blocker == 'interstitial':
        details.append(
            _format_labeled_text('override', '--force sorts anyway; review the diff first')
        )
    sections = ['\n'.join(details)]

    blocking_code = _format_blocking_code(result)
    if blocking_code:
        sections.append(blocking_code)

    table = format_movement_table(result, color=color, indent=2, verbose=verbose)
    if table:
        sections.append(table)

    print('\n\n'.join(sections), file=sys.stderr)
