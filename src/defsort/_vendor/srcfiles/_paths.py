"""Group paths that name one file."""

from collections.abc import Iterable
from pathlib import Path

ALIAS_CONFLICT = 'names of one file need different results'


def _identity(path: Path) -> tuple[int, int] | None:
    """Return a path's device and inode, or None when `stat` fails or reports no inode."""
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino) if status.st_ino else None


def _root(roots: list[int], index: int) -> int:
    """Return the first index of the group that holds an index, compressing the path to it."""
    while roots[index] != index:
        roots[index] = roots[roots[index]]
        index = roots[index]
    return index


def group_paths(paths: Iterable[Path]) -> list[list[Path]]:
    """Group paths that name one file, in first-seen order.

    Paths with the same device and inode share a group, which covers repeated
    operands, case aliases, symlinks, and hard links. Paths with the same
    resolved path share a group too, so a path whose `stat` fails stays with
    the other spellings of that path when resolution succeeds.

    Args:
        paths: Paths in caller order. Paths that do not exist are allowed.

    Returns:
        Groups ordered by their first path; each group lists its paths in
        caller order.

    Raises:
        OSError: If resolving a path fails.
    """
    ordered = list(paths)
    identities = [_identity(path) for path in ordered]
    keys: list[list[tuple[int, int] | Path]] = [
        [] if identity is None else [identity] for identity in identities
    ]
    for path_keys, path in zip(keys, ordered, strict=True):
        path_keys.append(path.resolve())
    roots = list(range(len(ordered)))
    owners: dict[tuple[int, int] | Path, int] = {}
    for index, path_keys in enumerate(keys):
        for key in path_keys:
            first, second = _root(roots, owners.setdefault(key, index)), _root(roots, index)
            roots[max(first, second)] = min(first, second)
    groups: dict[int, list[Path]] = {}
    for index, path in enumerate(ordered):
        groups.setdefault(_root(roots, index), []).append(path)
    return list(groups.values())


def names_agree(needed: Iterable[bytes | None]) -> bool:
    """Return whether every name needs the same bytes, counting unknown results as a value.

    Args:
        needed: One result per name, with `None` for a name without a result.
    """
    return len(set(needed)) <= 1
