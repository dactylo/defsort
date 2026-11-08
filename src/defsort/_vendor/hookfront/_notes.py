"""Word shared Git failures and hook notes for human output."""

from collections.abc import Collection, Iterable

from ._terminal import escape_terminal_text, path_text

OUTSIDE_REPOSITORY = 'not in a Git repository'


def git_unavailable_reason(detail: str, *, incremental: bool, guard: bool) -> str:
    """Describe a Git failure for the effective processing modes.

    Args:
        detail: The Git failure detail.
        incremental: Whether incremental processing is effective.
        guard: Whether the reorder guard is effective.

    Returns:
        The diagnostic without an `error: ` prefix or a recovery suggestion.
    """
    if incremental and guard:
        modes = 'incremental mode and the reorder guard'
    elif incremental:
        modes = 'incremental mode'
    else:
        modes = 'the reorder guard'
    return f'{modes} cannot use Git: {detail}'


def git_unavailable_message(detail: str, *, incremental: bool, guard: bool) -> str:
    """Describe a Git failure and suggest whole-file mode when sufficient.

    Args:
        detail: The Git failure detail.
        incremental: Whether incremental processing is effective.
        guard: Whether the reorder guard is effective.

    Returns:
        The diagnostic without an `error: ` prefix.
    """
    reason = git_unavailable_reason(detail, incremental=incremental, guard=guard)
    if incremental and not guard:
        return f'{reason}; pass --no-incremental to process whole files'
    return reason


def post_tool_note(tool: str, path: str, reason: str) -> str:
    """Render a PostToolUse skip with a display path and visible controls.

    Args:
        tool: The consumer's name.
        path: The skipped file's path.
        reason: The raw reason for skipping the file.
    """
    return escape_terminal_text(f'{tool}: PostToolUse skipped {path_text(path)}: {reason}')


def sync_note(tool: str, names: Iterable[str], reason: str, *, result: str) -> str:
    """Render a working-tree sync failure and its committed result.

    Args:
        tool: The consumer's name.
        names: Names associated with the file, in display order.
        reason: The raw reason the working tree was not updated.
        result: The committed result, `sorted` or `wrapped`.
    """
    displayed = ', '.join(path_text(name) for name in names)
    return escape_terminal_text(
        f'{tool}: did not update {displayed} in the working tree: {reason}; '
        f'the commit has the {result} version'
    )


def syntax_reason(error: SyntaxError) -> str:
    """Describe a syntax failure, including its line when available.

    Args:
        error: The parser's syntax failure.
    """
    reason = f'syntax error: {error.msg}'
    if error.lineno is not None:
        return f'{reason} (line {error.lineno})'
    return reason


def unstaged_reason(names: Collection[str], *, of: int) -> str:
    """Name the unstaged path when one file has multiple names.

    Args:
        names: Names with unstaged changes, in display order.
        of: Total number of names associated with the file.
    """
    if of == 1:
        return 'it has unstaged changes'
    displayed = ', '.join(path_text(name) for name in names)
    verb = 'has' if len(names) == 1 else 'have'
    return f'{displayed} {verb} unstaged changes'
