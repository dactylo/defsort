import difflib
import io
import os
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from defsort._vendor.hookfront._color import (
    BOLD,
    GREEN,
    RED,
    RESET,
)
from defsort._vendor.hookfront._diff import (
    DiffSides,
    _diff_bytes,
    _diff_text,
    _patch_name,
    _quote_patch_name,
    write_diff,
)
from defsort._vendor.hookfront._streams import (
    _ClosableTextOutput,
)

_CASES = [
    pytest.param(DiffSides(b'x = 1\n', b'x = 2\n', 'utf-8'), '-x = 1\n+x = 2\n', id='lf'),
    pytest.param(DiffSides(b'x = 1\r\n', b'x = 2\r\n', 'utf-8'), '-x = 1\n+x = 2\n', id='crlf'),
    pytest.param(
        DiffSides(b'x = 1\ry = 1\n', b'x = 2\ry = 1\n', 'utf-8'),
        '-x = 1\\x0dy = 1\n+x = 2\\x0dy = 1\n',
        id='lone_cr',
    ),
    pytest.param(
        DiffSides(b'# caf\xe9\n', b'# caf\xe9s\n', 'latin-1'), '-# café\n+# cafés\n', id='latin1'
    ),
    pytest.param(
        DiffSides(b'\xef\xbb\xbfx = 1\n', b'\xef\xbb\xbfx = 2\n', 'utf-8-sig'),
        '-x = 1\n+x = 2\n',
        id='bom',
    ),
    pytest.param(
        DiffSides(b'x = 1', b'x = 2', 'utf-8'),
        '-x = 1\n\\ No newline at end of file\n+x = 2\n\\ No newline at end of file\n',
        id='no_eol',
    ),
    pytest.param(
        DiffSides(b'\tx = 1\n', b'\tx = 2\n', 'utf-8'), '-\tx = 1\n+\tx = 2\n', id='tabs'
    ),
]
_HEADER = '--- m.py\n+++ m.py\n@@ -1 +1 @@\n'
_MARKER = b'\\ No newline at end of file\n'


def _expected_patch(sides: DiffSides) -> bytes:
    old = sides.original.split(b'\n')
    new = sides.new.split(b'\n')
    old_records = [part + b'\n' for part in old[:-1]] + ([old[-1]] if old[-1] else [])
    new_records = [part + b'\n' for part in new[:-1]] + ([new[-1]] if new[-1] else [])
    lines = difflib.diff_bytes(difflib.unified_diff, old_records, new_records, b'm.py', b'm.py')
    return b''.join(line if line.endswith(b'\n') else line + b'\n' + _MARKER for line in lines)


@pytest.mark.parametrize(('sides', '_terminal_lines'), _CASES)
def test_bytes_view_matches_difflib(sides: DiffSides, _terminal_lines: str) -> None:
    assert _diff_bytes(sides, b'm.py', color=False) == _expected_patch(sides)


def test_context_is_uncolored() -> None:
    sides = DiffSides(b'context\r\nold\n', b'context\r\nnew\n', 'utf-8')
    assert b' context\r\n' in _diff_bytes(sides, b'm.py', color=True)


def test_crlf_record_keeps_cr_as_data() -> None:
    output = _diff_bytes(DiffSides(b'x = 1\r\n', b'x = 2\r\n', 'utf-8'), b'm.py', color=False)
    assert b'-x = 1\r\n+x = 2\r\n' in output


def test_decoder_consumes_records_outside_hunks_and_finalizes() -> None:
    prefix = b'\x1b$B' + b'$"\n' * 10
    sides = DiffSides(prefix + b'$$\n', prefix + b'$&', 'iso2022_jp')
    assert _diff_text(sides, b'm.py', color=False, terminal=True).endswith(
        ' あ\n あ\n あ\n-い\n+う\n\\ No newline at end of file\n'
    )
    malformed = DiffSides(b'old\n', b'\xe2', 'utf-8')
    assert _diff_text(malformed, b'm.py', color=False, terminal=True).endswith(
        '+�\n\\ No newline at end of file\n'
    )


def test_lone_cr_file_is_one_record() -> None:
    output = _diff_bytes(DiffSides(b'a\rb\r', b'A\rb\r', 'utf-8'), b'm.py', color=False)
    assert output == _HEADER.encode() + b'-a\rb\r\n' + _MARKER + b'+A\rb\r\n' + _MARKER


def test_terminal_view_decodes_stateful_source() -> None:
    sides = DiffSides(b'\x1b$B$"\n$$\n', b'\x1b$B$"\n$&\n', 'iso2022_jp')
    assert sides.original.decode('iso2022_jp') == 'あ\nい\n'
    assert sides.new.decode('iso2022_jp') == 'あ\nう\n'
    assert _diff_text(sides, b'm.py', color=False, terminal=True) == (
        '--- m.py\n+++ m.py\n@@ -1,2 +1,2 @@\n あ\n-い\n+う\n'
    )


def test_terminal_view_escapes_controls_but_keeps_tabs() -> None:
    sides = DiffSides(b'old\n', b'\tx = "\x1b"\r\n', 'utf-8')
    assert (
        _diff_text(sides, b'm.py', color=False, terminal=True)
        == _HEADER + '-old\n+\tx = "\\x1b"\n'
    )


@pytest.mark.parametrize(('sides', 'terminal_lines'), _CASES)
def test_terminal_view_matrix(sides: DiffSides, terminal_lines: str) -> None:
    assert _diff_text(sides, b'm.py', color=False, terminal=True) == _HEADER + terminal_lines


@pytest.mark.parametrize(
    ('original', 'new'),
    [
        (b'', b'new\n'),
        (b'old\n', b''),
        (b'old\nsame', b'new\nsame'),
        (b'old\n', b'new'),
        (b'old', b'new\n'),
        (b'a\rb\r', b'A\rb\r'),
        (b'a\fb\nold\n', b'a\fb\nnew\n'),
        (b'\xe2\x80\xa8\nold\n', b'\xe2\x80\xa8\nnew\n'),
        (
            b'\n'.join(str(i).encode() for i in range(30)),
            b'new\n' + b'\n'.join(str(i).encode() for i in range(1, 29)) + b'\nNEW',
        ),
    ],
)
def test_record_and_hunk_boundaries_match_difflib(original: bytes, new: bytes) -> None:
    sides = DiffSides(original, new, 'utf-8')
    assert _diff_bytes(sides, b'm.py', color=False) == _expected_patch(sides)


@pytest.mark.parametrize(
    ('path', 'cwd', 'expected'),
    [
        ('src/m.py', '/w', 'src/m.py'),
        ('/w/src/m.py', '/w', 'src/m.py'),
        ('/other/m.py', '/w', '/other/m.py'),
        ('/w/a/../m.py', '/w', '/w/a/../m.py'),
        ('link/../m.py', '/w', 'link/../m.py'),
        ('./src//m.py', '/w', './src//m.py'),
        ('/world/m.py', '/w', '/world/m.py'),
    ],
)
def test_posix_patch_name(path: str, cwd: str, expected: str) -> None:
    assert _patch_name(path, cwd, PurePosixPath) == expected


def test_text_view_keeps_endings_and_controls() -> None:
    sides = DiffSides(b'old\x1b\r\n', b'new\x9b\r\n', 'latin-1')
    assert (
        _diff_text(sides, b'm.py', color=False, terminal=False)
        == _HEADER + '-old\x1b\r\n+new\x9b\r\n'
    )


def test_unchanged_sides_write_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', stream)
    sides = DiffSides(b'same', b'same', 'utf-8')
    write_diff(sides, 'm.py', color=True)
    assert stream.getvalue() == ''
    assert _diff_bytes(sides, b'm.py', color=False) == b''
    assert _diff_text(sides, b'm.py', color=False, terminal=True) == ''


@pytest.mark.parametrize(
    ('path', 'cwd', 'expected'),
    [
        (r'c:\w\src\m.py', r'C:\w', 'src/m.py'),
        (r'C:\W\src\m.py', r'C:\w', 'C:/W/src/m.py'),
        (r'\\srv\share\w\m.py', r'\\srv\share\w', 'm.py'),
        (r'\\SRV\share\w\m.py', r'\\srv\share\w', '//SRV/share/w/m.py'),
        (r'\\srv\SHARE\w\m.py', r'\\srv\share\w', '//srv/SHARE/w/m.py'),
        (r'\\?\C:\w\m.py', r'C:\w', r'\\?\C:\w\m.py'),
        (r'\\?\UNC\srv\share\m.py', r'C:\w', r'\\?\UNC\srv\share\m.py'),
        (r'\\.\pipe\x', r'C:\w', r'\\.\pipe\x'),
        (r'\??\C:\w\m.py', r'C:\w', r'\??\C:\w\m.py'),
        ('//?/C:/w/m.py', r'C:\w', '//?/C:/w/m.py'),
        (r'src\m.py', r'C:\w', 'src/m.py'),
        (r'.\src\\m.py', r'C:\w', './src//m.py'),
        (r'C:\w\a\..\m.py', r'C:\w', 'C:/w/a/../m.py'),
        (r'C:src\m.py', r'C:\w', 'C:src/m.py'),
    ],
)
def test_windows_patch_name(path: str, cwd: str, expected: str) -> None:
    assert _patch_name(path, cwd, PureWindowsPath) == expected


@pytest.mark.skipif(os.name == 'nt', reason='directory symlinks require Windows privileges')
def test_patch_name_does_not_resolve_symlinked_directories(tmp_path: Path) -> None:
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'm.py').touch()
    (tmp_path / 'link').symlink_to(real, target_is_directory=True)
    cwd = str(tmp_path)
    assert _patch_name('link/m.py', cwd, PurePosixPath) == 'link/m.py'
    assert _patch_name(f'{cwd}/link/m.py', cwd, PurePosixPath) == 'link/m.py'
    assert _patch_name('link/../m.py', cwd, PurePosixPath) == 'link/../m.py'
    assert _patch_name(f'{cwd}/link/../m.py', cwd, PurePosixPath) == f'{cwd}/link/../m.py'
    assert _patch_name(str(real / 'm.py'), str(tmp_path / 'link'), PurePosixPath) == str(
        real / 'm.py'
    )


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        (b'm.py', b'm.py'),
        (b'caf\xc3\xa9.py', b'caf\xc3\xa9.py'),
        (b'my file.py', b'"my file.py"'),
        (b'a\tb.py', b'"a\\tb.py"'),
        (b'a\nb.py', b'"a\\nb.py"'),
        (b'q"x.py', b'"q\\"x.py"'),
        (b'b\\s.py', b'"b\\\\s.py"'),
        (b'bad-\xff.py', b'"bad-\\377.py"'),
        (b'c1-\xc2\x9b.py', b'"c1-\\302\\233.py"'),
        (b'esc-\x1b.py', b'"esc-\\033.py"'),
        (b'\a\b\v\f\r\x00\x7f', b'"\\a\\b\\v\\f\\r\\000\\177"'),
        (b'caf\xc3\xa9-\xff.py', b'"caf\xc3\xa9-\\377.py"'),
    ],
)
def test_quote_patch_name(name: bytes, expected: bytes) -> None:
    assert _quote_patch_name(name) == expected


@pytest.mark.parametrize('terminal', [False, True])
def test_different_bytes_survive_identical_display(*, terminal: bool) -> None:
    sides = DiffSides(b'\x07\n', b'\\x07\n', 'utf-8')
    output = _diff_text(sides, b'm.py', color=False, terminal=terminal)
    assert '@@ -1 +1 @@\n' in output
    assert '+\\x07\n' in output


@pytest.mark.parametrize('ending', [b'\n', b'\r\n', b''])
def test_color_marks_headers_hunks_and_records(ending: bytes) -> None:
    sides = DiffSides(b'--x' + ending, b'++y' + ending, 'utf-8')
    marker = _MARKER.decode() if not ending else ''
    expected = (
        f'{BOLD}--- m.py{RESET}\n{BOLD}+++ m.py{RESET}\n{BOLD}@@ -1 +1 @@{RESET}\n'
        f'{RED}---x{RESET}{ending.decode() or chr(10)}{marker}'
        f'{GREEN}+++y{RESET}{ending.decode() or chr(10)}{marker}'
    )
    assert _diff_bytes(sides, b'm.py', color=True) == expected.encode()
    assert _diff_text(sides, b'm.py', color=True, terminal=False) == expected
    assert _diff_text(sides, b'm.py', color=True, terminal=True) == expected.replace('\r\n', '\n')


@pytest.mark.parametrize('terminal', [False, True])
def test_write_diff_text_views(*, terminal: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: terminal)
    monkeypatch.setattr(sys, 'stdout', stream)
    sides = DiffSides(b'old\x1b\r\n', b'new\x1b\r\n', 'utf-8')
    write_diff(sides, 'm.py', color=False)
    assert stream.getvalue() == _diff_text(sides, b'm.py', color=False, terminal=terminal)


def test_write_diff_writes_bytes_when_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='ascii', newline='\r\n')
    output = _ClosableTextOutput(stream)
    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.chdir(tmp_path)
    sides = DiffSides(b'caf\xe9\r\n', b'caf\xe9s\r\n', 'latin-1')
    path = str(tmp_path / 'my file.py')
    name = _quote_patch_name(os.fsencode(_patch_name(path, str(Path.cwd()), PurePath)))
    output.write('earlier\n')
    write_diff(sides, path, color=False)
    assert buffer.getvalue() == b'earlier\r\n' + _diff_bytes(sides, name, color=False)
