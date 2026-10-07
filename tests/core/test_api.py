import pytest

import defsort._core._api as api_module
import defsort._core._methods as methods_module
from defsort._core import detect_order, sort_file
from defsort._core._api import sort_file_incremental
from defsort._core._layout import LayoutPreservationError
from defsort._core._parse import parse_definitions
from defsort._display._movement import count_moved_split
from defsort._types import (
    BannerMode,
    DetectResult,
    EngineOptions,
    SortPolicy,
    SortScopeOrder,
    is_soft_blocker,
)
from tests._helpers import execute_source

_SRC_DEP = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
_SRC_UNORDERED = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'


class TestDetectDefinitionOrder:
    def test_detect_definition_order_matches_global_detection(self) -> None:
        definitions = parse_definitions(_SRC_UNORDERED, banners='ignore').definitions

        assert api_module.detect_definition_order(definitions, banners='ignore') == DetectResult(
            order='unordered', dep_score=-1.0, alpha_score=-1.0
        )

    def test_detect_definition_order_matches_section_detection(self) -> None:
        source = (
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    return bravo()\n\n\n'
            '# --------\n\n\n'
            'def delta():\n    pass\n\n\n'
            'def charlie():\n    return delta()\n'
        )
        definitions = parse_definitions(source, banners='barriers').definitions

        assert api_module.detect_definition_order(definitions, banners='barriers') == DetectResult(
            order='dependency', dep_score=1.0, alpha_score=-1.0
        )


class TestDetectOrder:
    def test_alphabetical_order(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef charlie():\n    pass\n'
        result = detect_order(src)
        assert result is not None
        assert result.order in ('alphabetical', 'both')

    def test_banner_barriers_drop_sections_without_comparable_pairs(self) -> None:
        src = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        assert detect_order(src, banners='barriers') is None

    def test_banner_barriers_mixed_section_classifications_are_unordered(self) -> None:
        src = (
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    return bravo()\n\n\n'
            '# --------\n\n\n'
            'def charlie():\n    return delta()\n\n\n'
            'def delta():\n    pass\n'
        )

        assert detect_order(src, banners='barriers') == DetectResult(
            order='unordered', dep_score=0.0, alpha_score=0.0
        )

    def test_banner_barriers_multiple_perfect_sections_score_one(self) -> None:
        src = (
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    return bravo()\n\n\n'
            '# --------\n\n\n'
            'def delta():\n    pass\n\n\n'
            'def charlie():\n    return delta()\n'
        )

        assert detect_order(src, banners='barriers') == DetectResult(
            order='dependency', dep_score=1.0, alpha_score=-1.0
        )

    def test_banner_barriers_multiple_reversed_sections_score_minus_one(self) -> None:
        src = (
            'def alpha():\n    return bravo()\n\n\n'
            'def bravo():\n    pass\n\n\n'
            '# --------\n\n\n'
            'def charlie():\n    return delta()\n\n\n'
            'def delta():\n    pass\n'
        )

        assert detect_order(src, banners='barriers') == DetectResult(
            order='alphabetical', dep_score=-1.0, alpha_score=1.0
        )

    def test_banner_barriers_without_banners_preserve_global_scores(self) -> None:
        expected = detect_order(_SRC_UNORDERED, banners='ignore')

        assert detect_order(_SRC_UNORDERED, banners='barriers') == expected
        assert detect_order(_SRC_UNORDERED, banners='strip') == expected

    @pytest.mark.parametrize('mode', ['ignore', 'strip'])
    def test_banner_ignore_and_strip_detect_globally(self, mode: BannerMode) -> None:
        src = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef bravo():\n    pass\n'

        result = detect_order(src, banners=mode)

        assert result is not None
        assert result.order == 'both'

    def test_dependency_order(self) -> None:
        result = detect_order(_SRC_DEP)
        assert result is not None
        assert result.order in ('dependency', 'both')

    def test_detect_ignores_configured_policy(self) -> None:
        src = 'def zed():\n    return 1\n\n\ndef alpha():\n    return 2\n'
        result = detect_order(src)
        assert result is not None
        assert result.order == 'unordered'

    def test_detect_main_sorted_conforming_file_is_unordered(self) -> None:
        src = 'def main():\n    return 1\n\n\ndef zed():\n    return 2\n'
        result = detect_order(src)
        assert result is not None
        assert result.order == 'unordered'

    def test_detect_tracks_eager_main_rule(self) -> None:
        src = 'def main():\n    return 1\n\n\ndef alpha(cb=main):\n    return cb\n'
        result = detect_order(src)
        assert result is not None
        assert result.order == 'dependency'

    def test_no_definitions(self) -> None:
        result = detect_order('x = 1\n')
        assert result is None

    def test_returns_scores(self) -> None:
        assert detect_order(_SRC_DEP) == DetectResult(order='both', dep_score=1.0, alpha_score=1.0)

    def test_too_few_definitions(self) -> None:
        result = detect_order('def only():\n    pass\n')
        assert result is None

    def test_unordered(self) -> None:
        result = detect_order(_SRC_UNORDERED)
        assert result is not None
        assert result.order == 'unordered'


class TestEvaluationOrder:
    @pytest.mark.parametrize('incremental', [False, True])
    @pytest.mark.parametrize('order', ['dependency', 'alphabetical'])
    def test_attrs_classes_reorder_without_changing_fields(
        self, *, incremental: bool, order: SortScopeOrder
    ) -> None:
        prefix = 'import attrs\n\n\n'
        zeta = '@attrs.define(frozen=True)\nclass Zeta:\n    z: int\n    a: int\n'
        alpha = '@attrs.define(frozen=True)\nclass Alpha:\n    y: int\n    b: int\n'
        baseline = prefix + zeta
        source = baseline + '\n\n' + alpha
        expected = prefix + alpha + '\n\n' + zeta
        options = EngineOptions(
            policy=SortPolicy(evaluation_order='allow-reordering', definition_order=order),
            add_future=False,
            remove_future=False,
        )

        result = (
            sort_file_incremental(source, baseline, options=options)
            if incremental
            else sort_file(source, options=options)
        )

        assert result.blocker is None
        assert result.new_source == expected
        assert sort_file(expected, options=options).new_source == expected

    @pytest.mark.parametrize('incremental', [False, True])
    @pytest.mark.parametrize('order', ['dependency', 'alphabetical'])
    @pytest.mark.parametrize('allow_reordering', [False, True])
    @pytest.mark.parametrize(
        'blocks',
        [
            (
                (
                    'events = []\n\n\n'
                    'def _record(function):\n'
                    '    events.append(function.__name__)\n'
                    '    return function\n\n\n'
                ),
                '@_record\ndef zeta():\n    pass\n',
                '@_record\ndef alpha():\n    pass\n',
            ),
            (
                'events = []\n\n\n',
                "def zeta(value=events.append('zeta')):\n    pass\n",
                "def alpha(*, value=events.append('alpha')):\n    pass\n",
            ),
            (
                'events = []\n\n\n',
                "class Zeta:\n    value = events.append('zeta')\n",
                "class Alpha:\n    value = events.append('alpha')\n",
            ),
            (
                (
                    'events = []\n\n\n'
                    'def _record(function):\n'
                    '    events.append(function.__name__)\n'
                    '    return function\n\n\n'
                    'class Example:\n'
                ),
                '    @_record\n    def zeta(self):\n        pass\n',
                '    @_record\n    def alpha(self):\n        pass\n',
            ),
            (
                'events = []\n\n\nclass Example:\n',
                "    def zeta(self, value=events.append('zeta')):\n        pass\n",
                "    def alpha(self, *, value=events.append('alpha')):\n        pass\n",
            ),
        ],
        ids=['decorators', 'defaults', 'class-body', 'method-decorators', 'method-defaults'],
    )
    def test_policy_controls_execution_order(
        self,
        blocks: tuple[str, str, str],
        *,
        incremental: bool,
        order: SortScopeOrder,
        allow_reordering: bool,
    ) -> None:
        prefix, zeta, alpha = blocks
        baseline = prefix + zeta
        source = baseline + '\n\n' + alpha
        expected = prefix + alpha + '\n\n' + zeta if allow_reordering else source
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none' if 'class Example:' in prefix else order,
                method_order=order,
                evaluation_order='allow-reordering' if allow_reordering else 'preserve',
            ),
            add_future=False,
            remove_future=False,
        )

        result = (
            sort_file_incremental(source, baseline, options=options)
            if incremental
            else sort_file(source, options=options)
        )

        assert result.blocker is None
        assert result.new_source == expected
        assert execute_source(source)['events'] == ['zeta', 'alpha']
        assert execute_source(result.new_source)['events'] == (
            ['alpha', 'zeta'] if allow_reordering else ['zeta', 'alpha']
        )
        assert sort_file(expected, options=options).new_source == expected
        if incremental:
            repeated = sort_file_incremental(expected, baseline, options=options)
            assert repeated.new_source == expected


class TestIncrementalFutureAnnotations:
    # `worker` annotates against `main`, and `main` is anchored last, so the
    # annotation can never be evaluated at definition time. Both sort modes must
    # add the postponed-annotations import.
    ANCHORED_MAIN = 'def worker(value: main):\n    return value\n\n\ndef main():\n    return 1\n'
    FUTURE_IMPORT = 'from __future__ import annotations'

    def _options(self) -> EngineOptions:
        return EngineOptions(policy=SortPolicy(), add_future=True, remove_future=True)

    def test_adds_future_import_when_no_definitions_are_new(self) -> None:
        result = sort_file_incremental(
            self.ANCHORED_MAIN,
            self.ANCHORED_MAIN,
            options=self._options(),
        )
        assert self.FUTURE_IMPORT in (result.new_source or self.ANCHORED_MAIN)

    def test_adds_future_import_when_only_methods_move(self) -> None:
        baseline = (
            'def worker(value: main):\n'
            '    return value\n'
            '\n'
            '\n'
            'class Thing:\n'
            '    def beta(self) -> None: ...\n'
            '\n'
            '\n'
            'def main():\n'
            '    return 1\n'
        )
        source = baseline.replace(
            '    def beta(self) -> None: ...\n',
            '    def beta(self) -> None: ...\n\n    def alpha(self) -> None: ...\n',
        )
        result = sort_file_incremental(source, baseline, options=self._options())
        assert self.FUTURE_IMPORT in (result.new_source or source)

    def test_agrees_with_full_sort(self) -> None:
        options = self._options()
        full = sort_file(self.ANCHORED_MAIN, options=options)
        incremental = sort_file_incremental(
            self.ANCHORED_MAIN,
            self.ANCHORED_MAIN,
            options=options,
        )
        full_source = full.new_source or self.ANCHORED_MAIN
        incremental_source = incremental.new_source or self.ANCHORED_MAIN
        assert (self.FUTURE_IMPORT in full_source) == (self.FUTURE_IMPORT in incremental_source)


class TestIncrementalMovementReporting:
    def test_reordered_file_reports_nonzero_moved(self) -> None:
        baseline = 'def bravo():\n    pass\n'
        source = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_file_incremental(
            source,
            baseline,
            options=EngineOptions(policy=SortPolicy(definition_order='alphabetical')),
        )
        assert result.new_source != source
        assert [definition.name for definition in result.new_order] == ['alpha', 'bravo']
        top_moved, _methods_moved = count_moved_split(result)
        assert top_moved > 0

    def test_soft_blocker_with_method_sort_does_not_raise(self) -> None:
        baseline = (
            'def zulu():\n    pass\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'class Foo:\n    def zed(self):\n        pass\n'
        )
        source = (
            'def zulu():\n    pass\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'class Foo:\n    def zed(self):\n        pass\n\n'
            '    def aaa(self):\n        pass\n'
        )
        result = sort_file_incremental(
            source,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                )
            ),
        )
        assert result.blocker is not None
        assert is_soft_blocker(result.blocker)
        top_moved, methods_moved = count_moved_split(result)
        assert top_moved == 0
        assert methods_moved > 0

    def test_unchanged_file_reports_zero_moved(self) -> None:
        source = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        result = sort_file_incremental(
            source,
            source,
            options=EngineOptions(policy=SortPolicy(definition_order='alphabetical')),
        )
        top_moved, _methods_moved = count_moved_split(result)
        assert top_moved == 0


class TestRestrictedBannerPatternBoundary:
    def test_incremental_sort_rejects_mutated_banner_pattern_when_banners_ignored(self) -> None:
        policy = SortPolicy(banners='ignore')
        object.__setattr__(policy, 'banner_pattern', '^# (a+)+$')
        baseline = 'def bravo():\n    return alpha()\n'
        source = _SRC_UNORDERED

        with pytest.raises(ValueError, match='banner_pattern'):
            sort_file_incremental(
                source,
                baseline,
                options=EngineOptions(policy=policy, add_future=False),
            )

        assert baseline == 'def bravo():\n    return alpha()\n'
        assert source == _SRC_UNORDERED


def test_incremental_hoist_layout_refusal_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _SRC_DEP.replace('\n', '\r\n')

    def refuse_layout(*_args: object, **_kwargs: object) -> str:
        raise LayoutPreservationError('required ownership guard has no candidate')

    monkeypatch.setattr(api_module, 'reassemble_source', refuse_layout)
    result = sort_file_incremental(
        source, source, options=EngineOptions(policy=SortPolicy(), hoist=True)
    )

    assert result.skipped
    assert result.blocker == 'layout'
    assert result.original_source == result.new_source == source
    assert [definition.name for definition in result.original_order] == ['alpha', 'bravo']
    assert result.new_order == result.original_order
    assert not result.method_sorts


def test_incremental_hoist_unrelated_layout_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed_layout(*_args: object, **_kwargs: object) -> str:
        raise ValueError('malformed layout')

    monkeypatch.setattr(api_module, 'reassemble_source', malformed_layout)
    with pytest.raises(ValueError, match='malformed layout'):
        sort_file_incremental(
            _SRC_DEP, _SRC_DEP, options=EngineOptions(policy=SortPolicy(), hoist=True)
        )


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize(
    ('policy', 'expected_names'),
    [
        (SortPolicy(method_order='alphabetical'), ('alpha', 'beta', 'zeta')),
        (SortPolicy(method_order='none', banners='strip'), ('beta', 'zeta', 'alpha')),
        (SortPolicy(method_bodies='ignore'), ('alpha', 'beta', 'zeta')),
        (SortPolicy(method_bodies='within-categories'), ('zeta', 'beta', 'alpha')),
        (SortPolicy(method_bodies='across-categories'), ('zeta', 'beta', 'alpha')),
    ],
    ids=['alphabetical', 'none-strip', 'dependency-ignore', 'within', 'across'],
)
def test_method_body_collection_follows_policy(
    monkeypatch: pytest.MonkeyPatch,
    policy: SortPolicy,
    expected_names: tuple[str, ...],
    incremental: bool,
) -> None:
    methods = {
        'beta': '    def beta(self):\n        return self.zeta()\n',
        'zeta': '    def zeta(self):\n        return 1\n',
        'alpha': '    def alpha(self):\n        return self.beta()\n',
    }
    prefix = 'class C:\n    # ----------\n\n'
    function = 'def aardvark():\n    return C()\n'
    source = function + '\n\n' + prefix + '\n'.join(methods.values())
    baseline = prefix + methods['zeta']
    if policy.method_order != 'dependency' or policy.method_bodies == 'ignore':

        def fail_body_collection(*_args: object, **_kwargs: object) -> set[str]:
            pytest.fail('policy-unused method body was analyzed')

        monkeypatch.setattr(methods_module, 'collect_body_refs', fail_body_collection)

    options = EngineOptions(policy=policy, add_future=False, remove_future=False)
    result = (
        sort_file_incremental(source, baseline, options=options)
        if incremental
        else sort_file(source, options=options)
    )

    expected_prefix = 'class C:\n\n' if policy.banners == 'strip' else prefix
    assert result.new_source == (
        expected_prefix + '\n'.join(methods[name] for name in expected_names) + '\n\n' + function
    )
    assert [definition.name for definition in result.original_order] == ['aardvark', 'C']
    assert [definition.name for definition in result.new_order] == ['C', 'aardvark']
    assert result.blocker is None
    assert result.class_blockers == ()
    assert result.banner_constraints == []
    assert not result.skipped
    assert result.total_methods == 3
    if policy.method_order == 'none':
        assert result.method_sorts == []
    else:
        assert len(result.method_sorts) == 1
        movement = result.method_sorts[0]
        assert movement.class_name == 'C'
        assert [method.name for method in movement.original_order] == ['beta', 'zeta', 'alpha']
        assert tuple(method.name for method in movement.new_order) == expected_names
        first_line = 4 if incremental else 8
        assert [method.start_line for method in movement.original_order] == [
            first_line,
            first_line + 3,
            first_line + 6,
        ]
        assert [method.section for method in movement.original_order] == [1, 1, 1]
