"""Git plumbing for CLI baselines and hooks."""

import contextlib
import enum
import io
import os
import stat
import subprocess
import tokenize
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from defsort._files import encode_source
from defsort._git_executable import resolve_git_executable
from defsort._shell import normalize_msys_path
from defsort._types import BlobReadResult, BlobReadStatus, PreCommitAdd

_CAT_FILE_HEADER_FIELD_COUNT = 3
_GIT_RAW_DIFF_FIELD_COUNT = 5
_MINIMUM_BATCH_Z_GIT_VERSION = (2, 42)
_PATHSPEC_ESCAPES = dict(zip(b'abfnrtv\\"', b'\a\b\f\n\r\t\v\\"', strict=True))
_PATHSPEC_OCTAL_WIDTH = 3
_REGULAR_FILE_MODES = frozenset(('100644', '100755'))


class NonRegularPathspecError(ValueError):
    """The pathspec resource is not an ordinary file and must not be read."""


class RepoDiscoveryStatus(enum.Enum):
    """Classification of checked Git worktree discovery.

    Attributes:
        WORKTREE: Git successfully discovered a worktree.
        NOT_WORKTREE: Repository inspection confirmed there is no worktree.
        ERROR: Defsort could not establish repository state.
    """

    WORKTREE = 'worktree'
    NOT_WORKTREE = 'not-worktree'
    ERROR = 'error'


class UnsupportedGitVersionError(RuntimeError):
    """Raised when Git-backed sorting finds Git older than 2.42."""

    def __init__(self, version: str) -> None:
        """Initialize an error for an unsupported Git version.

        Args:
            version: Detected Git version string.
        """
        self.version = version
        super().__init__(
            f'defsort: Git 2.42 or newer is required for Git-backed sorting (found Git {version})'
        )


class _CatFileRequest(NamedTuple):
    """One keyed object request for `git cat-file --batch -Z`."""

    key: str
    object_name: bytes


@dataclass(frozen=True, slots=True)
class _RepoDiscoveryResult:
    """Checked Git worktree-discovery result."""

    status: RepoDiscoveryStatus
    root: str | None = None

    def __post_init__(self) -> None:
        """Validate the repository discovery status and root."""
        valid = (
            bool(self.root) if self.status is RepoDiscoveryStatus.WORKTREE else self.root is None
        )
        if not valid:
            msg = 'invalid repository discovery status/root combination'
            raise ValueError(msg)


class _StagedFileInfo(NamedTuple):
    """Information about a staged file from git diff --cached --raw."""

    path: str
    mode: str
    blob_hash: str
    old_hash: str
    status: str


def _add_arguments(tokens: Sequence[str]) -> list[str]:
    """Return the arguments after `add` in a literal `git [-C ROUTE] add ...` argv."""
    index = 3 if len(tokens) > 1 and tokens[1] == '-C' else 1
    return list(tokens[index + 1 :])


def _decode_nul_names(raw: bytes, *, py_only: bool = False) -> list[str]:
    """Decode a NUL-separated git path list, optionally keeping only `.py` files."""
    return [
        name.decode('utf-8', errors='surrogateescape')
        for name in raw.split(b'\0')
        if name and (not py_only or name.endswith(b'.py'))
    ]


def _decode_pathspec_record(record: bytes) -> bytes | None:
    """Decode a Git-quoted LF record, rejecting malformed or empty quoted paths."""
    if not record.startswith(b'"'):
        return record

    decoded = bytearray()
    index = 1
    while index < len(record):
        byte = record[index]
        index += 1
        if byte == 0:
            return None
        if byte == ord('"'):
            # Git ignores suffix bytes and consumes the decoded C string up to NUL.
            return bytes(decoded).split(b'\0', 1)[0] or None
        if byte != ord('\\'):
            decoded.append(byte)
            continue
        if index == len(record):
            return None
        byte = record[index]
        if byte in _PATHSPEC_ESCAPES:
            decoded.append(_PATHSPEC_ESCAPES[byte])
            index += 1
            continue
        octal = record[index : index + _PATHSPEC_OCTAL_WIDTH]
        if (
            len(octal) != _PATHSPEC_OCTAL_WIDTH
            or octal[0] not in b'0123'
            or any(digit not in b'01234567' for digit in octal[1:])
        ):
            return None
        decoded.append(int(octal, 8))
        index += _PATHSPEC_OCTAL_WIDTH
    return None


def _get_empty_tree_hash(cwd: str) -> str:
    """Return the empty-tree hash, using fixed SHA-1 fallback if `git mktree` fails."""
    result = subprocess.run(
        ['git', 'mktree'],  # noqa: S607
        input='',
        capture_output=True,
        text=True,
        encoding='utf-8',
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


def _get_staged_py_files(cwd: str) -> list[str] | None:
    """Get staged Python paths from `git diff --cached`."""
    result = subprocess.run(
        ['git', 'diff', '--cached', '--name-only', '--no-relative', '--diff-filter=d', '-z'],  # noqa: S607
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    if result.returncode != 0:
        return None
    return sorted(_decode_nul_names(result.stdout, py_only=True))


def _git_config_bool(key: str, cwd: str | None = None) -> bool | None:
    """Read a git config boolean, returning `None` if not set."""
    try:
        result = subprocess.run(  # noqa: S603
            ['git', 'config', '--type=bool', '--get', key],  # noqa: S607
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == 'true'


def _git_marker_present(cwd: str) -> bool | None:
    """Return whether cwd or a parent has a .git marker, or None on error."""
    try:
        path = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_dir():
        return None
    for directory in (path, *path.parents):
        try:
            (directory / '.git').lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return True
    return False


def _parse_add_pathspecs(arguments: list[str]) -> tuple[bool, list[str]]:
    """Separate supported all-file flags and literal pathspecs."""
    all_files = False
    pathspecs: list[str] = []
    flags_done = False
    for argument in arguments:
        if not flags_done and argument == '--':
            flags_done = True
            continue
        if not flags_done and argument in ('-A', '-u', '--all', '--update'):
            all_files = True
            continue
        pathspecs.append(normalize_msys_path(argument))
    return all_files, pathspecs


def _parse_cat_file_response(
    data: bytes,
    pos: int,
    request: _CatFileRequest,
) -> tuple[BlobReadResult, int] | None:
    """Parse one complete NUL-framed response, or report lost framing."""
    header_end = data.find(b'\0', pos)
    if header_end == -1:
        return None
    header = data[pos:header_end]
    if header == request.object_name + b' missing':
        return BlobReadResult(status=BlobReadStatus.MISSING), header_end + 1

    parts = header.rsplit(b' ', 2)
    if len(parts) != _CAT_FILE_HEADER_FIELD_COUNT:
        return None
    object_id, object_type, raw_size = parts
    try:
        size = int(raw_size)
    except ValueError:
        size = None
    if (
        not object_id
        or b' ' in object_id
        or not object_type
        or not raw_size.isdigit()
        or size is None
    ):
        return None

    content_start = header_end + 1
    content_end = content_start + size
    if content_end >= len(data) or data[content_end : content_end + 1] != b'\0':
        return None
    raw = data[content_start:content_end]
    next_pos = content_end + 1
    if object_type != b'blob':
        result = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
    else:
        try:
            encoding_probe = raw.replace(b'\0', b' ')
            detected = tokenize.detect_encoding(io.BytesIO(encoding_probe).readline)[0]
            content = raw.decode(detected)
        except (UnicodeDecodeError, SyntaxError):
            result = BlobReadResult(status=BlobReadStatus.DECODE_FAILED)
        else:
            result = BlobReadResult(
                status=BlobReadStatus.CONTENT,
                content=content,
                encoding=detected,
            )
    return result, next_pos


def _parse_cat_file_batch(
    data: bytes,
    requests: list[_CatFileRequest],
) -> dict[str, BlobReadResult]:
    """Parse one ordered `git cat-file --batch -Z` response stream."""
    results: dict[str, BlobReadResult] = {}

    def fail_from(start: int) -> dict[str, BlobReadResult]:
        for remaining in requests[start:]:
            results[remaining.key] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
        return results

    pos = 0
    for index, request in enumerate(requests):
        parsed = _parse_cat_file_response(data, pos, request)
        if parsed is None:
            return fail_from(index)
        result, pos = parsed
        results[request.key] = result
    return results


def _probe_git_version(cwd: str) -> tuple[str, tuple[int, int]] | None:
    """Read the leading Git major and minor version fields."""
    try:
        result = subprocess.run(
            ['git', '--version'],  # noqa: S607
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return None
    if result.returncode != 0:
        return None
    match result.stdout.strip().split():
        case ['git', 'version', version, *_]:
            pass
        case _:
            return None
    major, separator, remainder = version.partition('.')
    minor, _, _ = remainder.partition('.')
    if (
        not separator
        or not major.isascii()
        or not major.isdecimal()
        or not minor.isascii()
        or not minor.isdecimal()
    ):
        return None
    try:
        fields = int(major), int(minor)
    except ValueError:
        return None
    return version, fields


def apply_index_entries(cwd: str, entries: Collection[tuple[str, str, str]]) -> bool:
    """Apply prepared index entries via git update-index --index-info.

    Takes a list of (mode, blob_hash, path) tuples and writes them all in a
    single git update-index call using stdin. This is atomic at the git level
    -- either all entries update or none do.

    Args:
        cwd: Repository working directory.
        entries: File modes, blob hashes, and repository-relative paths.

    Returns:
        `True` on success, or `False` on failure.
    """
    if not entries:
        return True
    stdin_data = b''.join(
        f'{mode} {blob_hash}\t'.encode('ascii')
        + path.encode('utf-8', errors='surrogateescape')
        + b'\0'
        for mode, blob_hash, path in entries
    )
    result = subprocess.run(
        ['git', 'update-index', '-z', '--index-info'],  # noqa: S607
        input=stdin_data,
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    return result.returncode == 0


def detect_renames(cwd: str) -> set[str] | None:
    """Detect paths involved in staged renames.

    Uses `git diff --cached -M --diff-filter=R` to find renamed destination
    paths. These should be skipped in incremental mode because the destination
    has no meaningful baseline at the same path.

    Args:
        cwd: Repository working directory.

    Returns:
        Renamed destination paths, or `None` on Git failure.
    """
    try:
        r = subprocess.run(
            [  # noqa: S607
                'git',
                'diff',
                '--cached',
                '-M',
                '--diff-filter=R',
                '--name-only',
                '-z',
                '--no-relative',
            ],
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return set(_decode_nul_names(r.stdout))


def discover_repo_root(cwd: str) -> _RepoDiscoveryResult:
    """Classify cwd as a worktree, confirmed non-worktree, or discovery error.

    Args:
        cwd: Directory from which Git should inspect the repository.

    Returns:
        The checked discovery status and normalized worktree root, if present.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],  # noqa: S607
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)
    if result.returncode == 0:
        root = result.stdout.strip()
        if not root:
            return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)
        return _RepoDiscoveryResult(
            RepoDiscoveryStatus.WORKTREE,
            normalize_msys_path(root),
        )
    try:
        bare_result = subprocess.run(
            ['git', 'rev-parse', '--is-bare-repository'],  # noqa: S607
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return _RepoDiscoveryResult(RepoDiscoveryStatus.ERROR)
    if bare_result.returncode == 0:
        status = (
            RepoDiscoveryStatus.NOT_WORKTREE
            if bare_result.stdout.strip() == 'true'
            else RepoDiscoveryStatus.ERROR
        )
    else:
        marker_present = _git_marker_present(cwd)
        status = (
            RepoDiscoveryStatus.ERROR
            if marker_present is None or marker_present
            else RepoDiscoveryStatus.NOT_WORKTREE
        )
    return _RepoDiscoveryResult(status)


def get_modified_tracked_py_files(cwd: str) -> list[str] | None:
    """Get modified tracked .py files for auto-stage commits.

    Compares against HEAD to find all modified tracked files (both staged and
    unstaged). Uses --diff-filter=d to exclude deleted files. Uses
    -z for NUL-terminated output (safe for filenames with spaces).

    For initial commits (no HEAD), uses git's empty tree hash as the diff base
    to capture all staged files.

    Args:
        cwd: Repository working directory.

    Returns:
        Repository-relative paths, or `None` on failure, matching
        `get_staged_py_info()`'s fail-closed contract.
    """
    # Check if HEAD exists
    head_check = subprocess.run(
        ['git', 'rev-parse', '--verify', 'HEAD'],  # noqa: S607
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    diff_target = 'HEAD' if head_check.returncode == 0 else _get_empty_tree_hash(cwd)

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            'git',
            'diff',
            '-z',
            '--no-relative',
            '--no-renames',
            '--diff-filter=d',
            '--name-only',
            diff_target,
        ],
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    if result.returncode != 0:
        return None
    return sorted(_decode_nul_names(result.stdout, py_only=True))


def get_staged_py_info(cwd: str) -> list[_StagedFileInfo] | None:
    """Get staged .py file info (path, mode, blob hash) in a single git call.

    Uses `git diff --cached --raw -M` to retrieve all staged (non-deleted)
    files with their index modes and blob hashes, including rename/copy
    detection. Rename/copy entries use the destination path. Filters to `.py`
    files with regular file modes, skipping symlinks and submodules.

    Args:
        cwd: Repository working directory.

    Returns:
        Staged Python file records, or `None` when Git fails.
    """
    try:
        result = subprocess.run(
            [  # noqa: S607
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
            capture_output=True,
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    output = result.stdout.decode('utf-8', errors='surrogateescape')
    if not output:
        return []

    # Raw format with -z and -M:
    #   Normal entries:  :old_mode new_mode old_hash new_hash status\0path\0
    #   R/C entries:     :old_mode new_mode old_hash new_hash Rnnn\0old_path\0new_path\0
    entries: list[_StagedFileInfo] = []
    parts = output.split('\0')
    i = 0
    while i + 1 < len(parts):
        header = parts[i]

        # Header has 5 fields: old_mode, new_mode, old_hash, new_hash, status
        fields = header.lstrip(':').split()
        if len(fields) < _GIT_RAW_DIFF_FIELD_COUNT:
            i += 2  # fail-safe advance
            continue

        new_mode = fields[1]
        old_hash = fields[2]
        new_hash = fields[3]
        status = fields[4]

        # Rename/copy entries have 3 NUL-separated segments: header\0old\0new\0
        if status[0] in ('R', 'C'):
            if i + 2 >= len(parts):
                break
            path = parts[i + 2]  # destination path
            advance = 3
        else:
            path = parts[i + 1]
            advance = 2

        i += advance

        if not path.endswith('.py'):
            continue
        if new_mode not in _REGULAR_FILE_MODES:
            continue

        entries.append(_StagedFileInfo(path, new_mode, new_hash, old_hash, status))

    return sorted(entries, key=lambda e: e.path)


def get_unstaged_paths(cwd: str, paths: Collection[str]) -> frozenset[str] | None:
    """Return paths with worktree changes relative to the index.

    Uses Git's own comparison, so attributes, eol rules, filters, and
    core.autocrlf are handled by Git instead of reimplemented here.

    Args:
        cwd: Repository working directory.
        paths: Repository-relative paths to inspect.

    Returns:
        Paths with unstaged changes, or `None` on Git failure.
    """
    if not paths:
        return frozenset()
    try:
        result = subprocess.run(  # noqa: S603
            ['git', 'diff', '--name-only', '-z', '--no-ext-diff', '--', *paths],  # noqa: S607
            capture_output=True,
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return frozenset(_decode_nul_names(result.stdout))


def is_hook_enabled(cwd: str | None = None) -> bool:
    """Check if hook is enabled via git config.

    The hook defaults to enabled. Set `defsort.hook=false` to disable it for a
    repository or user.

    Args:
        cwd: Optional repository directory for the lookup.

    Returns:
        True when the hook should run.
    """
    enabled = _git_config_bool('defsort.hook', cwd=cwd)
    return True if enabled is None else enabled


def is_null_hash(h: str) -> bool:
    """Check if a Git hash is the all-zeros null hash.

    Args:
        h: SHA-1 or SHA-256 hash text.

    Returns:
        `True` when the nonempty hash contains only zeroes.
    """
    return len(h) > 0 and h == '0' * len(h)


def prepare_index_entry(
    cwd: str,
    path: str,
    content: str,
    mode: str,
    *,
    encoding: str = 'utf-8',
) -> tuple[str, str, str] | None:
    """Create a blob for sorted content, without touching the index.

    Takes a pre-resolved file mode from `get_staged_py_info()`.
    This is the first half of a two-phase index update: prepare all entries
    first, then apply them atomically with `apply_index_entries()`.
    Encoding preserves the content's exact line-ending sequence.

    Args:
        cwd: Repository working directory.
        path: Repository-relative file path.
        content: Exact source text for the new blob.
        mode: Pre-resolved Git file mode.
        encoding: Encoding for the blob content.

    Returns:
        The mode, new blob hash, and path, or `None` on failure.
    """
    hash_result = subprocess.run(
        ['git', 'hash-object', '-w', '--stdin'],  # noqa: S607
        input=encode_source(content, encoding),
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    if hash_result.returncode != 0:
        return None

    return (mode, hash_result.stdout.decode('ascii').strip(), path)


def read_pathspec_file(cwd: str, path: str) -> bytes | None:
    """Read an ordinary pathspec file, accepting symlinks to ordinary files.

    Args:
        cwd: Working directory for relative pathspec files.
        path: Pathspec filename, with MSYS path normalization applied.

    Returns:
        File bytes, including `b''` for an empty file, or `None` on I/O
        failure.

    Raises:
        NonRegularPathspecError: The path or opened descriptor is not a regular
            file.
    """
    resource = Path(normalize_msys_path(path))
    if not resource.is_absolute():
        resource = Path(cwd) / resource
    message = 'defsort: pathspec resource must be a regular file'
    try:
        if not stat.S_ISREG(resource.stat().st_mode):
            raise NonRegularPathspecError(message)
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(resource, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NonRegularPathspecError(message)
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)
    except OSError:
        return None


def repo_relative(path: str, repo_root: str) -> str:
    """Return a forward-slash-relative repository path.

    Args:
        path: Path to make relative.
        repo_root: Repository root directory.

    Returns:
        The path relative to `repo_root`, with forward slashes.
    """
    return os.path.relpath(path, repo_root).replace('\\', '/')


def require_batch_z_support(cwd: str) -> None:
    """Reject a known Git version that lacks `cat-file --batch -Z`.

    Args:
        cwd: Directory from which Git should report its version.

    Raises:
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    version = _probe_git_version(cwd)
    if version is not None and version[1] < _MINIMUM_BATCH_Z_GIT_VERSION:
        raise UnsupportedGitVersionError(version[0])


def _run_cat_file_batch(
    cwd: str,
    requests: list[_CatFileRequest],
) -> dict[str, BlobReadResult] | None:
    """Run one NUL-framed Git batch and parse its keyed responses."""
    if not requests:
        return {}
    input_data = b''.join(request.object_name + b'\0' for request in requests)
    try:
        proc = subprocess.run(
            ['git', 'cat-file', '--batch', '-Z'],  # noqa: S607
            input=input_data,
            capture_output=True,
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        require_batch_z_support(cwd)
        return None
    return _parse_cat_file_batch(proc.stdout, requests)


def _read_blobs_by_hash(
    cwd: str,
    requests: Sequence[tuple[str, str]],
) -> dict[str, BlobReadResult] | None:
    """Read Git blob contents by hash in a single `cat-file` call."""
    batch_requests = [
        _CatFileRequest(path, blob_hash.encode('ascii')) for path, blob_hash in requests
    ]
    return _run_cat_file_batch(cwd, batch_requests)


def batch_fetch_worktree_baselines(
    cwd: str,
    paths: Iterable[str],
) -> dict[str, BlobReadResult] | None:
    """Batch-read worktree baselines via `git cat-file --batch -Z`.

    Args:
        cwd: Repository working directory.
        paths: Repository-relative paths to read from `HEAD`.

    Returns:
        Results keyed by the original path, or `None` on a generic Git failure.

    Raises:
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    requests = [
        _CatFileRequest(path, b'HEAD:' + path.encode('utf-8', errors='surrogateescape'))
        for path in paths
    ]
    return _run_cat_file_batch(cwd, requests)


def read_baseline_blobs(
    cwd: str,
    info_list: Iterable[_StagedFileInfo],
) -> dict[str, BlobReadResult] | None:
    """Read baseline blob contents in one NUL-framed Git batch.

    Args:
        cwd: Repository working directory.
        info_list: Staged-file records whose old hashes identify baselines.

    Returns:
        Results keyed by path, or `None` on a generic Git failure.

    Raises:
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    return _read_blobs_by_hash(
        cwd,
        [(info.path, info.old_hash) for info in info_list if not is_null_hash(info.old_hash)],
    )


def read_staged_blobs(
    cwd: str,
    info_list: Iterable[_StagedFileInfo],
) -> dict[str, BlobReadResult] | None:
    """Read staged blob contents in one NUL-framed Git batch.

    Args:
        cwd: Repository working directory.
        info_list: Staged-file records whose new hashes identify contents.

    Returns:
        Results keyed by path, or `None` on a generic Git failure.

    Raises:
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    return _read_blobs_by_hash(
        cwd,
        [(info.path, info.blob_hash) for info in info_list],
    )


def resolve_pathspec_py_files(
    cwd: str,
    pathspecs: Iterable[str],
    *,
    repo_root: str,
    pathspec_file_nul: bool = False,
    pathspec_file_content: bytes | None = None,
) -> list[str] | None:
    """Resolve pathspecs to tracked .py files that exist on disk.

    Args:
        cwd: Working directory for Git.
        pathspecs: Pathspecs supplied directly to `git add`.
        repo_root: Repository root used to resolve matched files.
        pathspec_file_nul: Whether the pathspec file is NUL-delimited.
        pathspec_file_content: Validated file bytes; `None` supplies no file
            pathspecs.

    Returns:
        Sorted repository-relative Python paths, or `None` on failure.
    """
    all_pathspecs = list(pathspecs)
    if pathspec_file_content is not None:
        raw = pathspec_file_content
        records = (
            raw.split(b'\0')
            if pathspec_file_nul
            else [_decode_pathspec_record(part.removesuffix(b'\r')) for part in raw.split(b'\n')]
        )
        if any(record is None for record in records):
            return None
        all_pathspecs.extend(os.fsdecode(record) for record in records if record)

    if not all_pathspecs:
        return []

    result = subprocess.run(  # noqa: S603
        ['git', 'ls-files', '-z', '--full-name', '--', *all_pathspecs],  # noqa: S607
        capture_output=True,
        cwd=cwd,
        executable=resolve_git_executable(cwd=cwd),
        check=False,
    )
    if result.returncode != 0:
        return None

    py_paths: list[str] = []
    for name in _decode_nul_names(result.stdout, py_only=True):
        full_path = Path(repo_root) / name
        if full_path.exists() and not full_path.is_symlink():
            py_paths.append(name)

    return sorted(py_paths)


def run_pre_commit_adds(adds: Iterable[PreCommitAdd]) -> None:
    """Execute git add commands for pre-commit compound commands.

    Iterates PreCommitAdd objects, replaying each add's literal argv in its
    exec_cwd. Failures are silently ignored (check=False).

    Args:
        adds: Supported pre-commit `git add` commands to execute.
    """
    for add in adds:
        tokens = list(add.tokens)
        with contextlib.suppress(OSError):
            subprocess.run(  # noqa: S603
                tokens,
                cwd=add.exec_cwd,
                executable=resolve_git_executable(tokens[0], cwd=add.exec_cwd),
                capture_output=True,
                check=False,
            )


def try_get_repo_root(cwd: str) -> str | None:
    """Resolve the Git repository root.

    Args:
        cwd: Directory from which Git should resolve the repository.

    Returns:
        The normalized repository root, or `None` on failure.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],  # noqa: S607
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd,
            executable=resolve_git_executable(cwd=cwd),
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return None
    if result.returncode == 0 and result.stdout.strip():
        return normalize_msys_path(result.stdout.strip())
    return None


def get_repo_root(cwd: str) -> str:
    """Resolve the git repository root directory.

    Args:
        cwd: Directory from which Git should resolve the repository.

    Returns:
        The output of `git rev-parse --show-toplevel`, or `cwd` when the
        command fails.
    """
    return try_get_repo_root(cwd) or cwd


def resolve_sync_paths(adds: Iterable[PreCommitAdd], repo_root: str) -> frozenset[str]:
    """Determine which staged .py files will be re-staged by git add.

    Non-flag arguments are resolved with Git pathspec semantics through `git
    ls-files` in each add's Git CWD. Adds targeting another repository are
    ignored. `-A`/`-u`/`--all`/`--update`, and any Git resolution failure after
    confirming the current repository context, fall back to the currently
    staged `.py` set.

    Args:
        adds: Literal pre-commit adds from an accepted commit plan.
        repo_root: Repository root for the staged-set fallback.

    Returns:
        Repository-relative Python paths that a later add will restage.
    """
    all_files = False
    paths: set[str] = set()
    for add in adds:
        if try_get_repo_root(add.git_cwd) != repo_root:
            continue
        add_all_files, pathspecs = _parse_add_pathspecs(_add_arguments(add.tokens))
        all_files |= add_all_files
        if not pathspecs:
            continue
        resolved = resolve_pathspec_py_files(
            add.git_cwd,
            pathspecs,
            repo_root=repo_root,
        )
        if resolved is None:
            all_files = True
            continue
        paths.update(resolved)
    if all_files:
        staged = _get_staged_py_files(repo_root)
        if staged:
            return frozenset(staged)
        if staged is None:
            return frozenset(paths)
    return frozenset(paths)
