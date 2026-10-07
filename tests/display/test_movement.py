import pytest

from defsort._color import (
    _BLUE,
    _CYAN,
    _MAGENTA,
    RED,
)
from defsort._display._movement import (
    _format_method_tables,
    count_moved_split,
    format_movement_table,
    movement_positions,
    print_preview,
)
from defsort._types import (
    BannerConstraint,
    HeldItem,
    MethodDefinition,
    MethodSortInfo,
    SectionBoundary,
    SortResult,
)
from tests.display._helpers import _make_def, _make_result


class TestCountMovedSplit:
    def test_both_defs_and_methods(self) -> None:
        _d = _make_def
        m = MethodDefinition
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'bravo'), _d(1, 'alpha')],
            new_order=[_d(1, 'alpha'), _d(0, 'bravo')],
            warning=None,
            blocking_code=[],
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
        top, methods = count_moved_split(result)
        assert top == 2
        assert methods == 2

    def test_methods_only(self) -> None:
        _d = _make_def
        m = MethodDefinition
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha')],
            new_order=[_d(0, 'alpha')],
            warning=None,
            blocking_code=[],
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
        top, methods = count_moved_split(result)
        assert top == 0
        assert methods == 2

    def test_no_movement(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha'), _d(1, 'bravo')],
            new_order=[_d(0, 'alpha'), _d(1, 'bravo')],
            warning=None,
            blocking_code=[],
        )
        top, methods = count_moved_split(result)
        assert top == 0
        assert methods == 0


class TestFormatMethodTables:
    def test_all_moved_header(self) -> None:
        m = MethodDefinition
        methods_orig = [
            m(index=0, name='bravo', start_line=2, end_line=3, source=''),
            m(index=1, name='alpha', start_line=5, end_line=6, source=''),
        ]
        methods_new = [
            m(index=1, name='alpha', start_line=5, end_line=6, source=''),
            m(index=0, name='bravo', start_line=2, end_line=3, source=''),
        ]
        info = MethodSortInfo(class_name='Foo', original_order=methods_orig, new_order=methods_new)
        output = _format_method_tables([info])
        assert 'Foo (2 methods):' in output
        assert 'of' not in output.split('Foo')[1].split(':')[0]

    def test_multiple_classes(self) -> None:
        m = MethodDefinition
        info1 = MethodSortInfo(
            class_name='Foo',
            original_order=[
                m(index=0, name='b', start_line=2, end_line=3, source=''),
                m(index=1, name='a', start_line=5, end_line=6, source=''),
            ],
            new_order=[
                m(index=1, name='a', start_line=5, end_line=6, source=''),
                m(index=0, name='b', start_line=2, end_line=3, source=''),
            ],
        )
        info2 = MethodSortInfo(
            class_name='Bar',
            original_order=[
                m(index=0, name='z', start_line=2, end_line=3, source=''),
                m(index=1, name='y', start_line=5, end_line=6, source=''),
            ],
            new_order=[
                m(index=1, name='y', start_line=5, end_line=6, source=''),
                m(index=0, name='z', start_line=2, end_line=3, source=''),
            ],
        )
        output = _format_method_tables([info1, info2])
        assert 'Foo' in output
        assert 'Bar' in output

    def test_single_class_compact(self) -> None:
        m = MethodDefinition
        methods_orig = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=1, name='bravo', start_line=5, end_line=6, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
        ]
        methods_new = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
            m(index=1, name='bravo', start_line=5, end_line=6, source=''),
        ]
        info = MethodSortInfo(
            class_name='Widget', original_order=methods_orig, new_order=methods_new
        )
        output = _format_method_tables([info])
        assert 'Widget (2 of 3 methods):' in output
        assert 'bravo' in output
        assert 'charlie' in output
        assert 'alpha' not in output.split('Widget')[1]  # alpha didn't move

    def test_verbose_shows_all_methods(self) -> None:
        m = MethodDefinition
        methods_orig = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=1, name='bravo', start_line=5, end_line=6, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
        ]
        methods_new = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
            m(index=1, name='bravo', start_line=5, end_line=6, source=''),
        ]
        info = MethodSortInfo(
            class_name='Widget', original_order=methods_orig, new_order=methods_new
        )
        output = _format_method_tables([info], verbose=True)
        lines_after_header = output.split('\n')[2:]
        assert len(lines_after_header) == 3
        assert lines_after_header[0].endswith('  -')


class TestFormatMethodTablesSpectralColor:
    def test_moved_methods_get_spectral_color(self) -> None:
        m = MethodDefinition
        methods_orig = [
            m(index=0, name='bravo', start_line=2, end_line=3, source=''),
            m(index=1, name='alpha', start_line=5, end_line=6, source=''),
        ]
        methods_new = [
            m(index=1, name='alpha', start_line=5, end_line=6, source=''),
            m(index=0, name='bravo', start_line=2, end_line=3, source=''),
        ]
        info = MethodSortInfo(class_name='Foo', original_order=methods_orig, new_order=methods_new)
        output = _format_method_tables([info], color=True)
        lines = output.splitlines()
        # Class header (line 0) is bold
        assert '\033[1m' in lines[0]
        # Column header (line 1) is bold
        assert '\033[1m' in lines[1]
        # Data rows have spectral color, not bold
        for line in lines[2:]:
            assert '\033[' in line  # has ANSI
            assert '\033[1m' not in line  # no bold
        # Verify specific color mapping:
        # new_order: [alpha(idx=1)@pos0 -> RED, bravo(idx=0)@pos1 -> _CYAN]

        # First data row: Proposed=alpha(RED), Current=bravo(_CYAN)
        assert lines[2].split() == [
            f'{_CYAN}bravo\033[0m',
            f'{RED}alpha\033[0m',
            f'{RED}^1\033[0m',
        ]
        # Second data row: Proposed=bravo(_CYAN), Current=alpha(RED)
        assert lines[3].split() == [
            f'{RED}alpha\033[0m',
            f'{_CYAN}bravo\033[0m',
            f'{_CYAN}v1\033[0m',
        ]

    def test_per_class_independent_spectrum(self) -> None:
        m = MethodDefinition
        info1 = MethodSortInfo(
            class_name='Foo',
            original_order=[
                m(index=0, name='b', start_line=2, end_line=3, source=''),
                m(index=1, name='a', start_line=5, end_line=6, source=''),
            ],
            new_order=[
                m(index=1, name='a', start_line=5, end_line=6, source=''),
                m(index=0, name='b', start_line=2, end_line=3, source=''),
            ],
        )
        info2 = MethodSortInfo(
            class_name='Bar',
            original_order=[
                m(index=0, name='z', start_line=2, end_line=3, source=''),
                m(index=1, name='y', start_line=5, end_line=6, source=''),
            ],
            new_order=[
                m(index=1, name='y', start_line=5, end_line=6, source=''),
                m(index=0, name='z', start_line=2, end_line=3, source=''),
            ],
        )
        output = _format_method_tables([info1, info2], color=True)
        # Both classes should have colored rows
        # Both class sections start their gradient at RED
        sections = output.split('\n\n')
        assert len(sections) == 2
        for section in sections:
            data_lines = section.splitlines()[2:]  # skip class + column headers
            assert RED in data_lines[0]  # first moved row starts at RED

    def test_spectral_colors_span_only_moved_methods(self) -> None:
        m = MethodDefinition
        # 4 methods: alpha stays at pos 0, others all change position.
        # 3 moved items -> RED, GREEN, _BLUE
        methods_orig = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=1, name='delta', start_line=5, end_line=6, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
            m(index=3, name='bravo', start_line=11, end_line=12, source=''),
        ]
        methods_new = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=3, name='bravo', start_line=11, end_line=12, source=''),
            m(index=1, name='delta', start_line=5, end_line=6, source=''),
            m(index=2, name='charlie', start_line=8, end_line=9, source=''),
        ]
        info = MethodSortInfo(class_name='Foo', original_order=methods_orig, new_order=methods_new)
        output = _format_method_tables([info], color=True)

        lines = output.splitlines()
        # Line 0 = class header, line 1 = column header, lines 2-4 = moved rows
        assert len(lines) == 5  # header + col header + 3 data rows
        # First moved row (proposed='bravo') -> RED (position 0 of 3 moved)
        assert RED in lines[2]
        # Last moved row (proposed='charlie') -> _BLUE (position 2 of 3: 2*6//3=4=_BLUE)
        assert _BLUE in lines[4]

    def test_unmoved_methods_no_color_in_verbose(self) -> None:
        m = MethodDefinition
        methods_orig = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=1, name='charlie', start_line=5, end_line=6, source=''),
            m(index=2, name='bravo', start_line=8, end_line=9, source=''),
        ]
        methods_new = [
            m(index=0, name='alpha', start_line=2, end_line=3, source=''),
            m(index=2, name='bravo', start_line=8, end_line=9, source=''),
            m(index=1, name='charlie', start_line=5, end_line=6, source=''),
        ]
        info = MethodSortInfo(class_name='Foo', original_order=methods_orig, new_order=methods_new)
        output = _format_method_tables([info], color=True, verbose=True)
        lines = output.splitlines()
        # Line 2 = alpha (unmoved) -- no ANSI color
        assert lines[2].endswith('  -')
        assert '\033[' not in lines[2]


class TestFormatMovementTable:
    def test_repeated_names_correct_movement(self) -> None:
        _d = _make_def
        # original [bravo, alpha, alpha] -> new [alpha, alpha, bravo]
        # Second alpha moved from pos 2 to pos 1: should show ^1
        result = SortResult(
            original_source='',
            new_source='',
            original_order=[_d(0, 'bravo'), _d(1, 'alpha'), _d(2, 'alpha')],
            new_order=[_d(1, 'alpha'), _d(2, 'alpha'), _d(0, 'bravo')],
            skipped=False,
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result)
        data_lines = table.splitlines()[1:]  # skip header
        # Second alpha (new pos 1, old pos 2): should show ^1
        assert '^1' in data_lines[1]


class TestFormatMovementTableCompactMode:
    def test_default_shows_only_moved_rows(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha'), _d(1, 'bravo'), _d(2, 'charlie'), _d(3, 'delta')],
            new_order=[_d(0, 'alpha'), _d(2, 'charlie'), _d(1, 'bravo'), _d(3, 'delta')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result)
        data_lines = table.splitlines()[1:]  # skip header
        assert len(data_lines) == 2  # only bravo and charlie moved
        assert 'bravo' in data_lines[0] or 'bravo' in data_lines[1]
        assert 'charlie' in data_lines[0] or 'charlie' in data_lines[1]
        assert 'alpha' not in '\n'.join(data_lines)
        assert 'delta' not in '\n'.join(data_lines)

    def test_verbose_shows_all_rows(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha'), _d(1, 'bravo'), _d(2, 'charlie'), _d(3, 'delta')],
            new_order=[_d(0, 'alpha'), _d(2, 'charlie'), _d(1, 'bravo'), _d(3, 'delta')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, verbose=True)
        data_lines = table.splitlines()[1:]
        assert len(data_lines) == 4
        assert data_lines[0] == 'alpha    alpha     -'
        assert data_lines[3] == 'delta    delta     -'

    def test_verbose_spectral_color_on_moved_rows(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha'), _d(1, 'bravo')],
            new_order=[_d(1, 'bravo'), _d(0, 'alpha')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, verbose=True, color=True)
        lines = table.splitlines()
        # Header bold, data rows have spectral color but not bold
        assert '\033[1m' in lines[0]
        for line in lines[1:]:
            assert '\033[' in line  # has ANSI (spectral color on moved row)
            assert '\033[1m' not in line  # not bold


class TestFormatMovementTableSpectralColor:
    def test_current_colored_by_destination(self) -> None:
        _d = _make_def
        # Simple swap: bravo(0) <-> alpha(1)
        # In proposed order: alpha(1) is pos 0 (RED), bravo(0) is pos 1
        # Current column: row 0 shows bravo (original_order[0])
        #   bravo's index is 0, color_map[0] = _CYAN (pos 1 in proposed)
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'bravo'), _d(1, 'alpha')],
            new_order=[_d(1, 'alpha'), _d(0, 'bravo')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, color=True)

        lines = table.splitlines()
        # Row 0: current=bravo (index 0 -> _CYAN), proposed=alpha (index 1 -> RED)
        assert lines[1].split() == [
            f'{_CYAN}bravo\033[0m',
            f'{RED}alpha\033[0m',
            f'{RED}^1\033[0m',
        ]

    def test_moved_rows_get_spectral_color(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'bravo'), _d(1, 'alpha')],
            new_order=[_d(1, 'alpha'), _d(0, 'bravo')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, color=True)
        lines = table.splitlines()
        # Header is bold
        assert '\033[1m' in lines[0]
        # Data rows have color escapes (spectral, not bold)
        for line in lines[1:]:
            assert '\033[' in line  # has ANSI color
            assert '\033[1m' not in line  # no bold

    def test_proposed_and_move_share_color(self) -> None:
        _d = _make_def
        # 6 defs: each gets a unique spectrum color
        orig = [_d(i, name) for i, name in enumerate(['f', 'e', 'd', 'c', 'b', 'a'])]
        new = [
            _d(i, name) for i, name in [(5, 'a'), (4, 'b'), (3, 'c'), (2, 'd'), (1, 'e'), (0, 'f')]
        ]
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=orig,
            new_order=new,
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, color=True)
        lines = table.splitlines()
        # Proposed name and move value share the same spectral color
        assert lines[1].split() == [
            f'{_MAGENTA}f\033[0m',
            f'{RED}a\033[0m',
            f'{RED}^5\033[0m',
        ]

    def test_spectral_colors_span_only_moved_items(self) -> None:
        _d = _make_def
        # 8 defs total, first 2 unmoved (already in sorted position).
        # Moved items (6) should each get a distinct spectrum color.
        orig = [
            _d(0, 'a'),
            _d(1, 'b'),  # these stay in place
            _d(2, 'h'),
            _d(3, 'g'),
            _d(4, 'f'),
            _d(5, 'e'),
            _d(6, 'd'),
            _d(7, 'c'),
        ]
        new = [
            _d(0, 'a'),
            _d(1, 'b'),  # unmoved
            _d(7, 'c'),
            _d(6, 'd'),
            _d(5, 'e'),
            _d(4, 'f'),
            _d(3, 'g'),
            _d(2, 'h'),
        ]
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=orig,
            new_order=new,
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, color=True)

        lines = table.splitlines()
        # 6 moved rows displayed (a and b are hidden, unmoved)
        assert len(lines) == 7  # header + 6 data rows
        # First visible moved row (proposed='c') should be RED
        assert RED in lines[1]
        # Last visible moved row (proposed='h') should be _MAGENTA
        assert _MAGENTA in lines[6]
        # Middle rows should have intermediate colors, not RED or _MAGENTA
        assert _BLUE in lines[5]

    def test_unmoved_rows_no_color(self) -> None:
        _d = _make_def
        result = SortResult(
            original_source='',
            new_source='x',
            original_order=[_d(0, 'alpha'), _d(1, 'charlie'), _d(2, 'bravo')],
            new_order=[_d(0, 'alpha'), _d(2, 'bravo'), _d(1, 'charlie')],
            warning=None,
            blocking_code=[],
        )
        table = format_movement_table(result, color=True, verbose=True)
        lines = table.splitlines()
        # Line 0 = header (bold), line 1 = alpha (unmoved), lines 2-3 = moved
        assert lines[1].endswith('  -')
        assert '\033[' not in lines[1]  # alpha row has no ANSI


class TestMovementPositions:
    def test_repeated_names_use_stable_indices(self) -> None:
        original = [_make_def(4, 'same'), _make_def(9, 'middle'), _make_def(7, 'same')]
        proposed = [original[2], original[1], original[0]]

        positions = movement_positions(original, proposed)

        assert positions.old_by_index == {4: 0, 9: 1, 7: 2}
        assert positions.moved_indices == [7, 4]


class TestPrintPreview:
    def test_banner_constraint_only_noop_requires_verbose(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = _make_result(changed=False)
        result.banner_constraints = [
            BannerConstraint(
                scope='definitions',
                class_name=None,
                boundary=SectionBoundary(1, 7, 7, '# --------\n', '# --------'),
                held=(HeldItem('alpha', 'function', 1, 10, 'before'),),
            )
        ]

        compact_emitted = print_preview(
            'test.py',
            result,
            verbose=False,
            ordering='dependency',
            color=False,
            multi=False,
            first=True,
        )
        compact = capsys.readouterr()
        verbose_emitted = print_preview(
            'test.py',
            result,
            verbose=True,
            ordering='dependency',
            color=False,
            multi=False,
            first=True,
        )
        verbose = capsys.readouterr()

        assert compact_emitted is False
        assert compact.out == ''
        assert verbose_emitted is True
        assert 'line 7: --------: alpha (before)' in verbose.out

    def test_changed_multi_color_bolds_path_only(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        emitted = print_preview(
            'test.py',
            _make_result(changed=True),
            verbose=False,
            ordering='dependency',
            color=True,
            multi=True,
            first=True,
        )

        assert emitted is True
        assert capsys.readouterr().out.startswith('\033[1mtest.py\033[0m:\n\n')

    def test_emits_movement_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _make_result(changed=True)
        emitted = print_preview(
            'test.py',
            result,
            verbose=False,
            ordering='dependency',
            color=False,
            multi=False,
            first=True,
        )
        assert emitted is True
        output = capsys.readouterr().out
        assert 'Current' in output
        assert 'Proposed' in output
        assert 'bravo' in output
        assert 'alpha' in output

    def test_path_controls_escape_before_owned_color(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        emitted = print_preview(
            'bad\x1b]2;path\x07.py',
            _make_result(changed=True),
            verbose=False,
            ordering='dependency',
            color=True,
            multi=True,
            first=True,
        )

        assert emitted is True
        output = capsys.readouterr().out
        assert output.startswith('\033[1mbad\\x1b]2;path\\x07.py\033[0m:\n\n')
        assert output.count('\n') > 2

    def test_unchanged_no_verbose(self) -> None:
        result = _make_result(changed=False)
        emitted = print_preview(
            'test.py',
            result,
            verbose=False,
            ordering='dependency',
            color=False,
            multi=False,
            first=True,
        )
        assert emitted is False

    def test_unchanged_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _make_result(changed=False)
        emitted = print_preview(
            'test.py',
            result,
            verbose=True,
            ordering='dependency',
            color=False,
            multi=False,
            first=True,
        )
        assert emitted is True
        assert 'already in dependency order' in capsys.readouterr().out

    def test_unchanged_verbose_color_bolds_path_only(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        emitted = print_preview(
            'test.py',
            _make_result(changed=False),
            verbose=True,
            ordering='dependency',
            color=True,
            multi=False,
            first=True,
        )

        assert emitted is True
        assert capsys.readouterr().out == ('\033[1mtest.py\033[0m: already in dependency order\n')
