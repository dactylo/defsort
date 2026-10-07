from pathlib import PurePath, PurePosixPath, PureWindowsPath

import pytest

from defsort import _terminal
from defsort._terminal import escape_terminal_text


@pytest.mark.parametrize('codepoint', [*range(0x20), *range(0x7F, 0xA0)])
def test_all_terminal_control_code_points_are_visible_ascii(codepoint: int) -> None:
    value = f'before{chr(codepoint)}after'

    assert escape_terminal_text(value) == f'before\\x{codepoint:02x}after'


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        (PureWindowsPath('src/package/file.py'), 'src/package/file.py'),
        (PureWindowsPath('C:/my project/file.py'), 'C:/my project/file.py'),
        (PureWindowsPath('C:relative/file.py'), 'C:relative/file.py'),
        (PureWindowsPath('//server/share/file.py'), '//server/share/file.py'),
        (PureWindowsPath('//?/C:/repo/file.py'), str(PureWindowsPath('//?/C:/repo/file.py'))),
        (PureWindowsPath('//./pipe/tool'), str(PureWindowsPath('//./pipe/tool'))),
        (PurePosixPath(r'src/literal\name.py'), r'src/literal\name.py'),
        (PurePosixPath('/repo/my project/file.py'), '/repo/my project/file.py'),
    ],
)
def test_display_paths_preserve_meaning(path: PurePath, expected: str) -> None:
    assert _terminal.path_text(path) == expected


def test_literal_backslashes_and_printable_unicode_are_unchanged() -> None:
    value = r'\x1b \n \u0085 café'
    assert escape_terminal_text(value) == value


def test_native_windows_text_keeps_relative_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_terminal, 'PurePath', PureWindowsPath)
    assert _terminal.path_text(r'.\src\file.py') == './src/file.py'
    assert _terminal.path_text(r'\??\C:\repo\file.py') == r'\??\C:\repo\file.py'
