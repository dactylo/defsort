"""Git plumbing for CLI baselines and hooks."""

import io
import os
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from defsort._files import encode_source
from defsort._types import BlobReadResult, BlobReadStatus
from defsort._vendor.gitfiles._blobs import BlobRead, read_blobs_by_hash, read_blobs_by_name
from defsort._vendor.gitfiles._blobs import (
    require_batch_z_support as _shared_require_batch_z_support,
)
from defsort._vendor.gitfiles._config import is_hook_enabled as _shared_is_hook_enabled
from defsort._vendor.gitfiles._index import IndexEntry, hash_object
from defsort._vendor.gitfiles._listings import (
    StagedFileInfo,
    get_tracked_files,
    head_rename_sources,
)
from defsort._vendor.hookfront._terminal import path_text
from defsort._vendor.srcfiles import (
    SpellingIndex,
    encode_git_path,
    repository_key,
)

_BY_NAME_PURPOSE = 'incremental mode and the reorder guard on working-tree files'


def _blob_result(read: BlobRead) -> BlobReadResult:
    """Decode one shared blob result as Python source or a baseline failure."""
    if read.status == 'missing':
        return BlobReadResult(status=BlobReadStatus.MISSING)
    if read.status == 'not-blob':
        return BlobReadResult(status=BlobReadStatus.INFRA_ERROR, detail='git object is not a blob')
    if read.status == 'unavailable':
        return BlobReadResult(
            status=BlobReadStatus.INFRA_ERROR, detail='unexpected git cat-file output'
        )
    raw = read.data
    try:
        encoding_probe = raw.replace(b'\0', b' ')
        detected = tokenize.detect_encoding(io.BytesIO(encoding_probe).readline)[0]
        content = raw.decode(detected)
    except (UnicodeDecodeError, SyntaxError) as error:
        detail = error.reason if isinstance(error, UnicodeDecodeError) else error.msg
        return BlobReadResult(status=BlobReadStatus.DECODE_FAILED, detail=detail)
    return BlobReadResult(status=BlobReadStatus.CONTENT, content=content, encoding=detected)


def _read_blobs_by_hash(
    cwd: str,
    requests: Sequence[tuple[str, str]],
) -> dict[str, BlobReadResult]:
    """Read Git blob contents by hash in a single `cat-file` call."""
    reads = read_blobs_by_hash(cwd, [blob_hash for _, blob_hash in requests])
    return {path: _blob_result(read) for (path, _), read in zip(requests, reads, strict=True)}


def batch_fetch_worktree_baselines(
    cwd: str,
    paths: Iterable[str],
    *,
    sources: Mapping[str, str] | None = None,
) -> dict[str, BlobReadResult]:
    """Batch-read worktree baselines via `git cat-file --batch -Z`.

    Args:
        cwd: Repository working directory.
        paths: Repository-relative destination paths.
        sources: Optional mapping from destinations to their rename sources in
            `HEAD`. Unmapped paths are read under their own names.

    Returns:
        Results keyed by the original path, with per-object failures recorded
        in each result.

    Raises:
        GitCommandError: Git could not run or returned an unsuccessful status.
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    keys = list(paths)
    names = [sources.get(path, path) for path in keys] if sources is not None else keys
    reads = read_blobs_by_name(
        cwd,
        [b'HEAD:' + encode_git_path(path) for path in names],
        tool='defsort',
        purpose=_BY_NAME_PURPOSE,
    )
    return {path: _blob_result(read) for path, read in zip(keys, reads, strict=True)}


def is_hook_enabled(cwd: str | None = None) -> bool:
    """Check if hook is enabled via git config.

    The hook defaults to enabled. Set `defsort.hook=false` to disable it for a
    repository or user.

    Args:
        cwd: Optional repository directory for the lookup.

    Returns:
        `True` when the hook should run.

    Raises:
        GitCommandError: If Git cannot read the hook setting.
    """
    return _shared_is_hook_enabled(cwd, tool='defsort')


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
) -> IndexEntry:
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
        The mode, new blob hash, and path.

    Raises:
        LookupError: If the encoding name is unknown.
        UnicodeEncodeError: If the content cannot be represented in the
            requested encoding.
        GitCommandError: Git could not run or returned an unsuccessful status.
    """
    return IndexEntry(mode, hash_object(cwd, encode_source(content, encoding)), path)


def read_baseline_blobs(
    cwd: str,
    info_list: Iterable[StagedFileInfo],
) -> dict[str, BlobReadResult]:
    """Read baseline blob contents in one LF-framed Git batch.

    Args:
        cwd: Repository working directory.
        info_list: Staged-file records whose old hashes identify baselines.

    Returns:
        Results keyed by path, with per-object failures recorded in each
        result.

    Raises:
        GitCommandError: Git could not run or returned an unsuccessful status.
    """
    return _read_blobs_by_hash(
        cwd,
        [(info.path, info.old_hash) for info in info_list if not is_null_hash(info.old_hash)],
    )


def read_head_baselines(repo_root: str, paths: Sequence[Path]) -> dict[Path, BlobReadResult]:
    """Read HEAD baselines, following rename sources and missing spellings.

    Args:
        repo_root: Repository working-tree root.
        paths: Caller paths whose identities select the baselines.

    Returns:
        Results keyed by the caller's paths. Exact HEAD matches are final.
        Missing names first retry their rename sources. Remaining missing
        spellings use at most one tracked-path query and one retry batch.
        Ambiguous identities become per-path infrastructure errors;
        untracked files remain missing.

    Raises:
        GitCommandError: A baseline batch, rename query, or tracked-file query
            failed.
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    unavailable = BlobReadResult(
        status=BlobReadStatus.INFRA_ERROR,
        detail='cannot match the file to a single path in Git',
    )
    keys = {path: repository_key(path, repo_root) for path in paths}
    exact = batch_fetch_worktree_baselines(
        repo_root, [key for key in keys.values() if key is not None]
    )
    results = {path: exact[key] if key is not None else unavailable for path, key in keys.items()}
    misses = {
        path: key
        for path, key in keys.items()
        if key is not None and results[path].status is BlobReadStatus.MISSING
    }
    if not misses:
        return results
    sources = head_rename_sources(repo_root)
    renamed = {path: sources[key] for path, key in misses.items() if key in sources}
    if renamed:
        retried_sources = batch_fetch_worktree_baselines(repo_root, renamed.values())
        results.update((path, retried_sources[source]) for path, source in renamed.items())
        misses = {
            path: key
            for path, key in misses.items()
            if results[path].status is BlobReadStatus.MISSING
        }
        if not misses:
            return results
    index = SpellingIndex(get_tracked_files(repo_root))
    retries: dict[Path, str] = {}
    for path, key in misses.items():
        matched = index.match(path, repo_root, key)
        if matched.status == 'unavailable':
            results[path] = unavailable
        elif matched.key is not None and matched.key != key:
            retries[path] = sources.get(matched.key, matched.key)
    if retries:
        retried = batch_fetch_worktree_baselines(repo_root, retries.values())
        results.update((path, retried[key]) for path, key in retries.items())
    return results


def read_staged_blobs(
    cwd: str,
    info_list: Iterable[StagedFileInfo],
) -> dict[str, BlobReadResult]:
    """Read staged blob contents in one LF-framed Git batch.

    Args:
        cwd: Repository working directory.
        info_list: Staged-file records whose new hashes identify contents.

    Returns:
        Results keyed by path, with per-object failures recorded in each
        result.

    Raises:
        GitCommandError: Git could not run or returned an unsuccessful status.
    """
    return _read_blobs_by_hash(
        cwd,
        [(info.path, info.blob_hash) for info in info_list],
    )


def repo_relative(path: str, repo_root: str) -> str:
    """Return a forward-slash-relative repository path.

    Args:
        path: Path to make relative.
        repo_root: Repository root directory.

    Returns:
        The path relative to `repo_root`, with forward slashes.
    """
    return path_text(os.path.relpath(path, repo_root))


def require_batch_z_support(cwd: str) -> None:
    """Reject a known Git version that lacks `cat-file --batch -Z`.

    Args:
        cwd: Directory from which Git should report its version.

    Raises:
        UnsupportedGitVersionError: Git is older than 2.42.
    """
    _shared_require_batch_z_support(cwd, tool='defsort', purpose=_BY_NAME_PURPOSE)
