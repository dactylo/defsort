import ast
from collections import Counter
from collections.abc import Iterable, Iterator

import pytest

from defsort._core._analysis import collect_name_references, iter_child_nodes
from defsort._core._api import sort_file
from defsort._core._hoist import (
    _count_constant_bindings,
    _is_literal_only,
    extract_hoistable_constants,
    get_safe_to_hoist_by_gap,
    remove_hoisted_lines,
)
from defsort._core._parse import parse_definitions
from defsort._core._reassemble import reassemble_source
from defsort._source import join_physical_fragments
from defsort._types import EngineOptions, SortPolicy


class TestCountConstantBindings:
    def test_augmented_assignment(self) -> None:
        tree = ast.parse('MAX = 1\nMAX += 1\n')
        assert _count_constant_bindings(tree, {'MAX'})['MAX'] == 2

    def test_class_def(self) -> None:
        tree = ast.parse('class MAX_SIZE: pass\n')
        assert _count_constant_bindings(tree, {'MAX_SIZE'})['MAX_SIZE'] == 1

    def test_for_target(self) -> None:
        tree = ast.parse('for ITEM in items: pass\n')
        assert _count_constant_bindings(tree, {'ITEM'})['ITEM'] == 1

    def test_import_binding(self) -> None:
        tree = ast.parse('from config import MAX_SIZE\n')
        assert _count_constant_bindings(tree, {'MAX_SIZE'})['MAX_SIZE'] == 1

    def test_single_assignment(self) -> None:
        tree = ast.parse('MAX_SIZE = 100\n')
        assert _count_constant_bindings(tree, {'MAX_SIZE'})['MAX_SIZE'] == 1

    def test_two_assignments(self) -> None:
        tree = ast.parse('MAX_SIZE = 100\nMAX_SIZE = 200\n')
        assert _count_constant_bindings(tree, {'MAX_SIZE'})['MAX_SIZE'] == 2

    def test_walrus_operator(self) -> None:
        tree = ast.parse('x = (MAX := 10)\n')
        assert _count_constant_bindings(tree, {'MAX'})['MAX'] == 1

    def test_zero_bindings(self) -> None:
        tree = ast.parse('other = 100\n')
        assert _count_constant_bindings(tree, {'MAX_SIZE'})['MAX_SIZE'] == 0


class TestExtractHoistableConstants:
    def test_comment_after_constant(self) -> None:
        trailing = '\nMAX_SIZE = 100\n# a comment\n'
        result, _lines = extract_hoistable_constants(trailing, {'MAX_SIZE'})
        assert result == ['MAX_SIZE = 100\n\n']

    def test_constants_do_not_share_authored_boundary(self) -> None:
        trailing = '\nFIRST = 1\n\nSECOND = 2\n'

        hoisted, lines = extract_hoistable_constants(trailing, {'FIRST', 'SECOND'})
        remainder = remove_hoisted_lines(trailing, lines)

        assert hoisted == ['FIRST = 1\n\n', 'SECOND = 2\n']
        assert lines == {2, 3, 4}
        assert remainder == '\n'
        assert Counter((''.join(hoisted) + remainder).encode()) == Counter(trailing.encode())

    def test_empty_trailing(self) -> None:
        result, _lines = extract_hoistable_constants('', set())
        assert result == []

    @pytest.mark.parametrize(
        ('trailing', 'expected'),
        [
            ('\nMAX_SIZE = 100  # limit\n\n', 'MAX_SIZE = 100  # limit\n\n'),
            ('\r\nMAX_SIZE = 100 \t\r\n\r\n', 'MAX_SIZE = 100 \t\r\n\r\n'),
            ('\rMAX_SIZE = 100  \r\r', 'MAX_SIZE = 100  \r\r'),
            (
                '\r\nMAPPING = {\n    1: 2,\r}\r\n\t\r',
                'MAPPING = {\n    1: 2,\r}\r\n\t\r',
            ),
            ('\nMAX_SIZE = 100  # limit\t', 'MAX_SIZE = 100  # limit\t\n'),
        ],
        ids=['lf', 'crlf', 'lone-cr', 'mixed-multiline', 'unterminated'],
    )
    def test_exact_assignment_and_boundary_span(self, trailing: str, expected: str) -> None:
        names = {'MAPPING'} if 'MAPPING' in trailing else {'MAX_SIZE'}

        result, _lines = extract_hoistable_constants(trailing, names)

        assert result == [expected]

    def test_function_call_value_rejected(self) -> None:
        trailing = '\nMAX_SIZE = compute_max()\n'
        result, _lines = extract_hoistable_constants(trailing, {'MAX_SIZE'})
        assert result == []

    def test_leading_blank_and_form_feed_comment_use_trailing_coordinates(self) -> None:
        trailing = '\n# page\fnote\nLIMIT = 10\n'
        hoisted, lines = extract_hoistable_constants(trailing, {'LIMIT'})
        assert hoisted == ['LIMIT = 10\n']
        assert lines == {3}
        assert remove_hoisted_lines(trailing, lines) == '\n# page\fnote\n'

    def test_multiline_string_indentation_preserved(self) -> None:
        trailing = '\nTEXT = """\n  keep this\n    and this\n"""\n'
        result, _lines = extract_hoistable_constants(trailing, {'TEXT'})
        assert len(result) == 1
        assert '  keep this' in result[0]
        assert '    and this' in result[0]

    def test_non_safe_remaining_blocks(self) -> None:
        trailing = '\nMAX_SIZE = 100\nregistry = {}\n'
        result, _lines = extract_hoistable_constants(trailing, {'MAX_SIZE'})
        assert result == []

    def test_simple_constant(self) -> None:
        trailing = '\nMAX_SIZE = 100\n'
        result, _lines = extract_hoistable_constants(trailing, {'MAX_SIZE'})
        assert result == ['MAX_SIZE = 100\n\n']

    def test_statement_collection_has_constant_traversal_count(self) -> None:

        class CountingStatements(list[ast.stmt]):
            def __init__(self, statements: list[ast.stmt]) -> None:
                super().__init__(statements)
                self.iterations = 0

            def __iter__(self) -> Iterator[ast.stmt]:
                self.iterations += 1
                return super().__iter__()

        count = 1_000
        names = {f'CONST_{index}' for index in range(count)}
        trailing = ''.join(f'CONST_{index} = {index}\n' for index in range(count))
        trail_tree = ast.parse(trailing)
        statements = CountingStatements(trail_tree.body)
        trail_tree.body = statements

        hoisted, _lines = extract_hoistable_constants(
            trailing,
            names,
            _trail_tree=trail_tree,
        )

        assert len(hoisted) == count
        assert statements.iterations <= 2

    def test_tuple_unpacking_rejected(self) -> None:
        trailing = '\nMIN, MAX = 0, 100\n'
        result, _lines = extract_hoistable_constants(trailing, {'MIN', 'MAX'})
        assert result == []


class TestGetSafeToHoist:
    def test_candidate_binding_single_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = 'ALPHA = 1\nBRAVO = 2\nCHARLIE = 3\n'
        tree = ast.parse(source)
        candidates = {'ALPHA', 'BRAVO', 'CHARLIE'}
        root_walks = 0
        original_iter_child_nodes = iter_child_nodes

        def spy_iter_child_nodes(node: ast.AST) -> Iterator[ast.AST]:
            nonlocal root_walks
            if node is tree:
                root_walks += 1
            return original_iter_child_nodes(node)

        monkeypatch.setattr('defsort._core._hoist.iter_child_nodes', spy_iter_child_nodes)

        assert get_safe_to_hoist_by_gap(
            tree,
            {0: candidates},
            candidate_positions=None,
        ) == {0: candidates}
        assert root_walks == 1

    def test_early_ref_blocked(self) -> None:
        src = 'def alpha(): return MAX\nMAX = 100\n'
        tree = ast.parse(src)
        assert (
            'MAX'
            not in get_safe_to_hoist_by_gap(
                tree,
                {0: {'MAX'}},
                candidate_positions=None,
            )[0]
        )

    def test_rebound_blocked(self) -> None:
        src = 'MAX = 100\nMAX = 200\n'
        tree = ast.parse(src)
        assert (
            'MAX'
            not in get_safe_to_hoist_by_gap(
                tree,
                {0: {'MAX'}},
                candidate_positions=None,
            )[0]
        )

    def test_simple_safe(self) -> None:
        src = 'def alpha(): pass\nMAX = 100\n'
        tree = ast.parse(src)
        assert (
            'MAX'
            in get_safe_to_hoist_by_gap(
                tree,
                {0: {'MAX'}},
                candidate_positions=None,
            )[0]
        )


class TestHoistPreservesComments:
    @pytest.mark.parametrize(
        'case',
        [
            pytest.param(
                (
                    '\n',
                    ' \n',
                    'MAX_SIZE = 100  # limit\t\n',
                    '\t\n',
                    '\n',
                    '\n',
                ),
                id='lf-final-newline',
            ),
            pytest.param(
                (
                    '\r\n',
                    ' \r\n',
                    'MAX_SIZE = 100  # limit\t\r\n',
                    '\t\r\n',
                    '\r\n',
                    '',
                ),
                id='crlf-no-final-newline',
            ),
            pytest.param(
                (
                    '\r',
                    ' \r',
                    'MAX_SIZE = 100  \r',
                    '\t\r',
                    '\r',
                    '\r',
                ),
                id='lone-cr-final-newline',
            ),
            pytest.param(
                (
                    '\r\n',
                    ' \r',
                    'MAX_SIZE = 100  # limit\t\n',
                    '\t\r\n',
                    '\r',
                    '',
                ),
                id='mixed-no-final-newline',
            ),
        ],
    )
    def test_hoist_layout_owns_exact_source_components(
        self,
        case: tuple[str, str, str, str, str, str],
    ) -> None:
        first_ending, before, assignment, after, second_ending, final_ending = case
        structural_preamble = f'import os{first_ending}'
        initial_separator = first_ending
        first_source = f'def zulu():{first_ending}    pass{first_ending}'
        second_source = f'def alpha():{second_ending}    return 2{final_ending}'
        source = (
            structural_preamble
            + initial_separator
            + first_source
            + before
            + assignment
            + after
            + second_source
        )
        first_block = first_source[: -len(first_ending)]
        second_block = second_source[: -len(final_ending)] if final_ending else second_source
        hoisted = assignment + before
        separator = first_ending + after
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        parsed = parse_definitions(source, hoist=True)

        expected_prefix = structural_preamble + hoisted + initial_separator

        assert parsed.hoistable == hoisted
        assert parsed.layout.prefix == expected_prefix
        assert parsed.layout.separators == (separator,)
        assert parsed.layout.suffix == final_ending
        assert parsed.blocks == {0: first_block, 1: second_block}
        non_hoisted = (
            structural_preamble
            + initial_separator
            + parsed.blocks[0]
            + parsed.layout.separators[0]
            + parsed.blocks[1]
            + parsed.layout.suffix
        )
        assert non_hoisted == source.replace(before + assignment, '', 1)

        reordered = reassemble_source(
            parsed.layout,
            parsed.blocks,
            list(reversed(parsed.definitions)),
        )
        expected = expected_prefix + second_block + separator + first_block + final_ending

        assert reordered == expected
        assert sort_file(source, options=options).new_source == expected
        assert sort_file(expected, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('source', 'assignment', 'boundary'),
        [
            (
                (
                    'def alpha():\n'
                    '    pass\n'
                    ' \n'
                    'MAX_SIZE = 100  # limit\t\n'
                    '\t\n'
                    'def bravo():\n'
                    '    return MAX_SIZE'
                ),
                'MAX_SIZE = 100  # limit\t\n',
                ' \n',
            ),
            (
                (
                    'def alpha():\r\n'
                    '    pass\r\n'
                    ' \r\n'
                    'MAX_SIZE = 100  # limit\t\r\n'
                    '\t\r\n'
                    'def bravo():\r\n'
                    '    return MAX_SIZE'
                ),
                'MAX_SIZE = 100  # limit\t\r\n',
                ' \r\n',
            ),
            (
                (
                    'def alpha():\r'
                    '    pass\r'
                    ' \r'
                    'MAX_SIZE = 100  # limit\t\r'
                    '\t\r'
                    'def bravo():\r'
                    '    return MAX_SIZE'
                ),
                'MAX_SIZE = 100  # limit\t\r',
                ' \r',
            ),
            (
                (
                    'def alpha():\r\n'
                    '    pass\r\n'
                    ' \r'
                    'MAX_SIZE = 100  # limit\t\n'
                    '\t\r\n'
                    'def bravo():\r'
                    '    return MAX_SIZE'
                ),
                'MAX_SIZE = 100  # limit\t\n',
                ' \r',
            ),
        ],
        ids=['lf', 'crlf', 'lone-cr', 'mixed'],
    )
    def test_hoist_moves_assignment_and_authored_boundary(
        self,
        source: str,
        assignment: str,
        boundary: str,
    ) -> None:
        hoisted = assignment + boundary
        expected = hoisted + source.replace(boundary + assignment, '', 1)
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='none'),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_hoist_preserves_inline_comment(self) -> None:
        src = 'def b():\n    pass\n\nMAX_SIZE = 100  # limit\n\ndef a():\n    pass\n'
        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                ),
                hoist=True,
                add_future=False,
            ),
        )
        assert 'MAX_SIZE = 100  # limit' in result.new_source


class TestHoistStatementOwnership:
    def test_multiline_assignment_sharing_end_line_is_not_moved(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            'VALUES = (\n'
            '    1,\n'
            '); side_effect()\n'
            '\n'
            'def bravo():\n'
            '    return VALUES\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='none'),
            hoist=True,
            add_future=False,
            remove_future=False,
            force=True,
        )

        result = sort_file(source, options=options)

        assert result.new_source == source

    def test_same_line_constants_are_not_duplicated(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            'MAX_SIZE = 100; MIN_SIZE = 1\n'
            '\n'
            'def bravo():\n'
            '    return MAX_SIZE + MIN_SIZE\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='none'),
            hoist=True,
            add_future=False,
            remove_future=False,
            force=True,
        )

        result = sort_file(source, options=options)

        assert result.new_source == source
        assert result.new_source.count('MAX_SIZE = 100; MIN_SIZE = 1') == 1

    def test_same_line_side_effect_is_not_moved(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            'MAX_SIZE = 100; side_effect()\n'
            '\n'
            'def bravo():\n'
            '    return MAX_SIZE\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='none'),
            hoist=True,
            add_future=False,
            remove_future=False,
            force=True,
        )

        result = sort_file(source, options=options)

        assert result.new_source == source
        assert result.new_source.index('side_effect()') > result.new_source.index('def alpha')


class TestInitialGapHoistPlacement:
    def test_banner_directive_precede_hoisted_span(self) -> None:
        head = '# --------\n\n# type: ignore\n'
        assignment = 'MAX_SIZE = 100\n'
        boundary = '\n'
        source = (
            head + 'def zeta():\n'
            '    return 1\n'
            '\n' + assignment + boundary + 'def alpha():\n'
            '    return 2\n'
        )
        expected = (
            head + assignment + boundary + 'def alpha():\n'
            '    return 2\n'
            '\n'
            'def zeta():\n'
            '    return 1\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners='barriers',
            ),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        parsed = parse_definitions(source, banners='barriers', hoist=True)
        result = sort_file(source, options=options)

        assert parsed.layout.prefix == head + assignment + boundary
        assert len(parsed.boundaries) == 1
        assert parsed.definitions[0].section == 1
        assert result.new_source == expected

    def test_head_banner_follows_hoists_without_directive(self) -> None:
        assignment = 'MAX_SIZE = 100\n'
        boundary = '\n'
        source = (
            '# --------\n'
            '\n'
            'def zeta():\n'
            '    return 1\n'
            '\n' + assignment + boundary + 'def alpha():\n'
            '    return 2\n'
        )
        expected = (
            assignment + boundary + '# --------\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            'def zeta():\n'
            '    return 1\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners='barriers',
            ),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        parsed = parse_definitions(source, banners='barriers', hoist=True)
        result = sort_file(source, options=options)

        assert parsed.layout.prefix == assignment + boundary + '# --------\n\n'
        assert result.new_source == expected

    @pytest.mark.parametrize(
        'source',
        [
            (
                '# file note\n'
                '\n'
                'def zeta():\n'
                '    return 1\n'
                '\n'
                'MAX_SIZE = 100\n'
                '\n'
                'def alpha():\n'
                '    return MAX_SIZE\n'
            ),
            ('def zeta():\n    return 1\n\nMAX_SIZE = 100\n\ndef alpha():\n    return MAX_SIZE\n'),
            (
                '# --------\n'
                '\n'
                'def zeta():\n'
                '    return 1\n'
                '\n'
                'MAX_SIZE = 100\n'
                '\n'
                'def alpha():\n'
                '    return MAX_SIZE\n'
            ),
            (
                '# --------\n'
                '\n'
                '# type: ignore\n'
                'def zeta():\n'
                '    return 1\n'
                '\n'
                'MAX_SIZE = 100\n'
                '\n'
                'def alpha():\n'
                '    return MAX_SIZE\n'
            ),
            (
                'def zeta():\n'
                '    return 1\n'
                '\n'
                'MAX_SIZE = 100\n'
                'MIN_SIZE = 1\n'
                '\n'
                'def alpha():\n'
                '    return MAX_SIZE + MIN_SIZE\n'
            ),
        ],
        ids=[
            'ordinary-separator',
            'no-separator',
            'head-banner',
            'banner-directive',
            'multiple-hoists',
        ],
    )
    def test_initial_gap_hoist_cases_are_idempotent(self, source: str) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners='barriers',
            ),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        first = sort_file(source, options=options).new_source
        second = sort_file(first, options=options).new_source

        assert second == first

    def test_multiple_hoists_keep_source_order(self) -> None:
        first_assignment = 'MAX_SIZE = 100\n'
        second_assignment = 'MIN_SIZE = 1\n'
        before_boundary = '\n'
        after_boundary = '\n'
        assignments = first_assignment + second_assignment
        expected_hoisted = first_assignment + before_boundary + second_assignment
        source = (
            'def alpha():\n'
            '    return 1\n'
            f'{before_boundary}{assignments}{after_boundary}'
            'def bravo():\n'
            '    return MAX_SIZE + MIN_SIZE\n'
        )

        parsed = parse_definitions(source, hoist=True)

        assert parsed.hoistable == expected_hoisted
        assert parsed.layout.prefix == expected_hoisted

    def test_no_initial_separator_is_synthesized(self) -> None:
        assignment = 'MAX_SIZE = 100\n'
        boundary = '\n'
        hoisted = assignment + boundary
        source = (
            'def zeta():\n'
            '    return 1\n'
            f'{boundary}{assignment}{boundary}'
            'def alpha():\n'
            '    return MAX_SIZE\n'
        )
        expected = hoisted + 'def zeta():\n    return 1\n\ndef alpha():\n    return MAX_SIZE\n'
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
            ),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        parsed = parse_definitions(source, hoist=True)
        result = sort_file(source, options=options)

        assert parsed.layout.prefix == hoisted
        assert result.new_source == expected


class TestIsLiteralOnly:
    def _parse_expr(self, code: str) -> ast.AST:
        return ast.parse(code, mode='eval').body

    def test_binary_op_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr('1 + 2')) is False

    def test_bool_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('True')) is True

    def test_bytes_literal(self) -> None:
        assert _is_literal_only(self._parse_expr("b'data'")) is True

    def test_dict_literal(self) -> None:
        assert _is_literal_only(self._parse_expr("{'a': 1}")) is True

    def test_ellipsis_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('...')) is True

    def test_float_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('3.14')) is True

    def test_function_call_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr('compute_max()')) is False

    def test_int_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('42')) is True

    def test_list_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('[1, 2, 3]')) is True

    def test_name_reference_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr('some_var')) is False

    def test_nested_call_in_dict_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr("{'a': foo()}")) is False

    def test_nested_call_in_list_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr('[foo()]')) is False

    def test_nested_call_in_tuple_rejected(self) -> None:
        assert _is_literal_only(self._parse_expr('(1, foo())')) is False

    def test_none_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('None')) is True

    def test_set_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('{1, 2}')) is True

    def test_string_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('"hello"')) is True

    def test_tuple_literal(self) -> None:
        assert _is_literal_only(self._parse_expr('(1, 2)')) is True

    def test_unary_invert(self) -> None:
        assert _is_literal_only(self._parse_expr('~0')) is True

    def test_unary_minus(self) -> None:
        assert _is_literal_only(self._parse_expr('-1')) is True

    def test_unary_not(self) -> None:
        assert _is_literal_only(self._parse_expr('not True')) is True

    def test_unary_plus(self) -> None:
        assert _is_literal_only(self._parse_expr('+1')) is True


class TestRemoveHoistedLines:
    def test_removed_line_seam_joins_fragment_sequence_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0

        def count_join(fragments: Iterable[str]) -> str:
            nonlocal calls
            calls += 1
            return join_physical_fragments(fragments)

        monkeypatch.setattr('defsort._core._hoist.join_physical_fragments', count_join)

        assert remove_hoisted_lines('before\rREMOVE = 1\n\nafter', {2}) == 'before\n\rafter'
        assert calls == 1


def test_one_binding_traversal_per_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        ''.join(
            (f'def f{index}():\n    return {index}\n\nCONST_{index} = {index}\n\n')
            for index in range(4)
        )
        + 'def f4():\n    return 4\n'
    )
    expected_hoisted = ''.join(f'CONST_{index} = {index}\n\n' for index in range(4))
    expected_source = (
        expected_hoisted
        + ''.join(f'def f{index}():\n    return {index}\n\n' for index in range(4))
        + 'def f4():\n    return 4\n'
    )
    binding_traversals = 0
    reference_analyses = 0
    original_count_bindings = _count_constant_bindings
    original_collect_references = collect_name_references

    def count_bindings(tree: ast.AST, candidates: set[str]) -> Counter[str]:
        nonlocal binding_traversals
        binding_traversals += 1
        return original_count_bindings(tree, candidates)

    def collect_references(node: ast.AST, names: set[str], exclude: str) -> set[str]:
        nonlocal reference_analyses
        reference_analyses += 1
        return original_collect_references(node, names, exclude)

    monkeypatch.setattr('defsort._core._hoist._count_constant_bindings', count_bindings)
    monkeypatch.setattr('defsort._core._hoist.collect_name_references', collect_references)

    parsed = parse_definitions(source, hoist=True)
    rendered = reassemble_source(parsed.layout, parsed.blocks, parsed.definitions)

    assert binding_traversals == 1
    assert reference_analyses == 5
    assert parsed.hoistable == expected_hoisted
    assert rendered == expected_source

    reparsed = parse_definitions(rendered, hoist=True)
    assert reassemble_source(reparsed.layout, reparsed.blocks, reparsed.definitions) == rendered
