import pytest

from defsort._core import sort_file
from defsort._core._parse import parse_definitions
from defsort._core._safety import (
    _has_eager_order_hazard,
    _has_forward_annotation_refs,
    check_sort_safety,
    has_annotation_order_hazard,
    has_interstitial_code,
    has_nonconsecutive_duplicates,
    is_safe_trailing,
)
from defsort._types import (
    BlockingCode,
    Definition,
    DefinitionKind,
    EngineOptions,
    MethodDefinition,
    MethodSortInfo,
    SortPolicy,
)

_NO_FUTURE = False


class TestBindingIdentitySafety:
    def test_import_shadow_capture_blocked(self) -> None:
        src = (
            'from helpermod import memo\n'
            '\n'
            '\n'
            'class zeta:\n'
            "    TAG = memo('x')\n"
            '\n'
            '\n'
            'def memo(value):\n'
            '    return value\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.skipped
        assert result.blocker == 'eager_order'
        assert result.new_source == src


class TestCheckSortSafetyBlockerReturn:
    def test_duplicates_returns_blocker(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('bravo', 1),
            _def('alpha', 2),
        ]
        skipped, blocker, _ = check_sort_safety(
            defs, defs, [], set(), _NO_FUTURE, force=False, add_future=False
        )
        assert skipped is True
        assert blocker == 'duplicates'

    def test_force_does_not_bypass_top_level_duplicates(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('bravo', 1),
            _def('alpha', 2),
        ]

        skipped, blocker, _ = check_sort_safety(
            defs, defs, [], set(), _NO_FUTURE, force=True, add_future=False
        )

        assert skipped is True
        assert blocker == 'duplicates'

    def test_forward_refs_returns_blocker(self) -> None:
        # alpha refs bravo, but sorted order puts alpha first -> forward ref
        defs = [
            _def('bravo', 0, kind='class'),
            _def('alpha', 1, ann_refs={'bravo'}),
        ]
        # Sorted order: alpha before bravo -> alpha refs bravo (forward)
        new_order = [
            _def('alpha', 1, ann_refs={'bravo'}),
            _def('bravo', 0, kind='class'),
        ]
        skipped, blocker, _ = check_sort_safety(
            defs, new_order, [], set(), _NO_FUTURE, force=False, add_future=False
        )
        assert skipped is True
        assert blocker == 'forward_refs'

    def test_interstitial_returns_blocker(self) -> None:
        defs = [
            _def('a', 0),
            _def('b', 1, interstitial=True),
        ]
        skipped, blocker, _ = check_sort_safety(
            defs, defs, [], set(), _NO_FUTURE, force=False, add_future=False
        )
        assert skipped is True
        assert blocker == 'interstitial'

    def test_no_issues_returns_none(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('bravo', 1),
        ]
        skipped, blocker, blocking = check_sort_safety(
            defs, defs, [], set(), _NO_FUTURE, force=False, add_future=False
        )
        assert skipped is False
        assert blocker is None
        assert blocking == []


class TestCheckSortSafetyBlockingCode:
    def test_blocking_code_from_next_definition_leading(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\n# ordinary comment\n\n'),
            _def('bravo', 1, leading="if sys.platform == 'win32':\n    patch()\n"),
        ]

        skipped, blocker, blocking = check_sort_safety(
            defs,
            defs,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'interstitial'
        assert blocking == [BlockingCode(line=8, text="if sys.platform == 'win32':")]

    def test_blocking_code_includes_line_and_text(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\n# ordinary comment\n\nregistry = {}\n'),
            _def('bravo', 1),
        ]

        skipped, blocker, blocking = check_sort_safety(
            defs,
            defs,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'interstitial'
        assert blocking == [BlockingCode(line=9, text='registry = {}')]

    def test_blocking_preview_counts_physical_lines(self) -> None:
        parsed = parse_definitions(
            'def alpha():\n'
            '    pass\n'
            '\n'
            '# note\fwith page break\n'
            'X = compute()\n'
            '\n'
            'def beta():\n'
            '    pass\n'
        )
        skipped, blocker, blocking = check_sort_safety(
            parsed.definitions,
            parsed.definitions,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )
        assert skipped is True
        assert blocker == 'interstitial'
        assert [(item.line, item.text) for item in blocking] == [(5, 'X = compute()')]

    def test_directive_comment_reports_line(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\n# type: ignore[override]\n'),
            _def('bravo', 1),
        ]

        skipped, blocker, blocking = check_sort_safety(
            defs,
            defs,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'interstitial'
        assert blocking == [BlockingCode(line=7, text='# type: ignore[override]')]

    def test_multiple_blocking_sections(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\nregistry = {}\n'),
            _def('bravo', 1, trailing='\ncache = {}\n'),
            _def('charlie', 2),
        ]

        skipped, blocker, blocking = check_sort_safety(
            defs,
            defs,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'interstitial'
        assert blocking == [
            BlockingCode(line=7, text='registry = {}'),
            BlockingCode(line=17, text='cache = {}'),
        ]

    def test_truncation_at_preview_limit(self) -> None:
        long_code = 'x' * 100 + ' = 1'
        defs = [
            _def('alpha', 0, trailing=f'\n{long_code}\n'),
            _def('bravo', 1),
        ]

        skipped, blocker, blocking = check_sort_safety(
            defs,
            defs,
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'interstitial'
        assert blocking == [BlockingCode(line=7, text='x' * 60 + '...')]


class TestHasForwardAnnotationRefs:
    def test_forward_ref_blocked(self) -> None:
        defs = [
            _def('alpha', 0, ann_refs={'Bravo'}),
            _def('Bravo', 1, kind='class'),
        ]
        assert _has_forward_annotation_refs(defs, set()) is True

    def test_no_annotation_refs(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('bravo', 1),
        ]
        assert _has_forward_annotation_refs(defs, set()) is False

    def test_no_forward_ref(self) -> None:
        defs = [
            _def('Bravo', 0, kind='class'),
            _def('alpha', 1, ann_refs={'Bravo'}),
        ]
        assert _has_forward_annotation_refs(defs, set()) is False

    def test_postamble_ref_blocked(self) -> None:
        defs = [
            _def('alpha', 0, ann_refs={'WidgetList'}),
        ]
        assert _has_forward_annotation_refs(defs, {'WidgetList'}) is True

    def test_same_position_blocked(self) -> None:
        defs = [
            _def('alpha', 0, ann_refs={'bravo'}),
            _def('bravo', 1, ann_refs={'alpha'}),
        ]
        # alpha refs bravo (at position 1, later) -> forward ref
        assert _has_forward_annotation_refs(defs, set()) is True

    def test_self_referential_blocked(self) -> None:
        defs = [
            _def('Node', 0, kind='class', ann_refs={'Node'}),
        ]
        assert _has_forward_annotation_refs(defs, set()) is True


class TestHasInterstitialCode:
    def test_blank_lines_safe(self) -> None:
        defs = [_def('alpha', 0, trailing='\n\n')]
        assert has_interstitial_code(defs) is False

    def test_code_between_defs(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\nregistry = {}\n'),
            _def('bravo', 1),
        ]
        assert has_interstitial_code(defs) is True

    def test_code_in_next_definition_leading(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\n# ordinary comment\n\n'),
            _def('bravo', 1, leading='registry = {}\n'),
        ]
        assert has_interstitial_code(defs) is True

    def test_comment_safe(self) -> None:
        defs = [_def('alpha', 0, trailing='\n# a comment\n')]
        assert has_interstitial_code(defs) is False

    def test_directive_comment_unsafe(self) -> None:
        defs = [
            _def('alpha', 0, trailing='\n# type: ignore[override]\n'),
            _def('bravo', 1),
        ]
        assert has_interstitial_code(defs) is True

    def test_empty_trailing_safe(self) -> None:
        defs = [_def('alpha', 0)]
        assert has_interstitial_code(defs) is False

    def test_had_interstitial_code_flag(self) -> None:
        defs = [_def('alpha', 0, interstitial=True)]
        assert has_interstitial_code(defs) is True

    def test_last_def_trailing_ignored(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('bravo', 1, trailing='\nregistry = {}\n'),
        ]
        assert has_interstitial_code(defs) is False


class TestHasNonconsecutiveDuplicates:
    def test_consecutive_same_name_ok(self) -> None:
        defs = [_def('parse', 0), _def('parse', 1), _def('parse', 2)]
        assert has_nonconsecutive_duplicates(defs) is False

    def test_main_duplicate_detected(self) -> None:
        defs = [_def('main', 0), _def('alpha', 1), _def('main', 2)]
        assert has_nonconsecutive_duplicates(defs) is True

    def test_no_duplicates(self) -> None:
        defs = [_def('alpha', 0), _def('bravo', 1)]
        assert has_nonconsecutive_duplicates(defs) is False

    def test_nonconsecutive_detected(self) -> None:
        defs = [_def('alpha', 0), _def('bravo', 1), _def('alpha', 2)]
        assert has_nonconsecutive_duplicates(defs) is True


class TestIsSafeTrailing:
    def test_blank_lines_safe(self) -> None:
        assert is_safe_trailing('\n\n') is True

    def test_comment_safe(self) -> None:
        assert is_safe_trailing('# a comment\n') is True

    def test_comment_with_form_feed_is_safe(self) -> None:
        assert is_safe_trailing('# alpha\fbeta\n') is True

    def test_empty_string_safe(self) -> None:
        assert is_safe_trailing('') is True

    def test_executable_code_unsafe(self) -> None:
        assert is_safe_trailing('registry = {}\n') is False

    @pytest.mark.parametrize('line', ['# ruff: noqa\n', '# pyright: strict\n'])
    def test_extended_directives_are_unsafe(self, line: str) -> None:
        assert is_safe_trailing(line) is False

    def test_fixme_safe(self) -> None:
        assert is_safe_trailing('# FIXME: broken\n') is True

    def test_mixed_safe_and_unsafe(self) -> None:
        assert is_safe_trailing('# ok comment\nregistry = {}\n') is False

    def test_noqa_unsafe(self) -> None:
        assert is_safe_trailing('# noqa: E501\n') is False

    def test_note_safe(self) -> None:
        assert is_safe_trailing('# NOTE: important\n') is True

    def test_pragma_no_cover_unsafe(self) -> None:
        assert is_safe_trailing('# pragma: no cover\n') is False

    def test_todo_safe(self) -> None:
        assert is_safe_trailing('# TODO: fix this\n') is True

    def test_type_ignore_unsafe(self) -> None:
        assert is_safe_trailing('# type: ignore\n') is False

    def test_whitespace_only_safe(self) -> None:
        assert is_safe_trailing('   \n') is True


class TestReorderHazards:
    def test_default_evaluation_order_is_not_bypassed_by_force(self) -> None:
        zeta = _def('zeta', 0, eager=True)
        alpha = _def('alpha', 1, eager=True)

        skipped, blocker, _ = check_sort_safety(
            [zeta, alpha],
            [alpha, zeta],
            [],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_dynamic_all_changed_order_is_hazard(self) -> None:
        zeta = _def('zeta', 0)
        alpha = _def('alpha', 1)

        skipped, blocker, _ = check_sort_safety(
            [zeta, alpha],
            [alpha, zeta],
            [],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
            dynamic_all=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_dynamic_all_unchanged_order_is_safe(self) -> None:
        zeta = _def('zeta', 0)
        alpha = _def('alpha', 1)

        skipped, blocker, _ = check_sort_safety(
            [zeta, alpha],
            [zeta, alpha],
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
            dynamic_all=True,
        )

        assert skipped is False
        assert blocker is None

    def test_eager_binder_moved_behind_reader_is_hazard(self) -> None:
        binder = _def('zeta', 0)
        reader = _def('alpha', 1, eager_refs={'zeta'})

        assert _has_eager_order_hazard([binder, reader], [reader, binder], []) is True

    def test_eager_identical_order_is_safe(self) -> None:
        binder = _def('zeta', 0)
        reader = _def('alpha', 1, eager_refs={'zeta'})

        assert _has_eager_order_hazard([binder, reader], [binder, reader], []) is False

    def test_eager_missing_movable_binder_is_safe(self) -> None:
        reader = _def('alpha', 0, eager_refs={'external'})

        assert _has_eager_order_hazard([reader], [reader], []) is False

    def test_eager_order_is_not_bypassed_by_force(self) -> None:
        binder = _def('zeta', 0)
        reader = _def('alpha', 1, eager_refs={'zeta'})

        skipped, blocker, _ = check_sort_safety(
            [binder, reader],
            [reader, binder],
            [],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_eager_order_precedes_forward_refs(self) -> None:
        eager_binder = _def('zeta', 0)
        annotation_binder = _def('yankee', 1, kind='class')
        reader = _def(
            'alpha',
            2,
            ann_refs={'yankee'},
            eager_refs={'zeta'},
        )

        skipped, blocker, _ = check_sort_safety(
            [eager_binder, annotation_binder, reader],
            [reader, annotation_binder, eager_binder],
            [],
            set(),
            _NO_FUTURE,
            force=False,
            add_future=False,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_eager_preexisting_forward_read_is_safe(self) -> None:
        reader = _def('alpha', 0, eager_refs={'zeta'})
        binder = _def('zeta', 1)
        other = _def('yankee', 2)

        assert (
            _has_eager_order_hazard(
                [reader, binder, other],
                [reader, other, binder],
                [],
            )
            is False
        )

    def test_eager_repeated_binder_identity_change_is_hazard(self) -> None:
        first = _def('zeta', 0)
        reader = _def('alpha', 1, eager_refs={'zeta'})
        second = _def('zeta', 2)

        assert (
            _has_eager_order_hazard(
                [first, reader, second],
                [second, reader, first],
                [],
            )
            is True
        )

    def test_independent_eager_definitions_keep_relative_order(self) -> None:
        zeta = _def('zeta', 0, eager_refs={'events'}, eager=True)
        alpha = _def('alpha', 1, eager_refs={'events'}, eager=True)

        skipped, blocker, _ = check_sort_safety(
            [zeta, alpha],
            [alpha, zeta],
            [],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_method_dependencies_and_annotations_are_classified_separately(self) -> None:
        eager_binder = _method('zeta_eager', 0)
        eager_reader = _method('alpha_eager', 1, eager_reads={'zeta_eager'})
        eager_sort = _method_sort(
            [eager_binder, eager_reader],
            [eager_reader, eager_binder],
        )
        annotation_binder = _method('zeta_annotation', 2)
        annotation_reader = _method(
            'alpha_annotation',
            3,
            ann_refs={'zeta_annotation'},
        )
        annotation_sort = _method_sort(
            [annotation_binder, annotation_reader],
            [annotation_reader, annotation_binder],
        )

        assert _has_eager_order_hazard([], [], [eager_sort]) is True
        assert has_annotation_order_hazard([], [], [eager_sort], set()) is False
        assert _has_eager_order_hazard([], [], [annotation_sort]) is False
        assert has_annotation_order_hazard([], [], [annotation_sort], set()) is True

    def test_method_eager_groups_keep_relative_order(self) -> None:
        zeta = _method('zeta', 0)
        middle = _method('middle', 1)
        alpha = _method('alpha', 2)
        zeta.has_eager_evaluation = True
        alpha.has_eager_evaluation = True
        method_sort = _method_sort([zeta, middle, alpha], [alpha, middle, zeta])

        skipped, blocker, _ = check_sort_safety(
            [],
            [],
            [method_sort],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'

    def test_property_family_is_one_eager_group(self) -> None:
        zeta_getter = _method('zeta', 0)
        zeta_setter = _method('zeta', 1)
        alpha_getter = _method('alpha', 2)
        zeta_getter.property_group = 'zeta'
        zeta_setter.property_group = 'zeta'
        alpha_getter.property_group = 'alpha'
        zeta_getter.has_eager_evaluation = True
        zeta_setter.has_eager_evaluation = True
        alpha_getter.has_eager_evaluation = True
        method_sort = _method_sort(
            [zeta_getter, zeta_setter, alpha_getter],
            [alpha_getter, zeta_getter, zeta_setter],
        )

        skipped, blocker, _ = check_sort_safety(
            [],
            [],
            [method_sort],
            set(),
            _NO_FUTURE,
            force=True,
            add_future=True,
        )

        assert skipped is True
        assert blocker == 'eager_order'


def _def(  # noqa: PLR0913
    name: str,
    index: int,
    *,
    ann_refs: set[str] | None = None,
    deps: set[str] | None = None,
    eager_refs: set[str] | None = None,
    interstitial: bool = False,
    kind: DefinitionKind = 'function',
    leading: str = '',
    trailing: str = '',
    eager: bool = False,
) -> Definition:
    return Definition(
        index=index,
        name=name,
        start_line=index * 10,
        end_line=index * 10 + 5,
        kind=kind,
        source=f'def {name}(): pass',
        leading=leading,
        trailing=trailing,
        dependencies=deps or set(),
        annotation_refs=ann_refs or set(),
        had_interstitial_code=interstitial,
        eager_refs=eager_refs or set(),
        has_eager_evaluation=eager,
    )


def _method(
    name: str,
    index: int,
    *,
    ann_refs: set[str] | None = None,
    deps: set[str] | None = None,
    eager_reads: set[str] | None = None,
) -> MethodDefinition:
    return MethodDefinition(
        index=index,
        name=name,
        start_line=index * 10,
        end_line=index * 10 + 5,
        source=f'    def {name}(self): pass',
        semantic_deps=deps or set(),
        annotation_refs=ann_refs or set(),
        eager_reads=eager_reads or set(),
    )


def _method_sort(
    original: list[MethodDefinition],
    candidate: list[MethodDefinition],
) -> MethodSortInfo:
    return MethodSortInfo('Example', original, candidate)
