import dataclasses
import inspect
import re
import typing
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

import pytest

import defsort
from defsort import ClassBlocker, DefsortConfig, DetectResult, SortCheckResult
from defsort._types import (
    _BLOCKER_MESSAGES,
    BANNER_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
    BannerMode,
    BlobReadResult,
    BlobReadStatus,
    Blocker,
    BoundaryLayoutPart,
    ClassSlice,
    ColorMode,
    Definition,
    EngineOptions,
    EvaluationOrder,
    FallbackMode,
    HookContext,
    JsonStatus,
    MainPlacement,
    MethodBodies,
    MethodDefinition,
    MethodDunders,
    MethodVisibility,
    OrderKind,
    PropertyKind,
    SectionBoundary,
    SortOverrides,
    SortPolicy,
    SortResult,
    SortScopeOrder,
    SortStatus,
    TieBreak,
    blocker_message,
    is_soft_blocker,
    sort_policy_from_config,
    validate_banner_pattern,
    validate_vocabulary,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_PUBLIC_RECORD_TYPES = [ClassBlocker, DefsortConfig, DetectResult, SortCheckResult]
_RECORD_SAMPLES: dict[type, object] = {
    ClassBlocker: ClassBlocker(
        class_name='C',
        occurrence=1,
        blocker='interstitial',
        message='code between definitions',
    ),
    DefsortConfig: DefsortConfig(),
    DetectResult: DetectResult(
        order='dependency',
        dep_score=1.0,
        alpha_score=0.0,
    ),
    SortCheckResult: SortCheckResult(status='unchanged'),
}


class TestBannerRecords:
    def test_boundary_is_frozen(self) -> None:
        boundary = SectionBoundary(1, 2, 2, '# ----\n', '# ----')
        with pytest.raises(FrozenInstanceError):
            boundary.section = 2  # pyright: ignore[reportAttributeAccessIssue]

    def test_boundary_layout_part_runtime_value(self) -> None:
        assert BoundaryLayoutPart.__value__ == str | SectionBoundary

    def test_type_module_has_no_interstitial_alias(self) -> None:
        source_path = Path(__file__).parents[1] / 'src' / 'defsort' / '_types.py'
        result = defsort.check(source_path.read_text(encoding='utf-8'))
        assert result.blocker != 'interstitial'


class TestBlobReadResult:
    def test_content_status_rejects_missing_content(self) -> None:
        with pytest.raises(ValueError, match='CONTENT blob reads require string content'):
            BlobReadResult(status=BlobReadStatus.CONTENT)

    def test_content_status_rejects_non_string_content(self) -> None:
        content = cast('str', 1)

        with pytest.raises(ValueError, match='CONTENT blob reads require string content'):
            BlobReadResult(status=BlobReadStatus.CONTENT, content=content)

    def test_content_status_uses_utf8_default(self) -> None:
        r = BlobReadResult(status=BlobReadStatus.CONTENT, content='def a():\n    pass\n')
        assert r.encoding == 'utf-8'

    def test_decode_failed_has_no_content(self) -> None:
        r = BlobReadResult(status=BlobReadStatus.DECODE_FAILED)
        assert r.content is None

    def test_frozen(self) -> None:
        r = BlobReadResult(status=BlobReadStatus.MISSING)
        with pytest.raises(FrozenInstanceError):
            r.status = BlobReadStatus.CONTENT  # pyright: ignore[reportAttributeAccessIssue]

    def test_infra_error_has_no_content(self) -> None:
        r = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
        assert r.content is None

    def test_kw_only_rejects_positional(self) -> None:
        with pytest.raises(TypeError, match='positional'):
            BlobReadResult(BlobReadStatus.CONTENT, 'text')  # pyright: ignore[reportCallIssue]

    def test_missing_has_no_content(self) -> None:
        r = BlobReadResult(status=BlobReadStatus.MISSING)
        assert r.status is BlobReadStatus.MISSING
        assert r.content is None

    @pytest.mark.parametrize(
        'status',
        [
            BlobReadStatus.DECODE_FAILED,
            BlobReadStatus.INFRA_ERROR,
            BlobReadStatus.MISSING,
        ],
    )
    def test_non_content_status_rejects_content(self, status: BlobReadStatus) -> None:
        with pytest.raises(ValueError, match='non-content blob reads cannot carry content'):
            BlobReadResult(status=status, content='unexpected')


class TestBlobReadStatus:
    def test_members(self) -> None:
        assert {m.name for m in BlobReadStatus} == {
            'CONTENT',
            'MISSING',
            'INFRA_ERROR',
            'DECODE_FAILED',
        }

    def test_values(self) -> None:
        assert BlobReadStatus.CONTENT.value == 'content'
        assert BlobReadStatus.MISSING.value == 'missing'
        assert BlobReadStatus.INFRA_ERROR.value == 'infra_error'
        assert BlobReadStatus.DECODE_FAILED.value == 'decode_failed'


class TestBlocker:
    def test_alias_values_are_stable(self) -> None:
        assert get_args(Blocker) == (
            'duplicates',
            'forward_refs',
            'interstitial',
            'layout',
            'non_conforming',
            'target_incompatible',
            'user_reordered',
            'eager_order',
        )

    def test_every_value_has_a_display_message(self) -> None:
        assert set(_BLOCKER_MESSAGES) == set(get_args(Blocker))
        for value in get_args(Blocker):
            message = blocker_message(value)
            assert message
            assert not message.startswith('ERROR:')
            assert message[0].islower()
            assert not message.endswith('.')

    def test_message_pins(self) -> None:
        assert blocker_message('interstitial') == 'code between definitions'
        assert blocker_message('eager_order') == (
            'sort would move a decorator, default value, base class, or class-body '
            'reference before the definition it uses'
        )


class TestClassBlocker:
    def test_message_is_required(self) -> None:
        with pytest.raises(TypeError, match='message'):
            ClassBlocker(  # pyright: ignore[reportCallIssue]
                class_name='C',
                occurrence=1,
                blocker='interstitial',
            )


class TestClassSlice:
    def test_frozen(self) -> None:
        cs = ClassSlice(lines=['x\n'], start_line=1)
        with pytest.raises(AttributeError):
            cs.start_line = 2  # pyright: ignore[reportAttributeAccessIssue]

    def test_source_derived_from_lines(self) -> None:
        lines = ['a\n', 'b\n']
        cs = ClassSlice(lines=lines, start_line=5)
        assert cs.source == 'a\nb\n'


class TestColorMode:
    def test_values(self) -> None:
        assert set(get_args(ColorMode)) == {'auto', 'always', 'never'}


class TestDefinition:
    def test_appended_eager_refs_default_independently(self) -> None:
        had_interstitial = True
        no_interstitial = False
        overload = True
        first = Definition(
            0,
            'alpha',
            1,
            2,
            'function',
            'def alpha(): pass',
            '\n',
            '# leading\n',
            {'dependency'},
            {'Annotation'},
            had_interstitial,
            overload,
            7,
        )
        second = Definition(
            1,
            'bravo',
            3,
            4,
            'class',
            'class Bravo: pass',
            '',
            '',
            {'other_dependency'},
            {'OtherAnnotation'},
            no_interstitial,
            overload,
            8,
        )

        assert first.section == 7
        assert second.section == 8
        assert first.eager_refs == set()
        assert second.eager_refs == set()
        assert first.eager_refs is not second.eager_refs

    def test_defaults(self) -> None:
        d = Definition(
            index=0, name='x', start_line=1, end_line=1, kind='class', source='class X: pass'
        )
        assert d.trailing == ''
        assert d.dependencies == set()
        assert d.annotation_refs == set()
        assert d.had_interstitial_code is False
        assert d.section == 0


class TestDefsortConfig:
    def test_defaults(self) -> None:
        opt = DefsortConfig()
        assert opt.definition_order == 'dependency'
        assert opt.method_order == 'dependency'
        assert opt.evaluation_order == 'preserve'
        assert opt.force is False
        assert opt.banners == 'barriers'
        assert opt.banner_pattern is None
        assert opt.method_bodies == 'within-categories'
        assert opt.method_visibility == 'mixed'
        assert opt.method_dunders == 'before-regular'
        assert opt.main == 'last'
        assert opt.tie_break == 'alphabetical'

    def test_exact_public_policy_fields(self) -> None:
        assert [field.name for field in fields(DefsortConfig)] == [
            'definition_order',
            'method_order',
            'evaluation_order',
            'force',
            'banners',
            'banner_pattern',
            'method_bodies',
            'method_visibility',
            'method_dunders',
            'main',
            'tie_break',
        ]

    def test_invalid_axis_value_raises_at_options_construction(self) -> None:
        with pytest.raises(ValueError, match='bogus'):
            DefsortConfig(method_bodies=cast('MethodBodies', 'bogus'))

    def test_invalid_evaluation_order_raises_at_options_construction(self) -> None:
        with pytest.raises(ValueError, match='evaluation_order'):
            DefsortConfig(evaluation_order=cast('EvaluationOrder', 'bogus'))

    def test_invalid_vocabulary_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match='bogus'):
            DefsortConfig(banners=cast('BannerMode', 'bogus'))

    def test_kw_only(self) -> None:
        with pytest.raises(TypeError, match='positional'):
            DefsortConfig('dependency')  # pyright: ignore[reportCallIssue]

    def test_non_bool_force_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match='force'):
            DefsortConfig(force=cast('bool', 1))

    def test_non_compiling_pattern_raises_value_error_with_cause(self) -> None:
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            DefsortConfig(banner_pattern='[')
        assert isinstance(excinfo.value.__cause__, re.error)

    def test_non_string_pattern_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match='banner_pattern'):
            DefsortConfig(banner_pattern=cast('str', 123))


class TestEngineOptions:
    def test_contains_one_required_policy_plus_execution_controls(self) -> None:
        assert not issubclass(EngineOptions, DefsortConfig)
        assert [field.name for field in fields(EngineOptions)] == [
            'policy',
            'force',
            'hoist',
            'add_future',
            'remove_future',
        ]
        parameters = inspect.signature(EngineOptions).parameters
        assert parameters['policy'].default is inspect.Parameter.empty
        assert parameters['policy'].kind is inspect.Parameter.KEYWORD_ONLY

        untyped_constructor = cast('Callable[..., EngineOptions]', EngineOptions)
        with pytest.raises(TypeError, match='policy'):
            untyped_constructor()

    def test_is_keyword_only(self) -> None:
        parameters = inspect.signature(EngineOptions).parameters.values()

        assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters)

    def test_is_not_publicly_exported(self) -> None:
        assert not hasattr(defsort, 'EngineOptions')

    def test_sort_options_convert_every_axis_to_one_policy(self) -> None:
        options = DefsortConfig(
            definition_order='alphabetical',
            method_order='none',
            evaluation_order='allow-reordering',
            method_bodies='across-categories',
            method_visibility='public-first',
            method_dunders='after-regular',
            main='sorted',
            tie_break='source',
            banners='strip',
            banner_pattern=r'# Section',
        )

        assert sort_policy_from_config(options) == SortPolicy(
            definition_order='alphabetical',
            method_order='none',
            evaluation_order='allow-reordering',
            method_bodies='across-categories',
            method_visibility='public-first',
            method_dunders='after-regular',
            main='sorted',
            tie_break='source',
            banners='strip',
            banner_pattern=r'# Section',
        )


class TestFallbackMode:
    def test_is_literal_not_enum(self) -> None:
        args = get_args(FallbackMode)
        assert args == ('warn', 'full-sort')


class TestHookContext:
    def test_contains_only_execution_state(self) -> None:
        assert [item.name for item in fields(HookContext)] == [
            'cwd',
            'repo_root',
            'allow_on_failure',
            'incremental',
            'overrides',
        ]


class TestJsonStatus:
    def test_is_literal_not_enum(self) -> None:
        args = get_args(JsonStatus)
        assert args == ('changed', 'error', 'invalid', 'skipped', 'unchanged')


class TestMethodDefinition:
    def test_appended_annotation_refs_default_independently(self) -> None:
        overload = True
        first = MethodDefinition(
            0,
            'alpha',
            1,
            2,
            '    def alpha(self): pass',
            {'dependency'},
            'alpha',
            'getter',
            overload,
            4,
        )
        second = MethodDefinition(
            1,
            'bravo',
            3,
            4,
            '    def bravo(self): pass',
            {'other_dependency'},
            None,
            None,
            overload,
            5,
        )

        assert first.section == 4
        assert second.section == 5
        assert first.annotation_refs == set()
        assert second.annotation_refs == set()
        assert first.annotation_refs is not second.annotation_refs

    def test_defaults(self) -> None:
        m = MethodDefinition(
            index=0, name='m', start_line=1, end_line=1, source='    def m(self): pass'
        )
        assert m.semantic_deps == set()
        assert m.property_group is None
        assert m.section == 0
        assert m.body_refs == set()


class TestOrderKind:
    def test_alias_values_are_stable(self) -> None:
        assert get_args(OrderKind) == ('alphabetical', 'both', 'dependency', 'unordered')


class TestPropertyKind:
    def test_is_literal_not_enum(self) -> None:
        args = get_args(PropertyKind)
        assert args == ('getter', 'setter', 'deleter')


class TestPublicRecordShape:
    @pytest.mark.parametrize('record_type', _PUBLIC_RECORD_TYPES)
    def test_frozen(self, record_type: type) -> None:
        instance = _RECORD_SAMPLES[record_type]
        first_field = dataclasses.fields(record_type)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, first_field, object())

    @pytest.mark.parametrize('record_type', _PUBLIC_RECORD_TYPES)
    def test_keyword_only(self, record_type: type) -> None:
        assert all(f.kw_only for f in dataclasses.fields(record_type))

    @pytest.mark.parametrize('record_type', _PUBLIC_RECORD_TYPES)
    def test_slotted(self, record_type: type) -> None:
        assert '__slots__' in vars(record_type)


class TestSoftBlockers:
    def test_hard_blockers(self) -> None:
        assert is_soft_blocker('duplicates') is False
        assert is_soft_blocker('eager_order') is False
        assert is_soft_blocker('forward_refs') is False
        assert is_soft_blocker('interstitial') is False
        assert is_soft_blocker('layout') is False

    def test_soft_blockers(self) -> None:
        assert is_soft_blocker('non_conforming') is True
        assert is_soft_blocker('target_incompatible') is True
        assert is_soft_blocker('user_reordered') is True


class TestSortCheckResult:
    def test_blocker_message_defaults_to_none(self) -> None:
        result = SortCheckResult(status='unchanged')
        assert result.blocker_message is None

    def test_defaults(self) -> None:
        result = SortCheckResult(status='unchanged')
        assert result.blocker is None
        assert result.banner_constrained is False
        assert result.class_blockers == ()

    def test_rejects_positional_construction(self) -> None:
        with pytest.raises(TypeError):
            SortCheckResult('skipped')  # pyright: ignore[reportCallIssue]


class TestSortOverrides:
    def test_defaults_all_none(self) -> None:
        o = SortOverrides()
        assert o.banners is None
        assert o.banner_pattern is None
        assert o.clear_banner_pattern is False
        assert o.definition_order is None
        assert o.evaluation_order is None
        assert o.incremental is None
        assert o.incremental_fallback is None
        assert o.main is None
        assert o.method_bodies is None
        assert o.method_dunders is None
        assert o.method_order is None
        assert o.method_visibility is None
        assert o.order is None
        assert o.show_skips is None
        assert o.tie_break is None

    def test_frozen(self) -> None:
        o = SortOverrides()
        with pytest.raises(FrozenInstanceError):
            o.banners = 'ignore'  # pyright: ignore[reportAttributeAccessIssue]

    def test_kw_only(self) -> None:
        with pytest.raises(TypeError):
            SortOverrides(None, None, None)  # pyright: ignore[reportCallIssue]


class TestSortPolicy:
    def test_fields_and_defaults(self) -> None:
        assert [field.name for field in fields(SortPolicy)] == [
            'definition_order',
            'method_order',
            'evaluation_order',
            'method_bodies',
            'method_visibility',
            'method_dunders',
            'main',
            'tie_break',
            'banners',
            'banner_pattern',
        ]

        policy = SortPolicy()
        assert policy.definition_order == 'dependency'
        assert policy.method_order == 'dependency'
        assert policy.evaluation_order == 'preserve'
        assert policy.method_bodies == 'within-categories'
        assert policy.method_visibility == 'mixed'
        assert policy.method_dunders == 'before-regular'
        assert policy.main == 'last'
        assert policy.tie_break == 'alphabetical'
        assert policy.banners == 'barriers'
        assert policy.banner_pattern is None


class TestSortResult:
    def test_defaults(self) -> None:
        r = SortResult(
            original_source='a',
            new_source='b',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
        )
        assert r.skipped is False
        assert r.method_sorts == []
        assert r.banner_constraints == []

    def test_total_methods_default(self) -> None:
        r = SortResult(
            original_source='',
            new_source='',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
        )
        assert r.total_methods == 0


class TestSortStatus:
    def test_is_literal_not_enum(self) -> None:
        args = get_args(SortStatus)
        assert args == ('changed', 'invalid', 'skipped', 'unchanged')


class TestVocabularyAliases:
    def test_literal_args_match_value_tuples(self) -> None:
        assert typing.get_args(BannerMode) == BANNER_MODE_VALUES
        assert typing.get_args(EvaluationOrder) == EVALUATION_ORDER_VALUES
        assert typing.get_args(MainPlacement) == MAIN_PLACEMENT_VALUES
        assert typing.get_args(MethodBodies) == METHOD_BODIES_VALUES
        assert typing.get_args(MethodDunders) == METHOD_DUNDERS_VALUES
        assert typing.get_args(MethodVisibility) == METHOD_VISIBILITY_VALUES
        assert typing.get_args(SortScopeOrder) == SORT_SCOPE_ORDER_VALUES
        assert typing.get_args(TieBreak) == TIE_BREAK_VALUES

    def test_value_tuples_hold_expected_values(self) -> None:
        assert BANNER_MODE_VALUES == ('barriers', 'ignore', 'strip')
        assert EVALUATION_ORDER_VALUES == ('preserve', 'allow-reordering')
        assert MAIN_PLACEMENT_VALUES == ('last', 'sorted')
        assert METHOD_BODIES_VALUES == ('within-categories', 'ignore', 'across-categories')
        assert METHOD_DUNDERS_VALUES == ('before-regular', 'after-regular')
        assert METHOD_VISIBILITY_VALUES == ('mixed', 'public-first')
        assert SORT_SCOPE_ORDER_VALUES == ('dependency', 'alphabetical', 'none')
        assert TIE_BREAK_VALUES == ('alphabetical', 'source')


class TestVocabularyValidators:
    @pytest.mark.parametrize(
        'pattern',
        [
            '# CUSTOM',
            '^# CUSTOM$',
            '^# Section: .+$',
            '^# Item: [A-Z]+$',
            '^# +$',
            r'^# \.\*\+\?\(\)\{\}\|\^\$$',
            r'^# []A-Z]+$',
            r'^# [^]]+$',
            r'^# [{}]+$',
        ],
    )
    def test_validate_banner_pattern_accepts_safe_subset(self, pattern: str) -> None:
        validate_banner_pattern(None)
        validate_banner_pattern(pattern)

    def test_validate_banner_pattern_chains_re_error(self) -> None:
        with pytest.raises(ValueError, match='banner_pattern') as excinfo:
            validate_banner_pattern('[')
        assert isinstance(excinfo.value.__cause__, re.error)

    def test_validate_banner_pattern_rejects_non_string(self) -> None:
        with pytest.raises(TypeError, match='banner_pattern'):
            validate_banner_pattern(3)

    @pytest.mark.parametrize(
        'pattern',
        [
            r'^# (a+)+$',
            r'^# (TOP|METHOD)$',
            r'^# a{2}$',
            r'^# {x}$',
            r'^# a?$',
            r'^# (a)\1$',
            r'^# (?=a)a$',
            r'^# .+.*$',
            r'^+# CUSTOM$',
            r'# ^CUSTOM',
            r'# CUSTOM$ trailing',
            r'^# a+b+$',
            r'^# \0$',
        ],
    )
    def test_validate_banner_pattern_rejects_unsafe_subset(self, pattern: str) -> None:
        with pytest.raises(ValueError, match='banner_pattern'):
            validate_banner_pattern(pattern)

    def test_validate_vocabulary_accepts_every_value(self) -> None:
        for value in BANNER_MODE_VALUES:
            validate_vocabulary('banners', value, BANNER_MODE_VALUES)

    def test_validate_vocabulary_rejects_non_string(self) -> None:
        with pytest.raises(ValueError, match='banners'):
            validate_vocabulary('banners', 3, BANNER_MODE_VALUES)

    def test_validate_vocabulary_rejects_unknown_string(self) -> None:
        with pytest.raises(ValueError, match='bogus'):
            validate_vocabulary('banners', 'bogus', BANNER_MODE_VALUES)


def test_ineffective_policy_values_is_not_publicly_exported() -> None:
    assert not hasattr(defsort, 'ineffective_policy_values')
