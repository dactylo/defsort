from pathlib import PureWindowsPath

import pytest

from defsort import _terminal
from defsort._color import BOLD, RED, YELLOW
from defsort._display._summary import build_summary_row, format_summary_rows
from defsort._display._width import display_width
from defsort._types import Definition, MethodDefinition, MethodSortInfo, SortResult
from tests.display._helpers import _make_def


class TestCjkAlignment:
    def test_cjk_paths_align_columns(self) -> None:
        results = [
            ('plainname.py', _summary_result(changed=True)),
            ('文件测试.py', _summary_result(changed=True)),
        ]
        lines = format_summary_rows(
            [build_summary_row(path, result) for path, result in results],
            verbose=False,
            color=False,
        ).splitlines()
        widths = {display_width(line) for line in lines}
        assert len(widths) == 1

    def test_terminal_control_path_escapes_before_alignment(self) -> None:
        results = [
            ('bad\x1b]2;path\x07.py', _summary_result(changed=True)),
            ('plain.py', _summary_result(changed=True)),
        ]

        lines = format_summary_rows(
            [build_summary_row(path, result) for path, result in results],
            verbose=False,
            color=False,
        ).splitlines()

        assert lines[1].startswith(r'bad\x1b]2;path\x07.py')
        assert len({display_width(line) for line in lines}) == 1


class TestFormatSummary:
    def test_all_unchanged_no_output(self) -> None:
        _d = _make_def
        unchanged = SortResult(
            original_source='same',
            new_source='same',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('a.py', unchanged), ('b.py', unchanged)]
            ],
            verbose=False,
            color=False,
        )
        assert output == ''

    def test_all_unchanged_verbose_shows_table(self) -> None:
        _d = _make_def
        unchanged = SortResult(
            original_source='same',
            new_source='same',
            original_order=[_d(0, 'a'), _d(1, 'b')],
            new_order=[_d(0, 'a'), _d(1, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('a.py', unchanged)]],
            verbose=True,
            color=False,
        )
        assert 'Name' in output  # header present
        assert 'a.py' in output
        assert '0%' in output
        assert 'TOTAL' not in output

    def test_columns_right_aligned(self) -> None:
        _d = _make_def
        r1 = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
            total_methods=7,
        )
        r2 = SortResult(
            original_source='',
            new_source='y',
            original_order=[
                _d(0, 'j'),
                _d(1, 'i'),
                _d(2, 'h'),
                _d(3, 'g'),
                _d(4, 'f'),
                _d(5, 'e'),
                _d(6, 'd'),
                _d(7, 'c'),
                _d(8, 'b'),
                _d(9, 'a'),
            ],
            new_order=[
                _d(9, 'a'),
                _d(8, 'b'),
                _d(7, 'c'),
                _d(6, 'd'),
                _d(5, 'e'),
                _d(4, 'f'),
                _d(3, 'g'),
                _d(2, 'h'),
                _d(1, 'i'),
                _d(0, 'j'),
            ],
            warning=None,
            blocking_code=[],
            total_methods=25,
        )
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('short.py', r1), ('longer_path.py', r2)]
            ],
            verbose=False,
            color=False,
        )
        lines = output.splitlines()
        # All lines padded to consistent width (tabular alignment)
        lengths = [len(line) for line in lines]
        assert max(lengths) - min(lengths) <= 1, f'Column alignment inconsistent: {lengths}'

    def test_filename_colored_by_severity(self) -> None:
        _d = _make_def
        # 2 of 2 moved = 100% -> red
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        # Filename is severity-colored (some ANSI color code, not pinned to a specific one)
        assert data.startswith('\033[')
        color = data[: data.index('f.py')]
        assert color.endswith('m'), 'expected ANSI color code before filename'
        # Both filename and percentage share the same severity color
        assert data.startswith(f'{color}f.py\033[0m')
        assert data.rstrip().endswith(f'{color}100%\033[0m')

    def test_filename_not_colored_at_zero_pct(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='from __future__ import annotations\n\ndef a():\n    pass\n',
            new_source='def a():\n    pass\n',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert data.startswith('f.py')  # no ANSI at start

    def test_header_bold_when_color_enabled(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        lines = output.splitlines()
        assert '\033[1m' in lines[0]  # header is bold
        assert '\033[1m' not in lines[1]  # data row not bold (non-verbose)

    def test_high_pct_uses_yellow(self) -> None:
        _d = _make_def
        # 2 of 2 moved = 100%
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert YELLOW in data
        assert RED not in data

    def test_low_pct_uses_yellow(self) -> None:
        _d = _make_def
        # 2 of 5 moved = 40% (swap first two, rest unchanged)
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'a'), _d(1, 'b'), _d(2, 'c'), _d(3, 'd'), _d(4, 'e')],
            new_order=[_d(1, 'b'), _d(0, 'a'), _d(2, 'c'), _d(3, 'd'), _d(4, 'e')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert YELLOW in data
        assert RED not in data

    def test_methods_in_separate_column(self) -> None:
        _d = _make_def
        m = MethodDefinition
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha')],
            new_order=[_d(0, 'alpha')],
            warning=None,
            blocking_code=[],
            total_methods=2,
            method_sorts=[
                MethodSortInfo(
                    class_name='C',
                    original_order=[
                        m(index=0, name='b', start_line=1, end_line=2, source=''),
                        m(index=1, name='a', start_line=3, end_line=4, source=''),
                    ],
                    new_order=[
                        m(index=1, name='a', start_line=3, end_line=4, source=''),
                        m(index=0, name='b', start_line=1, end_line=2, source=''),
                    ],
                ),
            ],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=False,
        )
        lines = output.splitlines()
        header = lines[0]
        data = lines[1]
        # Defs/Methods show total counts, not moved counts
        header_fields = header.split()
        data_fields = data.split()
        defs_idx = header_fields.index('Defs')
        methods_idx = header_fields.index('Methods')
        assert data_fields[defs_idx] == '1'
        assert data_fields[methods_idx] == '2'
        assert '66%' in data  # 2 of 3 moved = 66%

    def test_multi_file_with_total(self) -> None:
        _d = _make_def
        r1 = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        r2 = SortResult(
            original_source='',
            new_source='y',
            original_order=[_d(0, 'c'), _d(1, 'b'), _d(2, 'a')],
            new_order=[_d(2, 'a'), _d(1, 'b'), _d(0, 'c')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('a.py', r1), ('b.py', r2)]],
            verbose=False,
            color=False,
        )
        lines = output.splitlines()
        assert len(lines) == 4  # header + 2 data + TOTAL
        assert 'TOTAL' in lines[3]

    def test_retained_rows_preserve_exact_output(self) -> None:
        rows = [
            build_summary_row('a.py', _summary_result(changed=True)),
            build_summary_row('b.py', _summary_result(changed=True)),
        ]

        assert format_summary_rows(rows, verbose=False, color=False) == (
            'Name   Defs  Methods  Total  Move  Move%\n'
            'a.py      2        0      2     2   100%\n'
            'b.py      2        0      2     2   100%\n'
            'TOTAL     4        0      4     4   100%'
        )

    def test_single_file_no_total(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=False,
        )
        lines = output.splitlines()
        assert len(lines) == 2  # header + 1 data row
        assert 'Name' in lines[0]
        assert 'Defs' in lines[0]
        assert 'Methods' in lines[0]
        assert 'Total' in lines[0]
        assert 'Move' in lines[0]
        assert 'Move%' in lines[0]
        assert 'Moved' not in lines[0]
        header_fields = lines[0].split()
        assert header_fields.index('Total') < header_fields.index('Move')
        assert 'f.py' in lines[1]
        assert '100%' in lines[1]
        assert 'TOTAL' not in output

    def test_source_only_change(self) -> None:
        _d = _make_def
        source_only = SortResult(
            original_source='from __future__ import annotations\n\ndef a():\n    pass\n',
            new_source='def a():\n    pass\n',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        changed = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('a.py', changed), ('s.py', source_only)]
            ],
            verbose=False,
            color=False,
        )
        assert 's.py' in output
        assert 'TOTAL' in output  # n_changed=2

    def test_total_row_bold_when_color_enabled(self) -> None:
        _d = _make_def
        r1 = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        r2 = SortResult(
            original_source='',
            new_source='y',
            original_order=[_d(0, 'c'), _d(1, 'b'), _d(2, 'a')],
            new_order=[_d(2, 'a'), _d(1, 'b'), _d(0, 'c')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('a.py', r1), ('b.py', r2)]],
            verbose=False,
            color=True,
        )
        lines = output.splitlines()
        assert len(lines) == 4  # header + 2 data + TOTAL
        assert '\033[1m' in lines[0]  # header bold
        assert '\033[1m' in lines[3]  # TOTAL bold

    def test_total_row_omitted_when_n_changed_lt_2(self) -> None:
        _d = _make_def
        changed = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        unchanged = SortResult(
            original_source='same',
            new_source='same',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        # 1 changed + 1 unchanged with verbose = 2 visible rows but n_changed=1
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('a.py', changed), ('b.py', unchanged)]
            ],
            verbose=True,
            color=False,
        )
        assert 'TOTAL' not in output
        assert 'a.py' in output
        assert 'b.py' in output

    def test_truncated_zero_pct_still_colored(self) -> None:
        _d = _make_def
        # 2 of 201 moved = 0.995% -> truncates to 0%, but n_moved > 0
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
            total_methods=199,
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert YELLOW in data

    def test_verbose_color_distinguishes_by_severity_not_bold(self) -> None:
        _d = _make_def
        changed = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        unchanged = SortResult(
            original_source='same',
            new_source='same',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('a.py', changed), ('b.py', unchanged)]
            ],
            verbose=True,
            color=True,
        )
        lines = output.splitlines()
        # header still bold
        assert BOLD in lines[0]
        # changed row: severity color, no bold
        assert YELLOW in lines[1]
        assert BOLD not in lines[1]
        # unchanged row: no color, no bold
        assert BOLD not in lines[2]
        assert YELLOW not in lines[2]

    def test_verbose_excludes_unchanged_from_total(self) -> None:
        _d = _make_def
        r1 = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'b'), _d(1, 'a')],
            new_order=[_d(1, 'a'), _d(0, 'b')],
            warning=None,
            blocking_code=[],
        )
        r2 = SortResult(
            original_source='',
            new_source='y',
            original_order=[_d(0, 'c'), _d(1, 'b'), _d(2, 'a')],
            new_order=[_d(2, 'a'), _d(1, 'b'), _d(0, 'c')],
            warning=None,
            blocking_code=[],
        )
        unchanged = SortResult(
            original_source='same',
            new_source='same',
            original_order=[_d(0, 'x'), _d(1, 'y'), _d(2, 'z')],
            new_order=[_d(0, 'x'), _d(1, 'y'), _d(2, 'z')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [
                build_summary_row(path, result)
                for path, result in [('a.py', r1), ('b.py', r2), ('c.py', unchanged)]
            ],
            verbose=True,
            color=False,
        )
        lines = output.splitlines()
        total_line = next(line for line in lines if 'TOTAL' in line)
        assert total_line.split() == ['TOTAL', '5', '0', '5', '4', '80%']

    def test_zero_pct_no_color(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='from __future__ import annotations\n\ndef a():\n    pass\n',
            new_source='def a():\n    pass\n',
            original_order=[_d(0, 'a')],
            new_order=[_d(0, 'a')],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert YELLOW not in data
        assert RED not in data

    def test_zero_total_file(self) -> None:
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('empty.py', result)]],
            verbose=False,
            color=False,
        )
        lines = output.splitlines()
        data_line = lines[1]  # skip header
        assert 'empty.py' in data_line
        assert '%' not in data_line  # no percentage for zero-total

    def test_zero_total_no_color(self) -> None:
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
        )
        output = format_summary_rows(
            [build_summary_row(path, result) for path, result in [('f.py', result)]],
            verbose=False,
            color=True,
        )
        data = output.splitlines()[1]
        assert YELLOW not in data
        assert RED not in data


class TestVerboseTotals:
    def test_totals_aggregate_changed_rows_only(self) -> None:
        results = [
            ('a.py', _summary_result(changed=True)),
            ('b.py', _summary_result(changed=True)),
            ('c.py', _summary_result(changed=False)),
            ('d.py', _summary_result(changed=False)),
        ]
        normal_total = format_summary_rows(
            [build_summary_row(path, result) for path, result in results],
            verbose=False,
            color=False,
        ).splitlines()[-1]
        verbose_total = format_summary_rows(
            [build_summary_row(path, result) for path, result in results],
            verbose=True,
            color=False,
        ).splitlines()[-1]
        assert normal_total.split() == verbose_total.split()
        assert normal_total.split()[:5] == ['TOTAL', '4', '0', '4', '4']


def _summary_result(*, changed: bool) -> SortResult:
    first = Definition(index=0, name='b', start_line=1, end_line=2, kind='function', source='x')
    second = Definition(index=1, name='a', start_line=3, end_line=4, kind='function', source='y')
    return SortResult(
        original_source='x',
        new_source='y' if changed else 'x',
        original_order=[first, second],
        new_order=[second, first] if changed else [first, second],
        warning=None,
        blocking_code=[],
    )


def test_windows_summary_normalizes_only_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_terminal, 'PurePath', PureWindowsPath)
    row = build_summary_row(r'C:\repo\file.py', _summary_result(changed=True))
    output = format_summary_rows([row], verbose=False, color=False)
    assert 'C:/repo/file.py' in output
    assert row.path == r'C:\repo\file.py'
