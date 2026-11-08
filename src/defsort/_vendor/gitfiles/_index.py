"""Write prepared blobs and entries to the Git index."""

from collections.abc import Collection
from typing import NamedTuple

from defsort._vendor.srcfiles import (
    encode_git_path,
)

from ._run import (
    run_git,
)


class IndexEntry(NamedTuple):
    """A prepared Git index entry.

    Attributes:
        mode: Git file mode.
        blob_hash: Object ID of the prepared content.
        path: Repository-relative path to update.
    """

    mode: str
    blob_hash: str
    path: str


def apply_index_entries(cwd: str, entries: Collection[IndexEntry]) -> None:
    """Apply prepared entries in a single NUL-delimited Git index update.

    Args:
        cwd: Repository working directory.
        entries: Prepared modes, object IDs, and repository-relative paths.

    Raises:
        GitCommandError: Git could not update the index.
    """
    if not entries:
        return
    records = b''.join(
        entry.mode.encode('ascii')
        + b' '
        + entry.blob_hash.encode('ascii')
        + b'\t'
        + encode_git_path(entry.path)
        + b'\0'
        for entry in entries
    )
    run_git(['git', 'update-index', '-z', '--index-info'], cwd, stdin=records)


def hash_object(cwd: str, data: bytes) -> str:
    """Store exact bytes as a Git blob and return its ASCII object ID.

    Args:
        cwd: Repository working directory.
        data: Exact blob bytes, including any original line endings.

    Raises:
        GitCommandError: Git could not write the blob.
    """
    result = run_git(['git', 'hash-object', '-w', '--stdin'], cwd, stdin=data)
    return result.stdout.strip().decode('ascii')
