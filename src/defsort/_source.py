"""Shared Python source-line primitives."""

import re
from collections.abc import Iterable

BANNER_RULE_CHARS = '-=*#~'
_LAYOUT_BLANK_CHARS = ' \t\f'
_PHYSICAL_LINE_ENDING = re.compile(r'\r\n|\r|\n')


def join_physical(before: str, after: str) -> str:
    r"""Join two fragments without fusing two physical endings into one.

    A fragment ending with a lone `\r` joined to a fragment starting with `\n`
    would read back as one CRLF ending. Swapping the two characters preserves
    both physical endings and the joined byte multiset.

    Args:
        before: Left fragment.
        after: Right fragment.

    Returns:
        The joined text with the seam rule applied.
    """
    if before.endswith('\r') and after.startswith('\n'):
        return before[:-1] + '\n\r' + after[1:]
    return before + after


def join_physical_fragments(fragments: Iterable[str]) -> str:
    r"""Join fragments without fusing two physical endings into one.

    Args:
        fragments: Text fragments to consume once in their given order.

    Returns:
        The joined text with lone-CR/leading-LF seams swapped.
    """
    joined: list[str] = []
    previous: str | None = None
    for fragment in fragments:
        if not fragment:
            continue
        if previous is None:
            previous = fragment
            continue
        if previous.endswith('\r') and fragment.startswith('\n'):
            joined.append(previous[:-1] + '\n')
            previous = '\r' + fragment[1:]
        else:
            joined.append(previous)
            previous = fragment
    if previous is not None:
        joined.append(previous)
    return ''.join(joined)


def split_physical_lines(source: str, *, keepends: bool = False) -> list[str]:
    """Split source into Python physical lines.

    Only CRLF, LF, and lone CR terminate a physical line. The eight extra
    boundary characters recognized by `str.splitlines` remain inside their
    line.

    Args:
        source: Text to divide into physical lines.
        keepends: Retain each terminated line's exact ending.

    Returns:
        The physical lines. Joining a `keepends=True` result reproduces
        `source` exactly.
    """
    lines: list[str] = []
    start = 0
    for match in _PHYSICAL_LINE_ENDING.finditer(source):
        end = match.end() if keepends else match.start()
        lines.append(source[start:end])
        start = match.end()
    if start < len(source):
        lines.append(source[start:])
    return lines


def terminal_line_ending(text: str) -> str:
    r"""Return the physical line ending that terminates text.

    Args:
        text: Text whose final characters may form a physical line ending.

    Returns:
        `'\r\n'`, `'\n'`, or `'\r'` when text ends with that ending, otherwise
        the empty string.
    """
    if text.endswith('\r\n'):
        return '\r\n'
    if text.endswith(('\n', '\r')):
        return text[-1]
    return ''


def is_layout_blank(line: str) -> bool:
    """Return whether one physical line is layout-blank.

    Args:
        line: One physical line, with or without its ending.

    Returns:
        True when the line body is empty or contains only ASCII spaces,
        horizontal tabs, or form feeds.
    """
    body = line.removesuffix(terminal_line_ending(line))
    return not body.strip(_LAYOUT_BLANK_CHARS)


def scope_indent_body(line: str, scope_indent: str) -> str | None:
    """Return one line's body after exact scope indentation.

    Python ignores text up to the last form feed in a line's leading whitespace
    during indentation calculation. The match honors that rule without changing
    any authored character.

    Args:
        line: One physical line, with or without its ending.
        scope_indent: Exact indentation required at the scope.

    Returns:
        The body after the scope indentation, or None when the line is not at
        that indentation.
    """
    body = line.removesuffix(terminal_line_ending(line))
    start = 0
    for index, char in enumerate(body):
        if char == '\f':
            start = index + 1
        elif char not in ' \t':
            break
    effective = body[start:]
    if not effective.startswith(scope_indent):
        return None
    return effective[len(scope_indent) :]
