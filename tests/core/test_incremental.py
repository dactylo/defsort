import ast
from collections.abc import Sequence
from types import FunctionType
from typing import Any, cast

import pytest

import defsort._core._incremental as incremental
import defsort._core._methods as methods_module
from defsort._core._api import sort_file, sort_file_incremental
from defsort._core._banners import collect_banner_constraints
from defsort._core._incremental import (
    _HookSortResult,
    _IncrementalSortResult,
    sort_for_hook_detailed,
    sort_incremental_detailed,
    sort_incremental_impl,
)
from defsort._core._methods import (
    collect_method_coordinates,
    parse_class_methods,
    per_class_incremental_sort,
)
from defsort._core._parse import parse_definitions
from defsort._types import (
    BannerConstraint,
    BannerMode,
    ClassBlocker,
    ClassSlice,
    Definition,
    DefsortConfig,
    EngineOptions,
    HeldItem,
    MethodDefinition,
    SectionBoundary,
    SortPolicy,
    SortResult,
    SortScopeOrder,
    sort_policy_from_config,
)
from tests._helpers import execute_source
from tests.core._helpers import _SRC_BASELINE, _SRC_WITH_NEW

_DEFAULT_SORT_POLICY = SortPolicy()

_SRC_OVERLOAD_CONFORMING = """from typing import overload


class C:
    def helper(self, x):
        return x

    @overload
    def foo(self, x: int) -> int: ...

    @overload
    def foo(self, x: str) -> str: ...

    def foo(self, x, cb=helper):
        return cb(x)
"""


class TestClassLocalBlockerOwnership:
    def test_blocked_class_does_not_block_file(self) -> None:
        baseline = (
            'class C:\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    CONST = 1\n'
            '\n'
            '    def c(self):\n'
            '        pass\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    pass\n'
            '\n'
            '\n'
            'def zeta():\n'
            '    pass\n'
        )
        blocked_class = (
            'class C:\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    CONST = 1\n'
            '\n'
            '    def c(self):\n'
            '        pass\n'
            '\n'
            '    def a(self):\n'
            '        pass\n'
        )
        current = (
            blocked_class
            + '\n\ndef alpha():\n    pass\n'
            + '\n\ndef zeta():\n    pass\n'
            + '\n\ndef beta():\n    pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        full = sort_file(current, options=options)
        inc = sort_file_incremental(current, baseline, options=options)
        for result in (full, inc):
            assert not result.skipped
            assert result.blocker is None
            assert result.new_source.index('def alpha') < result.new_source.index('def beta')
            assert result.new_source.index('def beta') < result.new_source.index('def zeta')
            assert result.new_source.startswith(blocked_class)
            assert [b.class_name for b in result.class_blockers] == ['C']
            assert result.class_blockers[0].blocker == 'interstitial'


class TestIncrementalAnnotationOrderSafety:
    def test_blocker_precedence_keeps_structural_top_level_blocker(self) -> None:
        baseline = (
            'class Example:\n'
            '    def zeta_decorator(function):\n'
            '        return function\n\n\n'
            'class ZetaValue:\n'
            '    pass\n'
        )
        current = (
            'class Example:\n'
            '    def zeta_decorator(function):\n'
            '        return function\n\n'
            '    @zeta_decorator\n'
            '    def alpha(self):\n'
            '        return None\n\n\n'
            'class ZetaValue:\n'
            '    pass\n\n'
            'registry = {}\n\n'
            'class Alpha:\n'
            '    pass\n'
        )
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'interstitial'
        assert result.new_source == current

    def test_class_suite_annotation_adds_future_with_soft_method_blocker(self) -> None:
        baseline = (
            'class Problem:\n'
            '    def bravo(self):\n'
            '        return self.alpha()\n\n'
            '    def alpha(self):\n'
            '        return None\n\n\n'
            'class ZetaType:\n'
            '    pass\n'
        )
        current = (
            'class Problem:\n'
            '    def bravo(self):\n'
            '        return self.alpha()\n\n'
            '    def alpha(self):\n'
            '        return None\n\n\n'
            'class ZetaType:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    class Nested:\n'
            '        value: ZetaType\n'
        )
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
            add_future=True,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.skipped is False
        assert result.blocker is None
        assert result.warning is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='Problem',
                occurrence=1,
                blocker='non_conforming',
                message='baseline not in target order',
            ),
        )
        assert 'from __future__ import annotations' in result.new_source
        assert result.new_source.index('class Alpha') < result.new_source.index('class ZetaType')
        execute_source(result.new_source)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        assert repeated.new_source == result.new_source

    def test_class_suite_annotation_cli_adds_future(self) -> None:
        baseline = 'class ZetaType:\n    pass\n'
        current = (
            'class ZetaType:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    class Nested:\n'
            '        value: ZetaType\n'
        )
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=True,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker is None
        assert 'from __future__ import annotations' in result.new_source
        assert result.new_source.index('class Alpha') < result.new_source.index('class ZetaType')
        execute_source(result.new_source)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        assert repeated.new_source == result.new_source

    @pytest.mark.parametrize('surface', ['public', 'hook'])
    def test_class_suite_annotation_existing_future_is_preserved(self, surface: str) -> None:
        future = 'from __future__ import annotations\n\n\n'
        baseline = f'{future}class ZetaType:\n    pass\n'
        current = (
            f'{future}class ZetaType:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    class Nested:\n'
            '        value: ZetaType\n'
        )

        if surface == 'public':
            result = _sort_incremental(
                current,
                baseline,
                policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            )
        else:
            result = sort_incremental_detailed(
                current,
                baseline,
                policy=sort_policy_from_config(
                    DefsortConfig(
                        definition_order='alphabetical',
                        method_order='none',
                    )
                ),
            )

        assert result.blocker is None
        assert result.new_source is not None
        assert 'from __future__ import annotations' in result.new_source
        assert result.new_source.index('class Alpha') < result.new_source.index('class ZetaType')
        execute_source(result.new_source)

    @pytest.mark.parametrize('surface', ['public', 'hook'])
    def test_class_suite_annotation_without_addition_blocks(self, surface: str) -> None:
        baseline = 'class ZetaType:\n    pass\n'
        current = (
            'class ZetaType:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    class Nested:\n'
            '        value: ZetaType\n'
        )

        if surface == 'public':
            result = _sort_incremental(
                current,
                baseline,
                policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            )
        else:
            result = sort_incremental_detailed(
                current,
                baseline,
                policy=sort_policy_from_config(
                    DefsortConfig(
                        definition_order='alphabetical',
                        method_order='none',
                    )
                ),
            )

        assert result.blocker == 'forward_refs'
        assert result.new_source is None
        assert 'from __future__ import annotations' not in current

    def test_eager_precedes_method_annotation_hazard(self) -> None:
        baseline = (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n\n'
            'class ZetaValue:\n'
            '    pass\n'
        )
        current = (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n\n\n'
            'class ZetaValue:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    value = ZetaValue()\n'
        )
        execute_source(current)

        result = _sort_incremental(
            current,
            baseline,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )

        assert result.blocker == 'eager_order'
        assert result.new_source is None

    def test_eager_precedes_top_level_annotation_hazard(self) -> None:
        baseline = (
            'class Example:\n'
            '    def zeta_decorator(function):\n'
            '        return function\n\n\n'
            'class ZetaType:\n'
            '    pass\n'
        )
        current = (
            'class Example:\n'
            '    def zeta_decorator(function):\n'
            '        return function\n\n'
            '    @zeta_decorator\n'
            '    def alpha(self):\n'
            '        return None\n\n\n'
            'class ZetaType:\n'
            '    pass\n\n\n'
            'class Alpha:\n'
            '    value: ZetaType\n'
        )
        execute_source(current)

        result = _sort_incremental(
            current,
            baseline,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )

        assert result.blocker == 'eager_order'
        assert result.new_source is None

    def test_method_annotation_cli_adds_future(self) -> None:
        baseline = 'class Example:\n    def zeta_type(self):\n        return None\n'
        current = (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        )
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=True,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker is None
        assert 'from __future__ import annotations' in result.new_source
        assert result.new_source.index('def alpha') < result.new_source.index('def zeta_type')
        execute_source(result.new_source)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        assert repeated.new_source == result.new_source

    @pytest.mark.parametrize('surface', ['public', 'hook'])
    def test_method_annotation_existing_future_is_preserved(self, surface: str) -> None:
        future = 'from __future__ import annotations\n\n\n'
        baseline = f'{future}class Example:\n    def zeta_type(self):\n        return None\n'
        current = (
            f'{future}class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        )

        if surface == 'public':
            result = _sort_incremental(
                current,
                baseline,
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            )
        else:
            result = sort_incremental_detailed(
                current,
                baseline,
                policy=sort_policy_from_config(
                    DefsortConfig(
                        definition_order='none',
                        method_order='alphabetical',
                    )
                ),
            )

        assert result.blocker is None
        assert result.new_source is not None
        assert 'from __future__ import annotations' in result.new_source
        assert result.new_source.index('def alpha') < result.new_source.index('def zeta_type')
        execute_source(result.new_source)

    @pytest.mark.parametrize('surface', ['public', 'hook'])
    def test_method_annotation_without_addition_blocks(self, surface: str) -> None:
        baseline = 'class Example:\n    def zeta_type(self):\n        return None\n'
        current = (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        )

        if surface == 'public':
            result = _sort_incremental(
                current,
                baseline,
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            )
        else:
            result = sort_incremental_detailed(
                current,
                baseline,
                policy=sort_policy_from_config(
                    DefsortConfig(
                        definition_order='none',
                        method_order='alphabetical',
                    )
                ),
            )

        assert result.blocker == 'forward_refs'
        assert result.new_source is None
        assert 'from __future__ import annotations' not in current


class TestIncrementalBanners:
    def test_banner_boundary_only_add_is_preserved_byte_for_byte(self) -> None:
        baseline = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        current = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef bravo():\n    pass\n'

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == current

    @pytest.mark.parametrize(
        'current',
        [
            'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n',
            (
                'def alpha():\n'
                '    pass\n'
                '\n\n'
                'def bravo():\n'
                '    pass\n'
                '\n\n'
                '# --------\n'
                '\n\n'
                'def charlie():\n'
                '    pass\n'
            ),
        ],
    )
    def test_banner_boundary_remove_or_move_preserves_global_existing_order(
        self,
        current: str,
    ) -> None:
        baseline = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
            '\n\n'
            'def charlie():\n'
            '    pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == current
        assert result.blocker is None

    def test_banner_constraints_keep_original_coordinates_after_top_level_reorder(self) -> None:
        baseline = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n\n'
            'def tail():\n'
            '    return C()\n'
        )
        current = 'def aardvark():\n    return C()\n\n\n' + baseline
        original_lines = current.splitlines()
        banner_line = original_lines.index('    # --------') + 1
        zulu_line = original_lines.index('    def zulu(self):') + 1
        alpha_line = original_lines.index('    def alpha(self):') + 1

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(banners='barriers'),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source.index('class C') < result.new_source.index('def aardvark')
        original_coordinates = collect_method_coordinates(
            current,
            ast.parse(current),
            banners='barriers',
            banner_pattern=None,
        )
        rewritten_tree = ast.parse(result.new_source)
        rewritten_cls = next(
            node for node in rewritten_tree.body if isinstance(node, ast.ClassDef)
        )
        assert rewritten_cls.end_lineno is not None
        rewritten_lines = result.new_source.splitlines(keepends=True)
        rewritten_slice = ClassSlice(
            lines=rewritten_lines[rewritten_cls.lineno - 1 : rewritten_cls.end_lineno],
            start_line=rewritten_cls.lineno,
        )
        baseline_tree = ast.parse(baseline)
        baseline_cls = next(node for node in baseline_tree.body if isinstance(node, ast.ClassDef))
        assert baseline_cls.end_lineno is not None
        baseline_lines = [str(line) for line in baseline.splitlines(keepends=True)]
        baseline_slice = ClassSlice(
            lines=baseline_lines[baseline_cls.lineno - 1 : baseline_cls.end_lineno],
            start_line=baseline_cls.lineno,
        )
        class_result = per_class_incremental_sort(
            rewritten_cls,
            baseline_cls,
            parse_class_methods(
                rewritten_cls,
                rewritten_slice,
                banners='barriers',
            ),
            parse_class_methods(
                baseline_cls,
                baseline_slice,
                banners='barriers',
            ),
            rewritten_slice,
            policy=SortPolicy(
                method_order='dependency',
                banners='barriers',
            ),
            original_parse=original_coordinates[('C', 1)],
        )

        constraint = class_result.banner_constraints[0]
        retained_boundary = next(
            part
            for part in original_coordinates[('C', 1)].boundary_layout
            if isinstance(part, SectionBoundary)
        )
        assert retained_boundary.start_line == banner_line
        assert constraint.boundary.start_line == banner_line
        assert {item.name: item.line for item in constraint.held} == {
            'alpha': alpha_line,
            'zulu': zulu_line,
        }

    def test_banner_constraints_survive_incremental_constraint_only_noop(self) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file_incremental(
            source,
            source,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == source
        assert len(result.banner_constraints) == 1
        assert [(item.name, item.direction) for item in result.banner_constraints[0].held] == [
            ('zulu', 'after'),
            ('alpha', 'before'),
        ]

    def test_banner_constraints_survive_incremental_target_blocker(self) -> None:
        baseline = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        current = (
            'def bravo():\n    pass\n\n\n'
            'def alpha():\n    pass\n\n\n'
            '# --------\n\n\n'
            'def aardvark():\n    pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.blocker == 'target_incompatible'
        assert len(result.banner_constraints) == 1
        constraint = result.banner_constraints[0]
        assert constraint.boundary.start_line == 9
        assert [(item.name, item.direction) for item in constraint.held] == [
            ('bravo', 'after'),
            ('aardvark', 'before'),
        ]

    def test_banner_current_section_conflict_is_target_incompatible(self) -> None:
        baseline = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        current = 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n\n\n# --------\n'

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.blocker == 'target_incompatible'

    def test_banner_global_existing_reorder_is_user_reordered(self) -> None:
        baseline = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef bravo():\n    pass\n'
        current = 'def bravo():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.blocker == 'user_reordered'

    def test_banner_method_boundary_move_is_preserved_byte_for_byte(self) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    def charlie(self):\n'
            '        pass\n'
        )
        current = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            ' \n'
            '    # --------\n'
            '\t\n'
            '    def charlie(self):\n'
            '        pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == current
        assert result.blocker is None

    def test_banner_method_boundary_only_edit_is_preserved_byte_for_byte(self) -> None:
        baseline = (
            'class C:\n    def alpha(self):\n        pass\n\n    def bravo(self):\n        pass\n'
        )
        current = baseline.replace(
            '\n    def bravo',
            '\n    # --------\n\n    def bravo',
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == current

    def test_banner_method_boundary_removal_is_preserved_byte_for_byte(self) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    def charlie(self):\n'
            '        pass\n'
        )
        current = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            ' \t\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\t\n'
            '    def charlie(self):\n'
            '        pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == current
        assert result.blocker is None

    def test_banner_method_current_section_conflict_is_target_incompatible(self) -> None:
        baseline = (
            'class C:\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        current = baseline.replace(
            '\n    # --------\n',
            '\n',
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.blocker is None
        assert [blocker.blocker for blocker in result.class_blockers] == ['target_incompatible']

    def test_banner_method_global_existing_reorder_is_user_reordered(self) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
        )
        current = (
            'class C:\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.blocker is None
        assert [blocker.blocker for blocker in result.class_blockers] == ['user_reordered']

    def test_banner_method_only_strip_does_not_suppress_hoist(self) -> None:
        source = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n\n'
            'MAX_SIZE = 100\n'
            '\n\n'
            'def tail():\n'
            '    return MAX_SIZE\n'
        )

        result = sort_file_incremental(
            source,
            source,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='none',
                    banners='strip',
                ),
                hoist=True,
                add_future=False,
                remove_future=False,
            ),
        )

        assert '# --------' not in result.new_source
        assert result.new_source.index('MAX_SIZE = 100') < result.new_source.index('class C')

    def test_banner_new_methods_merge_in_each_current_section(self) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def delta(self):\n'
            '        pass\n'
        )
        current = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def aardvark(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def delta(self):\n'
            '        pass\n'
            '\n'
            '    def charlie(self):\n'
            '        pass\n'
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        banner = result.new_source.index('    # --------')
        assert result.new_source.index('def aardvark') < result.new_source.index('def alpha')
        assert result.new_source.index('def alpha') < banner
        assert banner < result.new_source.index('def charlie')
        assert result.new_source.index('def charlie') < result.new_source.index('def delta')

    @pytest.mark.parametrize('new_name', ['aardvark', 'charlie'])
    def test_banner_new_top_level_definition_merges_in_current_section(
        self,
        new_name: str,
    ) -> None:
        baseline = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef delta():\n    pass\n'
        if new_name == 'aardvark':
            current = baseline.replace(
                '\n\n# --------',
                '\n\ndef aardvark():\n    pass\n\n\n# --------',
            )
        else:
            current = baseline + '\n\ndef charlie():\n    pass\n'

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='none',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        banner = result.new_source.index('# --------')
        if new_name == 'aardvark':
            assert result.new_source.index('def aardvark') < result.new_source.index('def alpha')
            assert result.new_source.index('def alpha') < banner
        else:
            assert banner < result.new_source.index('def charlie')
            assert result.new_source.index('def charlie') < result.new_source.index('def delta')

    def test_banner_non_strip_method_change_does_not_suppress_hoist(self) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def delta(self):\n'
            '        pass\n'
            '\n\n'
            'MAX_SIZE = 100\n'
            '\n\n'
            'def tail():\n'
            '    return MAX_SIZE\n'
        )
        current = baseline.replace(
            '\n\nMAX_SIZE = 100',
            '\n\n    def beta(self):\n        pass\n\n\nMAX_SIZE = 100',
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                hoist=True,
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source.index('MAX_SIZE = 100') < result.new_source.index('class C')
        assert result.new_source.index('def alpha') < result.new_source.index('def beta')
        assert result.new_source.index('def beta') < result.new_source.index('def delta')

    def test_banner_pipeline_keeps_duplicate_class_coordinates_from_original(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # first class\n'
            '    # --------\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n\n'
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # second class\n'
            '    # ========\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n\n'
            'def tail():\n'
            '    return C()\n'
        )
        current = 'def aardvark():\n    return C()\n\n\n' + baseline
        original_lines = current.splitlines()
        banner_line = original_lines.index('    # second class') + 1
        zulu_line = original_lines.index('    def zulu(self):') + 1
        alpha_line = len(original_lines) - original_lines[::-1].index('    def alpha(self):')
        captured: list[BannerConstraint] = []

        def recording_collect_banner_constraints[T: (Definition, MethodDefinition)](
            original: Sequence[T],
            unconstrained: Sequence[T],
            boundaries: Sequence[SectionBoundary],
            *,
            class_name: str | None = None,
        ) -> list[BannerConstraint]:
            constraints = collect_banner_constraints(
                original,
                unconstrained,
                boundaries,
                class_name=class_name,
            )
            captured.extend(constraints)
            return constraints

        monkeypatch.setattr(
            'defsort._core._methods.collect_banner_constraints',
            recording_collect_banner_constraints,
        )

        result = sort_file_incremental(
            current,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source.index('class C') < result.new_source.index('def aardvark')
        assert len(captured) == 1
        constraint = captured[0]
        assert constraint.boundary.start_line == banner_line
        assert constraint.boundary.text == '# second class\n# ========'
        assert {item.name: item.line for item in constraint.held} == {
            'alpha': alpha_line,
            'zulu': zulu_line,
        }

    @pytest.mark.parametrize(
        ('baseline', 'current', 'expected'),
        [
            (
                'def alpha():\r\n    pass\r',
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
                'class C:\r\n    def alpha(self):\n        pass\r',
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
        ids=['top-level', 'method-no-movement-fast-path'],
    )
    def test_banner_strip_exact_incremental_span_matrix(
        self,
        baseline: str,
        current: str,
        expected: str,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='alphabetical',
                banners='strip',
                banner_pattern='^# REGION .+$',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    def test_banner_strip_none_preserves_unrelated_top_level_bytes(self) -> None:
        banner = '# --------  \n# banner detail\t\n'
        source = f'def alpha():  \n    pass\n \t\n{banner}\t\ndef bravo():\n    pass  \n'

        result = sort_file_incremental(
            source,
            source,
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

        assert result.new_source == source.replace(banner, '')

    def test_banner_strip_removes_top_level_and_method_boundaries_without_new_items(self) -> None:
        source = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # ========\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def delta():\n'
            '    pass\n'
        )

        result = sort_file_incremental(
            source,
            source,
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

        assert '# ========' not in result.new_source
        assert '# --------' not in result.new_source

    @pytest.mark.parametrize(
        ('mode', 'expected'),
        [
            (
                'barriers',
                None,
            ),
            (
                'ignore',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    # REGION\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'strip',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
        ],
        ids=['barriers', 'ignore', 'strip'],
    )
    def test_custom_method_banner_pattern_preserves_exact_layout(
        self,
        mode: BannerMode,
        expected: str | None,
    ) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # REGION\n'
            '    def zulu(self):\n'
            '        pass\n'
        )
        current = baseline + ('    # for bravo\n    def bravo(self):\n        pass\n')
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
                banners=mode,
                banner_pattern='^# REGION$',
            ),
            add_future=False,
            remove_future=False,
        )

        if expected is None:
            result = sort_file_incremental(current, baseline, options=options)
            assert result.skipped
            assert result.blocker == 'layout'
            assert result.new_source == current
            assert not result.method_sorts
            return

        result = sort_file_incremental(current, baseline, options=options)
        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    @pytest.mark.parametrize(
        'policy',
        [SortPolicy(method_order='alphabetical'), SortPolicy(method_bodies='ignore')],
    )
    def test_invalid_baseline_banner_metadata_skips_unused_bodies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        policy: SortPolicy,
    ) -> None:
        source = (
            'class C:\n'
            '    def zeta(self):\n        return self.alpha()\n\n'
            '    # ----------\n\n'
            '    def alpha(self):\n        return 1\n'
        )

        def fail_body_collection(*_args: object, **_kwargs: object) -> set[str]:
            pytest.fail('banner diagnostic analyzed a policy-unused method body')

        monkeypatch.setattr(methods_module, 'collect_body_refs', fail_body_collection)
        result = sort_file_incremental(
            source,
            'def broken(',
            options=EngineOptions(policy=policy, add_future=False, remove_future=False),
        )

        assert result.new_source == source
        assert result.blocker == 'non_conforming'
        assert result.skipped
        assert result.method_sorts == []
        assert result.class_blockers == ()
        assert result.banner_constraints == [
            BannerConstraint(
                scope='methods',
                class_name='C',
                boundary=SectionBoundary(
                    section=1,
                    start_line=5,
                    end_line=5,
                    raw='    # ----------\n',
                    text='# ----------',
                ),
                held=(
                    HeldItem(name='zeta', kind='method', occurrence=1, line=2, direction='after'),
                    HeldItem(
                        name='alpha', kind='method', occurrence=1, line=7, direction='before'
                    ),
                ),
            ),
        ]

    @pytest.mark.parametrize(
        ('mode', 'order', 'expected'),
        [
            (
                'barriers',
                'alphabetical',
                None,
            ),
            (
                'barriers',
                'dependency',
                None,
            ),
            (
                'barriers',
                'none',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'ignore',
                'alphabetical',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'ignore',
                'dependency',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'ignore',
                'none',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'strip',
                'alphabetical',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'strip',
                'dependency',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'strip',
                'none',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # for bravo\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
        ],
        ids=[
            'barriers-alphabetical',
            'barriers-dependency',
            'barriers-none',
            'ignore-alphabetical',
            'ignore-dependency',
            'ignore-none',
            'strip-alphabetical',
            'strip-dependency',
            'strip-none',
        ],
    )
    def test_method_banner_mode_order_matrix_preserves_exact_layout(
        self,
        mode: BannerMode,
        order: SortScopeOrder,
        expected: str | None,
    ) -> None:
        baseline = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # --------\n'
            '    def zulu(self):\n'
            '        pass\n'
        )
        current = baseline + ('    # for bravo\n    def bravo(self):\n        pass\n')
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order=order, banners=mode),
            add_future=False,
            remove_future=False,
        )

        if expected is None:
            result = sort_file_incremental(current, baseline, options=options)
            assert result.skipped
            assert result.blocker == 'layout'
            assert result.new_source == current
            assert not result.method_sorts
            return

        result = sort_file_incremental(current, baseline, options=options)
        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected


class TestIncrementalEagerOrderSafety:
    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    def test_called_sibling_body_blocks_incremental_inversion(self, *, force: bool) -> None:
        baseline = 'def zeta_compute():\n    return 42\n'
        current = (
            baseline + '\n\nclass Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            force=force,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'eager_order'
        assert result.new_source == current
        assert result.original_source == current

    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    def test_dynamic_all_candidate_returns_original_source(self, *, force: bool) -> None:
        baseline = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            "__all__ = [name for name in globals() if not name.startswith('_')]\n"
        )
        current = (
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
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            force=force,
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'eager_order'
        assert result.original_source == current
        assert result.new_source == current

    def test_dynamic_all_nested_assignment_blocks_incremental_sort(self) -> None:
        postamble = "try:\n    __all__ = ['alpha']\nexcept Exception:\n    pass\n"
        baseline = f'def zeta():\n    return 1\n\n\n{postamble}'
        current = f'def zeta():\n    return 1\n\n\ndef alpha():\n    return 2\n\n\n{postamble}'
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'eager_order'
        assert result.original_source == current
        assert result.new_source == current

    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    @pytest.mark.parametrize(
        ('baseline', 'current'),
        [
            (
                'class Example:\n    def zeta_decorator(function):\n        return function\n',
                (
                    'class Example:\n'
                    '    def zeta_decorator(function):\n'
                    '        return function\n\n'
                    '    @zeta_decorator\n'
                    '    def alpha(self):\n'
                    '        return 1\n'
                ),
            ),
            (
                'class Example:\n    def zeta_default():\n        return 1\n',
                (
                    'class Example:\n'
                    '    def zeta_default():\n'
                    '        return 1\n\n'
                    '    def alpha(self, value=zeta_default()):\n'
                    '        return value\n'
                ),
            ),
        ],
        ids=['method-decorator', 'method-default'],
    )
    def test_eager_method_candidate_returns_original_source(
        self,
        baseline: str,
        current: str,
        *,
        force: bool,
    ) -> None:
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            force=force,
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'eager_order'
        assert result.original_source == current
        assert result.new_source == current

    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    @pytest.mark.parametrize(
        ('baseline', 'reader'),
        [
            (
                'def zeta_decorator(function):\n    return function\n',
                '@zeta_decorator\ndef alpha():\n    return 1\n',
            ),
            (
                'def zeta_default():\n    return 1\n',
                'def alpha(value=zeta_default()):\n    return value\n',
            ),
            (
                'class ZetaBase:\n    pass\n',
                'class Alpha(ZetaBase):\n    pass\n',
            ),
            (
                'def zeta_decorator(cls):\n    return cls\n',
                '@zeta_decorator\nclass Alpha:\n    pass\n',
            ),
            (
                (
                    'def zeta_meta(name, bases, namespace):\n'
                    '    return type(name, bases, namespace)\n'
                ),
                'class Alpha(metaclass=zeta_meta):\n    pass\n',
            ),
            (
                'class ZetaValue:\n    pass\n',
                'class Alpha:\n    value = ZetaValue()\n',
            ),
            (
                'class ZetaValue:\n    pass\n',
                'class Alpha:\n    class Nested:\n        value = ZetaValue()\n',
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
    def test_eager_top_level_candidate_returns_original_source(
        self,
        baseline: str,
        reader: str,
        *,
        force: bool,
        allow_reordering: bool,
    ) -> None:
        current = f'{baseline}\n\n{reader}'
        execute_source(current)
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                evaluation_order='allow-reordering' if allow_reordering else 'preserve',
            ),
            force=force,
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker == 'eager_order'
        assert result.original_source == current
        assert result.new_source == current

    def test_method_default_side_effect_order_is_preserved_when_forced(self) -> None:
        baseline = (
            'events = []\n\n\n'
            'class Example:\n'
            "    def zeta(self, value=(events.append('zeta'), 0)[1]):\n"
            '        pass\n'
        )
        current = baseline + (
            '\n    def omega(self):\n'
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
            add_future=False,
            remove_future=False,
        )

        original_namespace = execute_source(current)
        result = sort_file_incremental(current, baseline, options=options)
        accepted_namespace = execute_source(result.new_source)

        assert result.new_source == expected
        assert result.new_source != result.original_source
        assert result.skipped is False
        assert result.blocker is None
        assert len(result.method_sorts) == 1
        assert accepted_namespace['events'] == original_namespace['events']
        assert result.new_source.index('def zeta') < result.new_source.index('def alpha')
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        assert repeated.new_source == result.new_source

    def test_static_literal_all_allows_incremental_sort(self) -> None:
        baseline = "def zeta():\n    return 1\n\n\n__all__ = ['zeta', 'alpha']\n"
        current = (
            'def zeta():\n'
            '    return 1\n'
            '\n'
            '\n'
            'def alpha():\n'
            '    return 2\n'
            '\n'
            '\n'
            "__all__ = ['zeta', 'alpha']\n"
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker is None
        assert result.new_source.index('def alpha') < result.new_source.index('def zeta')
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        assert repeated.new_source == result.new_source


class TestIncrementalInitialCommentOwnership:
    def test_initial_definition_comment_survives_incremental_merge(self) -> None:
        baseline = '# about zeta\ndef zeta():\n    return 1\n'
        current = baseline + '\n\n# about alpha\ndef alpha():\n    return 2\n'
        expected = (
            '# about alpha\n'
            'def alpha():\n'
            '    return 2\n'
            '\n\n'
            '# about zeta\n'
            'def zeta():\n'
            '    return 1\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)

        assert result.new_source == expected
        assert repeated.new_source == expected

    def test_initial_method_comment_survives_incremental_merge(self) -> None:
        baseline = 'class C:\n    # about zeta\n    def zeta(self):\n        return 1\n'
        current = baseline + '\n\n    # about alpha\n    def alpha(self):\n        return 2\n'
        expected = (
            'class C:\n'
            '    # about alpha\n'
            '    def alpha(self):\n'
            '        return 2\n'
            '\n\n'
            '    # about zeta\n'
            '    def zeta(self):\n'
            '        return 1\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)

        assert result.new_source == expected
        assert repeated.new_source == expected


class TestIncrementalStructuralGate:
    def test_layout_only_hoist_respects_interstitial(self) -> None:
        src = (
            'def aaa():\n'
            '    pass\n'
            '\n'
            '\n'
            'SIDE_EFFECT = aaa()\n'
            '\n'
            '\n'
            'def bbb():\n'
            '    pass\n'
            '\n'
            '\n'
            'CONST = 3\n'
            '\n'
            '\n'
            'def ccc():\n'
            '    pass\n'
        )
        options = EngineOptions(policy=SortPolicy(), hoist=True)
        full = sort_file(src, options=options)
        inc = sort_file_incremental(src, src, options=options)
        assert full.skipped
        assert full.blocker == 'interstitial'
        assert inc.skipped
        assert inc.blocker == 'interstitial'
        assert inc.new_source == src

    def test_layout_only_strip_respects_interstitial(self) -> None:
        src = (
            'def aaa():\n'
            '    pass\n'
            '\n'
            '\n'
            'SIDE_EFFECT = aaa()\n'
            '\n'
            '\n'
            '# ------------------------------\n'
            '# Helpers\n'
            '# ------------------------------\n'
            '\n'
            '\n'
            'def bbb():\n'
            '    pass\n'
        )
        options = EngineOptions(policy=SortPolicy(banners='strip'))
        full = sort_file(src, options=options)
        inc = sort_file_incremental(src, src, options=options)
        assert full.skipped
        assert full.blocker == 'interstitial'
        assert inc.skipped
        assert inc.blocker == 'interstitial'
        assert inc.new_source == src

    def test_sectioned_duplicates_block_both_paths(self) -> None:
        baseline = (
            'def bbb():\n'
            '    pass\n'
            '\n'
            '\n'
            '# ------------------------------\n'
            '# Section A\n'
            '# ------------------------------\n'
            '\n'
            '\n'
            'def aaa():\n'
            '    pass\n'
            '\n'
            '\n'
            '# ------------------------------\n'
            '# Section B\n'
            '# ------------------------------\n'
            '\n'
            '\n'
            'def aaa():\n'
            '    pass\n'
        )
        current = baseline.replace(
            'def bbb():\n    pass\n',
            'def bbb():\n    pass\n\n\ndef abc():\n    pass\n',
            1,
        )
        options = EngineOptions(policy=SortPolicy(banners='barriers'))
        full = sort_file(current, options=options)
        inc = sort_file_incremental(current, baseline, options=options)
        assert full.skipped
        assert full.blocker == 'duplicates'
        assert inc.skipped
        assert inc.blocker == 'duplicates'
        assert inc.new_source == current


class TestIncrementalTrailingOwnershipGuard:
    def test_added_definitions_reuse_and_relocate_top_level_guards(self) -> None:
        baseline = 'def zulu():\n    pass\n# note for zulu\n\n# --------\ndef omega():\n    pass\n'
        current = baseline.replace(
            '# --------\n',
            'def alpha():\n    pass\n# --------\n',
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
        expected_second = (
            'def alpha():\n'
            '    pass\n'
            'def zulu():\n'
            '    pass\n'
            '# note for zulu\n'
            '\n'
            'def zzz():\n'
            '    pass\n'
            '# note for zzz\n'
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

        first = sort_file_incremental(current, baseline, options=options)
        second_baseline = first.new_source
        second_current = 'def zzz():\n    pass\n# note for zzz\n\n' + second_baseline
        second = sort_file_incremental(second_current, second_baseline, options=options)
        repeated = sort_file_incremental(second.new_source, second_baseline, options=options)
        reparsed = parse_definitions(second.new_source, banners='barriers')
        trailing = {definition.name: definition.trailing for definition in reparsed.definitions}

        assert first.new_source == expected_first
        assert second.new_source == expected_second
        assert trailing['zulu'] == '# note for zulu\n'
        assert trailing['zzz'] == '# note for zzz\n'
        assert repeated.new_source == expected_second

    def test_added_methods_reuse_and_relocate_class_local_guards(self) -> None:
        baseline = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    # --------\n'
            '    def omega(self):\n'
            '        pass\n'
        )
        current = baseline.replace(
            '    # --------\n',
            '    def alpha(self):\n        pass\n    # --------\n',
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    # --------\n'
            '    def omega(self):\n'
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
                banners='barriers',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)
        repeated = sort_file_incremental(result.new_source, baseline, options=options)
        cls = ast.parse(result.new_source).body[0]
        assert isinstance(cls, ast.ClassDef)
        parsed = parse_class_methods(
            cls,
            ClassSlice(result.new_source.splitlines(keepends=True), 1),
            banners='barriers',
        )
        zulu = next(method for method in parsed.methods if method.name == 'zulu')

        assert result.new_source == expected
        assert '    # note for zulu' in parsed.blocks[zulu.index]
        assert repeated.new_source == expected


class TestMethodIncremental:
    def test_existing_methods_not_moved(self) -> None:
        baseline = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n'
            '        self.alpha()\n\n'
            '    def alpha(self) -> None:\n'
            '        pass\n'
        )
        # Baseline methods are out of dependency order but that's fine --
        # non-conforming class -> skip method sorting for this class.
        # Add a new top-level def so something triggers incremental sort.
        current = baseline + '\n\ndef new_top():\n    pass\n'
        result = _sort_incremental(current, baseline)
        assert result.new_source is None
        assert [blocker.blocker for blocker in result.class_blockers] == ['non_conforming']

    def test_incremental_duplicate_class_blocker_is_local(self) -> None:
        baseline = (
            'class C:\n'
            '    def helper(self):\n'
            '        return 1\n\n'
            '    def foo(self, cb=helper):\n'
            '        return cb\n\n'
            '    def bar(self):\n'
            '        return 2\n\n'
            '    def foo(self):\n'
            '        return 3\n'
        )
        blocked_class = baseline + '\n    def new(self):\n        return 4\n'
        current = (
            blocked_class + '\n\ndef zed():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        )

        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy())
        )

        assert result.skipped is False
        assert result.blocker is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )
        assert result.new_source.startswith(blocked_class)
        assert result.new_source.index('def alpha():') < result.new_source.index('def zed():')

    def test_mixed_class_blockers_remain_local(self) -> None:
        baseline = (
            'class AlphaProblem:\n'
            '    def bravo(self):\n'
            '        return self.alpha()\n\n'
            '    def alpha(self):\n'
            '        return None\n\n\n'
            'class ZuluDuplicate:\n'
            '    def helper(self):\n'
            '        return 1\n\n'
            '    def foo(self, cb=helper):\n'
            '        return cb\n\n'
            '    def bar(self):\n'
            '        return 2\n\n'
            '    def foo(self):\n'
            '        return 3\n'
        )
        current = (
            'class AlphaProblem:\n'
            '    def bravo(self):\n'
            '        return self.alpha()\n\n'
            '    def alpha(self):\n'
            '        return None\n\n'
            '    def charlie(self):\n'
            '        return None\n\n\n'
            'class ZuluDuplicate:\n'
            '    def helper(self):\n'
            '        return 1\n\n'
            '    def foo(self, cb=helper):\n'
            '        return cb\n\n'
            '    def bar(self):\n'
            '        return 2\n\n'
            '    def foo(self):\n'
            '        return 3\n\n'
            '    def new(self):\n'
            '        return 4\n\n\n'
            'def zed():\n'
            '    return alpha()\n\n\n'
            'def alpha():\n'
            '    return 1\n'
        )

        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy())
        )

        assert result.skipped is False
        assert result.blocker is None
        assert result.warning is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='AlphaProblem',
                occurrence=1,
                blocker='non_conforming',
                message='baseline not in target order',
            ),
            ClassBlocker(
                class_name='ZuluDuplicate',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )
        assert result.new_source.index('def alpha():') < result.new_source.index('def zed():')

    def test_mixed_scope_partial_outcome(self) -> None:
        # Baseline: top-level in dep order, Widget methods NOT in order.
        # Dep order: Widget (no deps, 'W'), helper (no deps, 'h'),
        # zoo (depends on helper). This is conforming because uppercase
        # sorts before lowercase in ASCII: 'W' < 'h' < 'z'.
        baseline = (
            'class Widget:\n'
            '    def bravo(self) -> None:\n'
            '        self.alpha()\n\n'
            '    def alpha(self) -> None:\n'
            '        pass\n\n\n'
            'def helper() -> int:\n'
            '    return 1\n\n\n'
            'def zoo() -> int:\n'
            '    return helper()\n'
        )
        # Add a new top-level def AND a new method in Widget
        current = (
            'class Widget:\n'
            '    def bravo(self) -> None:\n'
            '        self.alpha()\n\n'
            '    def alpha(self) -> None:\n'
            '        pass\n\n'
            '    def charlie(self) -> None:\n'
            '        pass\n\n\n'
            'def helper() -> int:\n'
            '    return 1\n\n\n'
            'def zoo() -> int:\n'
            '    return helper()\n\n\n'
            'def new_func() -> None:\n'
            '    pass\n'
        )
        result = _sort_incremental(current, baseline)
        # Top-level should sort (new_func placed), even though
        # Widget's methods are non-conforming and skipped.
        assert result.new_source is not None
        assert result.blocker is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='Widget',
                occurrence=1,
                blocker='non_conforming',
                message='baseline not in target order',
            ),
        )

    def test_new_class_gets_full_sort(self) -> None:
        baseline = 'def existing():\n    pass\n'
        current = (
            'def existing():\n    pass\n\n\n'
            'class NewClass:\n'
            '    # fixed class leading\n'
            '    def zulu(self) -> None:\n'
            '        pass\n'
            ' \t\n'
            '    # alpha leading\n'
            '    def alpha(self) -> None:\n'
            '        pass\n'
            '    footer = 1\n'
        )
        expected = (
            'def existing():\n    pass\n\n\n'
            'class NewClass:\n'
            '    # alpha leading\n'
            '    def alpha(self) -> None:\n'
            '        pass\n'
            ' \t\n'
            '    # fixed class leading\n'
            '    def zulu(self) -> None:\n'
            '        pass\n'
            '    footer = 1\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    def test_new_method_sorted(self) -> None:
        baseline = (
            'class Foo:\n'
            '    def alpha(self) -> None:\n'
            '        pass\n'
            '    def zulu(self) -> None:\n'
            '        pass'
        )
        current = (
            'class Foo:\r\n'
            '    def alpha(self) -> None:\n'
            '        pass\r\n'
            ' \t\r'
            '    def zulu(self) -> None:\n'
            '        pass\n'
            '    # for bravo\r\n'
            '    def bravo(self) -> None:\r'
            '        pass'
        )
        expected = (
            'class Foo:\r\n'
            '    def alpha(self) -> None:\n'
            '        pass\r\n'
            ' \t\r'
            '    # for bravo\r\n'
            '    def bravo(self) -> None:\r'
            '        pass\n'
            '    def zulu(self) -> None:\n'
            '        pass'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.blocker is None
        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    def test_overload_in_class_not_treated_as_new(self) -> None:
        baseline = 'class Foo:\n    def process(self, x: int) -> int:\n        return x\n'
        current = (
            'class Foo:\n'
            '    @overload\n'
            '    def process(self, x: int) -> int: ...\n'
            '    @overload\n'
            '    def process(self, x: str) -> str: ...\n'
            '    def process(self, x: int | str) -> int | str:\n'
            '        return x\n'
        )
        result = _sort_incremental(current, baseline)
        assert result.blocker is None
        assert result.new_source is None
        assert result.class_blockers == ()

    def test_property_and_setter_not_confused(self) -> None:
        baseline = (
            'class Foo:\n'
            '    @property\n'
            '    def value(self) -> int:\n'
            '        return self._value\n\n'
            '    @value.setter\n'
            '    def value(self, v: int) -> None:\n'
            '        self._value = v\n'
        )
        # Add a new method (alpha) -- should be sorted without
        # confusing the getter/setter as duplicates.
        current = (
            'class Foo:\n'
            '    @property\n'
            '    def value(self) -> int:\n'
            '        return self._value\n\n'
            '    @value.setter\n'
            '    def value(self, v: int) -> None:\n'
            '        self._value = v\n\n'
            '    def alpha(self) -> None:\n'
            '        pass\n'
        )
        result = _sort_incremental(current, baseline)
        assert result.blocker is None
        assert result.new_source is not None
        assert result.new_source.index('def alpha') < result.new_source.index('def value')
        assert result.new_source.count('def value') == 2
        assert result.new_source.index('@value.setter') > result.new_source.index('def value')


class TestMethodTargetDrift:
    def test_method_drift_detected(self) -> None:
        # Baseline class with methods out of order
        baseline = (
            'class Foo:\n    def beta(self):\n        pass\n\n    def alpha(self):\n        pass\n'
        )
        current = baseline
        result = _sort_incremental(current, baseline)
        assert result.blocker is None
        assert [blocker.blocker for blocker in result.class_blockers] == ['non_conforming']


class TestSortFileIncremental:
    def test_add_future_adds_import(self) -> None:
        baseline = ''
        current = 'class zed_type:\n    x: alpha\n\n\nclass alpha:\n    x: zed_type\n'
        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy(), add_future=True)
        )
        assert 'from __future__ import annotations' in result.new_source

    def test_blocker_warning_is_human_readable(self) -> None:
        # Non-conforming baseline -> skipped with human-readable warning
        bad_baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        current = bad_baseline + '\n\ndef charlie():\n    pass\n'
        result = sort_file_incremental(
            current, bad_baseline, options=EngineOptions(policy=SortPolicy())
        )
        assert result.skipped
        assert result.warning is not None
        # Must be human-readable (not the raw machine key "non_conforming")
        assert result.warning != 'non_conforming'
        assert 'baseline' in result.warning.lower()
        assert 'order' in result.warning.lower()
        assert result.blocker == 'non_conforming'

    def test_empty_baseline_matches_full_sort_duplicate_blocker(self) -> None:
        source = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'
        options = EngineOptions(policy=SortPolicy(banners='barriers'), add_future=False)

        full = sort_file(source, options=options)
        incremental = sort_file_incremental(source, '', options=options)

        assert full.blocker == 'duplicates'
        assert incremental == full

    def test_empty_baseline_matches_full_sort_lossless_strip(self) -> None:
        source = 'def alpha():\n    pass\n\n\n\n# --------\n\n\n\n\ndef bravo():\n    pass\n'
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
            ),
            add_future=False,
            remove_future=False,
        )

        full = sort_file(source, options=options)
        incremental = sort_file_incremental(source, '', options=options)

        assert full.new_source == source.replace('# --------\n', '')
        assert incremental == full

    def test_empty_baseline_matches_full_sort_noop_metadata(self) -> None:
        source = (
            'def zulu():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n\n'
            'def mike():\n'
            '    pass\n'
            '\n\n'
            '# ========\n'
            '\n\n'
            'def alpha():\n'
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

        full = sort_file(source, options=options)
        incremental = sort_file_incremental(source, '', options=options)

        assert full.new_source == source
        assert len(full.banner_constraints) == 2
        assert incremental == full

    def test_empty_baseline_uses_same_full_method_phase(self) -> None:
        source = 'class C:\n    def bravo(self): pass\n    def alpha(self): pass\n'
        options = EngineOptions(
            policy=SortPolicy(banners='ignore'),
            add_future=False,
            remove_future=False,
        )

        full = sort_file(source, options=options)
        incremental = sort_file_incremental(source, '', options=options)

        assert incremental.new_source == full.new_source
        assert incremental.method_sorts == full.method_sorts
        assert incremental.class_blockers == full.class_blockers

    @pytest.mark.parametrize(
        ('mode', 'expected_banner'),
        [('barriers', '# --------\n\n# ========\n\n'), ('strip', '\n\n')],
        ids=['barriers', 'strip'],
    )
    def test_exact_incremental_banner_permutation(
        self,
        mode: BannerMode,
        expected_banner: str,
    ) -> None:
        baseline = 'def alpha():\n    pass\n\n# --------\n\n# ========\n\ndef zulu():\n    pass\n'
        current = baseline + ' \n' + 'def bravo():\n    pass'
        expected = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            f'{expected_banner}'
            'def bravo():\n'
            '    pass\n'
            ' \n'
            'def zulu():\n'
            '    pass'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners=mode,
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('order', 'zulu_body'),
        [
            ('alphabetical', '    pass'),
            ('dependency', '    return alpha()'),
        ],
        ids=['alphabetical', 'dependency'],
    )
    def test_exact_top_level_new_definition_permutation(
        self,
        order: SortScopeOrder,
        zulu_body: str,
    ) -> None:
        baseline = f'def alpha():\n    pass\n\n\ndef zulu():\n{zulu_body}\n'
        current = (
            'def alpha():\r\n'
            '    pass\r\n'
            '\t\r'
            'def zulu():\n'
            f'{zulu_body}\n'
            ' \n'
            'def bravo():\r'
            '    return alpha()'
        )
        expected = (
            'def alpha():\r\n'
            '    pass\r\n'
            '\t\r'
            'def bravo():\r'
            '    return alpha()\n'
            ' \n'
            'def zulu():\n'
            f'{zulu_body}'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order=order, method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(current, baseline, options=options)

        assert result.new_source == expected
        assert sort_file_incremental(expected, baseline, options=options).new_source == expected

    @pytest.mark.parametrize('with_new_definition', [False, True])
    def test_future_preserves_annotations(self, with_new_definition: bool) -> None:
        baseline, current = _future_sources(
            annotated=True,
            with_new_definition=with_new_definition,
        )
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)
        execute_source(current)

        result = sort_file_incremental(current, baseline, options=options)

        assert 'from __future__ import annotations' in result.new_source
        execute_source(result.new_source)
        assert (
            sort_file_incremental(result.new_source, baseline, options=options).new_source
            == result.new_source
        )

    @pytest.mark.parametrize('with_new_definition', [False, True])
    def test_future_removal_preserves_docstring_decoy(
        self,
        with_new_definition: bool,
    ) -> None:
        prefix = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\nfrom __future__ import annotations\r# REAL-AFTER\r\n'
        )
        expected_prefix = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\n# REAL-AFTER\r\n'
        )
        baseline = prefix + 'def zulu():\n    pass'
        if with_new_definition:
            current = baseline + '\n\ndef alpha():\n    pass'
            expected = (
                '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
                '# REAL-BEFORE\n'
                'def alpha():\n'
                '    pass\n'
                '\n'
                '# REAL-AFTER\r\n'
                'def zulu():\n'
                '    pass'
            )
        else:
            current = baseline
            expected = expected_prefix + 'def zulu():\n    pass'
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)

        result = sort_file_incremental(current, baseline, options=options)

        assert result.new_source == expected

    @pytest.mark.parametrize('with_new_definition', [False, True])
    def test_future_removes_annotation_free_import(self, with_new_definition: bool) -> None:
        baseline, current = _future_sources(
            annotated=False,
            with_new_definition=with_new_definition,
        )
        options = EngineOptions(policy=SortPolicy(), add_future=False, remove_future=True)

        result = sort_file_incremental(current, baseline, options=options)

        assert 'from __future__ import annotations' not in result.new_source
        assert (
            sort_file_incremental(result.new_source, baseline, options=options).new_source
            == result.new_source
        )

    def test_hoist_flag_supported(self) -> None:
        baseline = 'def alpha():\n    return 1\n'
        current = (
            'def alpha():\n    return 1\n\n\n'
            'MAX_SIZE = 100\n\n\n'
            'def bravo():\n    return MAX_SIZE\n'
        )
        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy(), hoist=True)
        )
        assert result.new_source != result.original_source
        # MAX_SIZE should be hoisted to before the first def
        assert result.new_source.index('MAX_SIZE') < result.new_source.index('def alpha')

    def test_module_docstring_form_feed_keeps_incremental_spans(self) -> None:
        baseline = '"""Doc\fpage."""\n\n\nclass C:\n    def zulu(self):\n        pass\n'
        source = (
            '"""Doc\fpage."""\n\n\n'
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        result = sort_file_incremental(
            source, baseline, options=EngineOptions(policy=SortPolicy())
        )
        assert result.new_source == (
            '"""Doc\fpage."""\n\n\n'
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zulu(self):\n'
            '        pass\n'
        )

    def test_module_docstring_form_feed_keeps_incremental_top_level_spans(self) -> None:
        baseline = '"""Doc\fpage."""\n\n\ndef zulu():\n    pass\n'
        source = '"""Doc\fpage."""\n\n\ndef zulu():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_file_incremental(
            source, baseline, options=EngineOptions(policy=SortPolicy())
        )
        assert result.new_source == (
            '"""Doc\fpage."""\n\n\ndef alpha():\n    pass\n\n\ndef zulu():\n    pass\n'
        )

    def test_no_new_definitions_hoist_respects_custom_banner_barrier(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n'
            '# REGION\n'
            '\n'
            'MAX_SIZE = 100\n'
            '\n'
            'def bravo():\n'
            '    return MAX_SIZE\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='barriers',
                banner_pattern='^# REGION$',
            ),
            hoist=True,
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(source, source, options=options)

        assert result.new_source == source
        assert sort_file_incremental(source, source, options=options).new_source == source

    def test_noop_preserves_exact_source_layout(self) -> None:
        source = 'def alpha():\r\n    pass\r\n \t\r\ndef zulu():\n    pass'
        options = EngineOptions(
            policy=SortPolicy(definition_order='alphabetical', method_order='none'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file_incremental(source, source, options=options)

        assert result.new_source == source

    def test_noop_returns_unchanged(self) -> None:
        result = sort_file_incremental(
            _SRC_BASELINE, _SRC_BASELINE, options=EngineOptions(policy=SortPolicy())
        )
        assert result.new_source == result.original_source

    def test_returns_sort_result(self) -> None:
        result = sort_file_incremental(
            _SRC_WITH_NEW, _SRC_BASELINE, options=EngineOptions(policy=SortPolicy())
        )
        assert isinstance(result, SortResult)
        assert result.new_source != result.original_source
        assert len(result.new_order) == 3

    def test_syntax_error_returns_unchanged(self) -> None:
        result = sort_file_incremental(
            'def broken(\n', _SRC_BASELINE, options=EngineOptions(policy=SortPolicy())
        )
        assert result.new_source == result.original_source
        assert result.original_order == []
        assert result.new_order == []


class TestSortForHook:
    def test_already_sorted(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n'
        assert sort_for_hook_detailed(src, policy=SortPolicy()).new_source is None

    def test_banner_duplicate_safety_breaks_groups_at_boundary(self) -> None:
        src = 'def alpha():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_for_hook_detailed(src, policy=SortPolicy())

        assert result.new_source is None
        assert result.blocker == 'duplicates'

    @pytest.mark.parametrize('incremental', [False, True])
    def test_class_only_skip_preserves_hook_diagnostic(self, incremental: bool) -> None:
        baseline = 'class C:\n    def z(self): pass\n    x = 1\n'
        source = baseline + '    def a(self): pass\n'
        policy = SortPolicy(method_order='alphabetical')

        result = (
            sort_incremental_detailed(source, baseline, policy=policy)
            if incremental
            else sort_for_hook_detailed(source, policy=policy)
        )

        assert result.new_source is None
        assert result.blocker == 'interstitial'
        assert result.sorted_defs == ()

    def test_compile_level_error_raises(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\nreturn 1\n'
        with pytest.raises(SyntaxError):
            sort_for_hook_detailed(src, policy=SortPolicy())

    def test_duplicates_block(self) -> None:
        src = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n\n\ndef alpha():\n    pass\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.new_source is None
        assert result.blocker == 'duplicates'

    def test_forward_refs_block(self) -> None:
        src = 'def zzz():\n    return aaa()\n\n\ndef aaa(x: zzz) -> None:\n    pass\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.new_source is None
        assert result.blocker == 'forward_refs'

    def test_forward_refs_ok_with_future(self) -> None:
        src = (
            'from __future__ import annotations\n\n\n'
            'def zzz():\n    return aaa()\n\n\n'
            'def aaa(x: zzz) -> None:\n    pass\n'
        )
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is None
        assert result.new_source is not None

    def test_interstitial_blocks(self) -> None:
        src = 'def bravo():\n    return alpha()\n\nx = 1\n\ndef alpha():\n    return 1\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.new_source is None
        assert result.blocker == 'interstitial'

    def test_method_only_sort_not_blocked(self) -> None:
        src = (
            'class Alpha:\n'
            '    x: Alpha\n'
            '\n'
            '    def z(self) -> None:\n'
            '        pass\n'
            '\n'
            '    def a(self) -> None:\n'
            '        pass\n'
            '\n'
            '\n'
            'def func_using_alpha() -> Alpha:\n'
            '    return Alpha()\n'
        )
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is None
        assert result.new_source is not None
        assert result.new_source.index('def a') < result.new_source.index('def z')

    def test_method_sorting(self) -> None:
        src = (
            'class C:\n    def bravo(self):\n        pass\n\n    def alpha(self):\n        pass\n'
        )
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is None
        assert result.new_source is not None
        assert result.new_source.index('def alpha') < result.new_source.index('def bravo')

    def test_no_definitions(self) -> None:
        assert sort_for_hook_detailed('import os\n', policy=SortPolicy()).new_source is None

    @pytest.mark.parametrize('mode', ['ignore', 'strip'])
    def test_nonbarrier_modes_do_not_report_constraints(self, mode: BannerMode) -> None:
        source = 'def zulu():\n    pass\n\n\n# --------\n\n\ndef alpha():\n    pass\n'

        result = sort_for_hook_detailed(
            source,
            policy=SortPolicy(
                definition_order='alphabetical',
                method_order='none',
                banners=mode,
            ),
        )

        assert result.banner_constraints == ()

    def test_removes_unnecessary_future(self) -> None:
        src = (
            'from __future__ import annotations\n\n\n'
            'def alpha():\n    pass\n\n\n'
            'def bravo():\n    pass\n'
        )
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is None
        assert result.new_source is not None
        assert 'from __future__ import annotations' not in result.new_source

    def test_sorts_definitions(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is None
        assert result.new_source is not None
        assert result.new_source.index('def alpha') < result.new_source.index('def bravo')

    def test_strip_only_returns_top_level_and_method_source_changes(self) -> None:
        source = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n\n'
            '    # ========\n\n'
            '    def bravo(self):\n'
            '        pass\n\n\n'
            '# --------\n\n\n'
            'def tail():\n'
            '    pass\n'
        )

        result = sort_for_hook_detailed(
            source,
            policy=SortPolicy(
                definition_order='none',
                method_order='none',
                banners='strip',
            ),
        )

        assert result.new_source == source.replace('    # ========\n', '').replace(
            '# --------\n', ''
        )
        assert result.banner_constraints == ()


class TestSortForHookDetailed:
    def test_already_sorted_returns_none_source(self) -> None:
        src = 'def a():\n    return 1\n\n\ndef b():\n    return a()\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.new_source is None
        assert result.sorted_defs == ()

    def test_blocker_returns_empty_defs(self) -> None:
        src = 'def b():\n    return a()\n\nx = 1\n\ndef a():\n    return 1\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.blocker is not None
        assert result.sorted_defs == ()

    def test_returns_hook_sort_result(self) -> None:
        src = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert isinstance(result, _HookSortResult)
        assert result.new_source is not None
        assert result.blocker is None
        assert len(result.sorted_defs) == 2

    def test_sorted_defs_are_reordered(self) -> None:
        src = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        result = sort_for_hook_detailed(src, policy=SortPolicy())
        assert result.sorted_defs[0].name == 'a'
        assert result.sorted_defs[1].name == 'b'

    def test_syntax_error_propagates(self) -> None:
        with pytest.raises(SyntaxError):
            sort_for_hook_detailed('def broken(\n', policy=SortPolicy())


class TestSortIncremental:
    def test_brand_new_file(self) -> None:
        src = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        result = _sort_incremental(src, '')
        new_source = result.new_source
        assert new_source is not None
        # Full sort: alpha should come before bravo
        assert new_source.index('alpha') < new_source.index('bravo')

    @pytest.mark.parametrize(
        'statement',
        ['return 1', 'break', 'nonlocal value'],
        ids=['return', 'break', 'nonlocal'],
    )
    def test_compile_invalid_baseline_skips(self, statement: str) -> None:
        baseline = f'{statement}\n\n\ndef zulu():\n    pass\n'
        current = 'def zulu():\n    pass\n\n\ndef alpha():\n    pass\n'

        result = _sort_incremental(current, baseline)

        assert result.new_source is None
        assert result.blocker == 'non_conforming'

    def test_existing_defs_not_moved(self) -> None:
        result = _sort_incremental(_SRC_WITH_NEW, _SRC_BASELINE)
        new_source = result.new_source
        assert new_source is not None
        assert new_source.index('def alpha') < new_source.index('def bravo')

    def test_force_does_not_bypass_duplicates(self) -> None:
        baseline = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        current = baseline + '\n\ndef alpha():\n    pass\n'

        result = _sort_incremental(current, baseline, force=True)

        assert result.blocker == 'duplicates'
        assert result.new_source is None

    def test_force_new_file_bypasses_interstitial(self) -> None:
        # Source with interstitial code (assignment between defs)
        src = 'def bravo():\n    return alpha()\n\nx = 1\n\ndef alpha():\n    return 1\n'
        # Without force: should be blocked
        result = _sort_incremental(src, '', force=False)
        assert result.blocker == 'interstitial'
        # With force: should proceed
        result = _sort_incremental(src, '', force=True)
        assert result.blocker is None
        assert result.new_source is not None

    def test_forward_refs_not_blocked_with_future_import(self) -> None:
        baseline = (
            'from __future__ import annotations\n\n\n'
            'def alpha() -> Bravo:\n    pass\n\n\n'
            'class Bravo:\n    pass\n'
        )
        # Add a new def with forward ref. Since future annotations is present,
        # this should NOT be blocked.
        current = (
            'from __future__ import annotations\n\n\n'
            'def alpha() -> Bravo:\n    pass\n\n\n'
            'class Bravo:\n    pass\n\n\n'
            'def charlie() -> Bravo:\n    pass\n'
        )
        result = _sort_incremental(current, baseline)
        assert result.blocker != 'forward_refs'

    def test_future_import_preserved_without_mutation(self) -> None:
        baseline, current = _future_sources(
            annotated=False,
            with_new_definition=True,
        )

        result = _sort_incremental(current, baseline)

        assert result.new_source is not None
        assert 'from __future__ import annotations' in result.new_source

    def test_header_like_first_body_comment_does_not_overlap(self) -> None:
        baseline = 'def zebra():\n    # encoding: utf-8 assumed\n    return 1\n'
        current = baseline + '\n\ndef apple():\n    return 2\n'

        result = _sort_incremental(current, baseline)

        assert result.new_source is not None
        ast.parse(result.new_source)
        assert result.new_source.count('# encoding: utf-8 assumed') == 1

    def test_interleaved_overloads_not_desync(self) -> None:
        # Use 'zoo' (after 'foo' alphabetically) so baseline is conforming
        baseline = (
            '@overload\ndef foo(x: int) -> int: ...\n\n\n'
            '@overload\ndef foo(x: str) -> str: ...\n\n\n'
            'def foo(x: int | str) -> int | str:\n    return x\n\n\n'
            'def zoo() -> None:\n    pass\n'
        )
        # Add a new definition after zoo
        current = baseline + '\n\ndef new_func() -> None:\n    pass\n'
        result = _sort_incremental(current, baseline)
        # Should not produce user_reordered or any false blocker
        assert result.blocker is None
        assert result.new_source == baseline.replace(
            'def zoo() -> None:\n    pass\n',
            'def new_func() -> None:\n    pass\n\n\ndef zoo() -> None:\n    pass\n',
        )

    def test_invalid_baseline_skips(self) -> None:
        current = _SRC_WITH_NEW
        result = _sort_incremental(current, 'def broken(\n')
        assert result.new_source is None
        assert result.blocker == 'non_conforming'

    def test_invalid_source_raises(self) -> None:
        with pytest.raises(SyntaxError):
            _sort_incremental('def broken(\n', _SRC_BASELINE)

    def test_new_def_already_in_order(self) -> None:
        baseline = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        # Add charlie after bravo -- in dep order, charlie depends on alpha
        # so it belongs between alpha and bravo (or after bravo if no bravo
        # dependency). If target places charlie after bravo, the merge
        # produces the same source -> return (None, None).
        current = baseline + '\n\ndef charlie():\n    return alpha()\n'
        result = _sort_incremental(current, baseline)
        assert result.blocker is None
        assert result.new_source is None

    def test_new_def_sorted(self) -> None:
        result = _sort_incremental(_SRC_WITH_NEW, _SRC_BASELINE)
        assert result.blocker is None
        assert result.new_source is not None
        # beta depends on alpha, sorts before bravo by alphabetical tiebreak
        assert result.new_source.index('def beta') < result.new_source.index('def bravo')

    def test_no_new_defs_noop(self) -> None:
        result = _sort_incremental(_SRC_BASELINE, _SRC_BASELINE)
        assert result.new_source is None
        assert result.blocker is None

    def test_non_conforming_baseline(self) -> None:
        # Baseline is out of order
        bad_baseline = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    return 1\n'
        current = bad_baseline + '\n\ndef charlie():\n    pass\n'
        result = _sort_incremental(current, bad_baseline)
        assert result.new_source is None
        assert result.blocker == 'non_conforming'

    def test_target_incompatible(self) -> None:
        baseline = 'def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        current = (
            'def alpha():\n    return bravo()\n\n\n'
            'def bravo():\n    pass\n\n\n'
            'def charlie():\n    pass\n'
        )
        result = _sort_incremental(current, baseline)
        assert result.new_source is None
        assert result.blocker == 'target_incompatible'

    def test_user_reordered_existing(self) -> None:
        baseline = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
        # Swap alpha and bravo (user reordered), then add charlie
        current = (
            'def bravo():\n    return alpha()\n\n\n'
            'def alpha():\n    return 1\n\n\n'
            'def charlie():\n    pass\n'
        )
        result = _sort_incremental(current, baseline)
        assert result.new_source is None
        assert result.blocker == 'user_reordered'


class TestSortIncrementalDetailedFutureRemoval:
    @pytest.mark.parametrize('with_new_definition', [False, True])
    @pytest.mark.parametrize('hook', [False, True], ids=['api', 'hook-engine'])
    def test_future_preserves_live_binding(self, *, with_new_definition: bool, hook: bool) -> None:
        baseline = (
            'from __future__ import annotations\n\n'
            'def feature():\n    return annotations.compiler_flag\n'
        )
        current = baseline + '\n\ndef other():\n    pass\n' if with_new_definition else baseline
        original = execute_source(current)['feature']
        assert isinstance(original, FunctionType)
        assert original() == 16777216
        options = EngineOptions(policy=SortPolicy())

        result = (
            sort_incremental_detailed(current, baseline, policy=options.policy)
            if hook
            else sort_file_incremental(current, baseline, options=options)
        )
        transformed_source = result.new_source or current
        transformed = execute_source(transformed_source)['feature']
        assert isinstance(transformed, FunctionType)
        assert transformed() == 16777216
        assert 'from __future__ import annotations' in transformed_source

    @pytest.mark.parametrize('with_new_definition', [False, True])
    @pytest.mark.parametrize('hook', [False, True], ids=['api', 'hook-engine'])
    @pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
    @pytest.mark.parametrize(
        'retained',
        ['', 'from __future__ import annotations  # keep\n'],
        ids=['eligible-only', 'comment-bearing'],
    )
    def test_future_duplicate_cleanup_is_idempotent(
        self, ending: str, retained: str, *, with_new_definition: bool, hook: bool
    ) -> None:
        body = '\n\ndef feature():\n    return 1\n'.replace('\n', ending)
        retained = retained.replace('\n', ending)
        baseline = (
            retained
            + f'from __future__ import annotations{ending}'
            + f'from __future__ import (annotations){ending}'
            + body
        )
        addition = (
            '\n\ndef other():\n    pass\n'.replace('\n', ending) if with_new_definition else ''
        )
        current = baseline + addition
        options = EngineOptions(policy=SortPolicy())

        result = (
            sort_incremental_detailed(current, baseline, policy=options.policy)
            if hook
            else sort_file_incremental(current, baseline, options=options)
        )

        assert result.new_source is not None
        assert result.new_source == retained + body + addition
        repeated = (
            sort_incremental_detailed(result.new_source, baseline, policy=options.policy)
            if hook
            else sort_file_incremental(result.new_source, baseline, options=options)
        )
        assert (repeated.new_source or result.new_source) == result.new_source

    @pytest.mark.parametrize('with_new_definition', [False, True])
    def test_future_preserves_annotations(self, with_new_definition: bool) -> None:
        baseline, current = _future_sources(
            annotated=True,
            with_new_definition=with_new_definition,
        )
        execute_source(current)

        result = sort_incremental_detailed(current, baseline, policy=SortPolicy())
        transformed = result.new_source or current

        assert 'from __future__ import annotations' in transformed
        execute_source(transformed)
        repeated = sort_incremental_detailed(transformed, baseline, policy=SortPolicy())
        assert (repeated.new_source or transformed) == transformed

    @pytest.mark.parametrize('with_new_definition', [False, True])
    def test_future_removes_annotation_free_import(self, with_new_definition: bool) -> None:
        baseline, current = _future_sources(
            annotated=False,
            with_new_definition=with_new_definition,
        )

        result = sort_incremental_detailed(current, baseline, policy=SortPolicy())

        assert result.new_source is not None
        assert 'from __future__ import annotations' not in result.new_source
        repeated = sort_incremental_detailed(result.new_source, baseline, policy=SortPolicy())
        assert repeated.new_source is None

    def test_incremental_removal_ignores_docstring_decoy(self) -> None:
        decoy = '"""DOC-A\fDOC-B\nfrom __future__ import annotations\n"""\n'
        baseline = decoy + 'from __future__ import annotations\n\n\ndef zulu():\n    pass\n'
        source = (
            decoy
            + 'from __future__ import annotations\n\n\ndef zulu():\n    pass\n'
            + '\n\ndef alpha():\n    pass\n'
        )
        result = sort_file_incremental(
            source, baseline, options=EngineOptions(policy=SortPolicy())
        )
        assert result.new_source.startswith(decoy)
        assert result.new_source.count('from __future__ import annotations') == 1


class TestTargetDrift:
    def test_no_drift_returns_none(self) -> None:
        source = 'def a():\n    return 1\n\n\ndef b():\n    return a()\n'
        result = _sort_incremental(source, source)
        assert result.blocker is None

    def test_non_conforming_baseline_detected(self) -> None:
        baseline = 'def b():\n    return a()\n\n\ndef a():\n    return 1\n'
        current = baseline
        result = _sort_incremental(current, baseline)
        assert result.blocker == 'non_conforming'

    def test_removal_causes_target_incompatible(self) -> None:
        # Baseline: c, b, a in dependency order (a depends on b, b depends on c).
        # All conforming.
        baseline = (
            'def c():\n    pass\n\n\ndef b():\n    return c()\n\n\ndef a():\n    return b()\n'
        )
        # Current: remove b. Now c and a remain. a's dependency on b() is unresolved
        # (b not in definition list), so a has no tracked deps. Alphabetical: a, c.
        # Physical order in current: c, a. Target says a should come first.
        # This is TARGET_INCOMPATIBLE (existing definitions not in target order).
        current = 'def c():\n    pass\n\n\ndef a():\n    return b()\n'
        result = _sort_incremental(current, baseline)
        assert result.blocker == 'target_incompatible'


class TestTreeThreading:
    def test_detailed_unchanged_source_parses_current_and_baseline_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = 'class Example:\n    def method(self):\n        pass\n'
        parse_count = 0
        original_parse = ast.parse

        def counting_parse(
            candidate: str,
            *args: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
            **kwargs: Any,  # noqa: ANN401 - `ast.parse` varies by Python version.
        ) -> ast.AST:
            nonlocal parse_count
            parse_count += 1
            return cast('ast.AST', original_parse(candidate, *args, **kwargs))

        monkeypatch.setattr(incremental.ast, 'parse', counting_parse)

        result = sort_incremental_detailed(source, source, policy=SortPolicy())

        assert result.new_source is None
        assert result.blocker is None
        assert parse_count == 2

    def test_incremental_method_sort_with_new_top_level(self) -> None:
        baseline = (
            'class Foo:\n'
            '    def alpha(self):\n'
            '        return 1\n\n'
            '    def delta(self):\n'
            '        return 2\n'
        )
        # aaa before Foo forces top-level reorder (aaa depends on Foo).
        # beta is a new method -> triggers method reordering and merges
        # between alpha and delta.
        current = (
            'def aaa():\n'
            '    return Foo()\n\n\n'
            'class Foo:\n'
            '    def alpha(self):\n'
            '        return 1\n\n'
            '    def delta(self):\n'
            '        return 2\n\n'
            '    def beta(self):\n'
            '        return 3\n'
        )
        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy())
        )
        # Top-level: Foo before aaa (dependency order).
        # Methods: alpha, beta (new, merged in), delta.
        assert result.new_source is not None
        assert result.new_source != current
        src = result.new_source
        # Top-level reorder happened
        assert src.index('class Foo') < src.index('def aaa')
        # Method merge happened: beta inserted between alpha and delta
        assert src.index('def alpha') < src.index('def beta')
        assert src.index('def beta') < src.index('def delta')

    def test_new_order_preserved_after_future_add(self) -> None:
        baseline = 'def alpha():\n    return 1\n'
        # Forward ref: beta's annotation references charlie which is defined after
        current = (
            'def alpha():\n    return 1\n\n\n'
            'def beta(x: charlie) -> None:\n    pass\n\n\n'
            'def charlie():\n    return 2\n'
        )
        result = sort_file_incremental(
            current, baseline, options=EngineOptions(policy=SortPolicy(), add_future=True)
        )
        assert result.new_source is not None
        new_names = [d.name for d in result.new_order]
        # Verify exact order, not just membership -- order must be preserved
        # through future-annotation insertion
        assert new_names == ['alpha', 'charlie', 'beta']


def _future_sources(*, annotated: bool, with_new_definition: bool) -> tuple[str, str]:
    annotation = 'value: Later\n\n\n' if annotated else ''
    baseline = f'from __future__ import annotations\n\n\n{annotation}class Later:\n    pass\n'
    current = baseline + '\n\nclass Alpha:\n    pass\n' if with_new_definition else baseline
    return baseline, current


def _sort_incremental(
    source: str,
    baseline: str,
    *,
    policy: SortPolicy = _DEFAULT_SORT_POLICY,
    force: bool = False,
) -> _IncrementalSortResult:
    return sort_incremental_impl(
        source,
        baseline,
        options=EngineOptions(
            policy=policy,
            force=force,
            add_future=False,
            remove_future=False,
        ),
    )


def test_incremental_does_not_split_overload_group() -> None:
    baseline = _SRC_OVERLOAD_CONFORMING
    current = baseline + '\n    def added(self, x):\n        return x\n'
    result = sort_file_incremental(current, baseline, options=EngineOptions(policy=SortPolicy()))
    assert result.blocker is None
    out = result.new_source
    stubs = out.index('@overload')
    impl = out.index('def foo(self, x, cb=helper)')
    added = out.index('def added')
    assert added < stubs or added > impl
