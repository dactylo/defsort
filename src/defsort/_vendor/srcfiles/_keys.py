"""Match filesystem paths to tracked repository spellings."""

import os
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_WINDOWS = os.name == 'nt'


@dataclass(frozen=True, slots=True)
class KeyMatch:
    """A tracked spelling when matched, or the reason none can be used.

    Attributes:
        status: Whether one tracked path matches, none does, or matching
            failed.
        key: Git's spelling when `status` is `matched`.
    """

    status: Literal['matched', 'untracked', 'unavailable']
    key: str | None = None


class SpellingIndex:
    """Group tracked Git paths for filesystem identity matching."""

    def __init__(self, paths: Iterable[str]) -> None:
        """Index Git path spellings by their normalized case fold.

        Args:
            paths: The tracked repository-relative paths.
        """
        self._folded: dict[str, set[str]] = {}
        for path in paths:
            self._folded.setdefault(_fold_key(path), set()).add(path)

    def has_variant(self, key: str) -> bool:
        """Return whether another tracked spelling folds like `key`."""
        return any(candidate != key for candidate in self._folded.get(_fold_key(key), ()))

    def match(self, path: Path, root: str, key: str) -> KeyMatch:
        """Find a tracked spelling by exact key or filesystem identity.

        A unique exact spelling matches without filesystem inspection.
        Otherwise, candidates with the same normalized case fold are compared
        with the selected file.

        Args:
            path: The file to match when identity checks are needed.
            root: The repository root to which tracked paths are relative.
            key: The selected repository-relative spelling.

        Returns:
            A unique matched spelling, `untracked` when no candidate matches,
            or `unavailable` when identity inspection fails or multiple
            candidates match.
        """
        candidates = self._folded.get(_fold_key(key), set())
        if candidates == {key}:
            return KeyMatch('matched', key)
        try:
            matches = [
                candidate for candidate in candidates if _same_file(path, Path(root) / candidate)
            ]
        except (OSError, ValueError):
            return KeyMatch('unavailable')
        if len(matches) > 1:
            return KeyMatch('unavailable')
        if matches:
            return KeyMatch('matched', matches[0])
        return KeyMatch('untracked')


def _fold_key(key: str) -> str:
    """Normalize a Git path for case-variant candidate discovery."""
    return unicodedata.normalize('NFC', key).casefold()


def _same_file(first: Path, second: Path) -> bool:
    """Compare filesystem identity, falling back to normalized paths for unknown inodes."""
    first_stat, second_stat = first.stat(), second.stat()
    if first_stat.st_ino == 0 or second_stat.st_ino == 0:
        return os.path.normcase(first) == os.path.normcase(second)
    return (first_stat.st_dev, first_stat.st_ino) == (second_stat.st_dev, second_stat.st_ino)


def decode_git_path(raw: bytes) -> str:
    """Decode Git path bytes while preserving undecodable bytes."""
    return raw.decode('utf-8', 'surrogateescape') if _WINDOWS else os.fsdecode(raw)


def encode_git_path(path: str) -> bytes:
    """Encode a Git path using the matching path decoder's codec."""
    return path.encode('utf-8', 'surrogateescape') if _WINDOWS else os.fsencode(path)


def repository_key(path: Path, root: str) -> str | None:
    """Return the path below an identity-matched repository ancestor, or `None`.

    Filesystem failures and paths outside the repository have no usable key.
    The spelling below the matching ancestor is retained for exact Git lookup.
    """
    try:
        resolved = path.resolve(strict=True)
        root_path = Path(root).resolve(strict=True)
        for ancestor in resolved.parents:
            if _same_file(ancestor, root_path):
                return resolved.relative_to(ancestor).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def tracked_paths(output: bytes) -> frozenset[str]:
    """Decode NUL-separated tracked paths while preserving undecodable bytes.

    Windows uses UTF-8 with `surrogateescape` for Git's path bytes. POSIX uses
    `os.fsdecode` and the filesystem encoding.
    """
    return frozenset(decode_git_path(path) for path in output.split(b'\0') if path)


def tracked_query() -> list[str]:
    """Build a NUL-delimited tracked-path query without launching Git."""
    return ['git', 'ls-files', '-z', '--']
