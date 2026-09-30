import ast

from defsort._source import (
    is_layout_blank,
    join_physical,
    join_physical_fragments,
    scope_indent_body,
    split_physical_lines,
    terminal_line_ending,
)

_ORDINARY_CHARACTERS = ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')


class TestIsLayoutBlank:
    def test_blank_bodies(self) -> None:
        assert is_layout_blank('') is True
        assert is_layout_blank('\n') is True
        assert is_layout_blank('\r\n') is True
        assert is_layout_blank(' \t\f') is True
        assert is_layout_blank('\f\r\n') is True

    def test_comment_line_is_not_blank(self) -> None:
        assert is_layout_blank('# note\n') is False

    def test_other_unicode_whitespace_is_not_blank(self) -> None:
        for char in ('\v', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029', '\xa0'):
            assert is_layout_blank(char) is False


class TestJoinPhysical:
    def test_plain_join_unchanged(self) -> None:
        assert join_physical('x\n', '\ny') == 'x\n\ny'
        assert join_physical('x\r\n', '\ny') == 'x\r\n\ny'
        assert join_physical('', '\ny') == '\ny'
        assert join_physical('x\r', 'y') == 'x\ry'

    def test_swaps_cr_lf_seam(self) -> None:
        assert join_physical('x\r', '\ny') == 'x\n\ry'


class TestJoinPhysicalFragments:
    def test_exact_fragments(self) -> None:
        cases: list[tuple[list[str], str]] = [
            ([], ''),
            (['', '', ''], ''),
            (['alpha\r'], 'alpha\r'),
            (['alpha\n', '\nbeta'], 'alpha\n\nbeta'),
            (['alpha\r\n', '\nbeta'], 'alpha\r\n\nbeta'),
            (['alpha\r', '\nbeta'], 'alpha\n\rbeta'),
            (
                ['head\r', '', '\nmid\r\n', '\ntail\r', '\n', 'end'],
                'head\n\rmid\r\n\ntail\n\rend',
            ),
        ]

        for fragments, expected in cases:
            assert join_physical_fragments(fragments) == expected

    def test_large_iterable(self) -> None:
        fragments = ('fragment' for _ in range(50_000))

        assert join_physical_fragments(fragments) == 'fragment' * 50_000


class TestScopeIndentBody:
    def test_exact_indent_matches(self) -> None:
        assert scope_indent_body('    # note\n', '    ') == '# note'

    def test_form_feed_then_short_indent_does_not_match(self) -> None:
        assert scope_indent_body('\f# note', '    ') is None

    def test_last_leading_form_feed_wins(self) -> None:
        assert scope_indent_body('  \f    # note', '    ') == '# note'

    def test_leading_form_feed_matches_top_level(self) -> None:
        assert scope_indent_body('\f# note\n', '') == '# note'

    def test_leading_form_feed_then_indent_matches(self) -> None:
        assert scope_indent_body('\f    # note\n', '    ') == '# note'

    def test_short_indent_does_not_match(self) -> None:
        assert scope_indent_body('  # note', '    ') is None


class TestSplitPhysicalLines:
    def test_ast_span_slice_selects_exact_source(self) -> None:
        source = '"""Doc\fpage."""\nalpha = (\n    1\n)\nbeta = 2\n'
        node = ast.parse(source).body[1]
        assert isinstance(node, ast.Assign)
        lines = split_physical_lines(source, keepends=True)
        assert ''.join(lines[node.lineno - 1 : node.end_lineno]) == 'alpha = (\n    1\n)\n'

    def test_consecutive_endings_preserve_empty_lines(self) -> None:
        assert split_physical_lines('alpha\n\nbeta') == ['alpha', '', 'beta']

    def test_crlf_is_one_indivisible_ending(self) -> None:
        assert split_physical_lines('alpha\r\nbeta') == ['alpha', 'beta']
        assert split_physical_lines('alpha\r\nbeta', keepends=True) == ['alpha\r\n', 'beta']

    def test_empty_source_has_no_lines(self) -> None:
        assert split_physical_lines('') == []

    def test_keepends_round_trip_is_exact(self) -> None:
        source = 'alpha\r\nbeta\rgamma\n\ndelta'
        assert ''.join(split_physical_lines(source, keepends=True)) == source

    def test_lone_newline_is_one_empty_line(self) -> None:
        assert split_physical_lines('\n') == ['']

    def test_mixed_endings_each_end_one_line(self) -> None:
        assert split_physical_lines('alpha\r\nbeta\rgamma\n') == ['alpha', 'beta', 'gamma']

    def test_no_endings_view_removes_only_the_ending(self) -> None:
        assert split_physical_lines('alpha\r\n') == ['alpha']
        assert split_physical_lines('alpha\r') == ['alpha']

    def test_ordinary_characters_never_change_cardinality(self) -> None:
        for char in _ORDINARY_CHARACTERS:
            source = f'alpha{char}beta\nomega'
            assert split_physical_lines(source) == [f'alpha{char}beta', 'omega']
            assert split_physical_lines(source, keepends=True) == [
                f'alpha{char}beta\n',
                'omega',
            ]

    def test_terminal_ending_adds_no_extra_element(self) -> None:
        assert split_physical_lines('alpha\n') == ['alpha']

    def test_unterminated_input_has_one_final_line(self) -> None:
        assert split_physical_lines('alpha') == ['alpha']


class TestTerminalLineEnding:
    def test_crlf(self) -> None:
        assert terminal_line_ending('alpha\r\n') == '\r\n'

    def test_empty_and_unterminated(self) -> None:
        assert terminal_line_ending('') == ''
        assert terminal_line_ending('alpha') == ''
        assert terminal_line_ending('alpha\f') == ''

    def test_lf_and_lone_cr(self) -> None:
        assert terminal_line_ending('alpha\n') == '\n'
        assert terminal_line_ending('alpha\r') == '\r'
