"""Render exact byte patches and terminal-safe source diffs."""

import codecs
import difflib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from ._color import BOLD, GREEN, RED, RESET
from ._streams import write_stdout_bytes
from ._terminal import escape_terminal_text

_C0_LIMIT = 0x20
_C1_MAX = 0x9F
_DEL = 0x7F
_DRIVE_LENGTH = 2
_MARKER = b'\\ No newline at end of file\n'
_NAME_ESCAPES = {
    '\a': b'\\a',
    '\b': b'\\b',
    '\t': b'\\t',
    '\n': b'\\n',
    '\v': b'\\v',
    '\f': b'\\f',
    '\r': b'\\r',
    '"': b'\\"',
    '\\': b'\\\\',
}
_SURROGATE_MAX = 0xDCFF
_SURROGATE_MIN = 0xDC80


@dataclass(frozen=True, slots=True)
class DiffSides:
    """Original and replacement file bytes with their source encoding.

    Attributes:
        original: File bytes as read.
        new: Bytes that would be written.
        encoding: Source codec used for the text views only.
    """

    original: bytes
    new: bytes
    encoding: str


def _colored(record: bytes, code: str) -> bytes:
    """Color a record's text while retaining CRLF or LF outside the span."""
    if not code:
        return record
    if record.endswith(b'\r\n'):
        body, ending = record[:-2], b'\r\n'
    elif record.endswith(b'\n'):
        body, ending = record[:-1], b'\n'
    else:
        body, ending = record, b''
    return code.encode() + body + RESET.encode() + ending


def _decoded(records: list[bytes], encoding: str) -> list[str]:
    """Decode every record with one side's persistent codec state."""
    decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
    return [
        decoder.decode(record, final=index == len(records) - 1)
        for index, record in enumerate(records)
    ]


def _format_range(start: int, stop: int) -> str:
    """Format unified-diff coordinates, including empty and single-line ranges."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f'{start + bool(length)},{length}'


def _diff_records(
    old: list[bytes], new: list[bytes], name: bytes
) -> Iterator[tuple[str, int, bytes]]:
    """Yield structural lines or prefixed source records with their side index."""
    started = False
    for group in difflib.SequenceMatcher(None, old, new).get_grouped_opcodes(3):
        if not started:
            started = True
            yield '', 0, b'--- ' + name + b'\n'
            yield '', 0, b'+++ ' + name + b'\n'
        first, last = group[0], group[-1]
        old_range = _format_range(first[1], last[2])
        new_range = _format_range(first[3], last[4])
        yield '', 0, f'@@ -{old_range} +{new_range} @@\n'.encode()
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == 'equal':
                for index in range(old_start, old_end):
                    yield ' ', index, old[index]
            if tag in ('delete', 'replace'):
                for index in range(old_start, old_end):
                    yield '-', index, old[index]
            if tag in ('insert', 'replace'):
                for index in range(new_start, new_end):
                    yield '+', index, new[index]


def _line_color(prefix: str, *, color: bool) -> str:
    """Select color by structural role rather than by source text."""
    if not color:
        return ''
    return {'': BOLD, '-': RED, '+': GREEN}.get(prefix, '')


def _parts(path: PurePath) -> tuple[str, ...]:
    """Compare Windows drive letters without folding any other component."""
    parts = path.parts
    drive = path.drive
    if (
        isinstance(path, PureWindowsPath)
        and len(drive) == _DRIVE_LENGTH
        and drive[0] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
        and drive[1] == ':'
    ):
        return (parts[0][0].lower() + parts[0][1:], *parts[1:])
    return parts


def _patch_name(path: str, cwd: str, flavor: type[PurePath]) -> str:
    """Shorten an absolute name only under its lexically spelled current directory."""
    named = flavor(path)
    windows = isinstance(named, PureWindowsPath)
    if windows and path.replace('/', '\\').startswith(('\\\\?\\', '\\\\.\\', '\\??\\')):
        return path
    directory = _parts(flavor(cwd))
    components = _parts(named)
    if (
        named.is_absolute()
        and '..' not in components
        and components[: len(directory)] == directory
    ):
        path = '/'.join(named.parts[len(directory) :]) or '.'
    return path.replace('\\', '/') if windows else path


def _quote_patch_name(name: bytes) -> bytes:
    """Quote unsafe filename bytes using Git's C-style UTF-8 patch spelling."""
    output: list[bytes] = []
    quoted = False
    for character in name.decode('utf-8', errors='surrogateescape'):
        codepoint = ord(character)
        encoded = character.encode('utf-8', errors='surrogateescape')
        if character in _NAME_ESCAPES:
            quoted = True
            output.append(_NAME_ESCAPES[character])
        elif (
            codepoint < _C0_LIMIT
            or _DEL <= codepoint <= _C1_MAX
            or _SURROGATE_MIN <= codepoint <= _SURROGATE_MAX
        ):
            quoted = True
            output.append(b''.join(f'\\{byte:03o}'.encode() for byte in encoded))
        else:
            quoted |= character == ' '
            output.append(encoded)
    return b'"' + b''.join(output) + b'"' if quoted else name


def _records(data: bytes) -> list[bytes]:
    """Split only after LF, retaining every source byte."""
    pieces = data.split(b'\n')
    return [piece + b'\n' for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])


def _diff_bytes(sides: DiffSides, name: bytes, *, color: bool) -> bytes:
    """Produce a unified patch over exact LF-delimited source bytes."""
    output: list[bytes] = []
    for prefix, _, record in _diff_records(_records(sides.original), _records(sides.new), name):
        output.append(_colored(prefix.encode() + record, _line_color(prefix, color=color)))
        if prefix and not record.endswith(b'\n'):
            output.extend((b'\n', _MARKER))
    return b''.join(output)


def _terminal_record(text: str) -> str:
    """Normalize one decoded record's ending and escape controls except tab."""
    if text.endswith('\r\n'):
        text = text[:-2]
    elif text.endswith('\n'):
        text = text[:-1]
    return '\t'.join(escape_terminal_text(part) for part in text.split('\t')) + '\n'


def _diff_text(sides: DiffSides, name: bytes, *, color: bool, terminal: bool) -> str:
    """Render byte-selected hunks using stateful source decoding."""
    old, new = _records(sides.original), _records(sides.new)
    old_text, new_text = _decoded(old, sides.encoding), _decoded(new, sides.encoding)
    output: list[str] = []
    for prefix, index, record in _diff_records(old, new, name):
        if prefix:
            text = (new_text if prefix == '+' else old_text)[index]
            if terminal:
                text = _terminal_record(text)
            text = prefix + text
        else:
            text = record.decode('utf-8')
        output.append(
            _colored(text.encode('utf-8'), _line_color(prefix, color=color)).decode('utf-8')
        )
        if prefix and not record.endswith(b'\n'):
            if not terminal:
                output.append('\n')
            output.append(_MARKER.decode())
    return ''.join(output)


def write_diff(sides: DiffSides, path: str, *, color: bool) -> None:
    """Write a terminal view or an exact patch through standard output.

    Terminal output decodes source bytes, normalizes displayed line endings,
    and escapes controls other than tabs. Redirected binary output preserves
    source bytes. Streams without a binary interface receive decoded text.
    Identical sides produce no output.

    Args:
        sides: File bytes before and after formatting, with their source codec.
        path: File name as held by the tool; quoted after lexical shortening.
        color: Whether to include ANSI colors even on redirected output.

    Raises:
        OSError: If the current directory cannot be determined or output cannot
            be written or flushed.
    """
    if sides.original == sides.new:
        return
    name = _quote_patch_name(os.fsencode(_patch_name(path, str(Path.cwd()), PurePath)))
    if sys.stdout.isatty():
        sys.stdout.write(_diff_text(sides, name, color=color, terminal=True))
    elif not write_stdout_bytes(_diff_bytes(sides, name, color=color)):
        sys.stdout.write(_diff_text(sides, name, color=color, terminal=False))
