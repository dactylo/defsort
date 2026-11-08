"""Read and decode Git pathspec files."""

import os
import stat
from pathlib import Path

from defsort._vendor.hookfront._terminal import (
    escape_terminal_text,
)
from defsort._vendor.hookshell._shell import (
    normalize_msys_path,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
)

_PATHSPEC_ESCAPES = dict(zip(b'abfnrtv\\"', b'\a\b\f\n\r\t\v\\"', strict=True))
_PATHSPEC_OCTAL_WIDTH = 3
_SURROGATE_FIRST = 0xD800
_SURROGATE_LAST = 0xDFFF
_WINDOWS = os.name == 'nt'


class NonRegularPathspecError(Exception):
    """A pathspec resource is not a regular file after following symlinks."""


class PathspecFileError(Exception):
    """A pathspec file has an unusable record.

    Attributes:
        detail: Reason the file cannot supply pathspecs.
    """

    def __init__(self, detail: str) -> None:
        """Keep the caller-facing reason for an invalid record.

        Args:
            detail: Reason the file cannot supply pathspecs.
        """
        super().__init__(detail)
        self.detail = detail


def _decode_pathspec_record(record: bytes) -> bytes | None:
    """Decode a Git-quoted LF record, rejecting malformed quoted paths."""
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
            return bytes(decoded).split(b'\0', 1)[0]
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


def parse_pathspec_records(data: bytes, *, nul: bool) -> list[str]:
    """Decode Git's NUL-literal or LF-delimited pathspec-file records.

    Args:
        data: Exact bytes read from a pathspec file.
        nul: Whether Git's `--pathspec-file-nul` mode is active.

    Returns:
        Pathspec names in file order, decoded by the shared Git path codec.

    Raises:
        PathspecFileError: A record is empty, malformed, or cannot reach Git on
            Windows.
    """
    if not data:
        return []
    separator = b'\0' if nul else b'\n'
    parts = data.split(separator)
    if parts[-1] == b'':
        parts.pop()
    names: list[str] = []
    for part in parts:
        record = part if nul else part.removesuffix(b'\r').split(b'\0', 1)[0]
        if not nul:
            decoded = _decode_pathspec_record(record)
            if decoded is None:
                raise PathspecFileError('malformed quoted pathspec record')
            record = decoded
        if not record:
            raise PathspecFileError('empty pathspec record')
        name = decode_git_path(record)
        if _WINDOWS and any(
            _SURROGATE_FIRST <= ord(character) <= _SURROGATE_LAST for character in name
        ):
            printable_name = name.encode('utf-8', 'backslashreplace').decode('utf-8')
            raise PathspecFileError(
                f'pathspec-file entry {escape_terminal_text(printable_name)} is not valid UTF-8; '
                'Git on Windows cannot receive it'
            )
        names.append(name)
    return names


def read_pathspec_file(cwd: str, path: str) -> bytes:
    """Read an ordinary pathspec file, accepting symlinks to ordinary files.

    Args:
        cwd: Working directory for relative pathspec files.
        path: Pathspec filename, with MSYS path normalization applied.

    Returns:
        File bytes, including `b''` for an empty file.

    Raises:
        OSError: The file could not be read.
        NonRegularPathspecError: The path or opened descriptor is not a regular
            file.
    """
    resource = Path(normalize_msys_path(path))
    if not resource.is_absolute():
        resource = Path(cwd) / resource
    if not stat.S_ISREG(resource.stat().st_mode):
        raise NonRegularPathspecError
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(resource, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NonRegularPathspecError
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)
