"""Terminal-safe rendering for repository-controlled text."""

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
