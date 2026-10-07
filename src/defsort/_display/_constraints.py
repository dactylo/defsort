"""Formatting for structured banner constraints."""

import re
from collections.abc import Collection, Iterable

from defsort._source import BANNER_RULE_CHARS, split_physical_lines
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import BannerConstraint

_RULE_LABEL_PATTERN = re.compile(rf'([{re.escape(BANNER_RULE_CHARS)}])\1{{3,}}')


def _banner_label(constraint: BannerConstraint) -> str:
    """Return the human-facing label for one banner."""
    lines = [
        line.removeprefix('#').strip() for line in split_physical_lines(constraint.boundary.text)
    ]
    for line in lines:
        if line and _RULE_LABEL_PATTERN.fullmatch(line) is None:
            return escape_terminal_text(line)
    return escape_terminal_text(next((line for line in lines if line), ''))


def format_banner_constraint_details(
    constraints: Iterable[BannerConstraint],
) -> str:
    """Format one compact detail line per constrained boundary.

    Args:
        constraints: Structured constraints in source boundary order.

    Returns:
        Newline-separated detail lines, or an empty string.
    """
    lines: list[str] = []
    for constraint in constraints:
        held = ', '.join(f'{item.name} ({item.direction})' for item in constraint.held)
        lines.append(f'line {constraint.boundary.start_line}: {_banner_label(constraint)}: {held}')
    return '\n'.join(lines)


def format_banner_constraint_summary(
    path: str,
    constraints: Collection[BannerConstraint],
) -> str:
    """Format the per-file constrained-sort diagnostic.

    Args:
        path: File path shown to the user.
        constraints: Structured constraints for the file.

    Returns:
        Compact diagnostic text, or an empty string.
    """
    count = len(constraints)
    if not count:
        return ''
    noun = 'banner' if count == 1 else 'banners'
    safe_path = escape_terminal_text(path_text(path))
    return f'defsort: {safe_path}: sort constrained by {count} {noun}'
