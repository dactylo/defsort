"""Expand file operands with Git exclusions and private labels."""

import functools
import os
import sys
from collections.abc import Collection, Iterable
from pathlib import Path

from defsort._vendor.hookfront._terminal import (
    escape_terminal_text,
    path_text,
)
from defsort._vendor.srcfiles import (
    DEFAULT_PRIVATE_ATTRIBUTE,
    decode_git_path,
    encode_git_path,
    group_paths,
    label_query,
    labelled_paths,
    private_attribute,
)

from ._run import GitCommandError, run_git

_SKIP_DIRS = frozenset(
    {
        '.git',
        '.hg',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        '.svn',
        '.tox',
        '.venv',
        '__pycache__',
        'build',
        'dist',
        'venv',
    }
)


@functools.cache
def _checked_private_attribute(tool: str, configured: str) -> str:
    """Validate the configured attribute, warning once and using the default if invalid."""
    try:
        return private_attribute(configured)
    except ValueError as error:
        warning = (
            f'warning: invalid git config {tool}.privateAttribute {configured!r}: '
            f'{error}; using {DEFAULT_PRIVATE_ATTRIBUTE!r}'
        )
        print(escape_terminal_text(warning), file=sys.stderr)
        return DEFAULT_PRIVATE_ATTRIBUTE


def _configured_private_attribute(directory: str | Path, *, tool: str) -> str:
    """Read the configured attribute name from Git, defaulting on query failure."""
    try:
        result = run_git(['git', 'config', '--get', f'{tool}.privateAttribute'], directory)
    except GitCommandError:
        return DEFAULT_PRIVATE_ATTRIBUTE
    return _checked_private_attribute(
        tool, result.stdout.decode(errors='replace').removesuffix('\n')
    )


def _filter_git_ignored(directory: Path, files: list[Path]) -> tuple[list[Path], str | None]:
    """Filter strict lexical descendants of a walk owner, returning any Git failure."""
    if not files:
        return files, None
    # The walker retains the owner prefix, so no absolute-path normalization is needed.
    prefix_length = len(directory.parts)
    relative_files = [(path, os.fsencode(Path(*path.parts[prefix_length:]))) for path in files]
    try:
        result = run_git(
            ['git', 'check-ignore', '-z', '--stdin'],
            directory,
            stdin=b'\0'.join(raw_path for _, raw_path in relative_files) + b'\0',
            allowed_returncodes=(0, 1),
        )
    except GitCommandError as error:
        return files, error.detail
    ignored = frozenset(result.stdout.split(b'\0'))
    return [path for path, raw_path in relative_files if raw_path not in ignored], None


def _ignored_directories(directory: Path) -> set[Path]:
    """Return directories whose entire content Git ignores, or none when Git fails."""
    try:
        result = run_git(
            [
                'git',
                'ls-files',
                '-z',
                '--others',
                '--ignored',
                '--exclude-standard',
                '--directory',
            ],
            directory,
        )
    except GitCommandError:
        return set()
    return {
        directory / decode_git_path(entry)
        for entry in result.stdout.split(b'\0')
        if entry.endswith(b'/')
    }


def _inside_worktree(directory: Path) -> bool:
    """Detect a Git marker in the directory or one of its parents."""
    resolved = directory.resolve()
    return any(
        (root / '.git').is_dir() or (root / '.git').is_file()
        for root in (resolved, *resolved.parents)
    )


def _labelled_private(
    directory: str | Path, paths: list[bytes], *, tool: str
) -> tuple[frozenset[bytes], str | None]:
    """Read private labels for paths relative to a Git worktree owner."""
    if not paths:
        return frozenset(), None
    attribute = _configured_private_attribute(directory, tool=tool)
    command, stdin = label_query(attribute, paths)
    try:
        result = run_git(command, directory, stdin=stdin)
    except GitCommandError as error:
        return frozenset(), error.detail
    try:
        return labelled_paths(result.stdout, attribute), None
    except ValueError as error:
        return frozenset(), str(error)


def _filter_private(
    directory: Path, files: list[Path], *, tool: str
) -> tuple[list[Path], str | None]:
    """Exclude privately labelled descendants of one walk owner."""
    if not files:
        return files, None
    prefix_length = len(directory.parts)
    relative_files = [(path, os.fsencode(Path(*path.parts[prefix_length:]))) for path in files]
    labelled, detail = _labelled_private(
        directory, [raw_path for _, raw_path in relative_files], tool=tool
    )
    if detail is not None:
        return files, detail
    return [path for path, raw_path in relative_files if raw_path not in labelled], None


def _walk_owner(owner: Path, pruned: set[Path]) -> tuple[list[Path], list[Path]]:
    """Return an owner's Python files and child worktree roots in walk order."""
    files: list[Path] = []
    child_roots: list[Path] = []
    for root, dirnames, filenames in os.walk(owner, topdown=True, followlinks=False):
        current = Path(root)
        if current != owner and ('.git' in dirnames or '.git' in filenames):
            child_roots.append(current)
            dirnames.clear()
            continue
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIRS and current / name not in pruned
        )
        files.extend(
            child
            for name in sorted(filenames)
            if name.endswith('.py') and not (child := current / name).is_symlink()
        )
    return files, child_roots


def _collect_directory(directory: Path, *, tool: str) -> list[Path]:
    """Collect Python files from one directory operand."""
    retained_files: list[Path] = []
    failures: list[tuple[Path, str]] = []
    # A stack processes nested worktrees depth-first, in the order the walk finds them.
    owners = [directory]
    while owners:
        owner = owners.pop()
        files, child_roots = _walk_owner(owner, _ignored_directories(owner))
        retained, detail = _filter_git_ignored(owner, [*files, *child_roots])
        if detail is not None:
            failures.append(
                (
                    owner,
                    (
                        f'cannot apply Git exclusions in {path_text(owner)}: {detail}; '
                        'keeping every discovered Python file'
                    ),
                )
            )
        else:
            retained, detail = _filter_private(owner, retained, tool=tool)
            if detail is not None:
                failures.append(
                    (
                        owner,
                        (
                            f'cannot apply private labels in {path_text(owner)}: {detail}; '
                            'keeping every file Git does not ignore'
                        ),
                    )
                )
        kept = set(retained)
        retained_files.extend(path for path in files if path in kept)
        owners.extend(reversed([root for root in child_roots if root in kept]))
    for owner, message in failures:
        if owner != directory or _inside_worktree(directory):
            print(escape_terminal_text(f'warning: {message}'), file=sys.stderr)
    return sorted(retained_files)


def collect_file_groups(paths: Iterable[str], *, tool: str) -> list[list[Path]]:
    """Expand paths into groups of file paths that name one file.

    Directory operands collect Python files, skip configured VCS, environment,
    cache, and build directories, and do not follow discovered symlinks.
    Git exclusions and private labels apply to discovered paths, including
    nested worktree roots. Explicit Python files bypass those filters.

    Git-query failures retain the applicable filesystem results and may print
    warnings. Repeated operands and case, hard-link, and symlink aliases of one
    file share a group.

    Args:
        paths: File and directory operands. Missing explicit paths are retained
            for later diagnostics, including names without a Python suffix.
        tool: Tool name whose private-attribute configuration applies.

    Returns:
        Groups in first-seen order, with spellings in caller order. Each
        directory operand contributes its retained files in sorted path order.

    Raises:
        OSError: If filesystem path resolution or warning output fails.
    """
    result: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            result.extend(_collect_directory(p, tool=tool))
        elif p.suffix == '.py' or not p.exists():
            result.append(p)
    return group_paths(result)


def collect_files(paths: Iterable[str], *, tool: str) -> list[Path]:
    """Expand paths into a deduplicated Python file list.

    A file named more than once is kept once under its first spelling.
    Selection and diagnostic behavior follow `collect_file_groups`.

    Args:
        paths: File and directory paths to expand.
        tool: Tool name whose private-attribute configuration applies.

    Returns:
        The first spelling of each group from `collect_file_groups`, including
        missing explicit paths retained for later diagnostics.

    Raises:
        OSError: If filesystem path resolution or warning output fails.
    """
    return [group[0] for group in collect_file_groups(paths, tool=tool)]


def private_paths(
    repo_root: str, paths: Collection[str], *, tool: str
) -> tuple[frozenset[str], str | None]:
    """Return repository-relative paths whose private attribute is set.

    An invalid configured attribute name prints a warning once per tool and
    value, then uses the default attribute.

    Args:
        repo_root: Repository root used to query Git attributes.
        paths: Repository-relative paths as Git reports them.
        tool: Tool name whose private-attribute configuration applies.

    Returns:
        The labelled paths and `None` on success, or an empty set and a failure
        detail when Git fails or its attribute output is malformed.

    Raises:
        OSError: If a configuration warning cannot be written.
    """
    labelled, detail = _labelled_private(
        repo_root, [encode_git_path(path) for path in paths], tool=tool
    )
    if detail is not None:
        return frozenset(), detail
    return frozenset(path for path in paths if encode_git_path(path) in labelled), None
