import pytest

from defsort._display._terminal import escape_terminal_text


@pytest.mark.parametrize('codepoint', [*range(0x20), *range(0x7F, 0xA0)])
def test_all_terminal_control_code_points_are_visible_ascii(codepoint: int) -> None:
    value = f'before{chr(codepoint)}after'

    assert escape_terminal_text(value) == f'before\\x{codepoint:02x}after'


def test_literal_backslashes_and_printable_unicode_are_unchanged() -> None:
    value = r'\x1b \n \u0085 café'
    assert escape_terminal_text(value) == value
