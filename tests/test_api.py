import ast
import dataclasses
import importlib
import inspect
import re
import sys
import typing
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import defsort
from defsort import (
    BannerMode,
    DefsortConfig,
    DetectResult,
    EvaluationOrder,
    SortScopeOrder,
    check,
    detect,
    sort,
)
from defsort._types import BANNER_MODE_VALUES, ClassBlocker
from tests._helpers import _ENV, decode_text, execute_source, process_transcript, run_setup

_API_INEFFECTIVE_CASES = [
    (
        'method-bodies',
        DefsortConfig(
            method_order='alphabetical',
            method_bodies='across-categories',
        ),
    ),
    (
        'tie-break',
        DefsortConfig(
            definition_order='alphabetical',
            method_order='alphabetical',
            tie_break='source',
        ),
    ),
    (
        'method-visibility',
        DefsortConfig(
            method_order='none',
            method_visibility='public-first',
        ),
    ),
    (
        'method-dunders',
        DefsortConfig(
            method_order='none',
            method_dunders='after-regular',
        ),
    ),
    ('main', DefsortConfig(definition_order='none', main='sorted')),
]
_BANNER_PATTERN_ERROR_PREFIX = 'banner_pattern is not a valid regular expression: '
_EXPECTED_PUBLIC_EXPORTS = frozenset(
    {
        'BannerMode',
        'Blocker',
        'ClassBlocker',
        'DefsortConfig',
        'DefsortConfigWarning',
        'DetectResult',
        'EvaluationOrder',
        'MainPlacement',
        'MethodBodies',
        'MethodDunders',
        'MethodVisibility',
        'OrderKind',
        'SortCheckResult',
        'SortScopeOrder',
        'SortStatus',
        'TieBreak',
        'check',
        'clear_config_cache',
        'detect',
        'resolve_config',
        'sort',
    }
)

# Source snippets used across check() and sort() tests.
# Not valid Python.
_INVALID = 'def alpha(\n'
# Class with methods in wrong alphabetical order (init before bravo, but
# bravo before alpha). sort() should sort methods within the class.
_METHODS_UNSORTED = (
    'class Foo:\n'
    '    def __init__(self) -> None:\n        pass\n\n'
    '    def bravo(self) -> None:\n        pass\n\n'
    '    def alpha(self) -> None:\n        pass\n'
)
# Out of order -- bravo calls alpha but is defined first.
_NEEDS_SORT = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
# Source containing null bytes -- ast.parse raises SyntaxError (Python 3.13+).
_NULL_BYTES = 'def alpha():\n    pass\n\x00'
# Interstitial code between definitions -- safety check blocks sort.
_SKIPPED = (
    'def bravo(alpha: None) -> None:\n    pass\n\n'
    'registry: dict[str, object] = {}\n\n'
    'def alpha():\n    pass\n'
)
# Already in dependency order (bravo calls alpha, so alpha must come first).
_SORTED = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
# Already in correct order with interstitial code; no sorting is needed.
_SORTED_WITH_INTERSTITIAL = (
    'def alpha():\n    pass\n\n'
    'registry: dict[str, object] = {}\n\n'
    'def bravo():\n    return alpha()\n'
)
_SRC_DUP_METHODS = """class C:
    def helper(self):
        return 1

    def foo(self, cb=helper):
        return cb

    def bar(self):
        return 2

    def foo(self):
        return 3
"""
_SRC_DUP_PROPERTY_BINDERS = """class C:
    @property
    def x(self):
        return 1

    @x.setter
    def x(self, value):
        pass

    @property
    def x(self):
        return 2
"""
_SRC_INIT_BODY_API = """class C:
    def __init__(self):
        self.load()

    def load(self):
        return 1
"""
_SRC_WITH_CLASS = (
    'class Bar:\n'
    '    def bravo(self) -> None:\n        pass\n\n'
    '    def alpha(self) -> None:\n        pass\n'
    '\n\n'
    'def zeta():\n    pass\n\n\n'
    'def able():\n    pass\n'
)
# Source containing a lone Unicode surrogate -- ast.parse raises ValueError.
_SURROGATE = 'def alpha():\n    pass\n\ud800'


class TestBlockerMessagePopulation:
    def test_changed_and_unchanged_populate_neither(self) -> None:
        for src in (_NEEDS_SORT, defsort.sort(_NEEDS_SORT)):
            result = defsort.check(src)
            assert result.blocker is None
            assert result.blocker_message is None

    def test_class_blockers_carry_messages(self) -> None:
        result = defsort.check(_SRC_DUP_METHODS)
        assert result.status == 'skipped'
        assert result.class_blockers[0].message == 'non-consecutive duplicate definitions'

    def test_full_sort_skip_carries_blocker_message(self) -> None:
        result = defsort.check(_SKIPPED)
        assert result.status == 'skipped'
        assert result.blocker == 'interstitial'
        assert result.blocker_message == 'code between definitions'

    def test_incremental_skip_carries_blocker_message(self) -> None:
        baseline = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        current = baseline + '\n\ndef charlie():\n    pass\n'
        result = defsort.check(current, baseline=baseline)
        assert result.status == 'skipped'
        assert result.blocker == 'non_conforming'
        assert result.blocker_message == 'baseline not in target order'


class TestCheck:
    def test_alphabetical(self) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = defsort.check(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert result.status == 'changed'

    def test_banner_constraint_boolean_for_changed_unchanged_and_skipped(self) -> None:
        unchanged = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        changed = unchanged.replace(
            'def zulu():\n    pass\n',
            'def zulu():\n    pass\n\n\ndef yankee():\n    pass\n',
        ).replace(
            'def alpha():\n    pass\n',
            'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
        )
        skipped = changed.replace(
            'def zulu():\n    pass\n\n\ndef yankee',
            'def zulu():\n    pass\n\nregistry = {}\n\ndef yankee',
        )
        options = DefsortConfig(
            definition_order='alphabetical',
            method_order='none',
        )

        assert check(unchanged, config=options).banner_constrained is True
        assert check(changed, config=options).banner_constrained is True
        assert check(skipped, config=options).banner_constrained is True

    def test_banner_constraint_boolean_is_false_for_disabled_scope(self) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = check(
            source,
            config=DefsortConfig(
                definition_order='none',
                method_order='none',
            ),
        )

        assert result.banner_constrained is False

    def test_banner_default_preserves_source_analysis_statuses(self) -> None:
        options = defsort.DefsortConfig(banner_pattern=r'# CUSTOM')
        invalid = defsort.check(_INVALID, config=options)
        skipped = defsort.check(_SKIPPED, config=options)

        assert invalid.status == 'invalid'
        assert skipped.status == 'skipped'

    def test_banner_strip_only_change_is_reported(self) -> None:
        source = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = defsort.check(
            source,
            config=defsort.DefsortConfig(
                definition_order='none',
                method_order='none',
                banners='strip',
            ),
        )

        assert result.status == 'changed'

    @pytest.mark.parametrize('source', ['break\n', 'nonlocal name\n'])
    def test_compile_invalid_syntax(self, source: str) -> None:
        assert defsort.check(source).status == 'invalid'

    def test_does_not_remove_future_import(self) -> None:
        src = (
            'from __future__ import annotations\n\n\n'
            'def bravo(x: Alpha) -> None:\n    pass\n\n\n'
            'class Alpha:\n    pass\n'
        )
        result = defsort.check(src)
        assert result.status in ('unchanged', 'changed')
        sorted_src = defsort.sort(src)
        assert 'from __future__ import annotations' in sorted_src

    def test_first_definition_header_like_body_comment_remains_valid(self) -> None:
        source = (
            'def zebra():\n'
            '    # encoding: utf-8 assumed\n'
            '    return 1\n'
            '\n\n'
            'def apple():\n'
            '    return 2\n'
        )

        output = sort(source)

        ast.parse(output)
        assert output.count('# encoding: utf-8 assumed') == 1
        assert check(source).status == 'changed'

    def test_force_bypasses_skip(self) -> None:
        result = defsort.check(_SKIPPED, config=DefsortConfig(force=True))
        assert result.status == 'changed'

    def test_force_does_not_bypass_forward_annotation_check(self) -> None:
        src = 'class Bravo:\n    pass\n\n\nclass Alpha:\n    x: Bravo = None\n'
        result = defsort.check(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
                force=True,
            ),
        )
        assert result.status == 'skipped'

    def test_force_does_not_bypass_top_level_duplicates(self) -> None:
        src = 'def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef a():\n    return 3\n'
        result = defsort.check(src, config=DefsortConfig(force=True))
        assert result.status == 'skipped'
        assert result.blocker == 'duplicates'

    def test_invalid_lone_surrogate(self) -> None:
        result = defsort.check(_SURROGATE)
        assert result.status == 'invalid'

    def test_invalid_null_bytes(self) -> None:
        result = defsort.check(_NULL_BYTES)
        assert result.status == 'invalid'

    def test_invalid_syntax(self) -> None:
        result = defsort.check(_INVALID)
        assert result.status == 'invalid'
        assert result.blocker is None

    def test_method_sort_detected(self) -> None:
        result = defsort.check(
            _METHODS_UNSORTED,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert result.status == 'changed'

    def test_needs_sort(self) -> None:
        result = defsort.check(_NEEDS_SORT)
        assert result.status == 'changed'

    def test_skipped_nonconsecutive_duplicates(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = defsort.check(src)
        assert result.status == 'skipped'
        assert result.blocker == 'duplicates'

    def test_skipped_same_name_different_kind(self) -> None:
        # def Foo and class Foo share the same name but differ by kind.
        # Alphabetical mode puts classes before functions, so a sort
        # is needed -- but interstitial code blocks it. check() must
        # return SKIPPED (not SORTED) despite the name lists matching.
        src = 'def Foo() -> None:\n    pass\n\nx = 1\n\nclass Foo:\n    pass\n'
        result = defsort.check(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert result.status == 'skipped'
        assert result.blocker is not None

    def test_sorted(self) -> None:
        result = defsort.check(_SORTED)
        assert result.status == 'unchanged'
        assert result.blocker is None

    def test_sorted_with_interstitial_returns_sorted(self) -> None:
        result = defsort.check(_SORTED_WITH_INTERSTITIAL)
        assert result.status == 'unchanged'
        assert result.blocker is None
        assert defsort.sort(_SORTED_WITH_INTERSTITIAL) == _SORTED_WITH_INTERSTITIAL


class TestCheckBaseline:
    def test_banner_constraint_boolean_survives_incremental_noop(self) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = check(
            source,
            baseline=source,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='none',
            ),
        )

        assert result.status == 'unchanged'
        assert result.banner_constrained is True

    def test_compile_invalid_baseline_is_non_conforming(self) -> None:
        baseline = 'return 1\n\n\ndef zulu():\n    pass\n'
        current = 'def zulu():\n    pass\n\n\ndef alpha():\n    pass\n'

        result = check(current, baseline=baseline)

        assert result.status == 'skipped'
        assert result.blocker == 'non_conforming'
        assert result.blocker_message == 'baseline not in target order'

    def test_invalid_syntax(self) -> None:
        baseline = 'def alpha():\n    return 1\n'
        result = defsort.check('def broken(', baseline=baseline)
        assert result.status == 'invalid'

    def test_needs_sort_with_new_defs(self) -> None:
        baseline = 'def alpha():\n    return 1\n\n\ndef charlie():\n    return alpha()\n'
        current = baseline + '\n\ndef bravo():\n    return alpha()\n'
        result = defsort.check(current, baseline=baseline)
        assert result.status == 'changed'

    def test_skipped_non_conforming(self) -> None:
        bad_baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        current = bad_baseline + '\n\ndef charlie():\n    pass\n'
        result = defsort.check(current, baseline=bad_baseline)
        assert result.status == 'skipped'
        assert result.blocker is not None

    def test_sorted_no_new_defs(self) -> None:
        src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        result = defsort.check(src, baseline=src)
        assert result.status == 'unchanged'


class TestCheckWithScopes:
    def test_definition_order_none_unchanged(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = defsort.check(
            src,
            config=DefsortConfig(definition_order='none'),
        )
        assert result.status == 'unchanged'

    def test_method_order_alphabetical(self) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
        )
        result = defsort.check(
            src,
            config=DefsortConfig(method_order='alphabetical'),
        )
        assert result.status == 'changed'


class TestClassBlockerScope:
    @pytest.mark.parametrize('incremental', [False, True])
    def test_interstitial_class_skip_has_no_file_blocker(self, incremental: bool) -> None:
        baseline = 'class C:\n    def z(self): pass\n    x = 1\n'
        source = baseline + '    def a(self): pass\n'
        config = DefsortConfig(method_order='alphabetical')

        result = defsort.check(source, baseline=baseline if incremental else None, config=config)

        assert result.status == 'skipped'
        assert result.blocker is None
        assert result.blocker_message is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='interstitial',
                message='code between definitions',
            ),
        )
        assert (
            defsort.sort(source, baseline=baseline if incremental else None, config=config)
            == source
        )


class TestDefsortConfigContract:
    @pytest.mark.parametrize('function', [sort, check])
    @pytest.mark.parametrize(
        'case',
        [
            (
                'definition_order',
                'bogus',
                ValueError,
                (
                    "definition_order must be one of 'dependency', 'alphabetical', 'none', "
                    "got 'bogus'"
                ),
                None,
            ),
            (
                'evaluation_order',
                'bogus',
                ValueError,
                ("evaluation_order must be one of 'preserve', 'allow-reordering', got 'bogus'"),
                None,
            ),
            (
                'method_order',
                'bogus',
                ValueError,
                "method_order must be one of 'dependency', 'alphabetical', 'none', got 'bogus'",
                None,
            ),
            ('force', 'yes', TypeError, 'force must be a bool, got str', None),
            (
                'banners',
                'bogus',
                ValueError,
                "banners must be one of 'barriers', 'ignore', 'strip', got 'bogus'",
                None,
            ),
            (
                'banner_pattern',
                5,
                TypeError,
                'banner_pattern must be a string or None, got int',
                None,
            ),
            (
                'banner_pattern',
                '[',
                ValueError,
                _BANNER_PATTERN_ERROR_PREFIX,
                re.error,
            ),
            (
                'method_bodies',
                'bogus',
                ValueError,
                (
                    "method_bodies must be one of 'within-categories', 'ignore', "
                    "'across-categories', got 'bogus'"
                ),
                None,
            ),
            (
                'method_visibility',
                'bogus',
                ValueError,
                "method_visibility must be one of 'mixed', 'public-first', got 'bogus'",
                None,
            ),
            (
                'method_dunders',
                'bogus',
                ValueError,
                "method_dunders must be one of 'before-regular', 'after-regular', got 'bogus'",
                None,
            ),
            (
                'main',
                'bogus',
                ValueError,
                "main must be one of 'last', 'sorted', got 'bogus'",
                None,
            ),
            (
                'tie_break',
                'bogus',
                ValueError,
                "tie_break must be one of 'alphabetical', 'source', got 'bogus'",
                None,
            ),
        ],
    )
    def test_operations_validate_before_source_parsing(
        self,
        function: Callable[..., object],
        case: tuple[
            str,
            object,
            type[Exception],
            str,
            type[BaseException] | None,
        ],
    ) -> None:
        # `message` is the whole message for unchained cases and the stable
        # prefix for chained ones, where the cause supplies the remainder.
        field_name, bad_value, exc, message, cause = case
        cfg = DefsortConfig()
        object.__setattr__(cfg, field_name, bad_value)
        with pytest.raises(exc) as excinfo:
            function('def broken(', config=cfg)
        chained = excinfo.value.__cause__
        if cause is None:
            assert chained is None
            assert str(excinfo.value) == message
        else:
            assert isinstance(chained, cause)
            assert str(excinfo.value) == f'{message}{chained}'


class TestDetect:
    def test_compile_invalid_syntax_raises(self) -> None:
        with pytest.raises(SyntaxError, match="'return' outside function"):
            detect('return 1\n')

    def test_dependency_order(self) -> None:
        src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        result = detect(src)
        assert result is not None
        assert isinstance(result, DetectResult)
        assert result.order in ('dependency', 'both')
        assert result.dep_score == 1.0

    def test_returns_none_for_single_def(self) -> None:
        result = detect('def only():\n    pass\n')
        assert result is None

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(SyntaxError):
            detect('def broken(\n')


class TestDetectValidation:
    def test_bad_pattern_raises_value_error_with_cause(self) -> None:
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            detect('def broken(', banner_pattern='[')
        cause = excinfo.value.__cause__
        assert isinstance(cause, re.error)
        assert str(excinfo.value) == f'{_BANNER_PATTERN_ERROR_PREFIX}{cause}'

    def test_invalid_banners_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match='banners') as excinfo:
            detect('def broken(', banners=cast('BannerMode', 'bogus'))
        assert str(excinfo.value) == (
            "banners must be one of 'barriers', 'ignore', 'strip', got 'bogus'"
        )
        assert excinfo.value.__cause__ is None

    def test_non_string_pattern_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match='banner_pattern') as excinfo:
            detect('def broken(', banner_pattern=cast('str', 5))
        assert str(excinfo.value) == 'banner_pattern must be a string or None, got int'
        assert excinfo.value.__cause__ is None


class TestDuplicateMethodGuard:
    def test_blocked_class_with_other_changes_reports_changed(self) -> None:
        src = (
            _SRC_DUP_METHODS
            + '\n\ndef zed():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )
        result = defsort.check(src)
        assert result.status == 'changed'
        assert result.class_blockers[0].class_name == 'C'

    @pytest.mark.parametrize('incremental', [False, True])
    def test_check_reports_class_blocker(self, incremental: bool) -> None:
        result = defsort.check(
            _SRC_DUP_METHODS, baseline=_SRC_DUP_METHODS if incremental else None
        )
        assert result.status == 'skipped'
        assert result.blocker is None
        assert result.blocker_message is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_duplicate_class_is_not_reordered(self) -> None:
        assert defsort.sort(_SRC_DUP_METHODS) == _SRC_DUP_METHODS

    def test_duplicate_property_binders_are_not_reordered(self) -> None:
        result = defsort.check(_SRC_DUP_PROPERTY_BINDERS)

        assert result.status == 'skipped'
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )
        assert defsort.sort(_SRC_DUP_PROPERTY_BINDERS) == _SRC_DUP_PROPERTY_BINDERS

    def test_force_does_not_bypass_method_duplicates(self) -> None:
        result = defsort.check(_SRC_DUP_METHODS, config=DefsortConfig(force=True))
        assert result.status == 'skipped'
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_method_order_none_leaves_duplicates_alone(self) -> None:
        opts = DefsortConfig(method_order='none')
        result = defsort.check(_SRC_DUP_METHODS, config=opts)
        assert result.class_blockers == ()


class TestEmptyBaselineParity:
    @pytest.mark.parametrize('baseline', [None, '', ' \n\t'])
    def test_check_banner_constraint_noop_matches_full(self, baseline: str | None) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        options = DefsortConfig(
            definition_order='alphabetical',
            method_order='none',
        )

        result = check(source, baseline=baseline, config=options)

        assert result == check(source, config=options)
        assert result.status == 'unchanged'
        assert result.blocker is None
        assert result.banner_constrained is True

    @pytest.mark.parametrize('baseline', [None, '', ' \n\t'])
    def test_check_blocker_matches_full(self, baseline: str | None) -> None:
        result = check(_SKIPPED, baseline=baseline)

        assert result == check(_SKIPPED)
        assert result.status == 'skipped'
        assert result.blocker == 'interstitial'

    @pytest.mark.parametrize('baseline', [None, '', ' \n\t'])
    def test_check_compile_invalid_matches_full(self, baseline: str | None) -> None:
        assert check('return 1\n', baseline=baseline).status == 'invalid'

    @pytest.mark.parametrize('baseline', [None, '', ' \n\t'])
    def test_sort_compile_invalid_matches_full(self, baseline: str | None) -> None:
        with pytest.raises(SyntaxError, match="'return' outside function"):
            sort('return 1\n', baseline=baseline)

    @pytest.mark.parametrize('baseline', [None, '', ' \n\t'])
    def test_sort_output_matches_full(self, baseline: str | None) -> None:
        assert sort(_NEEDS_SORT, baseline=baseline) == sort(_NEEDS_SORT)


class TestHardBlockerLeak:
    BASELINE = (
        'class Foo:\n'
        '    def alpha(self):\n'
        '        pass\n\n'
        '    def delta(self):\n'
        '        pass\n\n\n'
        'def a():\n'
        '    pass\n\n\n'
        'def b():\n'
        '    pass\n'
    )
    SOURCE = (
        'class Foo:\n'
        '    def alpha(self):\n'
        '        pass\n\n'
        '    def delta(self):\n'
        '        pass\n\n'
        '    def beta(self):\n'
        '        pass\n\n\n'
        'def a():\n'
        '    pass\n\n'
        'x = 1\n\n'
        'def b():\n'
        '    pass\n\n\n'
        'def c():\n'
        '    pass\n'
    )

    def test_check_reports_skipped(self) -> None:
        result = check(self.SOURCE, baseline=self.BASELINE)
        assert result.status == 'skipped'
        assert result.blocker is not None

    def test_sort_returns_original(self) -> None:
        result = sort(self.SOURCE, baseline=self.BASELINE)
        assert result == self.SOURCE


class TestIndependentScopes:
    def test_both_none_is_noop(self) -> None:
        result = sort(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='none',
                method_order='none',
            ),
        )
        assert result == _SRC_WITH_CLASS

    def test_check_def_none_method_changed(self) -> None:
        result = check(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='none',
                method_order='alphabetical',
            ),
        )
        assert result.status == 'changed'

    def test_def_alpha_method_dep(self) -> None:
        result = sort(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='dependency',
            ),
        )
        # Defs alphabetical: classes first (Bar), then functions (able, zeta)
        assert result.index('class Bar') < result.index('def able')
        assert result.index('def able') < result.index('def zeta')

    def test_def_alpha_method_none(self) -> None:
        result = sort(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='none',
            ),
        )
        # Defs alphabetical: classes first (Bar), then functions (able, zeta)
        assert result.index('class Bar') < result.index('def able')
        # Methods unchanged: bravo before alpha
        assert result.index('def bravo') < result.index('def alpha')

    def test_def_dep_method_alpha(self) -> None:
        result = sort(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='dependency',
                method_order='alphabetical',
            ),
        )
        # Defs in dependency order (no deps, so alphabetical tiebreak)
        assert result.index('class Bar') < result.index('def zeta')
        # Methods alphabetically sorted
        assert result.index('def alpha') < result.index('def bravo')

    def test_def_none_method_alpha(self) -> None:
        result = sort(
            _SRC_WITH_CLASS,
            config=DefsortConfig(
                definition_order='none',
                method_order='alphabetical',
            ),
        )
        # Top-level unchanged: Bar, zeta, able
        assert result.index('class Bar') < result.index('def zeta')
        assert result.index('def zeta') < result.index('def able')
        # Methods sorted
        assert result.index('def alpha') < result.index('def bravo')

    def test_def_none_skips_safety_checks(self) -> None:
        src_interstitial = (
            'def bravo():\n    pass\n\n'
            'registry = {}\n\n'
            'class Foo:\n'
            '    def z(self) -> None:\n        pass\n\n'
            '    def a(self) -> None:\n        pass\n'
            '\n\n'
            'def alpha():\n    pass\n'
        )
        result = sort(
            src_interstitial,
            config=DefsortConfig(
                definition_order='none',
                method_order='alphabetical',
            ),
        )
        # Definitions unchanged (interstitial code present but NONE skips)
        assert result.index('def bravo') < result.index('registry')
        # Methods still sorted
        assert result.index('def a') < result.index('def z')

    def test_incremental_def_none_methods_still_sort(self) -> None:
        baseline = (
            'class Foo:\n'
            '    def a(self) -> None:\n        pass\n\n'
            '    def z(self) -> None:\n        pass\n'
        )
        current = (
            'class Foo:\n'
            '    def a(self) -> None:\n        pass\n\n'
            '    def z(self) -> None:\n        pass\n\n'
            '    def m(self) -> None:\n        pass\n'
            '\n\n'
            'def new_func():\n    pass\n'
        )
        result = sort(
            current,
            baseline=baseline,
            config=DefsortConfig(
                definition_order='none',
                method_order='alphabetical',
            ),
        )
        # new_func stays where user placed it (no top-level merge)
        assert result.index('class Foo') < result.index('def new_func')
        # Methods sorted alphabetically (new method 'm' inserted between a and z)
        assert result.index('def a') < result.index('def m')
        assert result.index('def m') < result.index('def z')


class TestLazyFacade:
    def test_dir_lists_every_export(self) -> None:
        assert set(defsort.__all__) <= set(dir(defsort))
        assert defsort.__dir__() == dir(defsort)

    def test_exports_are_library_objects(self) -> None:
        library = importlib.import_module('defsort._library')
        assert list(library.__all__) == list(defsort.__all__)
        for name in defsort.__all__:
            assert getattr(defsort, name) is getattr(library, name)

    def test_first_access_in_fresh_interpreter_loads_library_lazily(self) -> None:
        script = (
            'import sys\n'
            'import defsort\n'
            "assert 'defsort._library' not in sys.modules, 'library loaded at import'\n"
            "assert 'defsort._core' not in sys.modules, 'engine loaded at import'\n"
            "assert not hasattr(defsort, '_cli')\n"
            'from defsort import _downstream\n'
            "assert 'defsort._library' not in sys.modules, 'library loaded by probes'\n"
            'for name in defsort.__all__:\n'
            '    getattr(defsort, name)\n'
            "assert 'defsort._library' in sys.modules\n"
        )
        result = run_setup([sys.executable, '-c', script], env=_ENV, check=False)
        assert result.returncode == 0, process_transcript(result)

    def test_library_import_failure_surfaces_on_first_access(self) -> None:
        script = (
            'import sys\n'
            "sys.modules['defsort._library'] = None\n"
            'import defsort\n'
            'try:\n'
            '    defsort.sort\n'
            'except ImportError:\n'
            '    sys.exit(0)\n'
            'sys.exit(1)\n'
        )
        result = run_setup([sys.executable, '-c', script], env=_ENV, check=False)
        assert result.returncode == 0, process_transcript(result)

    def test_unexported_name_is_not_an_attribute(self) -> None:
        name = 'validate_config'
        with pytest.raises(AttributeError):
            getattr(defsort, name)
        with pytest.raises(AttributeError):
            defsort.__getattr__(name)


class TestPublicAPI:
    def test_clear_config_cache_importable(self) -> None:
        assert callable(defsort.clear_config_cache)


class TestPublicBannerConfiguration:
    BUILTIN_SOURCE = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
    CUSTOM_SOURCE = 'def bravo():\n    pass\n\n\n# CUSTOM\n\n\ndef alpha():\n    pass\n'

    def test_defaults_treat_builtin_banners_as_barriers(self) -> None:
        assert sort(self.BUILTIN_SOURCE) == self.BUILTIN_SOURCE
        assert check(self.BUILTIN_SOURCE).status == 'unchanged'
        assert detect(self.BUILTIN_SOURCE) is None

    def test_explicit_ignore_mode_is_global(self) -> None:
        options = DefsortConfig(banners='ignore')
        sorted_source = sort(self.BUILTIN_SOURCE, config=options)
        detection = detect(self.BUILTIN_SOURCE, banners='ignore')

        assert sorted_source.index('def alpha') < sorted_source.index('def bravo')
        assert check(self.BUILTIN_SOURCE, config=options).status == 'changed'
        assert detection is not None
        assert detection.order == 'unordered'

    def test_explicit_pattern_is_used_by_every_public_mode(self) -> None:
        pattern = r'# CUSTOM'
        options = DefsortConfig(banner_pattern=pattern)

        assert sort(self.CUSTOM_SOURCE, config=options) == self.CUSTOM_SOURCE
        assert check(self.CUSTOM_SOURCE, config=options).status == 'unchanged'
        assert detect(self.CUSTOM_SOURCE, banner_pattern=pattern) is None

    def test_incremental_calls_receive_banner_pattern(self) -> None:
        baseline = 'def bravo():\n    pass\n\n\n# CUSTOM\n\n\ndef charlie():\n    pass\n'
        source = baseline + '\n\ndef alpha():\n    pass\n'
        options = DefsortConfig(
            definition_order='alphabetical',
            banner_pattern=r'# CUSTOM',
        )

        sorted_source = sort(source, baseline=baseline, config=options)
        ignored_source = sort(
            source,
            baseline=baseline,
            config=dataclasses.replace(options, banners='ignore'),
        )

        assert sorted_source.index('def bravo') < sorted_source.index('# CUSTOM')
        assert sorted_source.index('# CUSTOM') < sorted_source.index('def alpha')
        assert sorted_source.index('def alpha') < sorted_source.index('def charlie')
        assert check(source, baseline=baseline, config=options).status == 'changed'
        assert ignored_source.index('def alpha') < ignored_source.index('def bravo')

    @pytest.mark.parametrize('mode', BANNER_MODE_VALUES)
    def test_invalid_detect_pattern_precedes_source_parsing(self, mode: BannerMode) -> None:
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            detect('def broken(', banners=mode, banner_pattern='[')
        cause = excinfo.value.__cause__
        assert isinstance(cause, re.error)
        assert str(excinfo.value) == f'{_BANNER_PATTERN_ERROR_PREFIX}{cause}'

    @pytest.mark.parametrize('mode', BANNER_MODE_VALUES)
    @pytest.mark.parametrize('function', [sort, check])
    def test_invalid_options_pattern_precedes_source_parsing(
        self,
        function: Callable[..., object],
        mode: BannerMode,
    ) -> None:
        config = DefsortConfig(banners=mode)
        object.__setattr__(config, 'banner_pattern', '[')
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            function('def broken(', config=config)
        cause = excinfo.value.__cause__
        assert isinstance(cause, re.error)
        assert str(excinfo.value) == f'{_BANNER_PATTERN_ERROR_PREFIX}{cause}'

    @pytest.mark.parametrize('function', [sort, check])
    def test_invalid_pattern_precedes_incremental_source_parsing(
        self,
        function: Callable[..., object],
    ) -> None:
        config = DefsortConfig()
        object.__setattr__(config, 'banner_pattern', '[')
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            function(
                'def broken(',
                baseline='# baseline\n',
                config=config,
            )
        cause = excinfo.value.__cause__
        assert isinstance(cause, re.error)
        assert str(excinfo.value) == f'{_BANNER_PATTERN_ERROR_PREFIX}{cause}'


class TestPublicTypeExports:
    def test_public_types_exported(self) -> None:
        names = {
            'Blocker',
            'ClassBlocker',
            'DefsortConfig',
            'DetectResult',
            'EvaluationOrder',
            'OrderKind',
            'SortCheckResult',
            'SortScopeOrder',
            'SortStatus',
        }
        for name in names:
            assert getattr(defsort, name) is not None


class TestRestrictedBannerPatternBoundary:
    def test_check_rejects_mutated_banner_pattern_before_processing_source(self) -> None:
        config = DefsortConfig()
        object.__setattr__(config, 'banner_pattern', '^# (a+)+$')
        source = _NEEDS_SORT

        with pytest.raises(ValueError, match='banner_pattern'):
            check(source, config=config)

        assert source == _NEEDS_SORT

    def test_detect_rejects_unsafe_banner_pattern_before_processing_source(self) -> None:
        source = _NEEDS_SORT

        with pytest.raises(ValueError, match='banner_pattern'):
            detect(source, banner_pattern='^# (a+)+$')

        assert source == _NEEDS_SORT

    def test_sort_rejects_mutated_banner_pattern_before_processing_source(self) -> None:
        config = DefsortConfig()
        object.__setattr__(config, 'banner_pattern', '^# (a+)+$')
        source = _NEEDS_SORT

        with pytest.raises(ValueError, match='banner_pattern'):
            sort(source, config=config)

        assert source == _NEEDS_SORT


class TestSort:
    def test_alphabetical(self) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = defsort.sort(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert result.index('def alpha') < result.index('def bravo')

    def test_already_sorted_unchanged(self) -> None:
        assert defsort.sort(_SORTED) == _SORTED

    @pytest.mark.parametrize(
        'source',
        [
            """class Z:
    pass


class B:
    def __init_subclass__(cls):
        Z

    __init_subclass__: object


class A(B):
    pass
""",
            """class Z:
    pass


class Meta(type):
    def __new__(mcs, name, bases, namespace):
        Z
        return super().__new__(mcs, name, bases, namespace)

    __new__: object


class A(metaclass=Meta):
    pass
""",
            """class Z:
    pass


class Descriptor:
    def __set_name__(self, owner, name):
        Z

    __set_name__: object


class A:
    field = Descriptor()
""",
        ],
        ids=['__init_subclass__', '__new__', '__set_name__'],
    )
    def test_annotation_only_class_hook_preserves_binding(self, source: str) -> None:
        execute_source(source)

        sorted_source = defsort.sort(source)

        execute_source(sorted_source)
        assert sorted_source.index('class Z') < sorted_source.index('class A')
        assert defsort.sort(sorted_source) == sorted_source

    def test_does_not_add_future_import(self) -> None:
        src = 'class Bravo:\n    pass\n\n\nclass Alpha:\n    x: Bravo = None\n'
        result = defsort.sort(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert 'from __future__ import annotations' not in result

    def test_does_not_remove_future_import(self) -> None:
        src = (
            'from __future__ import annotations\n\n\n'
            'def bravo(x: Alpha) -> None:\n    pass\n\n\n'
            'class Alpha:\n    pass\n'
        )
        result = defsort.sort(src)
        assert 'from __future__ import annotations' in result

    def test_force_bypasses_safety(self) -> None:
        result = defsort.sort(_SKIPPED, config=DefsortConfig(force=True))
        assert result != _SKIPPED

    def test_method_sort(self) -> None:
        result = defsort.sort(
            _METHODS_UNSORTED,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        assert result != _METHODS_UNSORTED
        assert result.index('def alpha') < result.index('def bravo')

    def test_raises_on_invalid_syntax(self) -> None:
        with pytest.raises(SyntaxError):
            defsort.sort(_INVALID)

    def test_raises_on_lone_surrogate(self) -> None:
        with pytest.raises((SyntaxError, ValueError)):
            defsort.sort(_SURROGATE)

    def test_raises_on_null_bytes(self) -> None:
        with pytest.raises(SyntaxError):
            defsort.sort(_NULL_BYTES)

    def test_returns_original_on_safety_skip(self) -> None:
        assert defsort.sort(_SKIPPED) == _SKIPPED

    def test_sorts_out_of_order(self) -> None:
        result = defsort.sort(_NEEDS_SORT)
        assert result != _NEEDS_SORT
        assert result.index('def alpha') < result.index('def bravo')

    def test_uses_effective_direct_class_hook_binding(self) -> None:
        source = """class Z:
    pass


class B:
    def __init_subclass__(cls):
        pass

    def __init_subclass__(cls):
        Z


class A(B):
    pass
"""
        execute_source(source)

        sorted_source = defsort.sort(source)

        execute_source(sorted_source)
        assert sorted_source.index('class Z') < sorted_source.index('class A')
        assert defsort.sort(sorted_source) == sorted_source


class TestSortBaseline:
    def test_compile_invalid_baseline_preserves_current_source(self) -> None:
        baseline = 'return 1\n\n\ndef zulu():\n    pass\n'
        current = 'def zulu():\n    pass\n\n\ndef alpha():\n    pass\n'

        assert sort(current, baseline=baseline) == current

    def test_incremental_noop(self) -> None:
        src = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        result = defsort.sort(src, baseline=src)
        assert result == src

    def test_incremental_sorts_new_defs(self) -> None:
        baseline = 'def alpha():\n    return 1\n\n\ndef charlie():\n    return alpha()\n'
        current = baseline + '\n\ndef bravo():\n    return alpha()\n'
        result = defsort.sort(current, baseline=baseline)
        assert result != current  # something changed
        assert result.index('alpha') < result.index('bravo')
        assert result.index('bravo') < result.index('charlie')

    def test_no_baseline_is_full_sort(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = defsort.sort(src)
        assert result.index('alpha') < result.index('bravo')


class TestSortWithScopes:
    def test_both_none_is_noop(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = defsort.sort(
            src,
            config=DefsortConfig(
                definition_order='none',
                method_order='none',
            ),
        )
        assert result == src

    def test_definition_alpha_method_dep(self) -> None:
        src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = defsort.sort(
            src,
            config=DefsortConfig(
                definition_order='alphabetical',
                method_order='dependency',
            ),
        )
        assert result.index('alpha') < result.index('bravo')

    def test_definition_order_none(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = defsort.sort(
            src,
            config=DefsortConfig(definition_order='none'),
        )
        assert result == src

    def test_method_order_none(self) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
        )
        result = defsort.sort(
            src,
            config=DefsortConfig(method_order='none'),
        )
        # Methods unchanged (bravo before alpha)
        assert result.index('bravo') < result.index('alpha')


class TestSurfaceMutationContracts:
    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    def test_called_sibling_eager_hazard_stays_hard(self, *, incremental: bool) -> None:
        source = (
            'def zeta_compute():\n'
            '    return 42\n'
            '\n\n'
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )
        baseline = 'def zeta_compute():\n    return 42\n' if incremental else None
        config = DefsortConfig(definition_order='alphabetical', method_order='none', force=True)

        checked = check(source, baseline=baseline, config=config)
        transformed = sort(source, baseline=baseline, config=config)

        assert checked.status == 'skipped'
        assert checked.blocker == 'eager_order'
        assert transformed == source

    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize(
        ('source', 'definition_order', 'method_order'),
        [
            (
                (
                    'def zeta_default():\n'
                    '    return 1\n'
                    '\n\n'
                    'def alpha(value=zeta_default()):\n'
                    '    return value\n'
                ),
                'alphabetical',
                'none',
            ),
            (
                (
                    'class Example:\n'
                    '    def zeta_default():\n'
                    '        return 1\n'
                    '\n'
                    '    def alpha(self, value=zeta_default()):\n'
                    '        return value\n'
                ),
                'none',
                'alphabetical',
            ),
        ],
        ids=['eager-top-level', 'eager-method'],
    )
    def test_eager_hazards_stay_hard_without_import_mutation(
        self,
        source: str,
        definition_order: SortScopeOrder,
        method_order: SortScopeOrder,
        *,
        incremental: bool,
        force: bool,
    ) -> None:
        baseline = '# placeholder\n' if incremental else None
        options = DefsortConfig(
            definition_order=definition_order,
            method_order=method_order,
            force=force,
        )

        checked = check(source, baseline=baseline, config=options)
        transformed = sort(source, baseline=baseline, config=options)

        assert checked.status == 'skipped'
        assert checked.blocker == 'eager_order'
        assert transformed == source
        assert 'from __future__ import annotations' not in transformed

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize(
        ('source', 'definition_order', 'method_order'),
        [
            (
                'class ZetaType:\n    pass\n\n\nclass Alpha:\n    value: ZetaType\n',
                'alphabetical',
                'none',
            ),
            (
                (
                    'class Example:\n'
                    '    def zeta_type(self):\n'
                    '        return None\n'
                    '\n'
                    '    def alpha(self, value: zeta_type):\n'
                    '        return value\n'
                ),
                'none',
                'alphabetical',
            ),
        ],
        ids=['future-top-level', 'future-method'],
    )
    def test_future_hazards_do_not_add_imports(
        self,
        source: str,
        definition_order: SortScopeOrder,
        method_order: SortScopeOrder,
        *,
        incremental: bool,
    ) -> None:
        baseline = '# placeholder\n' if incremental else None
        options = DefsortConfig(
            definition_order=definition_order,
            method_order=method_order,
        )

        checked = check(source, baseline=baseline, config=options)
        transformed = sort(source, baseline=baseline, config=options)

        assert checked.status == 'skipped'
        assert checked.blocker == 'forward_refs'
        assert transformed == source
        assert 'from __future__ import annotations' not in transformed

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    def test_future_import_noop_is_unchanged(self, *, incremental: bool) -> None:
        source = (
            'from __future__ import annotations\n'
            '\n\n'
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
        )
        baseline = source if incremental else None
        options = DefsortConfig(
            definition_order='alphabetical',
            method_order='none',
        )

        checked = check(source, baseline=baseline, config=options)
        transformed = sort(source, baseline=baseline, config=options)

        assert checked.status == 'unchanged'
        assert transformed == source

    @pytest.mark.parametrize('incremental', [False, True], ids=['full', 'incremental'])
    @pytest.mark.parametrize(
        'source',
        [
            (
                'from __future__ import annotations\n'
                '\n\n'
                'class ZetaType:\n'
                '    pass\n'
                '\n\n'
                'class Alpha:\n'
                '    value: ZetaType\n'
            ),
            (
                'from __future__ import annotations\n'
                '\n\n'
                'def zeta():\n'
                '    pass\n'
                '\n\n'
                'def alpha():\n'
                '    pass\n'
            ),
        ],
        ids=['future-annotated', 'future-annotation-free'],
    )
    def test_future_imports_are_never_removed(
        self,
        source: str,
        *,
        incremental: bool,
    ) -> None:
        baseline = '# placeholder\n' if incremental else None
        options = DefsortConfig(
            definition_order='alphabetical',
            method_order='none',
        )

        checked = check(source, baseline=baseline, config=options)
        transformed = sort(source, baseline=baseline, config=options)

        assert checked.status == 'changed'
        assert transformed != source
        assert transformed.count('from __future__ import annotations') == 1


def _collect_defsort_types(hint: object, found: set[str]) -> None:
    if isinstance(hint, type) and hint.__module__.startswith('defsort'):
        found.add(hint.__name__)
    for arg in typing.get_args(hint):
        _collect_defsort_types(arg, found)


def test_banner_detect_parameters_are_keyword_only_with_barrier_defaults() -> None:
    signature = inspect.signature(detect)

    banners = signature.parameters['banners']
    pattern = signature.parameters['banner_pattern']
    assert banners.kind is inspect.Parameter.KEYWORD_ONLY
    assert banners.default == 'barriers'
    assert pattern.kind is inspect.Parameter.KEYWORD_ONLY
    assert pattern.default is None


def test_banner_mode_is_the_only_public_banner_type() -> None:
    assert typing.get_args(defsort.BannerMode) == ('barriers', 'ignore', 'strip')


@pytest.mark.parametrize('function', [sort, check])
def test_banner_sort_and_check_use_exact_config_signature(
    function: Callable[..., object],
) -> None:
    signature = inspect.signature(function)

    assert list(signature.parameters) == ['source', 'baseline', 'config']
    baseline = signature.parameters['baseline']
    config = signature.parameters['config']
    assert baseline.kind is inspect.Parameter.KEYWORD_ONLY
    assert baseline.default is None
    assert config.kind is inspect.Parameter.KEYWORD_ONLY
    assert config.default is None


def test_defsort_config_axis_fields() -> None:
    opts = DefsortConfig(method_bodies='across-categories')
    out = defsort.sort(_SRC_INIT_BODY_API, config=opts)
    assert out.index('def load') < out.index('def __init__')


def test_every_public_export_is_documented() -> None:
    reference = Path(__file__).parents[1] / 'docs' / 'library.md'
    text = reference.read_text(encoding='utf-8')
    undocumented = [
        name for name in defsort.__all__ if not re.search(rf'\b{re.escape(name)}\b', text)
    ]
    assert undocumented == []


@pytest.mark.parametrize(('key', 'opts'), _API_INEFFECTIVE_CASES)
@pytest.mark.parametrize('operation', [defsort.sort, defsort.check])
def test_ineffective_nondefault_warns(
    operation: Callable[..., object], key: str, opts: DefsortConfig
) -> None:
    with pytest.warns(defsort.DefsortConfigWarning, match=key):
        operation('class C:\n    def a(self):\n        return 1\n', config=opts)


def test_public_all_is_exactly_the_approved_set() -> None:
    assert set(defsort.__all__) == _EXPECTED_PUBLIC_EXPORTS
    assert list(defsort.__all__) == sorted(defsort.__all__)


def test_public_annotation_closure_is_exported() -> None:
    reachable: set[str] = set()
    for name in defsort.__all__:
        obj = getattr(defsort, name)
        if inspect.isfunction(obj):
            for hint in typing.get_type_hints(obj).values():
                _collect_defsort_types(hint, reachable)
        elif inspect.isclass(obj):
            if dataclasses.is_dataclass(obj):
                for hint in typing.get_type_hints(obj).values():
                    _collect_defsort_types(hint, reachable)
            for _, prop in inspect.getmembers(obj, lambda m: isinstance(m, property)):
                if prop.fget is not None:
                    for hint in typing.get_type_hints(prop.fget).values():
                        _collect_defsort_types(hint, reachable)
    missing = sorted(reachable - set(defsort.__all__))
    assert missing == []


def test_sort_preserves_final_binding_of_mixed_main_group() -> None:
    source = (
        'def main():\n'
        '    return "function"\n'
        '\n\n'
        'class main:\n'
        '    marker = "class"\n'
        '\n\n'
        'def zeta():\n'
        '    return None\n'
    )

    output = sort(source)
    executed = run_setup([sys.executable, '-c', output + '\nprint(type(main).__name__)\n'])

    assert decode_text(executed.stdout) == 'type\n'
    assert output.index('def main') < output.index('class main')


def test_toml_resolved_config_matches_direct_construction(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\norder = "alphabetical"\ntie-break = "source"\n', encoding='utf-8'
    )
    defsort.clear_config_cache()
    resolved = defsort.resolve_config(tmp_path)
    direct = DefsortConfig(
        definition_order='alphabetical', method_order='alphabetical', tie_break='source'
    )
    assert resolved == direct
    src = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
    with pytest.warns(defsort.DefsortConfigWarning, match='tie-break'):
        assert defsort.sort(src, config=resolved) == defsort.sort(src, config=direct)


@pytest.mark.parametrize('scope', ['module', 'class', 'later-class'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('force', [False, True])
def test_unsatisfied_ownership_preserves_whole_source(
    scope: str, ending: str, *, incremental: bool, force: bool
) -> None:
    baseline = 'def alpha():\n    pass\n# --------\ndef zulu():\n    pass\n'
    source = baseline + '# for bravo\ndef bravo():\n    pass\n'
    if scope != 'module':
        baseline = 'class C:\n' + ''.join(
            '    ' + line for line in baseline.splitlines(keepends=True)
        )
        source = 'class C:\n' + ''.join('    ' + line for line in source.splitlines(keepends=True))
    if scope == 'later-class':
        baseline = 'class A:\n    def zulu(self):\n        pass\n\n' + baseline
        source = (
            'class A:\n    def zulu(self):\n        pass\n'
            '    def alpha(self):\n        pass\n\n' + source
        )
    baseline = baseline.replace('\n', ending)
    source = source.replace('\n', ending)
    previous = baseline if incremental else None
    config = DefsortConfig(force=force)
    for _ in range(2):
        assert sort(source, baseline=previous, config=config) == source
        result = check(source, baseline=previous, config=config)
        assert result.status == 'skipped'
        assert result.blocker == 'layout'
        assert result.blocker_message


def test_vocabulary_aliases_are_literal_strings() -> None:
    assert typing.get_args(EvaluationOrder) == ('preserve', 'allow-reordering')
    assert typing.get_args(defsort.MethodBodies) == (
        'within-categories',
        'ignore',
        'across-categories',
    )
