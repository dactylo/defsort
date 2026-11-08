"""Read raw Git objects through ordered batch streams."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from ._run import GitCommandError, git_failure_detail, run_git

_HEADER_FIELD_COUNT = 3
_MINIMUM_BATCH_Z_VERSION = (2, 42)


@dataclass(frozen=True, slots=True)
class BlobRead:
    """Represent one requested object's bytes or its availability.

    Attributes:
        status: `content` for a blob, `missing` for an absent object,
            `not-blob` for another object type, or `unavailable` after framing
            is lost.
        data: Exact object bytes for `content`; otherwise empty.
    """

    status: Literal['content', 'missing', 'not-blob', 'unavailable']
    data: bytes = b''


class UnsupportedGitVersionError(RuntimeError):
    """Report that the installed Git cannot read NUL-framed batches."""

    def __init__(self, version: str, *, tool: str, purpose: str) -> None:
        """Identify the tool and operation requiring Git 2.42 or newer."""
        self._version = version
        super().__init__(
            f'{tool}: Git 2.42 or newer is required for {purpose} (found Git {self._version})'
        )


def _parse_header(header: bytes, request: bytes, delimiter: bytes) -> tuple[bytes, int] | None:
    """Validate a content header and retain each framing format's size rules."""
    parts = header.rsplit(b' ', 2)
    if len(parts) != _HEADER_FIELD_COUNT:
        return None
    object_id, object_type, raw_size = parts
    if (
        not object_id
        or b' ' in object_id
        or not object_type
        or (delimiter == b'\n' and object_id != request)
        or (delimiter == b'\0' and not raw_size.isdigit())
    ):
        return None
    try:
        size = int(raw_size)
    except ValueError:
        return None
    if size < 0:
        return None
    return object_type, size


def _parse_response(
    data: bytes, pos: int, request: bytes, delimiter: bytes
) -> tuple[BlobRead, int] | None:
    """Parse one complete response, or report lost framing."""
    header_end = data.find(delimiter, pos)
    if header_end == -1:
        return None
    header = data[pos:header_end]
    if header == request + b' missing':
        return BlobRead('missing'), header_end + 1
    parsed = _parse_header(header, request, delimiter)
    if parsed is None:
        return None
    object_type, size = parsed

    content_start = header_end + 1
    content_end = content_start + size
    if content_end >= len(data) or data[content_end : content_end + 1] != delimiter:
        return None
    if object_type != b'blob':
        return BlobRead('not-blob'), content_end + 1
    return BlobRead('content', data[content_start:content_end]), content_end + 1


def _parse_batch(data: bytes, requests: Sequence[bytes], delimiter: bytes) -> list[BlobRead]:
    """Retain resolved responses and mark the rest unavailable after framing loss."""
    results: list[BlobRead] = []
    pos = 0
    for index, request in enumerate(requests):
        parsed = _parse_response(data, pos, request, delimiter)
        if parsed is None:
            results.extend(BlobRead('unavailable') for _ in requests[index:])
            break
        result, pos = parsed
        results.append(result)
    return results


def _probe_git_version(cwd: str) -> tuple[str, tuple[int, int]] | None:
    """Read the leading Git major and minor version fields when recognizable."""
    try:
        output = run_git(['git', '--version'], cwd).stdout.decode('utf-8')
    except (GitCommandError, UnicodeDecodeError):
        return None
    match output.strip().split():
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


def read_blobs_by_hash(cwd: str, hashes: Collection[str]) -> list[BlobRead]:
    """Read exact blob bytes by object ID using LF-framed batch requests.

    Args:
        cwd: Repository working directory.
        hashes: Full object IDs in request order; empty requests launch
            nothing.

    Returns:
        One result per request, in order. Lost framing makes the affected and
        remaining requests unavailable. A framed non-blob affects only itself.

    Raises:
        GitCommandError: If Git cannot run or the batch command fails.
    """
    if not hashes:
        return []
    requests = [value.encode() for value in hashes]
    result = run_git(['git', 'cat-file', '--batch'], cwd, stdin=b'\n'.join(requests) + b'\n')
    return _parse_batch(result.stdout, requests, b'\n')


def require_batch_z_support(cwd: str, *, tool: str, purpose: str) -> None:
    """Reject known Git versions older than 2.42; allow unknown versions.

    Args:
        cwd: Working directory for the version probe.
        tool: Calling tool's name for the error message.
        purpose: Operation requiring NUL-framed batches, for the error message.

    Raises:
        UnsupportedGitVersionError: If Git reports a version older than 2.42.
    """
    probe = _probe_git_version(cwd)
    if probe is not None:
        version, fields = probe
        if fields < _MINIMUM_BATCH_Z_VERSION:
            raise UnsupportedGitVersionError(version, tool=tool, purpose=purpose)


def read_blobs_by_name(
    cwd: str, names: Sequence[bytes], *, tool: str, purpose: str
) -> list[BlobRead]:
    """Read exact blob bytes by object name using NUL-framed batch requests.

    Args:
        cwd: Repository working directory.
        names: Encoded object names, including IDs or `HEAD:<path>`
            expressions, in request order; empty requests launch nothing.
        tool: Calling tool's name for the version error message.
        purpose: Operation requiring NUL-framed batches, for the error message.

    Returns:
        One result per request, in order. Lost framing makes the affected and
        remaining requests unavailable. A framed non-blob affects only itself.

    Raises:
        GitCommandError: If Git cannot run or the batch command fails.
        UnsupportedGitVersionError: If the command fails and Git is older than
            2.42. Launch failures do not probe the version.
    """
    if not names:
        return []
    result = run_git(
        ['git', 'cat-file', '--batch', '-Z'],
        cwd,
        stdin=b'\0'.join(names) + b'\0',
        allowed_returncodes=None,
    )
    if result.returncode != 0:
        require_batch_z_support(cwd, tool=tool, purpose=purpose)
        raise GitCommandError(git_failure_detail(result))
    return _parse_batch(result.stdout, names, b'\0')
