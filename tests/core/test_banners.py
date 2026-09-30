import ast
from collections.abc import Iterable, Iterator, Sequence

import pytest

from defsort._core import sort_file
from defsort._core._banners import (
    GapClassification,
    GapLocation,
    _extract_boundary_layout,
    apply_lossless_strip,
    classify_gap,
    classify_initial_gap,
    collect_banner_constraints,
    compile_banner_pattern,
    order_within_sections,
    remove_boundary_lines,
    statement_owned_lines_by_gap,
)
from defsort._source import join_physical_fragments
from defsort._types import (
    BannerConstraint,
    Definition,
    EngineOptions,
    HeldItem,
    MethodDefinition,
    SectionBoundary,
    SortPolicy,
)


class TestBannerDetection:
    @pytest.mark.parametrize('character', ['-', '=', '*', '#', '~'])
    def test_all_builtin_rule_characters(self, character: str) -> None:
        rule = f'# {character * 4}\n'
        assert _boundaries(rule)[0].raw == rule

    @pytest.mark.parametrize(
        'block',
        [
            '# =========\n# Services\n# =========\n',
            '# ---------\n# Services\n',
            '# Services\n# ---------\n',
            '# ---------\n',
        ],
    )
    def test_builtin_block_shapes(self, block: str) -> None:
        boundary = _boundaries(block)[0]
        assert boundary.raw == block
        assert boundary.text == block.rstrip('\n')

    def test_class_indent_scope(self) -> None:
        block = '    # --------\n    # Methods\n'
        boundary = _boundaries(block, scope_indent='    ')[0]
        assert boundary.raw == block
        assert boundary.text == '# --------\n# Methods'

    def test_crlf_normalizes_text_but_preserves_raw(self) -> None:
        block = '# --------\r\n# Services\r\n'
        boundary = _boundaries(block)[0]
        assert boundary.raw == block
        assert boundary.text == '# --------\n# Services'

    def test_custom_pattern_does_not_bypass_protected_block(self) -> None:
        assert (
            _boundaries(
                '# Services\n# fmt: off\n',
                pattern=r'# Services',
            )
            == ()
        )

    def test_custom_pattern_fullmatches_logical_line_including_hash(self) -> None:
        assert _boundaries('# Services\n', pattern=r'# Services')
        assert _boundaries('# Services\n', pattern=r'Services') == ()

    def test_custom_pattern_obeys_scope_indent(self) -> None:
        assert _boundaries(
            '    # Services\n',
            scope_indent='    ',
            pattern=r'# Services',
        )

    def test_deeper_indented_comment_is_not_in_scope(self) -> None:
        assert _boundaries('        # --------\n', scope_indent='    ') == ()

    @pytest.mark.parametrize(
        'protected',
        [
            '# noqa',
            '# type: ignore',
            '# pyright: ignore',
            '# pylint: disable=all',
            '# flake8: noqa',
            '# fmt: off',
            '# yapf: disable',
            '# pragma: no cover',
        ],
    )
    def test_directive_family_rejects_whole_block(self, protected: str) -> None:
        assert _boundaries(f'# --------\n{protected}\n') == ()

    def test_extended_directive_blocks_matching_custom_banner(self) -> None:
        result = classify_gap(
            '# ruff: noqa\n\n',
            location=GapLocation(1, ''),
            section=0,
            banner_settings=(
                'barriers',
                compile_banner_pattern(r'^# ruff: noqa$'),
            ),
        )

        assert result.boundaries == ()

    def test_inline_comment_is_not_a_banner(self) -> None:
        assert _boundaries('value = 1  # --------\n') == ()

    def test_requires_blank_separation_after(self) -> None:
        assert _boundaries('# --------\ndef value():\n    pass\n') == ()

    def test_requires_blank_separation_before(self) -> None:
        assert _boundaries('value = 1\n# --------\n\n') == ()

    def test_same_line_title_is_not_a_builtin_rule(self) -> None:
        assert _boundaries('# -------- Services --------\n') == ()

    @pytest.mark.parametrize(
        'protected',
        [
            '#!/usr/bin/env python',
            '# -*- coding: utf-8 -*-',
            '# coding=utf-8',
        ],
    )
    def test_shebang_and_encoding_reject_whole_block(self, protected: str) -> None:
        assert _boundaries(f'{protected}\n# --------\n') == ()

    @pytest.mark.parametrize('length', [1, 2, 3])
    def test_short_rule_is_not_a_banner(self, length: int) -> None:
        assert _boundaries(f'# {"-" * length}\n') == ()


class TestBannerHelpers:
    def test_collect_banner_constraints(self) -> None:
        alpha = _definition(0, 'alpha', section=0, line=1)
        bravo = _definition(1, 'bravo', section=1, line=5)
        boundary = SectionBoundary(
            section=1,
            start_line=3,
            end_line=3,
            raw='# ----\n',
            text='# ----',
        )

        constraints = collect_banner_constraints(
            [alpha, bravo],
            [bravo, alpha],
            [boundary],
        )

        assert len(constraints) == 1
        constraint = constraints[0]
        assert constraint.scope == 'definitions'
        assert constraint.class_name is None
        assert constraint.boundary is boundary
        assert [(item.name, item.direction) for item in constraint.held] == [
            ('alpha', 'after'),
            ('bravo', 'before'),
        ]

    def test_collect_constraints_has_bounded_clean_work(self) -> None:
        item_count = 32
        original = _CountingSequence(
            [
                _definition(index, f'item_{index}', section=index, line=index + 1)
                for index in range(item_count)
            ],
            visit_limit=item_count * 6,
        )
        boundaries = [
            SectionBoundary(section, section, section, '# ----\n', '# ----')
            for section in range(1, item_count)
        ]

        constraints = collect_banner_constraints(original, list(original), boundaries)

        assert constraints == []
        assert original.visits <= item_count * 6

    def test_collect_constraints_numbers_same_name_occurrences(self) -> None:
        first_alpha = _definition(0, 'alpha', section=0, line=1)
        bravo = _definition(1, 'bravo', section=1, line=5)
        second_alpha = _definition(2, 'alpha', section=2, line=9)
        boundary = SectionBoundary(1, 3, 3, '# ----\n', '# ----')

        constraint = collect_banner_constraints(
            [first_alpha, bravo, second_alpha],
            [second_alpha, bravo, first_alpha],
            [boundary],
        )[0]

        assert [(item.name, item.occurrence, item.direction) for item in constraint.held] == [
            ('alpha', 1, 'after'),
            ('alpha', 2, 'before'),
        ]

    def test_collect_constraints_preserves_exact_records_across_empty_section(self) -> None:
        alpha = _definition(0, 'alpha', section=0, line=1)
        first_echo = _definition(1, 'echo', section=1, line=5)
        bravo = _definition(2, 'bravo', section=1, line=7)
        second_echo = _definition(3, 'echo', section=3, line=13)
        delta = _definition(4, 'delta', section=3, line=15)
        first = SectionBoundary(1, 3, 3, '# ----\n', '# ----')
        second = SectionBoundary(2, 9, 9, '# ====\n', '# ====')
        third = SectionBoundary(3, 11, 11, '# ~~~~\n', '# ~~~~')

        constraints = collect_banner_constraints(
            [alpha, first_echo, bravo, second_echo, delta],
            [second_echo, bravo, alpha, delta, first_echo],
            [first, second, third],
        )

        assert constraints == [
            BannerConstraint(
                scope='definitions',
                class_name=None,
                boundary=first,
                held=(
                    HeldItem('alpha', 'function', 1, 1, 'after'),
                    HeldItem('echo', 'function', 2, 13, 'before'),
                ),
            ),
            BannerConstraint(
                scope='definitions',
                class_name=None,
                boundary=second,
                held=(
                    HeldItem('echo', 'function', 1, 5, 'after'),
                    HeldItem('echo', 'function', 2, 13, 'before'),
                ),
            ),
            BannerConstraint(
                scope='definitions',
                class_name=None,
                boundary=third,
                held=(
                    HeldItem('echo', 'function', 1, 5, 'after'),
                    HeldItem('echo', 'function', 2, 13, 'before'),
                ),
            ),
        ]

    def test_collect_method_constraints_include_class_metadata(self) -> None:
        alpha = _method(0, 'alpha', section=0, line=2)
        bravo = _method(1, 'bravo', section=1, line=6)
        boundary = SectionBoundary(1, 4, 4, '    # ----\n', '# ----')

        assert collect_banner_constraints(
            [alpha, bravo], [bravo, alpha], [boundary], class_name='Service'
        ) == [
            BannerConstraint(
                scope='methods',
                class_name='Service',
                boundary=boundary,
                held=(
                    HeldItem('alpha', 'method', 1, 2, 'after'),
                    HeldItem('bravo', 'method', 1, 6, 'before'),
                ),
            )
        ]

    def test_compile_banner_pattern(self) -> None:
        pattern = compile_banner_pattern(r'^# Section: .+$')
        assert pattern is not None
        assert pattern.fullmatch('# Section: Services') is not None

    def test_order_within_sections(self) -> None:
        items = [
            _definition(0, 'bravo', section=0),
            _definition(1, 'alpha', section=0),
            _definition(2, 'delta', section=1),
            _definition(3, 'charlie', section=1),
        ]

        ordered = order_within_sections(
            items,
            lambda section: sorted(section, key=lambda definition: definition.name),
        )

        assert [item.name for item in ordered] == ['alpha', 'bravo', 'charlie', 'delta']

    def test_remove_boundary_lines_counts_physical_lines(self) -> None:
        src = '"""Doc\fpage."""\nA = 1\n# ----------\nB = 2\n'
        boundary = SectionBoundary(
            section=1, start_line=3, end_line=3, raw='# ----------\n', text='----------'
        )
        assert remove_boundary_lines(src, [boundary]) == '"""Doc\fpage."""\nA = 1\nB = 2\n'

    def test_strip_consumers_join_fragment_sequence_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0

        def count_join(fragments: Iterable[str]) -> str:
            nonlocal calls
            calls += 1
            return join_physical_fragments(fragments)

        monkeypatch.setattr('defsort._core._banners.join_physical_fragments', count_join)
        source = 'before\r# ----------\n\nafter'
        boundary = SectionBoundary(1, 2, 2, '# ----------\n', '# ----------')

        assert apply_lossless_strip(source, [], [boundary]) == 'before\n\rafter'
        assert remove_boundary_lines(source, [boundary]) == 'before\n\rafter'
        assert calls == 2


class TestBannerLayout:
    def test_adjacent_boundaries_preserve_empty_section(self) -> None:
        content = '# --------\n\n# ========\n'
        before, layout, after = _extract_boundary_layout(
            content,
            location=GapLocation(1, ''),
            first_section=4,
            pattern=None,
        )
        assert before == ''
        assert after == ''
        assert layout == (
            SectionBoundary(4, 1, 1, '# --------\n', '# --------'),
            '\n',
            SectionBoundary(5, 3, 3, '# ========\n', '# ========'),
        )

    def test_leading_form_feed_banner_is_recognized(self) -> None:
        content = '\f# --------\n'
        before, layout, after = _extract_boundary_layout(
            content,
            location=GapLocation(5, ''),
            first_section=1,
            pattern=None,
        )
        assert before == ''
        assert after == ''
        assert len(layout) == 1
        boundary = layout[0]
        assert isinstance(boundary, SectionBoundary)
        assert boundary.raw == '\f# --------\n'

    def test_leading_form_feed_indented_banner_is_recognized(self) -> None:
        content = '\f    # --------\n'
        _before, layout, _after = _extract_boundary_layout(
            content,
            location=GapLocation(7, '    '),
            first_section=1,
            pattern=None,
        )
        assert len(layout) == 1
        boundary = layout[0]
        assert isinstance(boundary, SectionBoundary)
        assert boundary.raw == '\f    # --------\n'

    def test_no_banner_returns_content_as_before(self) -> None:
        content = 'ordinary text\n'
        assert _extract_boundary_layout(
            content,
            location=GapLocation(1, ''),
            first_section=1,
            pattern=None,
        ) == (content, (), '')

    def test_preserves_outer_text_and_middle_layout(self) -> None:
        content = '\nlead\n\n# --------\n\nbetween\n\n# ========\n\ntail\n'
        before, layout, after = _extract_boundary_layout(
            content,
            location=GapLocation(20, ''),
            first_section=3,
            pattern=None,
        )
        boundaries = [part for part in layout if isinstance(part, SectionBoundary)]
        reconstructed = (
            before
            + ''.join(part.raw if isinstance(part, SectionBoundary) else part for part in layout)
            + after
        )

        assert before == '\nlead\n\n'
        assert after == '\ntail\n'
        assert [boundary.section for boundary in boundaries] == [3, 4]
        assert [(boundary.start_line, boundary.end_line) for boundary in boundaries] == [
            (23, 23),
            (27, 27),
        ]
        assert reconstructed == content


class TestBannerStatementOwnership:
    def test_forced_strip_preserves_multiline_string(self) -> None:
        src = (
            'def beta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'DOC = """\n'
            '\n'
            '# --------\n'
            '\n'
            'text\n'
            '"""\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', banners='strip'),
            force=True,
        )
        result = sort_file(src, options=options)
        assert 'DOC = """\n\n# --------\n\ntext\n"""' in result.new_source
        second = sort_file(result.new_source, options=options)
        assert second.new_source == result.new_source

    def test_forced_strip_still_removes_real_banner(self) -> None:
        src = 'def beta():\n    return 1\n\n\n# ----------\n\n\ndef alpha():\n    return 2\n'
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', banners='strip'),
            force=True,
        )
        result = sort_file(src, options=options)
        assert '# ----------' not in result.new_source

    def test_method_gap_string_rule_line_retained(self) -> None:
        src = (
            'class Alpha:\n'
            '    def b(self):\n'
            '        return 1\n'
            '\n'
            '    DOC = """\n'
            '\n'
            '    # --------\n'
            '\n'
            '"""\n'
            '\n'
            '    def a(self):\n'
            '        return 2\n'
        )
        options = EngineOptions(
            policy=SortPolicy(method_order='alphabetical', banners='strip'),
            force=True,
        )
        result = sort_file(src, options=options)
        assert '# --------' in result.new_source

    def test_ordered_gaps_preserve_multiline_statement_ownership(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            'DOC = """first\n'
            '# --------\n'
            'last"""\n'
            '\n'
            'def bravo():\n'
            '    pass\n'
            '\n'
            'VALUE = (\n'
            '    1\n'
            ')\n'
            '\n'
            'def charlie():\n'
            '    pass\n'
        )
        body = ast.parse(source).body
        gaps = ((3, 7), (10, 14))
        result = statement_owned_lines_by_gap(
            body,
            gaps,
            movable_types=(ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
        )

        assert result == (frozenset({1, 2, 3}), frozenset({1, 2, 3}))


class TestClassifyInitialGap:
    def test_adjacent_comment_becomes_leading(self) -> None:
        result = classify_initial_gap(
            '# about zeta\n',
            content_start_line=1,
            scope_indent='',
            banner_settings=('ignore', None),
        )

        assert (result.trailing, result.separator, result.leading) == (
            '',
            '',
            '# about zeta\n',
        )

    def test_banner_then_adjacent_directive_precedes_hoists(self) -> None:
        content = '# --------\n\n# type: ignore\n'

        result = classify_initial_gap(
            content,
            content_start_line=1,
            scope_indent='',
            banner_settings=('barriers', None),
        )

        assert result.trailing == content
        assert result.separator == ''
        assert result.leading == ''
        assert len(result.boundaries) == 1
        assert result.next_section == 1

    def test_blank_separated_comment_stays_fixed(self) -> None:
        result = classify_initial_gap(
            '# file note\n\n',
            content_start_line=1,
            scope_indent='',
            banner_settings=('ignore', None),
        )

        assert (result.trailing, result.separator, result.leading) == (
            '# file note\n',
            '\n',
            '',
        )

    def test_directive_before_banner_leaves_banner_as_separator(self) -> None:
        result = classify_initial_gap(
            '# type: ignore\n\n# --------\n\n# about zeta\n',
            content_start_line=1,
            scope_indent='',
            banner_settings=('barriers', None),
        )

        assert result.trailing == '# type: ignore\n'
        assert result.separator == '\n# --------\n\n'
        assert result.leading == '# about zeta\n'

    def test_separator_directive_preserves_later_leading(self) -> None:
        content = '# --------\n\n# type: ignore\n\n# about zeta\n'

        result = classify_initial_gap(
            content,
            content_start_line=1,
            scope_indent='',
            banner_settings=('barriers', None),
        )

        assert result.trailing == '# --------\n\n# type: ignore\n'
        assert result.separator == '\n'
        assert result.leading == '# about zeta\n'
        assert len(result.boundaries) == 1
        assert result.next_section == 1

    def test_strip_removes_only_boundary_bytes(self) -> None:
        result = classify_initial_gap(
            '# --------\n\n\n',
            content_start_line=1,
            scope_indent='',
            banner_settings=('strip', None),
        )

        assert result.trailing + result.separator + result.leading == '\n\n'
        assert result.leading == ''
        assert len(result.boundaries) == 1


class TestGapClassification:
    def test_adjacent_boundaries_preserve_exact_mixed_ending_layout(self) -> None:
        first = SectionBoundary(5, 11, 11, '# --------\r\n', '# --------')
        second = SectionBoundary(6, 13, 13, '# ========\n', '# ========')

        result = classify_gap(
            '\n# --------\r\n\t\r# ========\n\n',
            location=GapLocation(10, ''),
            section=4,
            banner_settings=('barriers', None),
        )

        assert result == GapClassification(
            trailing='',
            separator='\n# --------\r\n\t\r# ========\n\n',
            leading='',
            boundaries=(first, second),
            boundary_layout=(first, '\t\r', second, '\n'),
            next_section=6,
            fixed_ordinary='\t\r\n',
        )

    def test_barriers_expose_fixed_ordinary_text_for_scope_safety(self) -> None:
        boundary = SectionBoundary(4, 21, 21, '# --------\n', '# --------')

        result = classify_gap(
            '\n# --------\n\nvalue = 1\r\n\n# next\r',
            location=GapLocation(20, ''),
            section=3,
            banner_settings=('barriers', None),
        )

        assert result == GapClassification(
            trailing='',
            separator='\n# --------\n\nvalue = 1\r\n\n',
            leading='# next\r',
            boundaries=(boundary,),
            boundary_layout=(boundary, '\nvalue = 1\r\n\n'),
            next_section=4,
            fixed_ordinary='\nvalue = 1\r\n\n# next\r',
        )

    def test_custom_pattern_strip_honors_scope_and_preserves_comments(self) -> None:
        boundary = SectionBoundary(3, 42, 42, '    # REGION\n', '# REGION')

        result = classify_gap(
            '    # prior trailing\r\n \t\r\n    # REGION\n\t\r\n    # next leading\r',
            location=GapLocation(40, '    '),
            section=2,
            banner_settings=(
                'strip',
                compile_banner_pattern(r'^# REGION$'),
            ),
        )

        assert result == GapClassification(
            trailing='    # prior trailing\r\n',
            separator=' \t\r\n\t\r\n',
            leading='    # next leading\r',
            boundaries=(boundary,),
            boundary_layout=(boundary,),
            next_section=3,
            fixed_ordinary='',
        )

    def test_ignore_treats_banner_text_as_ordinary_ownership(self) -> None:
        result = classify_gap(
            '# prior\r\n \t\r\n# --------\n# next\n',
            location=GapLocation(40, ''),
            section=2,
            banner_settings=('ignore', None),
        )

        assert result == GapClassification(
            trailing='# prior\r\n',
            separator=' \t\r\n',
            leading='# --------\n# next\n',
            boundaries=(),
            boundary_layout=(),
            next_section=2,
            fixed_ordinary='',
        )

    def test_no_boundary_uses_ordinary_gap_ownership(self) -> None:
        result = classify_gap(
            '\n    # ordinary\r\n',
            location=GapLocation(40, '    '),
            section=2,
            banner_settings=('barriers', None),
        )

        assert result == GapClassification(
            trailing='',
            separator='\n',
            leading='    # ordinary\r\n',
            boundaries=(),
            boundary_layout=(),
            next_section=2,
            fixed_ordinary='',
        )


class _CountingSequence[T](list[T]):
    def __init__(self, items: Sequence[T], *, visit_limit: int) -> None:
        super().__init__(items)
        self._visit_limit = visit_limit
        self.visits = 0

    def __iter__(self) -> Iterator[T]:
        for item in super().__iter__():
            self._record_visit()
            yield item

    def _record_visit(self) -> None:
        self.visits += 1
        assert self.visits <= self._visit_limit


def _boundaries(
    content: str,
    *,
    content_start_line: int = 1,
    scope_indent: str = '',
    pattern: str | None = None,
) -> tuple[SectionBoundary, ...]:
    _, layout, _ = _extract_boundary_layout(
        content,
        location=GapLocation(content_start_line, scope_indent),
        first_section=1,
        pattern=compile_banner_pattern(pattern),
    )
    return tuple(part for part in layout if isinstance(part, SectionBoundary))


def _definition(index: int, name: str, *, section: int, line: int = 1) -> Definition:
    return Definition(
        index=index,
        name=name,
        start_line=line,
        end_line=line,
        kind='function',
        source=f'def {name}(): pass',
        section=section,
    )


def _method(index: int, name: str, *, section: int, line: int) -> MethodDefinition:
    return MethodDefinition(
        index=index,
        name=name,
        start_line=line,
        end_line=line,
        source=f'    def {name}(self): pass',
        section=section,
    )
