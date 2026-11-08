"""Unified diff output for defsort."""

from defsort._vendor.hookfront._diff import DiffSides, write_diff
from defsort._vendor.hookfront._terminal import escape_terminal_text, path_text


def print_diff(
    sides: DiffSides,
    path: str,
    *,
    verbose: bool,
    ordering: str,
    color: bool,
) -> None:
    """Print a byte-exact diff or describe an unchanged file.

    Args:
        sides: Original and replacement file bytes with source encoding.
        path: Display path for the source file.
        verbose: Whether to describe unchanged files.
        ordering: Effective ordering name for unchanged output.
        color: Whether to apply ANSI color.
    """
    if sides.original == sides.new:
        if verbose:
            print(f'{escape_terminal_text(path_text(path))}: already in {ordering} order')
        return
    write_diff(sides, path, color=color)
