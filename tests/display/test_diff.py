import sys
from pathlib import PureWindowsPath

import pytest

from defsort import _terminal
from defsort._color import BOLD, GREEN, RED
from defsort._display._diff import _colorize_diff_line, print_diff
from defsort._types import SortResult
from tests.display._helpers import _make_result

_RESET = '\033[0m'


class TestColorizeDiffLine:
    def test_crlf_carriage_return_stays_outside_the_color_span(self) -> None:
        colored = _colorize_diff_line('-foo\r\n', is_header=False)
        assert colored == f'{RED}-foo{_RESET}\r\n'
        assert '\r' not in colored[: colored.index(_RESET)]

    @pytest.mark.parametrize('ending', ['\n', '\r\n', '\r', ''])
    def test_reset_precedes_the_whole_line_ending(self, ending: str) -> None:
        assert _colorize_diff_line(f'+foo{ending}', is_header=False) == (
            f'{GREEN}+foo{_RESET}{ending}'
        )

    def test_uncolored_line_is_returned_unchanged(self) -> None:
        assert _colorize_diff_line(' context\r\n', is_header=False) == ' context\r\n'


class TestHeaderColoring:
    def test_file_headers_are_bold(self) -> None:
        assert _colorize_diff_line('--- a.py (original)\n', is_header=True).startswith(BOLD)
        assert _colorize_diff_line('+++ a.py (sorted)\n', is_header=True).startswith(BOLD)

    def test_removed_dashed_line_is_red(self) -> None:
        colored = _colorize_diff_line('---- banner\n', is_header=False)
        assert colored.startswith(RED)


class TestPrintDiff:
    def test_changed_file_prints_diff(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _make_result(changed=True)
        print_diff(result, 'test.py', verbose=False, ordering='dependency', color=False)
        output = capsys.readouterr().out
        assert '--- test.py (original)\n' in output
        assert '+++ test.py (sorted)\n' in output
        assert '-def bravo():' in output
        assert '+def bravo():' in output

    def test_hunk_numbers_count_physical_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = SortResult(
            original_source='"""D\fE"""\nb = 1\n',
            new_source='"""D\fE"""\nc = 1\n',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
        )
        print_diff(result, 'sample.py', verbose=False, ordering='dependency', color=False)
        assert '@@ -1,2 +1,2 @@' in capsys.readouterr().out

    @pytest.mark.parametrize(
        ('original', 'new', 'record'),
        [
            ('old', 'new\n', '-old\n\\ No newline at end of file\n'),
            ('old\n', 'new', '+new\n\\ No newline at end of file\n'),
            ('old\nsame', 'new\nsame', ' same\n\\ No newline at end of file\n'),
        ],
        ids=['old-record', 'new-record', 'context-record'],
    )
    def test_missing_final_newline_has_separate_marker(
        self,
        original: str,
        new: str,
        record: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result()
        result.original_source = original
        result.new_source = new

        print_diff(result, 'test.py', verbose=False, ordering='dependency', color=False)

        assert record in capsys.readouterr().out

    def test_missing_final_newline_marker_follows_colored_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = _make_result()
        result.original_source = 'old\n'
        result.new_source = 'new'

        print_diff(result, 'test.py', verbose=False, ordering='dependency', color=True)
        output = capsys.readouterr().out

        assert f'{GREEN}+new{_RESET}\n\\ No newline at end of file\n' in output

    def test_redirected_diff_preserves_repository_bytes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result()
        result.original_source = '\x1b]2;old\x07\n'
        result.new_source = '\x1b]2;new\x07\n'
        monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)

        print_diff(
            result,
            'bad\x1b]2;path\x07.py',
            verbose=False,
            ordering='dependency',
            color=False,
        )

        assert capsys.readouterr().out == (
            '--- bad\x1b]2;path\x07.py (original)\n'
            '+++ bad\x1b]2;path\x07.py (sorted)\n'
            '@@ -1 +1 @@\n'
            '-\x1b]2;old\x07\n'
            '+\x1b]2;new\x07\n'
        )

    def test_terminal_diff_escapes_path_and_source_records(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result()
        result.original_source = 'old\t\x1b]2;title\x07\r\nsame\x85'
        result.new_source = 'new\t\x1b]2;title\x07\r\nsame\x85'
        monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)

        print_diff(
            result,
            'bad\x1b]2;path\x07.py',
            verbose=False,
            ordering='dependency',
            color=False,
        )

        assert capsys.readouterr().out == (
            '--- bad\\x1b]2;path\\x07.py (original)\n'
            '+++ bad\\x1b]2;path\\x07.py (sorted)\n'
            '@@ -1,2 +1,2 @@\n'
            '-old\\x09\\x1b]2;title\\x07\r\n'
            '+new\\x09\\x1b]2;title\\x07\r\n'
            ' same\\x85\n'
            '\\ No newline at end of file\n'
        )

    def test_terminal_diff_keeps_owned_ansi_after_escaping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result()
        result.original_source = 'old\x07\n'
        result.new_source = 'new\x07\n'
        monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)

        print_diff(
            result,
            'bad\x07.py',
            verbose=False,
            ordering='dependency',
            color=True,
        )

        output = capsys.readouterr().out
        assert f'{BOLD}--- bad\\x07.py (original){_RESET}\n' in output
        assert f'{RED}-old\\x07{_RESET}\n' in output
        assert '\x07' not in output

    def test_terminal_diff_keeps_records_that_escape_to_the_same_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result()
        result.original_source = '\x07\n'
        result.new_source = '\\x07\n'
        monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)

        print_diff(
            result,
            'test.py',
            verbose=False,
            ordering='dependency',
            color=False,
        )

        assert capsys.readouterr().out == (
            '--- test.py (original)\n+++ test.py (sorted)\n@@ -1 +1 @@\n-\\x07\n+\\x07\n'
        )

    def test_terminated_sources_do_not_emit_missing_newline_marker(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = _make_result()
        result.original_source = 'old\n'
        result.new_source = 'new\n'

        print_diff(result, 'test.py', verbose=False, ordering='dependency', color=False)

        assert '\\ No newline at end of file' not in capsys.readouterr().out

    def test_unchanged_no_verbose_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _make_result(changed=False)
        print_diff(result, 'test.py', verbose=False, ordering='dependency', color=False)
        assert capsys.readouterr().out == ''

    def test_unchanged_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _make_result(changed=False)
        print_diff(result, 'test.py', verbose=True, ordering='dependency', color=False)
        assert 'already in dependency order' in capsys.readouterr().out


@pytest.mark.parametrize('terminal', [False, True])
def test_windows_diff_path_preserves_redirected_output(
    terminal: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_terminal, 'PurePath', PureWindowsPath)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: terminal)
    result = _make_result()
    print_diff(result, r'C:\repo\file.py', verbose=False, ordering='dependency', color=False)
    expected = 'C:/repo/file.py' if terminal else r'C:\repo\file.py'
    assert capsys.readouterr().out.startswith(f'--- {expected} (original)\n')
