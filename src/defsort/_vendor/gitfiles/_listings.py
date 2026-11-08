"""List Git paths and resolve tracked Python pathspec matches."""

from collections.abc import Collection, Iterator
from pathlib import Path
from typing import NamedTuple

from defsort._vendor.srcfiles import (
    decode_git_path,
    tracked_paths,
    tracked_query,
)

from ._run import (
    GitCommandError,
    run_git,
)

_EMPTY_TREE_SHA1 = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
_GIT_RAW_DIFF_FIELD_COUNT = 5
_REGULAR_FILE_MODES = frozenset(('100644', '100755'))


class StagedFileInfo(NamedTuple):
    """Information about a staged Python file from Git's raw diff.

    Attributes:
        path: Repository-relative destination path.
        mode: New Git file mode.
        blob_hash: New staged object ID.
        old_hash: Previous object ID, including for renames.
        status: Git raw-diff status code.
    """

    path: str
    mode: str
    blob_hash: str
    old_hash: str
    status: str


def _get_empty_tree_hash(cwd: str) -> str:
    """Return the repository's empty tree, with a fixed SHA-1 fallback."""
    try:
        result = run_git(['git', 'mktree'], cwd, stdin=b'', allowed_returncodes=None)
    except GitCommandError:
        return _EMPTY_TREE_SHA1
    if result.returncode == 0 and (tree_hash := result.stdout.strip()):
        return tree_hash.decode('ascii')
    return _EMPTY_TREE_SHA1


def _iter_raw_diff_records(output: bytes) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield raw diff headers with their decoded path fields."""
    parts = output.split(b'\0')
    index = 0
    while index + 1 < len(parts):
        header = parts[index].decode('utf-8', errors='surrogateescape')
        fields = header.lstrip(':').split()
        if len(fields) < _GIT_RAW_DIFF_FIELD_COUNT:
            index += 2
            continue

        status = fields[4]
        if status[0] in ('R', 'C'):
            if index + 2 >= len(parts):
                break
            yield (
                header,
                (
                    decode_git_path(parts[index + 1]),
                    decode_git_path(parts[index + 2]),
                ),
            )
            index += 3
        else:
            yield header, (decode_git_path(parts[index + 1]),)
            index += 2


def get_modified_tracked_py_files(cwd: str) -> list[str]:
    """Return sorted modified tracked Python paths against HEAD or an empty tree.

    Args:
        cwd: Repository working directory.

    Raises:
        GitCommandError: Git could not check HEAD or complete the diff.
    """
    head = run_git(['git', 'rev-parse', '--verify', 'HEAD'], cwd, allowed_returncodes=None)
    target = 'HEAD' if head.returncode == 0 else _get_empty_tree_hash(cwd)
    result = run_git(
        [
            'git',
            'diff',
            '-z',
            '--no-relative',
            '--no-renames',
            '--diff-filter=d',
            '--name-only',
            target,
        ],
        cwd,
    )
    return sorted(
        decode_git_path(name) for name in result.stdout.split(b'\0') if name.endswith(b'.py')
    )


def get_staged_py_info(cwd: str) -> list[StagedFileInfo]:
    """Return sorted staged regular Python files with raw-diff metadata.

    Rename and copy entries use the destination path while retaining the source
    object ID and Git status.

    Args:
        cwd: Repository working directory.

    Raises:
        GitCommandError: Git could not complete the staged diff.
    """
    result = run_git(
        [
            'git',
            'diff',
            '--cached',
            '--raw',
            '-z',
            '--diff-filter=d',
            '-M',
            '--no-relative',
            '--no-abbrev',
        ],
        cwd,
    )
    entries: list[StagedFileInfo] = []
    for header, paths in _iter_raw_diff_records(result.stdout):
        fields = header.lstrip(':').split()
        path = paths[-1]
        mode = fields[1]
        if path.endswith('.py') and mode in _REGULAR_FILE_MODES:
            entries.append(StagedFileInfo(path, mode, fields[3], fields[2], fields[4]))
    return sorted(entries, key=lambda entry: entry.path)


def get_tracked_files(repo_root: str) -> frozenset[str]:
    """Return tracked paths in Git's spelling from one bounded query.

    Raises:
        GitCommandError: Git could not complete the tracked path query.
    """
    result = run_git(tracked_query(), repo_root)
    return tracked_paths(result.stdout)


def is_head_unborn(cwd: str) -> bool:
    """Report whether `HEAD` names a branch that has no commits yet.

    Args:
        cwd: Working directory for Git commands.

    Returns:
        `True` only when Git confirms an unborn branch; `False` for any other
        state, including Git failure.
    """
    try:
        run_git(['git', 'symbolic-ref', '--quiet', 'HEAD'], cwd)
        head = run_git(
            ['git', 'rev-parse', '--verify', '--quiet', 'HEAD'],
            cwd,
            allowed_returncodes=(0, 1),
        )
    except GitCommandError:
        return False
    return head.returncode == 1


def head_rename_sources(cwd: str) -> dict[str, str]:
    """Map renamed working-tree destinations to their paths in `HEAD`.

    Args:
        cwd: Repository working directory.

    Returns:
        Destination-to-source paths, or an empty mapping for an unborn `HEAD`.

    Raises:
        GitCommandError: Git could not complete the diff for a born `HEAD`.
    """
    if is_head_unborn(cwd):
        return {}
    result = run_git(
        [
            'git',
            'diff',
            '--raw',
            '-z',
            '-M',
            '--no-relative',
            '--no-abbrev',
            '--diff-filter=R',
            'HEAD',
            '--',
        ],
        cwd,
    )
    sources: dict[str, str] = {}
    for header, paths in _iter_raw_diff_records(result.stdout):
        if header.lstrip(':').split()[4].startswith('R'):
            sources[paths[1]] = paths[0]
    return sources


def match_tracked_py_files(cwd: str, pathspecs: Collection[str], *, repo_root: str) -> list[str]:
    """Resolve pathspecs to sorted tracked Python files present on disk.

    Args:
        cwd: Working directory for Git pathspec interpretation.
        pathspecs: Git pathspecs to match; an empty collection makes no query.
        repo_root: Repository root used to check file existence and symlinks.

    Raises:
        GitCommandError: Git could not complete the pathspec query.
    """
    if not pathspecs:
        return []
    result = run_git(['git', 'ls-files', '-z', '--full-name', '--', *pathspecs], cwd)
    paths: list[str] = []
    for raw in result.stdout.split(b'\0'):
        if not raw.endswith(b'.py'):
            continue
        name = decode_git_path(raw)
        path = Path(repo_root) / name
        if path.exists() and not path.is_symlink():
            paths.append(name)
    return sorted(paths)
