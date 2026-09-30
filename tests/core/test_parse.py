import ast
from collections import Counter
from typing import TYPE_CHECKING, cast

import pytest

import defsort._core._parse as parse_module
from defsort._core._api import sort_file
from defsort._core._banners import collect_banner_constraints
from defsort._core._layout import render_layout
from defsort._core._parse import parse_definitions, parse_postamble_names
from defsort._types import (
    BannerMode,
    EngineOptions,
    SectionBoundary,
    SortPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class TestInitialGapOwnership:
    def test_adjacent_first_comment_is_owned(self) -> None:
        source = '# about zeta\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].leading == '# about zeta\n'
        assert parsed.definitions[0].start_line == 2
        _assert_parse_round_trip(source)

    def test_blank_separated_head_comment_stays_fixed(self) -> None:
        source = '# file note\n\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].leading == ''
        assert parsed.layout.prefix == '# file note\n\n'
        _assert_parse_round_trip(source)

    def test_decorated_first_definition_owns_head_comment(self) -> None:
        source = '# about zeta\n@decorator\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].leading == '# about zeta\n'
        assert parsed.definitions[0].source.startswith('@decorator\n')
        assert parsed.definitions[0].start_line == 2
        _assert_parse_round_trip(source)

    def test_decorator_internal_comment_stays_in_source(self) -> None:
        source = '# about zeta\n@decorator\n# decorator note\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)
        definition = parsed.definitions[0]

        assert definition.leading == '# about zeta\n'
        assert definition.source == ('@decorator\n# decorator note\ndef zeta():\n    pass\n')
        assert definition.source.startswith('@decorator\n')
        assert definition.start_line == 2
        _assert_parse_round_trip(source)

    def test_definition_on_line_one_has_empty_prefix(self) -> None:
        source = 'def alpha():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.layout.prefix == ''
        assert parsed.definitions[0].leading == ''
        _assert_parse_round_trip(source)

    @pytest.mark.parametrize(
        'structural',
        [
            '"""Module documentation."""\n',
            'pass\n',
            'if READY:\n    setup()\n',
            'from typing import TypeAlias\nAlias: TypeAlias = int\n',
        ],
        ids=['docstring', 'pass', 'if', 'type-alias'],
    )
    def test_direct_module_statements_stay_structural(self, structural: str) -> None:
        source = structural + '# about alpha\ndef alpha():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.layout.prefix == structural
        assert parsed.definitions[0].leading == '# about alpha\n'
        _assert_parse_round_trip(source)

    def test_first_comment_moves_with_definition(self) -> None:
        source = (
            '# about zeta\n'
            'def zeta():\n'
            '    return 1\n'
            '\n\n'
            '# about alpha\n'
            'def alpha():\n'
            '    return 2\n'
        )
        expected = (
            '# about alpha\n'
            'def alpha():\n'
            '    return 2\n'
            '\n\n'
            '# about zeta\n'
            'def zeta():\n'
            '    return 1\n'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == expected

    def test_form_feed_inside_head_comment_is_not_a_line(self) -> None:
        source = '# about\f zeta\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].leading == '# about\f zeta\n'
        assert parsed.definitions[0].start_line == 2
        _assert_parse_round_trip(source)

    def test_initial_form_feed_stays_positional(self) -> None:
        source = '# file note\n\n\f\ndef alpha():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].leading == ''
        assert parsed.layout.prefix == '# file note\n\n\f\n'
        _assert_parse_round_trip(source)

    @pytest.mark.parametrize(
        'source',
        [
            '# about zeta\ndef zeta():\n    pass\n',
            '# about zeta\r\ndef zeta():\r\n    pass\r\n',
            '# about zeta\rdef zeta():\r    pass\r',
            '# about zeta\r\ndef zeta():\r    pass\n',
        ],
        ids=['lf', 'crlf', 'lone-cr', 'mixed'],
    )
    def test_initial_gap_physical_endings(self, source: str) -> None:
        parsed = parse_definitions(source)
        first = render_layout(
            parsed.layout,
            parsed.blocks,
            [definition.index for definition in parsed.definitions],
        )
        repeated = parse_definitions(first)
        second = render_layout(
            repeated.layout,
            repeated.blocks,
            [definition.index for definition in repeated.definitions],
        )

        assert first == source
        assert second == first

    def test_initial_overload_group_owns_head_comment(self) -> None:
        source = (
            'from typing import overload\n'
            '\n\n'
            '# about zeta\n'
            '@overload\n'
            'def zeta(value: int) -> int: ...\n'
            '\n\n'
            '@overload\n'
            'def zeta(value: str) -> str: ...\n'
            '\n\n'
            'def zeta(value: int | str) -> int | str:\n'
            '    return value\n'
            '\n\n'
            'def alpha():\n'
            '    return 1\n'
        )
        expected = (
            'from typing import overload\n'
            '\n\n'
            'def alpha():\n'
            '    return 1\n'
            '\n\n'
            '# about zeta\n'
            '@overload\n'
            'def zeta(value: int) -> int: ...\n'
            '\n\n'
            '@overload\n'
            'def zeta(value: str) -> str: ...\n'
            '\n\n'
            'def zeta(value: int | str) -> int | str:\n'
            '    return value\n'
        )

        parsed = parse_definitions(source)
        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert parsed.blocks[0].startswith('# about zeta\n@overload\n')
        assert result.new_source == expected

    def test_protected_headers_stay_structural(self) -> None:
        source = '#!/usr/bin/env python\n# coding: utf-8\n# about zeta\ndef zeta():\n    pass\n'

        parsed = parse_definitions(source)

        assert parsed.layout.prefix == '#!/usr/bin/env python\n# coding: utf-8\n'
        assert parsed.definitions[0].leading == '# about zeta\n'
        assert parsed.definitions[0].start_line == 4
        _assert_parse_round_trip(source)

    def test_strip_head_banner_preserves_whitespace(self) -> None:
        source = '# --------\n\n\ndef alpha():\n    pass\n'
        expected = '\n\ndef alpha():\n    pass\n'

        parsed = parse_definitions(source, banners='strip')
        rendered = render_layout(
            parsed.layout,
            parsed.blocks,
            [definition.index for definition in parsed.definitions],
        )

        assert rendered == expected

    def test_zero_and_one_definition_round_trip(self) -> None:
        no_definitions = 'import os\nVALUE = 1\n'
        one_definition = '# about alpha\ndef alpha():\n    pass\n'

        _assert_parse_round_trip(no_definitions)
        _assert_parse_round_trip(one_definition)


class TestParseDefinitions:
    def test_banner_boundaries_lifted_and_sections_assigned(self) -> None:
        src = (
            'def zulu():\n'
            '    pass\n'
            '\n'
            '# --------\n'
            '# Services\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '\n'
            '# ========\n'
            '\n'
            'def bravo():\n'
            '    pass\n'
        )

        result = parse_definitions(src, banners='barriers')

        assert result.boundaries == [
            SectionBoundary(1, 4, 5, '# --------\n# Services\n', '# --------\n# Services'),
            SectionBoundary(2, 10, 10, '# ========\n', '# ========'),
        ]
        assert [definition.section for definition in result.definitions] == [0, 1, 2]
        for definition in result.definitions:
            attachment = definition.leading + definition.trailing
            assert '# --------' not in attachment
            assert '# Services' not in attachment
            assert '# ========' not in attachment

    def test_banner_layout_preserves_adjacent_boundaries_and_empty_section(self) -> None:
        src = 'def alpha():\n    pass\n\n\n# --------\n\n# ========\n\n\ndef bravo():\n    pass\n'

        result = parse_definitions(src, banners='barriers')

        assert result.boundary_layout == [
            SectionBoundary(1, 5, 5, '# --------\n', '# --------'),
            '\n',
            SectionBoundary(2, 7, 7, '# ========\n', '# ========'),
            '\n\n',
        ]
        assert [definition.section for definition in result.definitions] == [0, 2]

    def test_banner_layout_preserves_ordinary_block_between_boundaries(self) -> None:
        src = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n'
            '# ordinary block\n'
            '\n'
            '# ========\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
        )

        result = parse_definitions(src, banners='barriers')

        assert result.boundary_layout == [
            SectionBoundary(1, 5, 5, '# --------\n', '# --------'),
            '\n# ordinary block\n\n',
            SectionBoundary(2, 9, 9, '# ========\n', '# ========'),
            '\n\n',
        ]
        assert [definition.section for definition in result.definitions] == [0, 2]
        assert result.definitions[0].trailing == ''
        assert result.definitions[1].leading == ''

    def test_bare_form_feed_before_postamble_stays_positional(self) -> None:
        src = (
            'def zulu():\n    pass\n\ndef alpha():\n    pass\n\n\f\n'
            'if __name__ == "__main__":\n    alpha()\n'
        )
        parsed = parse_definitions(src)
        assert parsed.postamble == '\n\f\nif __name__ == "__main__":\n    alpha()\n'
        _assert_parse_round_trip(src)

    def test_bare_form_feed_line_is_positional_separator(self) -> None:
        src = 'def zulu():\n    pass\n\n\f\ndef alpha():\n    pass\n'
        parsed = parse_definitions(src)
        assert [d.name for d in parsed.definitions] == ['zulu', 'alpha']
        assert parsed.postamble == ''
        assert parsed.layout.separators == ('\n\n\f\n',)
        _assert_parse_round_trip(src)

    def test_barrier_layout_round_trips_adjacent_empty_section(self) -> None:
        source = (
            'def alpha():\r\n'
            '    pass\r\n'
            ' \t\r\n'
            '# --------\r\n'
            '\r\n'
            '# ========\r\n'
            '\t\r\n'
            'def bravo():\r\n'
            '    pass'
        )

        _assert_parse_round_trip(source, banners='barriers')

    def test_basic_two_functions(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        parsed = parse_definitions(src)
        assert len(parsed.definitions) == 2
        assert parsed.definitions[0].name == 'alpha'
        assert parsed.definitions[1].name == 'bravo'
        assert parsed.postamble == ''
        assert all(not definition.had_interstitial_code for definition in parsed.definitions)
        assert parsed.hoistable == ''

    def test_called_sibling_body_refs_feed_eager_and_dependency_metadata(self) -> None:
        source = (
            'def zeta_compute():\n'
            '    return 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )

        parsed = parse_definitions(source)
        alpha = parsed.definitions[1]

        assert alpha.dependencies == {'zeta_compute'}
        assert {'helper', 'zeta_compute'} <= alpha.eager_refs

    def test_called_sibling_constant_read_is_eager_but_not_movable(self) -> None:
        source = (
            'VALUE = 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        return VALUE\n'
            '    value = helper()\n'
        )

        alpha = parse_definitions(source).definitions[0]

        assert alpha.dependencies == set()
        assert 'VALUE' in alpha.eager_refs

    def test_class_definition(self) -> None:
        src = 'class Widget:\n    pass\n'
        definitions = parse_definitions(src).definitions
        assert len(definitions) == 1
        assert definitions[0].name == 'Widget'
        assert definitions[0].kind == 'class'

    def test_decorated_definition(self) -> None:
        src = '@decorator\ndef alpha():\n    pass\n'
        definitions = parse_definitions(src).definitions
        assert definitions[0].start_line == 1
        assert '@decorator' in definitions[0].source

    def test_dependencies_detected(self) -> None:
        src = 'def alpha(): pass\n\n\ndef bravo():\n    return alpha()\n'
        definitions = parse_definitions(src).definitions
        assert 'alpha' in definitions[1].dependencies

    def test_docstring_form_feed_does_not_shift_definition_spans(self) -> None:
        src = '"""Doc\fpage."""\n\n\ndef zulu():\n    pass\n'
        parsed = parse_definitions(src)
        assert parsed.layout.prefix == '"""Doc\fpage."""\n\n\n'
        assert parsed.definitions[0].source == 'def zulu():\n    pass\n'
        _assert_parse_round_trip(src)

    def test_docstring_form_feed_keeps_postamble_boundary(self) -> None:
        src = (
            'def alpha():\n    """A\fB."""\n\n\ndef beta():\n    pass\n\n\n'
            'if __name__ == "__main__":\n    beta()\n'
        )
        parsed = parse_definitions(src)
        assert parsed.postamble == '\n\nif __name__ == "__main__":\n    beta()\n'
        _assert_parse_round_trip(src)

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                "events = []\n\n\ndef alpha(value=(events.append('zeta'), 0)[1]):\n    pass\n",
                True,
            ),
            ('def alpha(*, value=None):\n    pass\n', True),
            ('@decorate\ndef alpha():\n    pass\n', True),
            ('async def alpha(value=0):\n    pass\n', True),
            ('def alpha():\n    pass\n', False),
            ('@decorate\nclass Alpha:\n    pass\n', True),
            ('class Alpha:\n    value = external()\n', True),
            ('class Alpha:\n    value = 1\n', False),
        ],
        ids=[
            'independent-default',
            'keyword-only-none-default',
            'decorator',
            'async-default',
            'plain-function',
            'decorated-class',
            'class-eager-read',
            'plain-class-body',
        ],
    )
    def test_eager_evaluation_metadata(self, source: str, expected: bool) -> None:
        definition = parse_definitions(source).definitions[0]

        assert definition.has_eager_evaluation is expected

    def test_eager_refs_attached(self) -> None:
        source = 'def zulu():\n    return 1\n\n\ndef alpha(value=zulu()):\n    return value\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].eager_refs == set()
        assert parsed.definitions[1].eager_refs == {'zulu'}

    def test_had_interstitial_code(self) -> None:
        src = 'def alpha():\n    pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
        definitions = parse_definitions(src).definitions

        assert definitions[0].had_interstitial_code is True

    def test_head_banner_barrier_metadata(self) -> None:
        src = '# --------\n\n\ndef alpha():\n    pass\n\n\n# ========\n'

        result = parse_definitions(src, banners='barriers')

        assert len(result.boundaries) == 1
        assert len(result.boundary_layout) == 2
        assert result.definitions[0].section == 1
        assert result.postamble == '\n\n# ========\n'
        assert (
            collect_banner_constraints(
                result.definitions,
                result.definitions,
                result.boundaries,
            )
            == []
        )

    def test_head_banner_round_trips_without_hoisting(self) -> None:
        source = '# --------\n\n\ndef alpha():\n    pass\n'

        _assert_parse_round_trip(source, banners='barriers')

    def test_hoisting(self) -> None:
        src = 'def alpha():\n    pass\n\nMAX_SIZE = 100\n\ndef bravo():\n    pass\n'
        parsed = parse_definitions(src, hoist=True)
        assert 'MAX_SIZE = 100' in parsed.hoistable

    def test_hoisting_disabled_by_default(self) -> None:
        src = 'def alpha():\n    pass\n\nMAX_SIZE = 100\n\ndef bravo():\n    pass\n'
        parsed = parse_definitions(src)
        assert parsed.definitions[0].had_interstitial_code is True
        assert parsed.hoistable == ''

    @pytest.mark.parametrize(
        'source',
        [
            '',
            'import os\nvalue = 1',
            'def alpha():\n    pass',
            'import os\n\ndef alpha():\n    pass\n\nif READY:\n    run()\n',
            'def alpha():\n    pass\ndef bravo():\n    pass\n',
            'def alpha():\n    pass\n\ndef bravo():\n    pass\n',
            'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n',
            'def alpha():\n    pass\n\n\n\n\ndef bravo():\n    pass\n',
            'def alpha():\n    pass\n \t\n\t \ndef bravo():\n    pass\n',
            (
                'def alpha():\n'
                '    pass\n'
                '# alpha trailing\n'
                '\n'
                '# bravo leading\n'
                'def bravo():\n'
                '    pass\n'
            ),
            ('def alpha():\n    pass\n# first block\n\n# second block\ndef bravo():\n    pass'),
            'def alpha():\r\n    pass\r\n\r\ndef bravo():\r\n    pass\r\n',
            'def alpha():\r    pass\r\rdef bravo():\r    pass',
            'def alpha():\r\n    pass\r\n\ndef bravo():\r    pass\n',
        ],
        ids=[
            'empty',
            'no-definitions',
            'one-definition-no-final-newline',
            'preamble-and-postamble',
            'zero-blank-lines',
            'one-blank-line',
            'two-blank-lines',
            'four-blank-lines',
            'whitespace-filled-blank-lines',
            'leading-and-trailing-comments',
            'adjacent-comment-blocks',
            'crlf',
            'lone-cr-no-final-newline',
            'mixed-endings',
        ],
    )
    def test_layout_round_trips_exact_source(self, source: str) -> None:
        _assert_parse_round_trip(source)

    def test_no_definitions(self) -> None:
        src = 'import os\nx = 1\n'
        assert parse_definitions(src).definitions == []

    def test_parsing_never_reorders_methods(self) -> None:
        source = 'class C:\n    def bravo(self): pass\n    def alpha(self): pass\n'

        parsed = parse_definitions(source)

        assert parsed.definitions[0].source.index('bravo') < (
            parsed.definitions[0].source.index('alpha')
        )

    def test_postamble_extraction(self) -> None:
        src = 'def alpha():\n    pass\n\nif __name__ == "__main__":\n    pass\n'
        parsed = parse_definitions(src)
        assert len(parsed.definitions) == 1
        assert '__name__' in parsed.postamble

    def test_scope_state_scanned_once_for_large_module(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ownership_helper = cast(
            'Callable[..., object]',
            parse_module.statement_owned_lines_by_gap,
        )
        bound_name_helper = parse_module.get_bound_names
        ownership_calls = 0
        binding_calls = 0

        def track_ownership(*args: object, **kwargs: object) -> object:
            nonlocal ownership_calls
            ownership_calls += 1
            return ownership_helper(*args, **kwargs)

        def track_bound_names(node: ast.AST) -> list[str]:
            nonlocal binding_calls
            binding_calls += 1
            return bound_name_helper(node)

        monkeypatch.setattr(parse_module, 'statement_owned_lines_by_gap', track_ownership)
        monkeypatch.setattr(parse_module, 'get_bound_names', track_bound_names)
        source = 'PREAMBLE = 1\n\n' + ''.join(
            f'def f{i}():\n    return {i}\n\n' for i in range(200)
        )

        parsed = parse_definitions(source)

        assert len(parsed.definitions) == 200
        assert binding_calls == 1
        assert ownership_calls == 1

    def test_structural_prefix_preserved(self) -> None:
        src = 'import os\n\n\ndef alpha(): pass\n'
        parsed = parse_definitions(src)
        assert parsed.layout.prefix == 'import os\n\n\n'
        assert len(parsed.definitions) == 1

    def test_syntax_error_returns_source(self) -> None:
        src = 'def alpha(\n'
        parsed = parse_definitions(src)
        assert parsed.definitions == []
        assert render_layout(parsed.layout, parsed.blocks, []) == src

    def test_trailing_content_associated(self) -> None:
        src = 'def alpha():\n    pass\n\n# comment\n\ndef bravo():\n    pass\n'
        definitions = parse_definitions(src).definitions
        assert '# comment' in definitions[0].trailing


class TestParsePostambleNames:
    def test_empty_postamble(self) -> None:
        assert parse_postamble_names('') == set()
        assert parse_postamble_names('   \n  ') == set()

    def test_with_assignments(self) -> None:
        postamble = 'x = 1\ny = 2\n'
        names = parse_postamble_names(postamble)
        assert 'x' in names
        assert 'y' in names

    def test_with_function(self) -> None:
        postamble = 'def helper(): pass\n'
        names = parse_postamble_names(postamble)
        assert 'helper' in names


class TestPostambleHasDynamicAll:
    @pytest.mark.parametrize(
        'postamble',
        [
            '__all__ = [name for name in globals()]\n',
            "__all__ = ('alpha', variable)\n",
            "__all__ = alias = ['alpha']\n",
            "__all__ = ['alpha']\n__all__ = ['zeta']\n",
            "__all__ += ['zeta']\n",
            'del __all__\n',
            "__all__.append('zeta')\n",
            "__all__[0] = 'zeta'\n",
            'del __all__[:]\n',
            "if enabled:\n    __all__ = ['alpha']\n",
            "try:\n    __all__ = ['alpha']\nexcept Exception:\n    pass\n",
            '(__all__[0], other) = values\n',
            'del (__all__[0], other)\n',
        ],
        ids=[
            'comprehension',
            'non-string-item',
            'multiple-targets',
            'repeated-assignment',
            'augmented-assignment',
            'delete',
            'method-call',
            'subscript-assignment',
            'subscript-delete',
            'conditional-assignment',
            'try-assignment',
            'destructured-subscript-assignment',
            'destructured-subscript-delete',
        ],
    )
    def test_nonliteral_or_mutating_construction_is_dynamic(self, postamble: str) -> None:
        assert parse_module.postamble_has_dynamic_all(postamble) is True

    @pytest.mark.parametrize(
        'postamble',
        [
            "__all__ = ['alpha', 'zeta']\n",
            "__all__ = ('alpha', 'zeta')\n",
            "__all__: list[str] = ['alpha', 'zeta']\n",
            "__all__: tuple[str, ...] = ('alpha', 'zeta')\n",
        ],
        ids=['list', 'tuple', 'annotated-list', 'annotated-tuple'],
    )
    def test_static_string_sequence_is_not_dynamic(self, postamble: str) -> None:
        assert parse_module.postamble_has_dynamic_all(postamble) is False


class TestProtectedModuleHeader:
    @pytest.mark.parametrize(
        'source',
        [
            'def alpha():\n    # encoding: utf-8 assumed\n    return 1\n',
            'async def alpha():\n    # encoding: utf-8 assumed\n    return 1\n',
            'class Alpha:\n    # encoding: utf-8 assumed\n    value = 1\n',
            '@decorator(\n    # encoding: utf-8 assumed\n)\ndef alpha():\n    return 1\n',
        ],
        ids=['function', 'async-function', 'class', 'decorated-function'],
    )
    def test_body_comment_is_not_claimed_as_module_header(self, source: str) -> None:
        parsed = parse_definitions(source)

        assert parsed.layout.prefix == ''
        assert parsed.definitions[0].source.count('# encoding: utf-8 assumed') == 1
        _assert_parse_round_trip(source)

    @pytest.mark.parametrize(
        'header',
        [
            '#!/usr/bin/env python\n',
            '# coding: utf-8\n',
            '# module note\n# coding: utf-8\n',
        ],
        ids=['line-1-shebang', 'line-1-encoding', 'line-2-encoding'],
    )
    def test_real_header_before_decorated_definition_stays_protected(self, header: str) -> None:
        source = header + '@decorator\ndef alpha():\n    return 1\n'

        parsed = parse_definitions(source)

        assert parsed.layout.prefix == header
        assert parsed.definitions[0].source.startswith('@decorator\n')
        _assert_parse_round_trip(source)


class TestTrailingOwnershipGuard:
    def test_original_order_keeps_inter_item_owner_and_eof_state(self) -> None:
        source = 'def alpha():\n    pass\n# note for alpha\n\ndef zulu():\n    pass'
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)
        reparsed = parse_definitions(result.new_source)
        alpha = next(
            definition for definition in reparsed.definitions if definition.name == 'alpha'
        )

        assert result.new_source == source
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert not result.new_source.endswith(('\n', '\r'))
        assert alpha.trailing == '# note for alpha\n'

    def test_owned_comment_moving_before_postamble_uses_destination_suffix(self) -> None:
        source = (
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '\n'
            "if __name__ == '__main__':\n"
            '    alpha()\n'
        )
        expected = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            "if __name__ == '__main__':\n"
            '    alpha()\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        original = parse_definitions(source)
        result = sort_file(source, options=options)
        reparsed = parse_definitions(result.new_source)
        zulu = next(definition for definition in reparsed.definitions if definition.name == 'zulu')

        assert original.layout.suffix == "\n\nif __name__ == '__main__':\n    alpha()\n"
        assert result.new_source == expected
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert result.new_source.endswith('    alpha()\n')
        assert reparsed.postamble == (
            "# note for zulu\n\nif __name__ == '__main__':\n    alpha()\n"
        )
        assert zulu.trailing == ''

    def test_owned_comment_moving_to_true_eof_uses_destination_suffix(self) -> None:
        source = 'def zulu():\n    pass\n# note for zulu\n\ndef alpha():\n    pass'
        expected = 'def alpha():\n    pass\n\ndef zulu():\n    pass\n# note for zulu'
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        original = parse_definitions(source)
        result = sort_file(source, options=options)
        reparsed = parse_definitions(result.new_source)
        zulu = next(definition for definition in reparsed.definitions if definition.name == 'zulu')

        assert original.layout.suffix == ''
        assert result.new_source == expected
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert not result.new_source.endswith(('\n', '\r'))
        assert reparsed.postamble == '# note for zulu'
        assert zulu.trailing == ''

    def test_reordered_adjacent_definition_comment_keeps_owner(self) -> None:
        source = (
            'def charlie():\n'
            '    pass\n'
            '# note for charlie\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            'def zulu():\n'
            '    pass\n'
        )
        expected = (
            'def alpha():\n'
            '    pass\n'
            'def charlie():\n'
            '    pass\n'
            '# note for charlie\n'
            '\n'
            'def zulu():\n'
            '    pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)
        parsed = parse_definitions(result.new_source)
        charlie = next(
            definition for definition in parsed.definitions if definition.name == 'charlie'
        )

        assert result.new_source == expected
        assert charlie.trailing == '# note for charlie\n'
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_reordered_inter_item_comment_keeps_owner(self) -> None:
        source = (
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '# --------\n'
            'def omega():\n'
            '    pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners='barriers',
            ),
            add_future=False,
            remove_future=False,
        )

        original = parse_definitions(source, banners='barriers')
        zulu_original = next(
            definition for definition in original.definitions if definition.name == 'zulu'
        )
        first = sort_file(source, options=options).new_source
        parsed = parse_definitions(first, banners='barriers')
        zulu = next(definition for definition in parsed.definitions if definition.name == 'zulu')

        assert sum(boundary.has_candidate for boundary in original.layout.ownership) == 1
        assert original.layout.ownership[zulu_original.index].has_candidate is True
        assert original.layout.guard_required == frozenset({zulu_original.index})
        assert (
            render_layout(
                original.layout,
                original.blocks,
                [definition.index for definition in original.definitions],
            )
            == source
        )
        assert first == (
            'def alpha():\n'
            '    pass\n'
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            '# --------\n'
            'def omega():\n'
            '    pass\n'
        )
        assert zulu.trailing == '# note for zulu\n'
        assert sort_file(first, options=options).new_source == first


def _assert_parse_round_trip(
    source: str,
    *,
    banners: BannerMode = 'ignore',
) -> None:
    parsed = parse_definitions(source, banners=banners)
    order = [definition.index for definition in parsed.definitions]

    assert render_layout(parsed.layout, parsed.blocks, order) == source
