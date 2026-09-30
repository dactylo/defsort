"""Shared file I/O utilities for defsort."""

import contextlib
import errno
import io
import os
import stat
import subprocess
import sys
import tempfile
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from defsort._git_executable import resolve_git_executable

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


@dataclass(frozen=True, slots=True)
class _ReadResult:
    """Result of reading exact Python source with its encoding."""

    source: str
    encoding: str


def _filter_git_ignored(directory: Path, files: list[Path]) -> list[Path]:
    """Remove Git-ignored paths while retaining the input on Git failure."""
    if not files:
        return files
    relative_files = [(path, os.fsencode(os.path.relpath(path, directory))) for path in files]
    try:
        result = subprocess.run(
            [  # noqa: S607
                'git',
                'check-ignore',
                '-z',
                '--stdin',
            ],
            capture_output=True,
            cwd=directory,
            executable=resolve_git_executable(cwd=directory),
            input=b'\0'.join(raw_path for _, raw_path in relative_files) + b'\0',
            check=False,
        )
    except OSError:
        return files
    if result.returncode not in (0, 1):
        return files
    ignored = frozenset(result.stdout.split(b'\0'))
    return [path for path, raw_path in relative_files if raw_path not in ignored]


def _collect_directory(directory: Path) -> list[Path]:
    """Collect Python files from one directory operand."""
    children: list[Path] = []
    nested_worktrees: list[Path] = []
    for root, dirnames, filenames in os.walk(directory):
        current = Path(root)
        if current != directory and ('.git' in dirnames or '.git' in filenames):
            nested_worktrees.append(current)
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        children.extend(
            child
            for name in filenames
            if name.endswith('.py') and not (child := current / name).is_symlink()
        )

    by_worktree: dict[Path, list[Path]] = {directory: []}
    included_worktrees = {directory}
    for root in nested_worktrees:
        parent = next(
            (
                candidate
                for candidate in reversed(nested_worktrees)
                if candidate != root and root.is_relative_to(candidate)
            ),
            directory,
        )
        if parent in included_worktrees and _filter_git_ignored(parent, [root]):
            included_worktrees.add(root)
    for path in sorted(children):
        owner = next(
            (root for root in reversed(nested_worktrees) if path.is_relative_to(root)),
            directory,
        )
        if owner in included_worktrees:
            by_worktree.setdefault(owner, []).append(path)
    return sorted(
        path
        for root, worktree_files in by_worktree.items()
        for path in _filter_git_ignored(root, worktree_files)
    )


def collect_files(paths: Iterable[str]) -> list[Path]:
    """Expand paths into a deduplicated Python file list.

    Directory operands filter discovered files through standard Git exclusions
    when available and retain filesystem results on Git failure.

    Args:
        paths: File and directory paths to expand.

    Returns:
        Python files in stable traversal order, including nonexistent explicit
        paths for later diagnostics.
    """
    result: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            result.extend(_collect_directory(p))
        elif p.suffix == '.py' or not p.exists():
            result.append(p)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in result:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(p)
    return deduped


def encode_source(source: str, encoding: str) -> bytes:
    """Encode source exactly without changing line endings.

    Args:
        source: Exact decoded source text.
        encoding: Target encoding name.

    Returns:
        Encoded source bytes.
    """
    return source.encode(encoding)


def ensure_utf8_stdio() -> None:
    """Reconfigure stdio: UTF-8 on Windows, escaped stdout everywhere.

    Standard output renders unencodable characters (for example
    surrogateescaped filesystem names) with `backslashreplace`, matching the
    standard-error default, instead of raising `UnicodeEncodeError`. Windows
    additionally forces UTF-8 for all three streams, preserving each existing
    error handler where it is not overridden here.
    """
    if hasattr(sys.stdout, 'reconfigure'):
        if sys.platform == 'win32':
            sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
        else:
            sys.stdout.reconfigure(errors='backslashreplace')  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
    if sys.platform != 'win32':
        return
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8', errors=sys.stdin.errors)  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors=sys.stderr.errors)  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]


def read_file_result(path: Path) -> _ReadResult:
    """Read exact file contents with encoding detection.

    Reads raw bytes once, detects encoding via tokenize (matching
    tokenize.open() behavior for BOM and PEP 263 cookies), and decodes without
    changing line endings.

    Args:
        path: Path to the Python source file.

    Returns:
        Exact decoded source plus its encoding.

    Raises:
        OSError: If the file cannot be read.
        SyntaxError: If the encoding cookie is malformed or unknown.
        UnicodeDecodeError: If the file bytes are invalid for the declared
            encoding.
    """
    raw = path.read_bytes()
    encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
    source = raw.decode(encoding)
    source = source.removeprefix('\ufeff')
    return _ReadResult(source=source, encoding=encoding)


def read_file(path: Path) -> tuple[str, str]:
    """Read file contents, auto-detecting encoding via PEP 263.

    Uses the exact binary decode boundary from `read_file_result()`, which
    handles UTF-8 BOM, encoding cookies, and the UTF-8 default.

    Args:
        path: Path to the Python source file.

    Returns:
        Tuple of (source_text, encoding_name).

    Raises:
        OSError: If the file cannot be read.
        SyntaxError: If the encoding cookie is malformed or unknown.
        UnicodeDecodeError: If the file bytes are invalid for the declared
            encoding.
    """
    r = read_file_result(path)
    return r.source, r.encoding


def write_source(
    path: Path,
    content: str,
    *,
    encoding: str = 'utf-8',
) -> None:
    """Write exact source bytes atomically without text-mode newline conversion.

    Writes to a temporary file in the target's directory and replaces the
    target with `os.replace`, so a failed write never truncates the target.
    A symlink target is resolved first, so the linked-to file is replaced.
    Permission bits of an existing target are preserved; other inode metadata
    (ownership, ACLs, extended attributes) is not. The target's parent
    directory must be writable.

    Args:
        path: Target file path.
        content: Exact source text.
        encoding: Encoding to use for write-back (default 'utf-8').

    Raises:
        OSError: If the target is not writable or the write or replace fails.
            The target is unchanged in every failure case.
    """
    data = encode_source(content, encoding)
    target = path.resolve()
    try:
        mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None
    if mode is not None and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
    if mode is None:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp_name = tempfile.mkstemp(prefix='.defsort-', suffix='.tmp', dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as tmp_file:
            tmp_file.write(data)
        tmp_path.chmod(mode)
        tmp_path.replace(target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
