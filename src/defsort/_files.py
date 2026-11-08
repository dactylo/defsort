"""Shared file I/O utilities for defsort."""

import io
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from defsort._vendor.gitfiles._discovery import collect_file_groups as _shared_collect_file_groups
from defsort._vendor.gitfiles._discovery import collect_files as _shared_collect_files
from defsort._vendor.gitfiles._discovery import private_paths as _shared_private_paths
from defsort._vendor.srcfiles import write_in_place


@dataclass(frozen=True, slots=True)
class _ReadResult:
    """Result of reading exact Python source with its encoding and the bytes it came from."""

    source: str
    encoding: str
    raw: bytes


def collect_file_groups(paths: Iterable[str]) -> list[list[Path]]:
    """Expand paths into groups of Python file paths that name one file.

    Directory operands filter discovered files through standard Git exclusions
    when available and retain filesystem results on Git failure. Repeated
    operands and case, hard-link, and symlink aliases of one file share a
    group.

    Args:
        paths: File and directory paths to expand.

    Returns:
        Groups in stable traversal order, each listing its spellings in that
        order, including nonexistent explicit paths for later diagnostics.
    """
    return _shared_collect_file_groups(paths, tool='defsort')


def collect_files(paths: Iterable[str]) -> list[Path]:
    """Expand paths into a deduplicated Python file list.

    A file named more than once is kept once under its first spelling.

    Args:
        paths: File and directory paths to expand.

    Returns:
        The first spelling of each group from `collect_file_groups`.
    """
    return _shared_collect_files(paths, tool='defsort')


def encode_source(source: str, encoding: str) -> bytes:
    """Encode source exactly without changing line endings.

    Args:
        source: Exact decoded source text.
        encoding: Target encoding name.

    Returns:
        Encoded source bytes.

    Raises:
        LookupError: If the encoding name is unknown.
        UnicodeEncodeError: If the source cannot be represented in the
            requested encoding.
    """
    return source.encode(encoding)


def private_paths(repo_root: str, paths: list[str]) -> tuple[frozenset[str], str | None]:
    """Return repository-relative paths whose private attribute is set.

    Args:
        repo_root: Repository root used to query Git attributes.
        paths: Repository-relative paths as Git reports them.

    Returns:
        Labelled paths, or an empty set and the Git failure detail.
    """
    return _shared_private_paths(repo_root, paths, tool='defsort')


def read_file_result(path: Path) -> _ReadResult:
    """Read exact file contents with encoding detection.

    Reads raw bytes once, detects encoding via `tokenize` (matching
    `tokenize.open()` behavior for BOM and PEP 263 cookies), and decodes
    without changing line endings.

    Args:
        path: Path to the Python source file.

    Returns:
        Exact decoded source, its encoding, and the bytes it was decoded from.

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
    return _ReadResult(source=source, encoding=encoding, raw=raw)


def write_source(path: Path, content: str, *, encoding: str, raw: bytes) -> bytes:
    """Write exact source in place without text-mode newline conversion.

    The file is written through its own descriptor only while it still holds
    `raw`, so hard links, ownership, permissions, ACLs, and extended attributes
    stay as an ordinary write leaves them, and a symlink writes its target. On
    a write error, restoration of the original bytes is attempted.

    Args:
        path: Existing target file.
        content: Exact source text.
        encoding: Encoding to use for write-back.
        raw: The bytes read from `path`.

    Returns:
        The bytes the file now holds, for a later rollback.

    Raises:
        LookupError: If the encoding name is unknown; no write is attempted.
        UnicodeEncodeError: If the content cannot be represented in the
            requested encoding; no write is attempted.
        FileChangedError: If the file no longer holds `raw` or no longer
            exists.
        OSError: If the file cannot be written; restoration is attempted first.
        RestoreError: If restoration fails or a close error leaves the outcome
            uncertain.
    """
    data = encode_source(content, encoding)
    write_in_place(path, data, expected=raw, tool='defsort')
    return data
