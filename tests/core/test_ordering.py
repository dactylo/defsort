import pytest

from defsort._core import sort_file
from defsort._core._banners import order_within_sections
from defsort._core._ordering import (
    _DefinitionGroup,
    group_consecutive_same_name,
    has_duplicate_group_names,
    order_alphabetical,
    order_definitions,
    order_dependency,
)
from defsort._core._parse import parse_definitions
from defsort._types import (
    Definition,
    DefinitionKind,
    EngineOptions,
    SortPolicy,
    SortScopeOrder,
)
from tests._helpers import execute_source

_DEFAULT_POLICY = SortPolicy()


class TestEagerDefinitionOrder:
    def test_alphabetical_keeps_decorated_relative_order(self) -> None:
        src = (
            'from regmod import register\n'
            '\n'
            '\n'
            '@register\n'
            'def zeta():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            '@register\n'
            'def alpha():\n'
            "    return 'a'\n"
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def zeta') < result.new_source.index('def alpha')

    def test_decorated_main_keeps_relative_order(self) -> None:
        src = (
            'from regmod import register\n'
            '\n'
            '\n'
            '@register\n'
            'def main():\n'
            '    return 0\n'
            '\n'
            '\n'
            '@register\n'
            'def zeta():\n'
            '    return 1\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def main') < result.new_source.index('def zeta')

    @pytest.mark.parametrize(
        'definition_order',
        ['dependency', 'alphabetical'],
    )
    def test_default_side_effect_order_is_preserved(
        self,
        definition_order: SortScopeOrder,
    ) -> None:
        source = (
            'events = []\n'
            '\n'
            '\n'
            "def zeta(value=(events.append('zeta'), 0)[1]):\n"
            '    return value\n'
            '\n'
            '\n'
            'def omega():\n'
            "    return 'omega'\n"
            '\n'
            '\n'
            "def alpha(value=(events.append('alpha'), 0)[1]):\n"
            '    return value\n'
            '\n'
            '\n'
            'def beta():\n'
            "    return 'beta'\n"
        )
        options = EngineOptions(policy=SortPolicy(definition_order=definition_order))

        result = sort_file(source, options=options)
        namespace = execute_source(result.new_source)

        assert namespace['events'] == ['zeta', 'alpha']
        assert result.new_source.index('def beta') < result.new_source.index('def omega')
        assert result.new_source.index('def beta') < result.new_source.index('def alpha')

    def test_dependency_keeps_decorated_relative_order(self) -> None:
        src = (
            'from regmod import register\n'
            '\n'
            '\n'
            '@register\n'
            'def zeta():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            '@register\n'
            'def alpha():\n'
            "    return 'a'\n"
            '\n'
            '\n'
            'def mmm():\n'
            '    return alpha()\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def zeta') < result.new_source.index('def alpha')

    @pytest.mark.parametrize(
        'signature',
        ['value=0', '*, value=None'],
        ids=['positional-default', 'keyword-only-default'],
    )
    @pytest.mark.parametrize(
        'definition_order',
        ['dependency', 'alphabetical'],
    )
    def test_independent_defaults_keep_source_relative_slots(
        self,
        signature: str,
        definition_order: SortScopeOrder,
    ) -> None:
        source = (
            f'def zeta({signature}):\n    pass\n\n\n'
            'def omega():\n    pass\n\n\n'
            f'def alpha({signature}):\n    pass\n\n\n'
            'def beta():\n    pass\n'
        )
        options = EngineOptions(policy=SortPolicy(definition_order=definition_order))

        result = sort_file(source, options=options)

        names = [definition.name for definition in result.new_order]
        assert names.index('zeta') < names.index('alpha')
        assert names.index('beta') < names.index('omega')
        assert names != ['zeta', 'omega', 'alpha', 'beta']

    def test_lazy_dependency_yields_to_decorated_order(self) -> None:
        src = (
            'from regmod import register\n'
            '\n'
            '\n'
            '@register\n'
            'def zeta():\n'
            '    return alpha()\n'
            '\n'
            '\n'
            '@register\n'
            'def alpha():\n'
            '    return 1\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def zeta') < result.new_source.index('def alpha')

    def test_preamble_bound_eager_name_does_not_false_block(self) -> None:
        src = (
            'from helpers import zeta\n'
            'from regmod import register\n'
            '\n'
            '\n'
            '@register\n'
            'def alpha(value=zeta()):\n'
            '    return value\n'
            '\n'
            '\n'
            '@register\n'
            'def zeta():\n'
            "    return 'local'\n"
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def alpha') < result.new_source.index('def zeta')

    def test_undecorated_definitions_still_sort(self) -> None:
        src = (
            'from regmod import register\n'
            '\n'
            '\n'
            'def zzz():\n'
            '    return 1\n'
            '\n'
            '\n'
            '@register\n'
            'def beta():\n'
            "    return 'b'\n"
            '\n'
            '\n'
            'def aaa():\n'
            '    return 2\n'
        )
        options = EngineOptions(policy=SortPolicy(definition_order='alphabetical'))
        result = sort_file(src, options=options)
        assert result.new_source.index('def aaa') < result.new_source.index('def zzz')


class TestGroupConsecutiveSameName:
    def test_consecutive_same_name_grouped(self) -> None:
        defs = [_def('parse', 0), _def('parse', 1), _def('parse', 2)]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 1
        assert groups[0].name == 'parse'
        assert len(groups[0].definitions) == 3

    def test_different_names_separate_groups(self) -> None:
        defs = [_def('alpha', 0), _def('bravo', 1), _def('charlie', 2)]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 3
        assert [g.name for g in groups] == ['alpha', 'bravo', 'charlie']

    def test_empty_input(self) -> None:
        assert group_consecutive_same_name([]) == []

    def test_first_index_property(self) -> None:
        defs = [_def('alpha', 0), _def('parse', 1), _def('parse', 2)]
        groups = group_consecutive_same_name(defs)
        assert groups[0]._first_index == 0
        assert groups[1]._first_index == 1

    def test_group_deps_are_union(self) -> None:
        defs = [
            _def('parse', 0, deps={'Alpha'}),
            _def('parse', 1, deps={'Bravo'}),
            _def('parse', 2, deps={'Alpha', 'Charlie'}),
        ]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 1
        assert groups[0].dependencies == {'Alpha', 'Bravo', 'Charlie'}

    def test_interstitial_code_breaks_grouping(self) -> None:
        first = _def('parse', 0)
        first.had_interstitial_code = True
        defs = [first, _def('parse', 1)]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 2
        assert groups[0].name == 'parse'
        assert groups[1].name == 'parse'

    def test_mixed_groups(self) -> None:
        defs = [
            _def('parse', 0),
            _def('parse', 1),
            _def('alpha', 2),
            _def('bravo', 3),
        ]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 3
        assert groups[0].name == 'parse'
        assert len(groups[0].definitions) == 2
        assert groups[1].name == 'alpha'
        assert groups[2].name == 'bravo'

    def test_non_consecutive_same_name_not_grouped(self) -> None:
        defs = [_def('alpha', 0), _def('bravo', 1), _def('alpha', 2)]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 3
        assert groups[0].name == 'alpha'
        assert groups[1].name == 'bravo'
        assert groups[2].name == 'alpha'

    def test_single_definition(self) -> None:
        defs = [_def('alpha', 0)]
        groups = group_consecutive_same_name(defs)
        assert len(groups) == 1
        assert groups[0].name == 'alpha'
        assert len(groups[0].definitions) == 1


class TestHasDuplicateGroupNames:
    def test_consecutive_overloads_not_duplicate(self) -> None:
        groups = [
            _DefinitionGroup('parse', [_def('parse', 0), _def('parse', 1)], dependencies=set()),
            _DefinitionGroup('alpha', [_def('alpha', 2)], dependencies=set()),
        ]
        assert has_duplicate_group_names(groups) is False

    def test_duplicate_main_detected(self) -> None:
        groups = [
            _DefinitionGroup('main', [_def('main', 0)], dependencies=set()),
            _DefinitionGroup('alpha', [_def('alpha', 1)], dependencies=set()),
            _DefinitionGroup('main', [_def('main', 2)], dependencies=set()),
        ]
        assert has_duplicate_group_names(groups) is True

    def test_no_duplicates(self) -> None:
        groups = [
            _DefinitionGroup('alpha', [_def('alpha', 0)], dependencies=set()),
            _DefinitionGroup('bravo', [_def('bravo', 1)], dependencies=set()),
        ]
        assert has_duplicate_group_names(groups) is False

    def test_non_consecutive_duplicates_detected(self) -> None:
        groups = [
            _DefinitionGroup('alpha', [_def('alpha', 0)], dependencies=set()),
            _DefinitionGroup('bravo', [_def('bravo', 1)], dependencies=set()),
            _DefinitionGroup('alpha', [_def('alpha', 2)], dependencies=set()),
        ]
        assert has_duplicate_group_names(groups) is True


class TestOrderAlphabetical:
    def test_classes_before_functions(self) -> None:
        defs = [
            _def('alpha', 0),
            _def('Widget', 1, kind='class'),
        ]
        result = order_alphabetical(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['Widget', 'alpha']

    def test_functions_sorted_alphabetically(self) -> None:
        defs = [_def('widget', 0), _def('gadget', 1)]
        result = order_alphabetical(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['gadget', 'widget']

    def test_main_class_not_duplicated(self) -> None:
        defs = [
            _def('main', 0, kind='class'),
            _def('alpha', 1),
        ]
        result = order_alphabetical(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['main', 'alpha']

    def test_main_last_when_not_depended_on(self) -> None:
        defs = [
            _def('bravo', 0),
            _def('alpha', 1),
            _def('main', 2),
        ]
        result = order_alphabetical(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'bravo', 'main']

    def test_main_not_forced_last_when_depended_on(self) -> None:
        defs = [
            _def('alpha', 0, deps={'main'}),
            _def('main', 1),
        ]
        result = order_alphabetical(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'main']

    def test_mixed_main_group_is_not_anchored(self) -> None:
        definitions = [
            _def('main', 0),
            _def('main', 1, kind='class'),
            _def('zeta', 2),
        ]

        result = order_alphabetical(definitions, policy=SortPolicy())

        assert [definition.index for definition in result] == [0, 1, 2]

    def test_overload_groups_stay_together(self) -> None:
        defs = [
            _def('parse', 0),
            _def('parse', 1),
            _def('alpha', 2),
        ]
        result = order_alphabetical(defs, policy=SortPolicy())
        names = [d.name for d in result]
        assert names == ['alpha', 'parse', 'parse']


class TestOrderDefinitionsScopeOrder:
    def test_alphabetical(self) -> None:
        defs = [_def('bravo', 0), _def('alpha', 1)]
        result = order_definitions(
            defs,
            order='alphabetical',
            policy=SortPolicy(),
        )
        assert [d.name for d in result] == ['alpha', 'bravo']

    def test_banner_sections_keep_cross_section_dependency_and_main_local(self) -> None:
        defs = [
            _def('main', 0, deps={'zulu'}, section=0),
            _def('zulu', 1, deps={'alpha'}, section=0),
            _def('bravo', 2, deps={'alpha'}, section=1),
            _def('alpha', 3, section=1),
        ]

        result = order_within_sections(
            defs,
            lambda section: order_definitions(
                section,
                order='dependency',
                policy=SortPolicy(),
            ),
        )

        assert [definition.name for definition in result] == ['zulu', 'main', 'alpha', 'bravo']

    def test_banner_sections_keep_overloads_flat_in_alphabetical_order(self) -> None:
        defs = [
            _def('zulu', 0, section=0),
            _def('parse', 1, section=0),
            _def('parse', 2, section=0),
            _def('delta', 3, section=1),
            _def('charlie', 4, section=1),
        ]

        result = order_within_sections(
            defs,
            lambda section: order_definitions(
                section,
                order='alphabetical',
                policy=SortPolicy(),
            ),
        )

        assert [definition.name for definition in result] == [
            'parse',
            'parse',
            'zulu',
            'charlie',
            'delta',
        ]

    def test_banner_sections_terminate_same_name_groups(self) -> None:
        defs = [
            _def('parse', 0, section=0),
            _def('parse', 1, section=1),
        ]

        groups = group_consecutive_same_name(defs, respect_sections=True)

        assert [group.name for group in groups] == ['parse', 'parse']

    def test_dependency(self) -> None:
        defs = [_def('bravo', 0, deps={'alpha'}), _def('alpha', 1)]
        result = order_definitions(
            defs,
            order='dependency',
            policy=SortPolicy(),
        )
        assert [d.name for d in result] == ['alpha', 'bravo']

    def test_none_returns_unchanged(self) -> None:
        defs = [_def('bravo', 0, deps={'alpha'}), _def('alpha', 1)]
        result = order_definitions(
            defs,
            order='none',
            policy=SortPolicy(),
        )
        assert [d.name for d in result] == ['bravo', 'alpha']


class TestPrivateDefinitionGroup:
    def test_first_index(self) -> None:
        d1 = Definition(
            index=3,
            name='foo',
            start_line=10,
            end_line=12,
            kind='function',
            source='def foo(): ...',
        )
        d2 = Definition(
            index=4,
            name='foo',
            start_line=14,
            end_line=16,
            kind='function',
            source='def foo(): pass',
        )
        g = _DefinitionGroup(name='foo', definitions=[d1, d2], dependencies=set())
        assert g._first_index == 3


def _def(
    name: str,
    index: int,
    *,
    deps: set[str] | None = None,
    kind: DefinitionKind = 'function',
    section: int = 0,
) -> Definition:
    return Definition(
        index=index,
        name=name,
        start_line=index * 10,
        end_line=index * 10 + 5,
        kind=kind,
        source=f'def {name}(): pass',
        dependencies=deps or set(),
        section=section,
    )


class TestOrderDependency:
    def test_all_function_main_group_stays_atomic_and_last(self) -> None:
        definitions = [_def('main', 0), _def('main', 1), _def('alpha', 2)]

        result = order_dependency(definitions, policy=SortPolicy())

        assert [definition.index for definition in result] == [2, 0, 1]

    def test_already_ordered(self) -> None:
        defs = [_def('alpha', 0), _def('bravo', 1, deps={'alpha'})]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'bravo']

    def test_chain_dependency(self) -> None:
        defs = [
            _def('charlie', 0, deps={'bravo'}),
            _def('bravo', 1, deps={'alpha'}),
            _def('alpha', 2),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'bravo', 'charlie']

    def test_class_inheritance(self) -> None:
        defs = [
            _def('Derived', 0, deps={'Base'}, kind='class'),
            _def('Base', 1, kind='class'),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['Base', 'Derived']

    def test_class_main_not_forced_last(self) -> None:
        defs = [
            _def('zeta', 0),
            _def('main', 1, kind='class', deps={'Base'}),
            _def('Base', 2, kind='class'),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        names = [d.name for d in result]
        # main should come before zeta (alphabetical tie-break), not forced last
        assert names.index('main') < names.index('zeta')

    def test_cycle_alphabetical(self) -> None:
        defs = [
            _def('bravo', 0, deps={'alpha'}),
            _def('alpha', 1, deps={'bravo'}),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'bravo']

    def test_decorator_dep(self) -> None:
        defs = [
            _def('widget', 0),
            _def('gadget', 1, deps={'widget'}),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['widget', 'gadget']

    def test_dependency_between_cycles_is_preserved(self) -> None:
        defs = [
            _def('alpha', 0, deps={'bravo', 'yankee'}),
            _def('bravo', 1, deps={'alpha'}),
            _def('yankee', 2, deps={'zulu'}),
            _def('zulu', 3, deps={'yankee'}),
        ]

        result = order_dependency(defs, policy=SortPolicy())

        names = [definition.name for definition in result]
        assert names.index('yankee') < names.index('alpha')

    def test_empty_input(self) -> None:
        assert order_dependency([], policy=SortPolicy()) == []

    def test_func_main_last_when_not_depended_on(self) -> None:
        defs = [
            _def('main', 0, deps={'alpha'}),
            _def('alpha', 1),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'main']

    def test_main_not_forced_last_when_depended_on(self) -> None:
        alpha = _def('alpha', 0, deps={'main'})
        alpha.eager_refs = {'main'}
        defs = [alpha, _def('main', 1)]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['main', 'alpha']

    def test_mixed_classes_and_functions(self) -> None:
        defs = [
            _def('format_widget', 0, deps={'Widget'}),
            _def('Widget', 1, kind='class'),
            _def('create_widget', 2, deps={'Widget'}),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['Widget', 'create_widget', 'format_widget']

    @pytest.mark.parametrize(
        'definitions',
        [
            [_def('main', 0), _def('main', 1, kind='class'), _def('zeta', 2)],
            [_def('main', 0, kind='class'), _def('main', 1), _def('zeta', 2)],
        ],
        ids=['function-then-class', 'class-then-function'],
    )
    def test_mixed_main_group_stays_atomic(self, definitions: list[Definition]) -> None:
        result = order_dependency(definitions, policy=SortPolicy())

        main_positions = [
            index for index, definition in enumerate(result) if definition.name == 'main'
        ]
        assert main_positions == [0, 1]
        assert [definition.index for definition in result[:2]] == [0, 1]

    def test_overload_group_deps(self) -> None:
        defs = [
            _def('parse', 0, deps={'Token'}),
            _def('parse', 1, deps={'Node'}),
            _def('Token', 2, kind='class'),
            _def('Node', 3, kind='class'),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        names = [d.name for d in result]
        # Both Token and Node must precede the parse group
        assert names.index('Token') < names.index('parse')
        assert names.index('Node') < names.index('parse')

    def test_simple_sort(self) -> None:
        defs = [
            _def('bravo', 0, deps={'alpha'}),
            _def('alpha', 1),
        ]
        result = order_dependency(defs, policy=SortPolicy())
        assert [d.name for d in result] == ['alpha', 'bravo']

    def test_source_tie_key_collision_preserves_input_order(self) -> None:
        defs = [
            _def('zed', 0),
            _def('alpha', 0),
            _def('bravo', 0),
            _def('charlie', 0),
        ]

        result = order_dependency(
            defs,
            policy=SortPolicy(tie_break='source'),
        )

        assert [definition.name for definition in result] == [
            'zed',
            'alpha',
            'bravo',
            'charlie',
        ]


def _order_for(source: str, *, policy: SortPolicy = _DEFAULT_POLICY) -> list[Definition]:
    parsed = parse_definitions(
        source,
        banners='ignore',
        banner_pattern=None,
        hoist=False,
    )
    return order_definitions(
        parsed.definitions,
        order='dependency',
        policy=policy,
    )


def test_annotation_reference_does_not_lift_main() -> None:
    src = 'def worker(value: main):\n    return value\n\n\ndef main():\n    return 1\n'
    result = sort_file(
        src,
        options=EngineOptions(policy=SortPolicy(), add_future=True),
    )
    out = result.new_source
    assert 'from __future__ import annotations' in out
    assert out.rstrip().endswith('return 1')  # main stays last


def test_body_reference_no_longer_lifts_main() -> None:
    src = 'def run():\n    main()\n\n\ndef main():\n    run()\n'
    assert [d.name for d in _order_for(src)] == ['run', 'main']


def test_eager_reference_lifts_main() -> None:
    src = 'def main():\n    return 1\n\n\ndef run(cb=main):\n    return cb\n'
    assert [d.name for d in _order_for(src)] == ['main', 'run']


def test_main_sorted_policy() -> None:
    src = 'def zed():\n    return 1\n\n\ndef main():\n    return 2\n'
    result = _order_for(src, policy=SortPolicy(main='sorted'))
    assert [d.name for d in result] == ['main', 'zed']


def test_source_ties_preserve_order() -> None:
    src = 'def zed():\n    return 1\n\n\ndef alpha():\n    return 2\n'
    result = _order_for(src, policy=SortPolicy(tie_break='source'))
    assert [d.name for d in result] == ['zed', 'alpha']
