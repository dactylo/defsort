"""Read the git attribute that labels private paths."""

import re
from collections.abc import Iterable

DEFAULT_PRIVATE_ATTRIBUTE = 'private'
_NAME_PATTERN = re.compile(r'[A-Za-z0-9._][A-Za-z0-9._-]*')
_NAME_RULE = "use letters, digits, '.', '_', or '-', not starting with '-' or 'builtin_'"
_RESERVED_PREFIX = 'builtin_'


def labelled_paths(output: bytes, attribute: str) -> frozenset[bytes]:
    """Return paths whose `git check-attr -z` value is exactly `set`.

    Args:
        output: NUL-delimited path, attribute, value records from git.
        attribute: The requested attribute name, repeated in each record.

    Returns:
        The paths reported with a `set` value.

    Raises:
        ValueError: The records are incomplete or name another attribute.
    """
    if not output:
        return frozenset()
    if not output.endswith(b'\0'):
        raise ValueError('malformed git check-attr output')
    fields = output.split(b'\0')[:-1]
    if len(fields) % 3:
        raise ValueError('malformed git check-attr output')
    encoded_attribute = attribute.encode()
    labelled = set[bytes]()
    for index in range(0, len(fields), 3):
        path, reported_attribute, value = fields[index : index + 3]
        if reported_attribute != encoded_attribute:
            raise ValueError('malformed git check-attr output')
        if value == b'set':
            labelled.add(path)
    return frozenset(labelled)


def private_attribute(configured: str | None) -> str:
    """Use the default name or validate a configured private attribute name.

    Args:
        configured: A git attribute name, or `None` for the default.

    Returns:
        The validated name to query.

    Raises:
        ValueError: The name contains forbidden characters or a reserved
            prefix.
    """
    if configured is None:
        return DEFAULT_PRIVATE_ATTRIBUTE
    if not _NAME_PATTERN.fullmatch(configured) or configured.startswith(_RESERVED_PREFIX):
        raise ValueError(_NAME_RULE)
    return configured


def label_query(attribute: str, paths: Iterable[bytes]) -> tuple[list[str], bytes]:
    """Build a NUL-delimited `git check-attr` query without launching git.

    Args:
        attribute: The private path attribute to query.
        paths: Repository-relative path bytes to send to git.

    Returns:
        The command arguments and its standard input bytes.

    Raises:
        ValueError: The attribute name is invalid.
    """
    private_attribute(attribute)
    return ['git', 'check-attr', '-z', '--stdin', attribute], b''.join(
        path + b'\0' for path in paths
    )
