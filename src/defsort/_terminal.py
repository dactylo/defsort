"""Terminal-safe rendering for repository-controlled text."""

from pathlib import PurePath, PureWindowsPath

_C0_LIMIT = 0x20
_C1_MAX = 0x9F
_DEL = 0x7F


def escape_terminal_text(value: str) -> str:
    r"""Replace terminal control code points with visible ASCII escapes.

    Args:
        value: Repository-controlled text to render for a human.

    Returns:
        Text with every C0, DEL, and C1 code point rendered as `\xNN`.
    """
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if codepoint < _C0_LIMIT or _DEL <= codepoint <= _C1_MAX:
            escaped.append(f'\\x{codepoint:02x}')
        else:
            escaped.append(character)
    return ''.join(escaped)


def path_text(path: str | PurePath) -> str:
    """Render ordinary Windows path separators as forward slashes.

    Preserve the supplied spelling of POSIX paths and Windows device or
    extended-length namespaces. String paths use the current platform's path
    flavor. This does not escape terminal controls or quote for a shell.
    """
    text = str(path)
    native = PurePath(path) if isinstance(path, str) else path
    if isinstance(native, PureWindowsPath) and not text.replace('/', '\\').startswith(
        ('\\\\?\\', '\\\\.\\', '\\??\\')
    ):
        return text.replace('\\', '/')
    return text
