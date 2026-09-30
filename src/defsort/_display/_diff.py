"""Unified diff output formatting."""

import difflib
import sys
from collections.abc import Iterator

from defsort._color import BOLD, GREEN, RED, colorize
from defsort._display._terminal import escape_terminal_text
from defsort._source import split_physical_lines, terminal_line_ending
from defsort._types import SortResult

_DIFF_HEADER_COUNT = 2


def _colorize_diff_line(line: str, *, is_header: bool) -> str:
    """Apply Git-style color to one unified diff line."""
    suffix = terminal_line_ending(line)
    content = line.removesuffix(suffix)
    if is_header and content.startswith(('--- ', '+++ ')):
        return colorize(content, BOLD) + suffix
    if content.startswith('@@'):
        return colorize(content, BOLD) + suffix
    if content.startswith('-'):
        return colorize(content, RED) + suffix
    if content.startswith('+'):
        return colorize(content, GREEN) + suffix
    return line


def _escape_diff_source_record(record: str) -> str:
    """Escape one diff source record while retaining its marker and ending."""
    suffix = terminal_line_ending(record)
    body = record.removesuffix(suffix)
    return body[:1] + escape_terminal_text(body[1:]) + suffix


def _terminate_diff_records(lines: Iterator[str]) -> Iterator[str]:
    """Terminate unterminated data records and mark missing source newlines."""
    for line in lines:
        if terminal_line_ending(line):
            yield line
            continue
        yield line + '\n'
        yield '\\ No newline at end of file\n'


def _iter_diff(
    result: SortResult,
    path: str,
    *,
    color: bool = False,
    terminal: bool = False,
) -> Iterator[str]:
    """Yield diff lines."""
    original_lines = split_physical_lines(result.original_source, keepends=True)
    new_lines = split_physical_lines(result.new_source, keepends=True)
    if terminal:
        path = escape_terminal_text(path)
    diff = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f'{path} (original)',
        tofile=f'{path} (sorted)',
    )
    if terminal:
        diff = (
            line
            if index < _DIFF_HEADER_COUNT or line.startswith('@@')
            else _escape_diff_source_record(line)
            for index, line in enumerate(diff)
        )
    records = _terminate_diff_records(diff)
    if color:
        yield from (
            _colorize_diff_line(line, is_header=index < _DIFF_HEADER_COUNT)
            for index, line in enumerate(records)
        )
    else:
        yield from records


def print_diff(
    result: SortResult,
    path: str,
    *,
    verbose: bool,
    ordering: str,
    color: bool,
) -> None:
    """Print unified diff for one file.

    Handles verbose 'already in order' for unchanged files.

    Args:
        result: Sorting result to compare.
        path: Display path for the source file.
        verbose: Whether to describe unchanged files.
        ordering: Effective ordering name for unchanged output.
        color: Whether to apply ANSI color.
    """
    if result.original_source == result.new_source:
        if verbose:
            print(f'{escape_terminal_text(path)}: already in {ordering} order')
        return
    sys.stdout.writelines(_iter_diff(result, path, color=color, terminal=sys.stdout.isatty()))
