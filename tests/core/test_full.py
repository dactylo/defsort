import sys
from collections import Counter
from types import FunctionType
from typing import cast

import pytest

from defsort._core import sort_file
from defsort._core._parse import parse_definitions
from defsort._types import (
    BannerMode,
    Blocker,
    EngineOptions,
    SortPolicy,
    SortResult,
    SortScopeOrder,
)
from tests._helpers import execute_source

_SRC_UNORDERED = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'


class TestHoistBannerStability:
    def test_banner_strip_decision_stable_after_hoist(self) -> None:
        src = (
            'def bbb():\n'
            '    return 1\n'
            '\n'
            '\n'
            '# ---------\n'
            'CONST = 1\n'
            '\n'
            'def zzz():\n'
            '    return CONST\n'
            '\n'
            '\n'
            'def aaa():\n'
            '    return 2\n'
        )
        options = EngineOptions(
            policy=SortPolicy(banners='strip'),
            hoist=True,
            force=True,
        )
        first = sort_file(src, options=options)
        second = sort_file(first.new_source, options=options)
        assert second.new_source == first.new_source
        assert '# ---------' not in first.new_source

    def test_constant_before_recognized_barrier_still_hoists(self) -> None:
        src = 'def zzz():\n    return 1\n\nCONST = 1\n\n# ---------\n\ndef aaa():\n    return 2\n'
        expected = (
            'CONST = 1\n\ndef zzz():\n    return 1\n\n# ---------\n\ndef aaa():\n    return 2\n'
        )
        options = EngineOptions(
            policy=SortPolicy(banners='barriers'),
            hoist=True,
            force=True,
        )

        result = sort_file(src, options=options)

        assert result.new_source == expected

    def test_duplicate_names_preserve_earlier_eager_reference(self) -> None:
        src = (
            'def same(value=CONST):\n'
            '    return value\n'
            'CONST = 1\n'
            '\n'
            'def same():\n'
            '    return 2\n'
            '\n'
            'def zzz():\n'
            '    return 3\n'
        )
        options = EngineOptions(policy=SortPolicy(), hoist=True, force=True)

        result = sort_file(src, options=options)

        assert result.new_source == src

    def test_hoist_decision_stable_across_reorder(self) -> None:
        src = (
            'def mmm():\n'
            '    return CONST\n'
            '\n'
            '\n'
            'def bbb():\n'
            '    return 1\n'
            'CONST = 1\n'
            '\n'
            'def aaa():\n'
            '    return 2\n'
        )
        options = EngineOptions(policy=SortPolicy(), hoist=True, force=True)
        first = sort_file(src, options=options)
        second = sort_file(first.new_source, options=options)
        assert first.new_source.startswith('CONST = 1\n')
        assert second.new_source == first.new_source

    def test_hoist_gap_seam_preserves_lone_cr_and_lf(self) -> None:
        src = 'def zulu():\n    return 1\n# kept\rCONST = 1\n\ndef alpha():\n    return 2\n'
        expected = 'CONST = 1\ndef zulu():\n    return 1\n# kept\n\rdef alpha():\n    return 2\n'
        options = EngineOptions(
            policy=SortPolicy(definition_order='none'),
            hoist=True,
            force=True,
        )

        result = sort_file(src, options=options)

        assert result.new_source == expected
        assert Counter(result.new_source.encode()) == Counter(src.encode())
        compile(result.new_source, '<test>', 'exec')
        assert sort_file(result.new_source, options=options).new_source == expected


class TestHoistBoundaryCarry:
    def test_hoisted_constant_keeps_separation(self) -> None:
        src = 'def bbb():\n    return 1\n\n\nCONST = 1\n\n\ndef aaa():\n    return 2\n'
        options = EngineOptions(policy=SortPolicy(), hoist=True)
        result = sort_file(src, options=options)
        assert result.new_source == (
            'CONST = 1\n\n\ndef aaa():\n    return 2\n\n\ndef bbb():\n    return 1\n'
        )
        second = sort_file(result.new_source, options=options)
        assert second.new_source == result.new_source


class TestLeadingGuardAtBanner:
    def test_leading_guard_borrows_global_donor(self) -> None:
        src = (
            'def mmm():\n'
            '    return 0\n'
            '# tail\n'
            '\n'
            '\n'
            '\n'
            'def zzz():\n'
            '    return 1\n'
            '# ---------\n'
            'def beta():\n'
            '    return 2\n'
            '# noqa: E501\n'
            'def alpha():\n'
            '    return 3\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical'),
            force=True,
        )
        first = sort_file(src, options=options)
        second = sort_file(first.new_source, options=options)
        assert second.new_source == first.new_source
        first_lines = first.new_source.splitlines()
        banner_index = first_lines.index('# ---------')
        assert first_lines[banner_index + 1] == ''
        # `# tail` keeps its own trailing guard after donating one spare blank.
        tail_index = first_lines.index('# tail')
        assert first_lines[tail_index + 1] == ''

    def test_leading_guard_without_donor_is_refused(self) -> None:
        src = (
            'def zzz():\n'
            '    return 1\n'
            '# ---------\n'
            'def beta():\n'
            '    return 2\n'
            '# noqa: E501\n'
            'def alpha():\n'
            '    return 3\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical'),
            force=True,
        )
        result = sort_file(src, options=options)
        assert result.skipped
        assert result.blocker == 'layout'
        assert result.new_source == src
        assert not result.method_sorts

    def test_owned_leading_comment_stays_separate_from_banner(self) -> None:
        src = (
            'def zzz():\n'
            '    return 1\n'
            '\n'
            '\n'
            '# ---------\n'
            'def beta():\n'
            '    return 2\n'
            '\n'
            '\n'
            '# noqa: E501\n'
            'def alpha():\n'
            '    return 3\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical'),
            force=True,
        )
        first = sort_file(src, options=options)
        second = sort_file(first.new_source, options=options)
        assert second.new_source == first.new_source
        first_lines = first.new_source.splitlines()
        banner_index = first_lines.index('# ---------')
        assert first_lines[banner_index + 1] == ''

    def test_required_target_guard_is_not_relocated(self) -> None:
        src = (
            'def zzz():\n'
            '    return 0\n'
            '# tail\n'
            '\n'
            'def yyy():\n'
            '    return 1\n'
            '\n'
            '# ---------\n'
            'def beta():\n'
            '    return 2\n'
            '# lead\n'
            'def alpha():\n'
            '    return 3\n'
        )
        expected = (
            'def yyy():\n'
            '    return 1\n'
            'def zzz():\n'
            '    return 0\n'
            '# tail\n'
            '\n'
            '# ---------\n'
            '\n'
            '# lead\n'
            'def alpha():\n'
            '    return 3\n'
            'def beta():\n'
            '    return 2\n'
        )
        options = EngineOptions(policy=SortPolicy())

        first = sort_file(src, options=options)

        assert '# tail\n\n# ---------\n\n# lead' in first.new_source
        assert first.new_source == expected
        assert Counter(first.new_source.encode()) == Counter(src.encode())
        assert sort_file(first.new_source, options=options).new_source == expected

    def test_spare_target_candidate_satisfies_leading_guard(self) -> None:
        src = (
            'def zzz():\n'
            '    return 0\n'
            '\n'
            'def yyy():\n'
            '    return 1\n'
            '# tail\n'
            '\n'
            '# ---------\n'
            'def beta():\n'
            '    return 2\n'
            '# lead\n'
            'def alpha():\n'
            '    return 3\n'
        )
        expected = (
            'def yyy():\n'
            '    return 1\n'
            '# tail\n'
            '\n'
            'def zzz():\n'
            '    return 0\n'
            '# ---------\n'
            '\n'
            '# lead\n'
            'def alpha():\n'
            '    return 3\n'
            'def beta():\n'
            '    return 2\n'
        )
        options = EngineOptions(policy=SortPolicy())

        first = sort_file(src, options=options)

        assert first.new_source == expected
        assert Counter(first.new_source.encode()) == Counter(src.encode())
        first_lines = first.new_source.splitlines()
        banner_index = first_lines.index('# ---------')
        assert first_lines[banner_index + 1 : banner_index + 3] == ['', '# lead']
        tail_index = first_lines.index('# tail')
        assert first_lines[tail_index + 1] == ''
        assert sort_file(first.new_source, options=options).new_source == expected


class TestMixedEndingTransforms:
    def test_hoist_survives_lone_cr_line(self) -> None:
        src = 'def b():\n    return 1\n\n# c\rCONST = 1\n\ndef a():\n    return CONST\n'
        options = EngineOptions(policy=SortPolicy(), hoist=True, force=True)
        result = sort_file(src, options=options)
        assert 'CONST = 1' in result.new_source
        second = sort_file(result.new_source, options=options)
        assert second.new_source == result.new_source

    def test_strip_only_preserves_lone_cr_lf_seam(self) -> None:
        src = 'def c():\n    return 1\r# ---------\r\n\ndef b():\n    return 3\n'
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
            )
        )
        expected = 'def c():\n    return 1\n\rdef b():\n    return 3\n'
        result = sort_file(src, options=options)
        assert result.new_source == expected
        second = sort_file(result.new_source, options=options)
        assert second.new_source == expected

    def test_strip_survives_lone_cr_before_banner(self) -> None:
        src = 'def c():\n    return 1\r# ---------\r\n\ndef b():\n    return 3\n'
        options = EngineOptions(policy=SortPolicy(banners='strip'))
        result = sort_file(src, options=options)
        assert result.new_source.count('\r\n') == 0
        endings = result.new_source.count('\r') + result.new_source.count('\n')
        assert endings == 5  # the banner line's CRLF is gone; no pair fuses
        second = sort_file(result.new_source, options=options)
        assert second.new_source == result.new_source


class TestOverloadGroupSeparation:
    def test_overload_group_keeps_internal_and_external_separation(self) -> None:
        src = (
            'from typing import overload\n'
            '\n'
            '\n'
            '@overload\n'
            'def f(x: int) -> int: ...\n'
            '@overload\n'
            'def f(x: str) -> str: ...\n'
            'def f(x):\n'
            '    return x\n'
            '\n'
            '\n'
            'def aaa():\n'
            '    return 1\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source == (
            'from typing import overload\n'
            '\n'
            '\n'
            'def aaa():\n'
            '    return 1\n'
            '\n'
            '\n'
            '@overload\n'
            'def f(x: int) -> int: ...\n'
            '@overload\n'
            'def f(x: str) -> str: ...\n'
            'def f(x):\n'
            '    return x\n'
        )
        second = sort_file(result.new_source, options=options)
        assert second.new_source == result.new_source

    def test_reversed_property_group_binding_change_is_refused(self) -> None:
        source = (
            'class C:\n'
            '    value = property(lambda self: self._value)\n\n'
            '    def zulu(self):\n'
            '        return 1\n'
            '\n\n'
            '    @value.setter\n'
            '    def value(self, new_value):\n'
            '        self._value = new_value\n'
            '    \n'
            '    @property\n'
            '    def value(self):\n'
            '        return self._value\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )
        original_class = execute_source(source)['C']
        assert isinstance(original_class, type)
        original_descriptor = vars(original_class)['value']
        assert isinstance(original_descriptor, property)
        assert original_descriptor.fset is None

        result = sort_file(source, options=options)
        repeated = sort_file(result.new_source, options=options)
        unchanged_class = execute_source(result.new_source)['C']
        assert isinstance(unchanged_class, type)
        unchanged_descriptor = vars(unchanged_class)['value']
        assert isinstance(unchanged_descriptor, property)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == source
        assert unchanged_descriptor.fset is None
        assert repeated.blocker == result.blocker
        assert repeated.new_source == source


class TestRestrictedBannerPatternBoundary:
    def test_core_sort_rejects_mutated_banner_pattern_before_changing_source(self) -> None:
        policy = SortPolicy()
        object.__setattr__(policy, 'banner_pattern', '^# (a+)+$')
        source = _SRC_UNORDERED

        with pytest.raises(ValueError, match='banner_pattern'):
            sort_file(source, options=EngineOptions(policy=policy, add_future=False))

        assert source == _SRC_UNORDERED

    def test_full_sort_rejects_mutated_banner_pattern_when_banners_ignored(self) -> None:
        policy = SortPolicy(banners='ignore')
        object.__setattr__(policy, 'banner_pattern', '^# (a+)+$')
        source = _SRC_UNORDERED

        with pytest.raises(ValueError, match='banner_pattern'):
            sort_file(source, options=EngineOptions(policy=policy, add_future=False))

        assert source == _SRC_UNORDERED

    def test_full_sort_rejects_mutated_banner_pattern_without_definitions(self) -> None:
        policy = SortPolicy()
        object.__setattr__(policy, 'banner_pattern', '^# (a+)+$')
        source = 'x = 1\n'

        with pytest.raises(ValueError, match='banner_pattern'):
            sort_file(source, options=EngineOptions(policy=policy, add_future=False))

        assert source == 'x = 1\n'


class TestSortFile:
    def test_add_future_resolves_forward_refs(self) -> None:
        src = (
            'class Bravo:\n    def method(self, x: Alpha) -> None:\n        pass\n\n\n'
            'class Alpha:\n    def method(self, x: Bravo) -> None:\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=True))
        assert result.skipped is False
        assert 'from __future__ import annotations' in result.new_source

    def test_alphabetical_mode(self) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                )
            ),
        )
        assert result.new_source.index('alpha') < result.new_source.index('bravo')

    def test_already_ordered(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.new_source == result.original_source

    def test_basic_sort(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert isinstance(result, SortResult)
        assert result.new_source != result.original_source
        assert result.new_source.index('alpha') < result.new_source.index('bravo')
        assert result.skipped is False

    def test_class_named_main_not_forced_last(self) -> None:
        src = 'def zeta():\n    pass\n\n\nclass main(Base):\n    pass\n\n\nclass Base:\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        names = [d.name for d in result.new_order]
        assert names.index('main') < names.index('zeta')

    def test_comment_above_def_stays_with_next(self) -> None:
        src = 'def b():\n    return a()\n\n\n# Comment for a\ndef a():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        lines = result.new_source.splitlines()
        comment_idx = next(i for i, line in enumerate(lines) if line.strip() == '# Comment for a')
        def_a_idx = next(i for i, line in enumerate(lines) if line.startswith('def a'))
        assert def_a_idx == comment_idx + 1

    def test_duplicate_method_names_preserved(self) -> None:
        src = (
            'class MyClass:\n'
            '    def beta(self):\n        pass\n\n'
            '    def process(self, x: int):\n        return x\n\n'
            '    def alpha(self):\n        pass\n\n'
            '    def process(self, x: str):\n        return str(x)\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.new_source.count('def process') == 2

    def test_duplicate_names_block(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.skipped is True

    @pytest.mark.parametrize(
        ('source', 'expected', 'order'),
        [
            (
                (
                    'class C:\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                ),
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
                'alphabetical',
            ),
            (
                (
                    'class C:\r\n'
                    '    def zulu(self):\r\n'
                    '        return alpha()\r\n'
                    '\r\n'
                    '    def alpha(self):\r\n'
                    '        pass'
                ),
                (
                    'class C:\r\n'
                    '    def alpha(self):\r\n'
                    '        pass\r\n'
                    '\r\n'
                    '    def zulu(self):\r\n'
                    '        return alpha()'
                ),
                'dependency',
            ),
            (
                (
                    'class C:\r\n'
                    '    def zulu(self):\n'
                    '        pass\r'
                    '    def alpha(self):\r\n'
                    '        pass\n'
                ),
                (
                    'class C:\r\n'
                    '    def alpha(self):\r\n'
                    '        pass\r'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
                'alphabetical',
            ),
            (
                (
                    'class C:\r'
                    '    def zulu(self):\r'
                    '        pass\r\r\r'
                    '    def alpha(self):\r'
                    '        pass\r'
                ),
                (
                    'class C:\r'
                    '    def alpha(self):\r'
                    '        pass\r\r\r'
                    '    def zulu(self):\r'
                    '        pass\r'
                ),
                'alphabetical',
            ),
        ],
        ids=['lf-final-newline', 'crlf-no-final-newline', 'mixed', 'lone-cr'],
    )
    def test_exact_method_permutation(
        self,
        source: str,
        expected: str,
        order: SortScopeOrder,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order=order),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('source', 'expected', 'order'),
        [
            (
                'def zebra():\n    return alpha()\n\t\ndef alpha():\n    pass\n',
                'def alpha():\n    pass\n\t\ndef zebra():\n    return alpha()\n',
                'dependency',
            ),
            (
                'def zebra():\r\n    pass\r\n \t\rdef alpha():\n    pass',
                'def alpha():\n    pass\r\n \t\rdef zebra():\r\n    pass',
                'alphabetical',
            ),
            (
                (
                    'import os\n\n'
                    'def zebra():\n'
                    '    pass\n'
                    '# zebra trailing\n'
                    '\n'
                    ' \t\n'
                    '# alpha leading\n'
                    'def alpha():\n'
                    '    pass\n'
                    '\n'
                    'if READY:\n'
                    '    run()\n'
                ),
                (
                    'import os\n\n'
                    '# alpha leading\n'
                    'def alpha():\n'
                    '    pass\n'
                    '\n'
                    ' \t\n'
                    'def zebra():\n'
                    '    pass\n'
                    '# zebra trailing\n'
                    '\n'
                    'if READY:\n'
                    '    run()\n'
                ),
                'alphabetical',
            ),
        ],
        ids=['dependency', 'mixed-endings-and-eof', 'comments-preamble-postamble'],
    )
    def test_exact_top_level_permutation(
        self,
        source: str,
        expected: str,
        order: SortScopeOrder,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(definition_order=order, method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_force_bypasses_interstitial(self) -> None:
        src = 'def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), force=True))
        assert result.skipped is False

    def test_force_does_not_bypass_duplicates(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), force=True))
        assert result.skipped is True
        assert result.blocker == 'duplicates'

    def test_force_does_not_bypass_same_name_different_kinds(self) -> None:
        src = 'def Foo():\n    pass\n\n\ndef A():\n    pass\n\n\nclass Foo:\n    pass\n'
        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                ),
                force=True,
                add_future=False,
            ),
        )
        assert result.skipped is True
        assert result.blocker == 'duplicates'

    def test_form_feed_separator_sorts_without_error(self) -> None:
        src = 'def zulu():\n    pass\n\n\f\ndef alpha():\n    pass\n'
        expected = 'def alpha():\n    pass\n\n\f\ndef zulu():\n    pass\n'
        options = EngineOptions(policy=SortPolicy())

        result = sort_file(src, options=options)

        assert result.skipped is False
        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_forward_annotation_blocks_with_no_future(self) -> None:
        src = (
            'class Bravo:\n    def method(self, x: Alpha) -> None:\n        pass\n\n\n'
            'class Alpha:\n    def method(self, x: Bravo) -> None:\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.skipped is True
        assert result.blocker == 'forward_refs'
        assert result.new_source == src
        assert 'from __future__ import annotations' not in result.new_source

    def test_forward_ref_priority_over_duplicates(self) -> None:
        src = (
            'class Bravo:\n    pass\n\n\n'
            'class Alpha:\n    def clone(self) -> Alpha:\n        pass\n\n\n'
            'class Bravo:\n    """Override."""\n\n    pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.skipped is True
        assert result.warning is not None
        assert result.blocker == 'forward_refs'

    def test_future_does_not_repair_eager_base_inversion(self) -> None:
        src = (
            'class Bravo(Alpha):\n    pass\n\n\n'
            'class Alpha:\n    def method(self, x: Bravo) -> None:\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == src
        assert 'from __future__ import annotations' not in result.new_source

    def test_hoist_and_future_transformations_compose_around_assembly(self) -> None:
        source = (
            'class Bravo:\n    value: Alpha\n\nMAX_SIZE = 100\n\nclass Alpha:\n    value: Bravo\n'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(),
                hoist=True,
                add_future=True,
                remove_future=False,
            ),
        )

        assert result.skipped is False
        future_index = result.new_source.index('from __future__ import annotations')
        assert future_index < result.new_source.index('MAX_SIZE')
        assert result.new_source.index('MAX_SIZE') < result.new_source.index('class Alpha')

    def test_hoist_only_no_sort(self) -> None:
        src = 'def alpha():\n    pass\n\nMAX_SIZE = 100\n\ndef bravo():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), hoist=True, force=True))
        assert result.new_source != src
        assert 'MAX_SIZE = 100' in result.new_source
        assert result.new_source.index('MAX_SIZE') < result.new_source.index('def alpha')

    def test_hoist_with_sort(self) -> None:
        src = 'def bravo():\n    pass\n\nMAX_SIZE = 100\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), hoist=True))
        assert result.skipped is False
        assert 'MAX_SIZE = 100' in result.new_source
        assert result.new_source.index('MAX_SIZE') < result.new_source.index('def alpha')

    def test_interstitial_code_blocks(self) -> None:
        src = 'def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.skipped is True
        assert result.blocking_code

    def test_method_only_sort_not_blocked_by_forward_ref(self) -> None:
        src = (
            'class Node:\n'
            '    def z(self):\n'
            '        return 1\n'
            '    def a(self) -> Node:\n'
            '        return self\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.skipped is False
        assert len(result.method_sorts) == 1
        lines = result.new_source.splitlines()
        a_idx = next(i for i, line in enumerate(lines) if 'def a(' in line)
        z_idx = next(i for i, line in enumerate(lines) if 'def z(' in line)
        assert a_idx < z_idx

    def test_method_sort_preserves_trailing_comment(self) -> None:
        src = (
            'class C:\n'
            '    def b(self):\n'
            '        return self.a()\n'
            '\n'
            '    # keep-me\n'
            '    def a(self):\n'
            '        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert 'keep-me' in result.new_source

    def test_methods_sorted_flag(self) -> None:
        src = 'class C:\n    def bravo(self): pass\n    def alpha(self): pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert len(result.method_sorts) == 1

    def test_module_docstring_form_feed_keeps_method_spans(self) -> None:
        src = (
            '"""Doc\fpage."""\n\n\n'
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.new_source == (
            '"""Doc\fpage."""\n\n\n'
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zulu(self):\n'
            '        pass\n'
        )

    def test_multiline_constant_not_duplicated(self) -> None:
        src = (
            'def bravo():\n    pass\n\n'
            'MAPPING = {\n    1: 2,\n    3: 4,\n}\n\n'
            'def alpha():\n    return MAPPING\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), hoist=True, force=True))
        assert result.new_source.count('MAPPING = {') == 1

    def test_nested_method_and_top_level_movement_are_both_lossless(self) -> None:
        source = (
            'class Zulu:\n'
            '    # fixed class leading\n'
            '    def zulu(self):\n'
            '        pass\n'
            ' \t\n'
            '    # alpha leading\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    footer = 1\n'
            '\n'
            '\n'
            'class Alpha:\n'
            '    pass'
        )
        expected = (
            'class Alpha:\n'
            '    pass\n'
            '\n'
            '\n'
            'class Zulu:\n'
            '    # alpha leading\n'
            '    def alpha(self):\n'
            '        pass\n'
            ' \t\n'
            '    # fixed class leading\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    footer = 1'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_nested_sorted_class_moves_as_top_level_block(self) -> None:
        source = (
            'class Zebra:\n'
            '    def zebra(self):\n'
            '        pass\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n\n'
            'class Alpha:\n'
            '    pass\n'
        )
        expected = (
            'class Alpha:\n'
            '    pass\n'
            '\n\n'
            'class Zebra:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zebra(self):\n'
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_no_movement_source_is_byte_identical(self) -> None:
        source = 'import os\r\n\r\ndef alpha():\r\n    pass\r\n \t\r\ndef bravo():\n    pass'
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == source

    @pytest.mark.parametrize('newline', ['\n', '\r\n'])
    def test_ordered_interstitial_is_a_true_noop(self, newline: str) -> None:
        source = 'def alpha():\n    pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
        source = source.replace('\n', newline)

        result = sort_file(source, options=EngineOptions(policy=SortPolicy()))

        assert result.new_source == source
        assert result.blocker is None
        assert not result.skipped
        assert result.warning is None
        assert result.blocking_code == []

    @pytest.mark.parametrize(
        ('source', 'policy'),
        [
            (
                (
                    'class Alpha:\n    def zulu(self): pass\n\n    def alpha(self): pass\n'
                    '\nregistry = {}\n\ndef bravo():\n    pass\n'
                ),
                SortPolicy(),
            ),
            (
                (
                    'class Alpha:\n    def zulu(self): pass\n\n    registry = {}\n\n'
                    '    def alpha(self): pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
                ),
                SortPolicy(),
            ),
            (
                (
                    'def alpha():\n    pass\n\nregistry = {}\n\n# ---------\n\n'
                    'def bravo():\n    pass\n'
                ),
                SortPolicy(banners='strip'),
            ),
            (
                (
                    'class Alpha:\n    def alpha(self): pass\n\n    # ---------\n\n'
                    '    def bravo(self): pass\n\nregistry = {}\n\ndef bravo():\n    pass\n'
                ),
                SortPolicy(banners='strip'),
            ),
            (
                (
                    'from __future__ import annotations\n\ndef alpha():\n    pass\n'
                    '\nregistry = {}\n\ndef bravo():\n    pass\n'
                ),
                SortPolicy(),
            ),
        ],
        ids=['methods', 'class-blocker', 'top-banner', 'method-banner', 'future-removal'],
    )
    def test_ordered_interstitial_retains_required_work_blocker(
        self, source: str, policy: SortPolicy
    ) -> None:
        result = sort_file(source, options=EngineOptions(policy=policy))

        assert result.new_source == source
        assert result.blocker == 'interstitial'
        assert result.skipped

    def test_ordinary_characters_in_class_docstring_keep_method_spans(self) -> None:
        for char in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            src = (
                'class C:\n'
                f'    """D{char}E."""\n'
                '\n'
                '    def zulu(self):\n'
                '        pass\n'
                '\n'
                '    def alpha(self):\n'
                '        pass\n'
            )
            expected = (
                'class C:\n'
                f'    """D{char}E."""\n'
                '\n'
                '    def alpha(self):\n'
                '        pass\n'
                '\n'
                '    def zulu(self):\n'
                '        pass\n'
            )
            result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
            assert result.new_source == expected
            compile(result.new_source, '<test>', 'exec')
            assert (
                sort_file(result.new_source, options=EngineOptions(policy=SortPolicy())).new_source
                == expected
            )

    def test_ordinary_characters_in_strings_and_comments_sort_exact(self) -> None:
        for char in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            src = (
                f'"""D{char}E."""\n'
                f'# note{char}here\n'
                '\n\n'
                'def zulu():\n    pass\n\n\ndef alpha():\n    pass\n'
            )
            expected = (
                f'"""D{char}E."""\n'
                f'# note{char}here\n'
                '\n\n'
                'def alpha():\n    pass\n\n\ndef zulu():\n    pass\n'
            )
            result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
            assert result.new_source == expected
            compile(result.new_source, '<test>', 'exec')
            assert (
                sort_file(result.new_source, options=EngineOptions(policy=SortPolicy())).new_source
                == expected
            )

    def test_parenthesized_future_import_removed(self) -> None:
        src = (
            'from __future__ import (annotations)\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'def bravo():\n    pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert 'from __future__' not in result.new_source

    def test_reorder_preserves_module_guard_separator(self) -> None:
        source = (
            'def zebra():\n'
            '    pass\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            "if __name__ == '__main__':\n"
            '    main()\n'
        )
        expected = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            'def zebra():\n'
            '    pass\n'
            '\n\n'
            "if __name__ == '__main__':\n"
            '    main()\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_reversed_property_group_binding_change_is_refused(self) -> None:
        src = (
            'name = property(lambda self: None)\n'
            '\n'
            '\n'
            'class MyClass:\n'
            '    @name.deleter\n'
            '    def name(self):\n        del self._name\n\n'
            '    @name.setter\n'
            '    def name(self, val):\n        self._name = val\n\n'
            '    @property\n'
            '    def name(self):\n        return self._name\n'
        )
        original_class = execute_source(src)['MyClass']
        assert isinstance(original_class, type)
        original_descriptor = vars(original_class)['name']
        assert isinstance(original_descriptor, property)
        assert original_descriptor.fset is None
        assert original_descriptor.fdel is None

        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        unchanged_class = execute_source(result.new_source)['MyClass']
        assert isinstance(unchanged_class, type)
        unchanged_descriptor = vars(unchanged_class)['name']
        assert isinstance(unchanged_descriptor, property)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == src
        assert unchanged_descriptor.fset is None
        assert unchanged_descriptor.fdel is None

    def test_syntax_error_unchanged(self) -> None:
        src = 'def alpha(\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert result.new_source == result.original_source

    def test_unnecessary_future_import_removed(self) -> None:
        src = (
            'from __future__ import annotations\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'def bravo():\n    pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy()))
        assert 'from __future__ import annotations' not in result.new_source


class TestSortFileClassSuiteAnnotations:
    @pytest.mark.parametrize(
        ('existing_future', 'add_future', 'expected_blocker'),
        [
            (True, True, None),
            (True, False, None),
            (False, True, None),
            (False, False, 'forward_refs'),
        ],
        ids=['preserve-add', 'preserve-no-add', 'add', 'block'],
    )
    @pytest.mark.parametrize(
        'suite',
        [
            '    if True:\n        value: ZetaType\n',
            ('    try:\n        value: ZetaType\n    except NameError:\n        raise\n'),
        ],
        ids=['if', 'try'],
    )
    def test_class_suite_annotation_matrix(
        self,
        suite: str,
        *,
        existing_future: bool,
        add_future: bool,
        expected_blocker: Blocker | None,
    ) -> None:
        future = 'from __future__ import annotations\n\n\n' if existing_future else ''
        source = f'{future}class ZetaType:\n    pass\n\n\nclass Alpha:\n{suite}'
        execute_source(source)
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=add_future,
        )

        result = sort_file(source, options=options)

        assert result.blocker == expected_blocker
        if expected_blocker is None:
            assert result.skipped is False
            assert result.new_source.index('class Alpha') < result.new_source.index(
                'class ZetaType'
            )
            assert 'from __future__ import annotations' in result.new_source
            execute_source(result.new_source)
        else:
            assert result.skipped is True
            assert result.new_source == source
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source


class TestEvaluationOrder:
    @pytest.mark.parametrize(
        ('source', 'blocker'),
        [
            (
                (
                    'def zeta():\n    pass\n\n\n'
                    'def alpha():\n    pass\n\n\n'
                    "__all__ = [name for name in globals() if not name.startswith('_')]\n"
                ),
                'eager_order',
            ),
            (
                'class Zeta:\n    pass\n\n\nclass Alpha:\n    value: Zeta\n',
                'forward_refs',
            ),
            (
                'def zeta():\n    pass\n\nprint(zeta)\n\ndef alpha():\n    pass\n',
                'interstitial',
            ),
            (
                'def zeta():\n    pass\n\ndef alpha():\n    pass\n\ndef zeta():\n    pass\n',
                'duplicates',
            ),
        ],
        ids=['dynamic-all', 'annotations', 'interstitial', 'duplicates'],
    )
    def test_opt_in_retains_other_safety_checks(self, source: str, blocker: Blocker) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                evaluation_order='allow-reordering', definition_order='alphabetical'
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.blocker == blocker
        assert result.skipped
        assert result.new_source == source


class TestSortFileEagerOrderSafety:
    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    def test_called_sibling_body_blocks_new_eager_inversion(self, *, force: bool) -> None:
        source = (
            'def zeta_compute():\r\n'
            '    return 42\r\n'
            '\r\n\r\n'
            'class Alpha:\r\n'
            '    def helper():\r\n'
            '        return zeta_compute()\r\n'
            '\r\n'
            '    value = helper()'
        )
        execute_source(source)
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            force=force,
        )

        result = sort_file(source, options=options)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == source
        assert result.original_source == source

    def test_dependency_order_handles_representative_rule_builder_shape(self) -> None:
        source = (
            'def zeta_pattern(level):\n'
            '    return str(level)\n'
            '\n\n'
            'class AlphaLexer:\n'
            '    def call_pattern(level):\n'
            '        return (zeta_pattern(level),)\n'
            '    rules = {"root": [call_pattern(1), call_pattern(2)]}\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='dependency', method_order='none')
        )

        result = sort_file(source, options=options)

        namespace = execute_source(result.new_source)
        assert result.blocker is None
        assert vars(namespace['AlphaLexer'])['rules'] == {'root': [('1',), ('2',)]}

    def test_dependency_order_preserves_safe_called_sibling_binding(self) -> None:
        source = (
            'def zeta_compute():\n'
            '    return 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='dependency', method_order='none')
        )

        result = sort_file(source, options=options)

        assert result.blocker is None
        assert result.new_source == source
        assert vars(execute_source(result.new_source)['Alpha'])['value'] == 42

    def test_dependency_order_refuses_preexisting_called_sibling_inversion(self) -> None:
        source = (
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
            '\n\n'
            'def zeta_compute():\n'
            '    return 42\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='dependency', method_order='none')
        )

        result = sort_file(source, options=options)
        repeated = sort_file(result.new_source, options=options)

        assert result.blocker == 'eager_order'
        assert result.skipped is True
        assert result.new_source == source
        assert repeated.blocker == result.blocker
        assert repeated.new_source == source

    def test_dynamic_all_blocks_changed_full_order(self) -> None:
        source = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            '\n'
            "__all__ = [name for name in globals() if not name.startswith('_')]\n"
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))

        result = sort_file(source, options=options)

        assert result.blocker == 'eager_order'
        assert result.new_source == source

    def test_dynamic_all_is_not_bypassed_by_force(self) -> None:
        source = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            '\n'
            "__all__ = [name for name in globals() if not name.startswith('_')]\n"
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical'),
            force=True,
        )

        result = sort_file(source, options=options)

        assert result.blocker == 'eager_order'
        assert result.new_source == source

    def test_dynamic_all_nested_assignment_blocks_changed_full_order(self) -> None:
        source = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            '\n'
            'if enabled:\n'
            "    __all__ = ['alpha']\n"
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))

        result = sort_file(source, options=options)

        assert result.blocker == 'eager_order'
        assert result.new_source == source

    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    @pytest.mark.parametrize(
        ('source', 'unsafe_source'),
        [
            (
                (
                    'def zeta_decorator(function):\n    return function\n\n\n'
                    '@zeta_decorator\n'
                    'def alpha():\n    return 1\n'
                ),
                (
                    '@zeta_decorator\n'
                    'def alpha():\n    return 1\n\n\n'
                    'def zeta_decorator(function):\n    return function\n'
                ),
            ),
            (
                (
                    'def zeta_default():\n    return 1\n\n\n'
                    'def alpha(value=zeta_default()):\n    return value\n'
                ),
                (
                    'def alpha(value=zeta_default()):\n    return value\n\n\n'
                    'def zeta_default():\n    return 1\n'
                ),
            ),
            (
                'class ZetaBase:\n    pass\n\n\nclass Alpha(ZetaBase):\n    pass\n',
                'class Alpha(ZetaBase):\n    pass\n\n\nclass ZetaBase:\n    pass\n',
            ),
            (
                (
                    'def zeta_decorator(cls):\n    return cls\n\n\n'
                    '@zeta_decorator\n'
                    'class Alpha:\n    pass\n'
                ),
                (
                    '@zeta_decorator\n'
                    'class Alpha:\n    pass\n\n\n'
                    'def zeta_decorator(cls):\n    return cls\n'
                ),
            ),
            (
                (
                    'def zeta_meta(name, bases, namespace):\n'
                    '    return type(name, bases, namespace)\n\n\n'
                    'class Alpha(metaclass=zeta_meta):\n    pass\n'
                ),
                (
                    'class Alpha(metaclass=zeta_meta):\n    pass\n\n\n'
                    'def zeta_meta(name, bases, namespace):\n'
                    '    return type(name, bases, namespace)\n'
                ),
            ),
            (
                'class ZetaValue:\n    pass\n\n\nclass Alpha:\n    value = ZetaValue()\n',
                'class Alpha:\n    value = ZetaValue()\n\n\nclass ZetaValue:\n    pass\n',
            ),
            (
                (
                    'class ZetaValue:\n    pass\n\n\n'
                    'class Alpha:\n'
                    '    class Nested:\n'
                    '        value = ZetaValue()\n'
                ),
                (
                    'class Alpha:\n'
                    '    class Nested:\n'
                    '        value = ZetaValue()\n\n\n'
                    'class ZetaValue:\n    pass\n'
                ),
            ),
        ],
        ids=[
            'function-decorator',
            'positional-default',
            'class-base',
            'class-decorator',
            'class-keyword',
            'class-body',
            'nested-class',
        ],
    )
    @pytest.mark.parametrize('allow_reordering', [False, True])
    def test_eager_candidate_returns_original_source(
        self,
        source: str,
        unsafe_source: str,
        *,
        force: bool,
        allow_reordering: bool,
    ) -> None:
        execute_source(source)
        if sys.version_info < (3, 14):
            with pytest.raises(NameError):
                execute_source(unsafe_source)
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                evaluation_order='allow-reordering' if allow_reordering else 'preserve',
            ),
            force=force,
        )

        result = sort_file(source, options=options)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.original_source == source
        assert result.new_source == source
        assert [unit.index for unit in result.new_order] != [
            unit.index for unit in result.original_order
        ]
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source
        assert repeated.blocker == 'eager_order'

    def test_independent_default_side_effect_order_is_preserved(self) -> None:
        source = (
            'events = []\n'
            '\n'
            '\n'
            "def zeta(value=events.append('zeta')):\n"
            '    return value\n'
            '\n'
            '\n'
            "def alpha(value=events.append('alpha')):\n"
            '    return value\n'
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))

        result = sort_file(source, options=options)
        namespace = execute_source(result.new_source)

        assert namespace['events'] == ['zeta', 'alpha']
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    def test_method_default_side_effect_order_is_preserved_when_forced(self) -> None:
        source = (
            'events = []\n\n\n'
            'class Example:\n'
            "    def zeta(self, value=(events.append('zeta'), 0)[1]):\n"
            '        pass\n\n'
            '    def omega(self):\n'
            '        pass\n\n'
            '    def beta(self):\n'
            '        pass\n\n'
            "    def alpha(self, *, value=(events.append('alpha'), 0)[1]):\n"
            '        pass\n'
        )
        expected = (
            'events = []\n\n\n'
            'class Example:\n'
            "    def zeta(self, value=(events.append('zeta'), 0)[1]):\n"
            '        pass\n\n'
            '    def beta(self):\n'
            '        pass\n\n'
            '    def omega(self):\n'
            '        pass\n\n'
            "    def alpha(self, *, value=(events.append('alpha'), 0)[1]):\n"
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            force=True,
        )

        original_namespace = execute_source(source)
        result = sort_file(source, options=options)
        accepted_namespace = execute_source(result.new_source)

        assert result.new_source == expected
        assert result.new_source != result.original_source
        assert result.skipped is False
        assert result.blocker is None
        assert len(result.method_sorts) == 1
        assert accepted_namespace['events'] == original_namespace['events']
        assert result.new_source.index('def zeta') < result.new_source.index('def alpha')
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    def test_nested_default_yield_avoids_false_eager_blocker(self) -> None:
        source = (
            'def zeta_compute():\n'
            '    return 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        def nested(value=(yield zeta_compute())):\n'
            '            return value\n'
            '        return nested\n'
            '\n'
            '    value = helper()\n'
        )
        execute_source(source)
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none')
        )

        result = sort_file(source, options=options)

        assert result.skipped is False
        assert result.blocker is None
        assert result.new_source.index('class Alpha') < result.new_source.index('def zeta_compute')
        execute_source(result.new_source)

    @pytest.mark.parametrize(
        'all_value',
        ["['zeta', 'alpha']", "('zeta', 'alpha')"],
        ids=['list', 'tuple'],
    )
    def test_static_literal_all_allows_safe_sort(self, all_value: str) -> None:
        source = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            '\n'
            f'__all__ = {all_value}\n'
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))

        result = sort_file(source, options=options)

        assert result.blocker is None
        assert result.new_source.index('def alpha') < result.new_source.index('def zeta')
        assert sort_file(result.new_source, options=options).new_source == result.new_source


class TestSortFileRemoveFuture:
    @pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
    @pytest.mark.parametrize(
        'retained',
        [
            '',
            'from __future__ import annotations  # keep\n',
            'from __future__ import annotations, generator_stop\n',
        ],
        ids=['eligible-only', 'comment-bearing', 'combined'],
    )
    def test_future_duplicate_cleanup_is_idempotent(self, ending: str, retained: str) -> None:
        body = '\n\ndef feature():\n    return 1\n'.replace('\n', ending)
        retained = retained.replace('\n', ending)
        source = (
            retained
            + f'from __future__ import annotations{ending}'
            + f'from __future__ import (annotations){ending}'
            + body
        )
        options = EngineOptions(policy=SortPolicy())

        result = sort_file(source, options=options)

        assert result.new_source == retained + body
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    @staticmethod
    def _assert_annotated_future_preserved(
        source: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)
        original_namespace = execute_source(source)

        result = sort_file(source, options=options)

        assert 'from __future__ import annotations' in result.new_source
        transformed_namespace = execute_source(result.new_source)
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source
        return original_namespace, transformed_namespace

    def test_future_live_binding_preserves_lossless_banner_strip(self) -> None:
        banner = '# --------  \r\n# banner detail\t\r\n'
        source = (
            'from __future__ import annotations\r\n\r\n'
            'def feature():  \r\n    return annotations.compiler_flag\r\n'
            f' \t\r\n{banner}\t\r\ndef other():\r\n    pass  \r\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='none', banners='strip')
        )

        result = sort_file(source, options=options)

        assert result.new_source == source.replace(banner, '')
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    @pytest.mark.parametrize(
        'source',
        [
            (
                'from __future__ import annotations\n\n'
                'from typing import TYPE_CHECKING\n\n'
                'if TYPE_CHECKING:\n'
                '    from typing import TextIO\n\n'
                'CURRENT: tuple[Later, TextIO] | None = None\n\n\n'
                'class Later:\n'
                '    pass\n'
            ),
            (
                'from __future__ import annotations\n\n\n'
                'def alpha():\n'
                '    value: int = 1\n'
                '    return value\n'
            ),
            (
                'from __future__ import annotations\n\n\n'
                'class Later:\n'
                '    pass\n\n\n'
                'class Example:\n'
                '    value: Later\n'
            ),
        ],
        ids=['module-type-checking', 'function-local', 'valueless-class'],
    )
    def test_future_preserves_annotations(self, source: str) -> None:
        self._assert_annotated_future_preserved(source)

    def test_future_preserves_live_binding(self) -> None:
        source = (
            'from __future__ import annotations\n\n'
            'def feature():\n    return annotations.compiler_flag\n'
        )
        original = execute_source(source)['feature']
        assert isinstance(original, FunctionType)
        assert original() == 16777216

        options = EngineOptions(policy=SortPolicy())
        result = sort_file(source, options=options)

        transformed = execute_source(result.new_source)['feature']
        assert isinstance(transformed, FunctionType)
        assert transformed() == 16777216
        assert result.new_source == source
        assert sort_file(result.new_source, options=options).new_source == source

    def test_future_preserves_raw_annotation_representation(self) -> None:
        source = (
            'from __future__ import annotations\n\n\nclass Later:\n    pass\n\n\nvalue: Later\n'
        )

        original_namespace, transformed_namespace = self._assert_annotated_future_preserved(source)

        assert cast('dict[str, str]', original_namespace['__annotations__'])['value'] == 'Later'
        assert cast('dict[str, str]', transformed_namespace['__annotations__'])['value'] == 'Later'

    def test_future_removal_preserves_docstring_decoy(self) -> None:
        source = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\nfrom __future__ import annotations\r# REAL-AFTER\r\n'
            'def alpha():\n    pass'
        )
        expected = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\n# REAL-AFTER\r\ndef alpha():\n    pass'
        )
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)

        result = sort_file(source, options=options)

        assert result.new_source == expected

    def test_future_remove_leaves_combined_import_unchanged(self) -> None:
        source = (
            'from __future__ import annotations, generator_stop\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'def bravo():\n    pass\n'
        )
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)

        result = sort_file(source, options=options)

        assert 'from __future__ import annotations, generator_stop' in result.new_source
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    def test_future_removes_annotation_free_import(self) -> None:
        source = (
            'from __future__ import annotations\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'def bravo():\n    pass\n'
        )
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)

        result = sort_file(source, options=options)

        assert 'from __future__ import annotations' not in result.new_source
        assert sort_file(result.new_source, options=options).new_source == result.new_source

    def test_remove_future_false_preserves_import(self) -> None:
        # Source has future import + ordering that makes it unnecessary after sort.
        # After sorting, forward refs are gone, so normally the import would be removed.
        src = (
            'from __future__ import annotations\n\n\n'
            'def bravo(x: Alpha) -> None:\n    pass\n\n\n'
            'class Alpha:\n    pass\n'
        )
        result = sort_file(
            src, options=EngineOptions(policy=SortPolicy(), add_future=False, remove_future=False)
        )
        assert 'from __future__ import annotations' in result.new_source

    def test_remove_future_false_preserves_when_already_ordered(self) -> None:
        # Already in correct dependency order, but the future import is
        # unnecessary (no forward refs). Normally sort_file would strip it.
        src = (
            'from __future__ import annotations\n\n\n'
            'class Alpha:\n    pass\n\n\n'
            'def bravo(x: Alpha) -> None:\n    pass\n'
        )
        result = sort_file(
            src, options=EngineOptions(policy=SortPolicy(), add_future=False, remove_future=False)
        )
        assert 'from __future__ import annotations' in result.new_source


class TestSortFileTopLevelBanners:
    def test_banner_barriers_alphabetical_order_stays_within_sections(self) -> None:
        src = (
            'def zulu():\n'
            '    pass\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def delta():\n'
            '    pass\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', banners='barriers'),
                add_future=False,
            ),
        )

        assert [definition.name for definition in result.new_order] == [
            'alpha',
            'zulu',
            'charlie',
            'delta',
        ]
        assert result.new_source.index('def zulu') < result.new_source.index('# --------')
        assert result.new_source.index('# --------') < result.new_source.index('def charlie')
        assert 'def charlie():\n    pass\n\n\ndef delta()' in result.new_source

    def test_banner_barriers_are_independent_in_top_level_and_method_scopes(self) -> None:
        src = (
            'def zulu():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'class Container:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # ========\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        top_banner = result.new_source.index('# --------')
        method_banner = result.new_source.index('    # ========')
        assert result.new_source.index('def zulu():') < top_banner
        assert top_banner < result.new_source.index('class Container')
        assert result.new_source.index('def zulu(self)') < method_banner
        assert method_banner < result.new_source.index('def alpha(self)')
        assert result.new_source.index('def alpha(self)') < result.new_source.index(
            'def bravo(self)'
        )

    def test_banner_barriers_dependency_order_stays_within_sections(self) -> None:
        src = (
            'def zulu():\n'
            '    return alpha()\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def bravo():\n'
            '    return alpha()\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='barriers'), add_future=False),
        )

        assert result.skipped is False
        assert result.new_source.index('def zulu') < result.new_source.index('# --------')
        assert result.new_source.index('# --------') < result.new_source.index('def alpha')
        assert result.new_source.index('def alpha') < result.new_source.index('def bravo')

    def test_banner_barriers_keep_main_last_inside_its_section(self) -> None:
        src = (
            'def main():\n'
            '    return zulu()\n'
            '\n\n'
            'def zulu():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='barriers'), add_future=False),
        )

        assert [definition.name for definition in result.new_order] == ['zulu', 'main', 'bravo']
        assert result.new_source.index('def main') < result.new_source.index('# --------')

    def test_banner_barriers_keep_ordinary_leading_comment_without_extra_blanks(self) -> None:
        src = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            '# delta comment\n'
            'def delta():\n'
            '    pass\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', banners='barriers'),
                add_future=False,
            ),
        )

        assert result.new_source == (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
            '\n\n'
            '# delta comment\n'
            'def delta():\n'
            '    pass\n'
        )

    def test_banner_barriers_keep_overload_group_inside_section(self) -> None:
        src = (
            'def zulu():\n'
            '    pass\n'
            '\n\n'
            'def parse(value: int):\n'
            '    pass\n'
            '\n\n'
            'def parse(value: str):\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', banners='barriers'),
                add_future=False,
            ),
        )

        assert [definition.name for definition in result.new_order] == [
            'parse',
            'parse',
            'zulu',
            'alpha',
        ]
        assert result.new_source.index('def zulu') < result.new_source.index('# --------')

    def test_banner_barriers_skip_future_for_backward_main_annotation(self) -> None:
        src = (
            'def main():\n'
            '    return 1\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def worker(value: main):\n'
            '    return value\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(banners='barriers'),
            ),
        )

        assert result.new_source == src
        assert 'from __future__ import annotations' not in result.new_source

    def test_banner_constraints_exclude_disabled_scopes(self) -> None:
        src = (
            'class Service:\n'
            '    def zulu(self):\n'
            '        pass\n\n'
            '    # --------\n\n'
            '    def alpha(self):\n'
            '        pass\n\n\n'
            '# ========\n\n\n'
            'def alpha():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='none'),
                add_future=False,
            ),
        )

        assert result.banner_constraints == []

    def test_banner_constraints_include_method_occurrences_and_coordinates(self) -> None:
        src = (
            'from typing import overload\n\n\n'
            'class Service:\n'
            '    @property\n'
            '    def zulu(self):\n'
            '        return 1\n\n'
            '    @zulu.setter\n'
            '    def zulu(self, new_value):\n'
            '        pass\n\n'
            '    @overload\n'
            '    def zeta(self, value: int): ...\n\n'
            '    # Methods\n'
            '    # --------\n\n'
            '    def alpha(self):\n'
            '        pass\n\n'
            '    @overload\n'
            '    def zeta(self, value: str): ...\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
                add_future=False,
            ),
        )

        assert len(result.banner_constraints) == 1
        constraint = result.banner_constraints[0]
        assert constraint.scope == 'methods'
        assert constraint.class_name == 'Service'
        assert constraint.boundary.start_line == 16
        assert constraint.boundary.text == '# Methods\n# --------'
        assert [
            (item.name, item.occurrence, item.line, item.direction) for item in constraint.held
        ] == [
            ('zeta', 1, 13, 'after'),
            ('alpha', 1, 19, 'before'),
        ]

    def test_banner_constraints_report_changed_top_level_target(self) -> None:
        src = (
            'def zulu():\n    pass\n\n\n'
            'def yankee():\n    pass\n\n\n'
            '# --------\n# Helpers\n# --------\n\n\n'
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', method_order='none'),
                add_future=False,
            ),
        )

        assert result.new_source != src
        assert len(result.banner_constraints) == 1
        constraint = result.banner_constraints[0]
        assert constraint.scope == 'definitions'
        assert constraint.class_name is None
        assert constraint.boundary.start_line == 9
        assert constraint.boundary.text == '# --------\n# Helpers\n# --------'
        assert [
            (item.name, item.kind, item.occurrence, item.line, item.direction)
            for item in constraint.held
        ] == [
            ('zulu', 'function', 1, 1, 'after'),
            ('yankee', 'function', 1, 5, 'after'),
            ('bravo', 'function', 1, 14, 'before'),
            ('alpha', 'function', 1, 18, 'before'),
        ]

    def test_banner_constraints_report_constraint_only_noop_across_boundaries(self) -> None:
        src = (
            'def zulu():\n    pass\n\n\n'
            '# First\n# --------\n\n\n'
            'def mike():\n    pass\n\n\n'
            '# ========\n\n\n'
            'def alpha():\n    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', method_order='none'),
                add_future=False,
            ),
        )

        assert result.new_source == src
        assert [constraint.boundary.start_line for constraint in result.banner_constraints] == [
            5,
            13,
        ]
        assert [
            [(item.name, item.direction) for item in constraint.held]
            for constraint in result.banner_constraints
        ] == [
            [('zulu', 'after'), ('alpha', 'before')],
            [('zulu', 'after'), ('alpha', 'before')],
        ]

    def test_banner_constraints_survive_safety_skip(self) -> None:
        src = (
            'def zulu():\n    pass\n\n'
            'registry = {}\n\n'
            'def yankee():\n    pass\n\n\n'
            '# --------\n\n\n'
            'def alpha():\n    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='alphabetical', method_order='none'),
                add_future=False,
            ),
        )

        assert result.skipped is True
        assert result.banner_constraints

    def test_banner_duplicate_safety_remains_global(self) -> None:
        src = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='barriers'), add_future=False),
        )

        assert result.skipped is True
        assert result.blocker == 'duplicates'

    def test_banner_ignore_matches_pre_feature_output(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='ignore'), add_future=False),
        )

        assert result.new_source == (
            'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n\n\n# --------\n'
        )

    def test_banner_interstitial_safety_remains_global(self) -> None:
        src = (
            'def bravo():\n'
            '    pass\n'
            '\n'
            'registry = {}\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='barriers'), add_future=False),
        )

        assert result.skipped is True
        assert result.blocker == 'interstitial'

    def test_banner_interstitial_safety_survives_outer_hoist(self) -> None:
        src = (
            'def bravo():\n'
            '    pass\n'
            '\n'
            'MAX_SIZE = 100\n'
            '\n'
            '# --------\n'
            '\n'
            'registry = {}\n'
            '\n'
            '# ========\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(banners='barriers'), hoist=True, add_future=False
            ),
        )

        assert result.skipped is True
        assert result.blocker == 'interstitial'

    @pytest.mark.parametrize(
        ('mode', 'has_banner'),
        [
            ('barriers', True),
            ('ignore', True),
            ('strip', False),
        ],
    )
    def test_banner_modes_apply_with_definition_order_none(
        self,
        mode: BannerMode,
        *,
        has_banner: bool,
    ) -> None:
        src = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', banners=mode),
                add_future=False,
            ),
        )

        assert ('# --------' in result.new_source) is has_banner
        assert result.new_source.index('def bravo') < result.new_source.index('def alpha')

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                (
                    'def alpha():\r\n'
                    '    pass\r'
                    '# keep-before-top\n'
                    ' \t\r\n'
                    '# --------\r'
                    '\r\n'
                    '# keep-between-top\r\n'
                    '\t\n'
                    '# REGION TOP\r\n'
                    ' \r'
                    '# keep-after-top\n'
                    '\r\n'
                    'def bravo():\r'
                    '    pass'
                ),
                (
                    'def alpha():\r\n'
                    '    pass\r'
                    '# keep-before-top\n'
                    ' \t\r\n'
                    '\r\n'
                    '# keep-between-top\r\n'
                    '\t\n'
                    ' \r'
                    '# keep-after-top\n'
                    '\r\n'
                    'def bravo():\r'
                    '    pass'
                ),
            ),
            (
                (
                    'class C:\r\n'
                    '    def alpha(self):\n'
                    '        pass\r'
                    '    # keep-before-method\r\n'
                    '    \t\n'
                    '    # ========\r\n'
                    '    \r'
                    '    # keep-between-method\n'
                    '    \t\r\n'
                    '    # REGION METHOD\r'
                    '    \n'
                    '    # keep-after-method\r\n'
                    '    \r'
                    '    def bravo(self):\n'
                    '        pass'
                ),
                (
                    'class C:\r\n'
                    '    def alpha(self):\n'
                    '        pass\r'
                    '    # keep-before-method\r\n'
                    '    \t\n'
                    '    \r'
                    '    # keep-between-method\n'
                    '    \t\r\n'
                    '    \n'
                    '    # keep-after-method\r\n'
                    '    \r'
                    '    def bravo(self):\n'
                    '        pass'
                ),
            ),
        ],
        ids=['top-level', 'method'],
    )
    def test_banner_strip_exact_span_matrix(self, source: str, expected: str) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
                banner_pattern='^# REGION .+$',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_banner_strip_globally_orders_and_removes_boundaries(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file(
            src,
            options=EngineOptions(policy=SortPolicy(banners='strip'), add_future=False),
        )

        assert '# --------' not in result.new_source
        assert result.new_source.index('def alpha') < result.new_source.index('def bravo')

    def test_banner_strip_none_preserves_combined_scope_crlf_bytes(self) -> None:
        method_banner = '    # ========  \r\n    # method detail\t\r\n'
        top_banner = '# --------  \r\n# top detail\t\r\n'
        src = (
            'class C:\r\n'
            '    def alpha(self):  \r\n'
            '        pass\r\n'
            ' \t\r\n'
            f'{method_banner}'
            '\t\r\n'
            '    def bravo(self):\r\n'
            '        pass  \r\n'
            ' \r\n'
            f'{top_banner}'
            '\t\r\n'
            'def tail():\r\n'
            '    return C()  \r\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='none',
                    banners='strip',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == src.replace(method_banner, '').replace(top_banner, '')

    def test_banner_strip_none_preserves_top_level_crlf_bytes(self) -> None:
        banner = '# --------  \r\n# banner detail\t\r\n'
        src = f'def alpha():  \r\n    pass\r\n \t\r\n{banner}\t\r\ndef bravo():\r\n    pass  \r\n'

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='none',
                    banners='strip',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == src.replace(banner, '')

    def test_banner_strip_preserves_ordinary_block_between_boundaries(self) -> None:
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

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', banners='strip'),
                add_future=False,
            ),
        )

        assert '# --------' not in result.new_source
        assert '# ========' not in result.new_source
        assert result.new_source.count('# ordinary block\n') == 1
        assert result.new_source.count('\n# ordinary block\n\n') == 1

    @pytest.mark.parametrize(
        ('mode', 'source', 'expected'),
        [
            (
                'barriers',
                (
                    'def zulu():\n'
                    '    pass\n'
                    '\n'
                    '# --------\n'
                    '\n'
                    '# ========\n'
                    '\n\n'
                    'def bravo():\n'
                    '    pass\n'
                    ' \n'
                    'def alpha():\n'
                    '    pass'
                ),
                (
                    'def zulu():\n'
                    '    pass\n'
                    '\n'
                    '# --------\n'
                    '\n'
                    '# ========\n'
                    '\n\n'
                    'def alpha():\n'
                    '    pass\n'
                    ' \n'
                    'def bravo():\n'
                    '    pass'
                ),
            ),
            (
                'ignore',
                'def zebra():\n    pass\n\n# --------\ndef alpha():\n    pass\n',
                '# --------\ndef alpha():\n    pass\n\ndef zebra():\n    pass\n',
            ),
            (
                'strip',
                'def zebra():\n    pass\n\n# --------\ndef alpha():\n    pass\n',
                'def alpha():\n    pass\n\ndef zebra():\n    pass\n',
            ),
        ],
        ids=['barriers-adjacent-empty-section', 'ignore-ordinary-comment', 'strip-exact-span'],
    )
    def test_exact_banner_layout_permutation(
        self,
        mode: BannerMode,
        source: str,
        expected: str,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners=mode,
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_force_does_not_bypass_banner_duplicate_safety(self) -> None:
        src = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(banners='barriers'), force=True, add_future=False
            ),
        )

        assert result.skipped is True
        assert result.blocker == 'duplicates'

    def test_strip_mode_form_feed_docstring_removes_only_banner(self) -> None:
        src = '"""Doc\fpage."""\n\ndef zulu():\n    pass\n\n# --------\n\ndef alpha():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(banners='strip')))
        assert result.new_source == (
            '"""Doc\fpage."""\n\ndef alpha():\n    pass\n\n\ndef zulu():\n    pass\n'
        )


class TestSortFileTotalMethods:
    def test_async_methods(self) -> None:
        src = (
            'class C:\n'
            '    def sync_method(self):\n        pass\n\n'
            '    async def async_method(self):\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.total_methods == 2

    def test_class_with_methods(self) -> None:
        src = (
            'class C:\n    def alpha(self):\n        pass\n\n    def bravo(self):\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.total_methods == 2

    def test_multiple_classes(self) -> None:
        src = (
            'class A:\n    def m1(self):\n        pass\n\n\n'
            'class B:\n    def m2(self):\n        pass\n\n    def m3(self):\n        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.total_methods == 3

    def test_nested_class_excluded(self) -> None:
        src = (
            'class Outer:\n'
            '    def outer_method(self):\n        pass\n\n'
            '    class Inner:\n'
            '        def inner_method(self):\n            pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.total_methods == 1

    def test_no_classes(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        assert result.total_methods == 0


class TestTrailingCommentOwnershipGuard:
    def test_barriers_then_strip_removes_only_banner_span(self) -> None:
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
        barrier_options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners='barriers',
            ),
            add_future=False,
            remove_future=False,
        )
        strip_options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
            ),
            add_future=False,
            remove_future=False,
        )
        expected_first = (
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
        expected_stripped = (
            'def alpha():\n'
            '    pass\n'
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            'def omega():\n'
            '    pass\n'
        )

        first = sort_file(source, options=barrier_options).new_source
        stripped = sort_file(first, options=strip_options).new_source

        assert first == expected_first
        assert first.count('# note for zulu\n') == 1
        assert stripped == expected_stripped
        assert stripped == first.replace('# --------\n', '')
        assert stripped.count('# note for zulu\n') == 1
        assert '# --------' not in stripped
        assert sort_file(stripped, options=strip_options).new_source == stripped

    def test_mixed_ending_guard_preserves_trailing_owner(self) -> None:
        source = (
            'def charlie():\n'
            '    pass\n'
            '# note for charlie\n'
            '\n'
            'def alpha():\r'
            '    pass\r'
            'def zulu():\r'
            '    pass\r'
        )
        expected = (
            'def alpha():\r'
            '    pass\n'
            'def charlie():\n'
            '    pass\n'
            '# note for charlie\n'
            '\r'
            'def zulu():\r'
            '    pass\r'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        first = sort_file(source, options=options).new_source
        parsed = parse_definitions(first)
        charlie = next(
            definition for definition in parsed.definitions if definition.name == 'charlie'
        )

        assert first == expected
        assert charlie.trailing == '# note for charlie\n'
        assert Counter(first.encode()) == Counter(source.encode())
        compile(first, '<test>', 'exec')
        assert sort_file(first, options=options).new_source == first

    def test_multiple_owners_reuse_and_relocate_guards(self) -> None:
        source = (
            'def bravo():\n'
            '    pass\n'
            '# note for bravo\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            'def charlie():\n'
            '    pass\n'
            'def delta():\n'
            '    pass\n'
            '# note for delta\n'
            '\n'
            'def echo():\n'
            '    pass\n'
        )
        expected = (
            'def alpha():\n'
            '    pass\n'
            'def bravo():\n'
            '    pass\n'
            '# note for bravo\n'
            '\n'
            'def charlie():\n'
            '    pass\n'
            'def delta():\n'
            '    pass\n'
            '# note for delta\n'
            '\n'
            'def echo():\n'
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
        trailing = {definition.name: definition.trailing for definition in parsed.definitions}

        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert trailing['bravo'] == '# note for bravo\n'
        assert trailing['delta'] == '# note for delta\n'
        assert sort_file(result.new_source, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                (
                    'def charlie():\n'
                    '    pass\n'
                    '# note for charlie\n'
                    ' \t\n'
                    'def alpha():\n'
                    '    pass\n'
                    'def zulu():\n'
                    '    pass\n'
                ),
                (
                    'def alpha():\n'
                    '    pass\n'
                    'def charlie():\n'
                    '    pass\n'
                    '# note for charlie\n'
                    ' \t\n'
                    'def zulu():\n'
                    '    pass\n'
                ),
            ),
            (
                (
                    'def charlie():\r\n'
                    '    pass\r\n'
                    '# note for charlie\r\n'
                    ' \t\r\n'
                    'def alpha():\r\n'
                    '    pass\r\n'
                    'def zulu():\r\n'
                    '    pass\r\n'
                ),
                (
                    'def alpha():\r\n'
                    '    pass\r\n'
                    'def charlie():\r\n'
                    '    pass\r\n'
                    '# note for charlie\r\n'
                    ' \t\r\n'
                    'def zulu():\r\n'
                    '    pass\r\n'
                ),
            ),
            (
                (
                    'def charlie():\r'
                    '    pass\r'
                    '# note for charlie\r'
                    ' \t\r'
                    'def alpha():\r'
                    '    pass\r'
                    'def zulu():\r'
                    '    pass\r'
                ),
                (
                    'def alpha():\r'
                    '    pass\r'
                    'def charlie():\r'
                    '    pass\r'
                    '# note for charlie\r'
                    ' \t\r'
                    'def zulu():\r'
                    '    pass\r'
                ),
            ),
            (
                (
                    'def charlie():\r\n'
                    '    pass\n'
                    '# note for charlie\r'
                    ' \t\r\n'
                    'def alpha():\r'
                    '    pass\r\n'
                    'def zulu():\n'
                    '    pass'
                ),
                (
                    'def alpha():\r'
                    '    pass\r'
                    'def charlie():\r\n'
                    '    pass\n'
                    '# note for charlie\r\n'
                    ' \t\r\n'
                    'def zulu():\n'
                    '    pass'
                ),
            ),
        ],
        ids=['lf', 'crlf', 'lone-cr', 'mixed'],
    )
    def test_physical_endings_and_whitespace_guard_are_preserved(
        self,
        source: str,
        expected: str,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert sort_file(result.new_source, options=options).new_source == expected

    @pytest.mark.parametrize(
        'comment',
        ['#!/usr/bin/env python\n', '# coding: utf-8\n'],
        ids=['shebang-like', 'encoding-like'],
    )
    def test_protected_looking_trailing_comment_is_idempotent(self, comment: str) -> None:
        source = (
            'def zulu():\n'
            '    pass\n'
            f'{comment}'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '# --------\n'
            'def omega():\n'
            '    pass\n'
        )
        expected = (
            'def alpha():\n'
            '    pass\n'
            'def zulu():\n'
            '    pass\n'
            f'{comment}'
            '\n'
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

        first = sort_file(source, options=options).new_source
        second = sort_file(first, options=options).new_source

        assert first == expected
        assert first.count(comment) == 1
        assert second == first
        assert second.count(comment) == 1
