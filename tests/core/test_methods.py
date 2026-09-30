import ast
import sys
from collections import Counter
from dataclasses import replace
from types import FunctionType
from typing import TYPE_CHECKING, cast

import pytest

import defsort
import defsort._core._methods as methods_module
from defsort._core._api import sort_file, sort_file_incremental
from defsort._core._layout import SourceLayout, render_layout
from defsort._core._methods import (
    _analyze_method_deps,
    _get_property_group_info,
    _group_property_methods,
    _is_property_decorator,
    _method_group_key,
    _order_methods_alphabetical,
    _order_methods_by_dependency,
    _sort_class_methods,
    collect_method_coordinates,
    parse_class_methods,
    sort_file_methods,
)
from defsort._source import split_physical_lines
from defsort._types import (
    BannerMode,
    Blocker,
    ClassBlocker,
    ClassSlice,
    EngineOptions,
    MethodDefinition,
    SortPolicy,
    SortScopeOrder,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from defsort._types import PropertyKind

_SRC_BODY_EDGE = """class Kitchen:
    def prepare(self):
        self.record_resource()

    def record_resource(self):
        return 1
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

_SRC_INIT_BODY = """class C:
    def __init__(self):
        self.load()

    def load(self):
        return 1
"""

_SRC_INIT_DEFAULT_EDGE = """class C:
    def factory(self):
        return 1

    def __init__(self, cb=factory):
        self.cb = cb

    def other(self):
        return 2
"""

_SRC_MIXED_CYCLE = """class C:
    def b(self):
        return self.a()

    def a(self, cb=b):
        return cb
"""

_SRC_OVERLOAD_EDGE = """from typing import overload


class C:
    def zulu(self):
        return 0

    def helper(self, x):
        return x

    @overload
    def foo(self, x: int) -> int: ...

    @overload
    def foo(self, x: str) -> str: ...

    def foo(self, x, cb=helper):
        return cb(x)
"""


class TestAnalyzeMethodDeps:
    def test_annotations_not_runtime_deps(self) -> None:
        src = 'class C:\n    def alpha(self): pass\n    def bravo(self) -> alpha: pass\n'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = cls.body
        method_names = {m.name for m in methods if isinstance(m, ast.FunctionDef)}
        func = methods[1]
        assert isinstance(func, ast.FunctionDef)
        result = _analyze_method_deps(func, method_names)
        assert result == set()

    def test_body_deps_not_collected(self) -> None:
        src = 'class C:\n    def alpha(self): pass\n    def bravo(self): self.alpha()\n'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = cls.body
        method_names = {m.name for m in methods if isinstance(m, ast.FunctionDef)}
        func = methods[1]
        assert isinstance(func, ast.FunctionDef)
        result = _analyze_method_deps(func, method_names)
        assert result == set()

    def test_decorator_dep(self) -> None:
        src = (
            'class C:\n'
            '    def validator(self): pass\n'
            '    @validator\n'
            '    def process(self): pass\n'
        )
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = cls.body
        method_names = {m.name for m in methods if isinstance(m, ast.FunctionDef)}
        func = methods[1]
        assert isinstance(func, ast.FunctionDef)
        result = _analyze_method_deps(func, method_names)
        assert result == {'validator'}

    def test_default_arg_dep(self) -> None:
        src = 'class C:\n    def factory(self): pass\n    def process(self, fn=factory): pass\n'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = cls.body
        method_names = {m.name for m in methods if isinstance(m, ast.FunctionDef)}
        func = methods[1]
        assert isinstance(func, ast.FunctionDef)
        result = _analyze_method_deps(func, method_names)
        assert result == {'factory'}

    def test_same_name_read_is_collected_before_binding(self) -> None:
        src = 'class C:\n    def process(self, fn=process): pass\n'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = cls.body
        method_names = {m.name for m in methods if isinstance(m, ast.FunctionDef)}
        func = methods[0]
        assert isinstance(func, ast.FunctionDef)
        result = _analyze_method_deps(func, method_names)
        assert result == {'process'}


class TestBlankLineBetweenSortedMethods:
    @pytest.mark.parametrize(
        ('newline', 'blank_line'),
        [
            ('\n', '\n'),
            ('\r\n', '\r\n'),
            ('\n', '   \n'),
            ('\r\n', '\t\r\n'),
        ],
        ids=['lf', 'crlf', 'spaces', 'tabs'],
    )
    def test_blank_physical_line_representations_are_preserved(
        self,
        newline: str,
        blank_line: str,
    ) -> None:
        source = (
            f'class C:{newline}'
            f'    def b(self):{newline}'
            f'        pass{newline}'
            f'{newline}'
            f'    def a(self):{newline}'
            f'        pass{newline}'
            f'    # section{newline}'
            f'{blank_line}'
            f'    def z(self):{newline}'
            f'        pass{newline}'
        )
        expected = (
            f'class C:{newline}'
            f'    def a(self):{newline}'
            f'        pass{newline}'
            f'    # section{newline}'
            f'{newline}'
            f'    def b(self):{newline}'
            f'        pass{newline}'
            f'{blank_line}'
            f'    def z(self):{newline}'
            f'        pass{newline}'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
                add_future=False,
            ),
        )

        assert result.new_source == expected

    def test_leading_comment_moves_without_synthetic_separator(self) -> None:
        source = (
            'class C:\n'
            '    def z(self):\n'
            '        pass\n'
            '    # for b\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    def a(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def a(self):\n'
            '        pass\n'
            '    # for b\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    def z(self):\n'
            '        pass\n'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
                add_future=False,
            ),
        )

        assert result.new_source == expected

    def test_multiple_source_blank_lines_are_preserved(self) -> None:
        source = (
            'class C:\n'
            '    def z(self):\n'
            '        pass\n'
            '\n'
            '    def y(self):\n'
            '        pass\n'
            '\n'
            '    # section\n'
            '\n'
            '\n'
            '    def a(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def a(self):\n'
            '        pass\n'
            '\n'
            '    def y(self):\n'
            '        pass\n'
            '\n'
            '    # section\n'
            '\n'
            '\n'
            '    def z(self):\n'
            '        pass\n'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
                add_future=False,
            ),
        )

        assert result.new_source == expected

    @pytest.mark.parametrize(
        ('method_order', 'source', 'expected'),
        [
            (
                'alphabetical',
                (
                    'class C:\n'
                    '    def z(self):\n'
                    '        pass\n'
                    '\n'
                    '    def y(self):\n'
                    '        pass\n'
                    '\n'
                    '    # --- Section ---\n'
                    '\n'
                    '    def a(self):\n'
                    '        pass\n'
                ),
                (
                    'class C:\n'
                    '    def a(self):\n'
                    '        pass\n'
                    '\n'
                    '    def y(self):\n'
                    '        pass\n'
                    '\n'
                    '    # --- Section ---\n'
                    '\n'
                    '    def z(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'dependency',
                (
                    'class C:\n'
                    '    def z(self):\n'
                    '        pass\n'
                    '\n'
                    '    def y(self):\n'
                    '        pass\n'
                    '\n'
                    '    # --- Section ---\n'
                    '\n'
                    '    def a(self, dependency=y):\n'
                    '        pass\n'
                ),
                (
                    'class C:\n'
                    '    def y(self):\n'
                    '        pass\n'
                    '\n'
                    '    # --- Section ---\n'
                    '\n'
                    '    def a(self, dependency=y):\n'
                    '        pass\n'
                    '\n'
                    '    def z(self):\n'
                    '        pass\n'
                ),
            ),
        ],
        ids=['alphabetical', 'dependency'],
    )
    def test_source_boundary_supplies_separator(
        self,
        method_order: SortScopeOrder,
        source: str,
        expected: str,
    ) -> None:
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order=method_order),
            add_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == expected

    def test_unchanged_class_is_byte_identical(self) -> None:
        source = (
            'class C:\n'
            '    def a(self):\n'
            '        pass\n'
            '\n'
            '\n'
            '    # for b\n'
            '    def b(self):\n'
            '        pass\n'
        )

        result = sort_file(
            source,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order='alphabetical'),
                add_future=False,
            ),
        )

        assert result.new_source == source


class TestBodyAwareDependency:
    def test_across_categories_sinks_init(self) -> None:
        policy = SortPolicy(method_bodies='across-categories')
        result = _sort_with_policy(_SRC_INIT_BODY, policy)
        assert result.index('def load') < result.index('def __init__')

    def test_body_ref_attaches_to_overload_group(self) -> None:
        src = (
            'from typing import overload\n\n\n'
            'class C:\n'
            '    def consume(self):\n'
            '        return self.get\n\n'
            '    @overload\n'
            '    def get(self, key: int) -> int: ...\n\n'
            '    @overload\n'
            '    def get(self, key: str) -> str: ...\n\n'
            '    def get(self, key):\n'
            '        return key\n'
        )
        result = defsort.sort(src)
        assert result.index('@overload') < result.index('def consume')
        assert result.index('def get(self, key):') < result.index('def consume')

    def test_body_ref_attaches_to_property_group(self) -> None:
        src = (
            'class C:\n'
            '    def consume(self):\n'
            '        return self.size\n\n'
            '    @property\n'
            '    def size(self):\n'
            '        return 1\n\n'
            '    @size.setter\n'
            '    def size(self, value):\n'
            '        raise AttributeError\n'
        )
        result = defsort.sort(src)
        assert result.index('@property') < result.index('def consume')
        assert result.index('@size.setter') < result.index('def consume')

    def test_helper_moves_above_caller(self) -> None:
        result = defsort.sort(_SRC_BODY_EDGE)
        assert result.index('def record_resource') < result.index('def prepare')

    def test_ignore_matches_head_behavior(self) -> None:
        policy = SortPolicy(method_bodies='ignore')
        assert _sort_with_policy(_SRC_BODY_EDGE, policy) == _SRC_BODY_EDGE

    def test_init_body_edge_does_not_cross_category(self) -> None:
        assert defsort.sort(_SRC_INIT_BODY) == _SRC_INIT_BODY

    def test_mixed_cycle_keeps_semantic_edge(self) -> None:
        result = defsort.sort(_SRC_MIXED_CYCLE)
        assert result.index('def b') < result.index('def a')
        check = defsort.check(_SRC_MIXED_CYCLE)
        assert check.status != 'skipped'

    def test_source_tie_break(self) -> None:
        src = (
            'class C:\n'
            '    def zed(self):\n'
            '        return 1\n\n'
            '    def alpha(self):\n'
            '        return 2\n'
        )
        policy = SortPolicy(tie_break='source')
        assert _sort_with_policy(src, policy) == src


class TestCategoryComposition:
    def _names(self, policy: SortPolicy) -> list[str]:
        methods = _make_methods(['store', '_evict', '__repr__', '__init__', '__new__'])
        ordered = _order_methods_alphabetical(methods, policy=policy)
        return [method.name for method in ordered]

    def test_after_regular_mixed(self) -> None:
        policy = SortPolicy(method_dunders='after-regular')
        assert self._names(policy) == [
            '__new__',
            '__init__',
            '_evict',
            'store',
            '__repr__',
        ]

    def test_after_regular_public_first(self) -> None:
        policy = SortPolicy(
            method_dunders='after-regular',
            method_visibility='public-first',
        )
        assert self._names(policy) == [
            '__new__',
            '__init__',
            'store',
            '_evict',
            '__repr__',
        ]

    def test_before_regular_mixed(self) -> None:
        assert self._names(SortPolicy()) == [
            '__new__',
            '__init__',
            '__repr__',
            '_evict',
            'store',
        ]

    def test_before_regular_public_first(self) -> None:
        policy = SortPolicy(method_visibility='public-first')
        assert self._names(policy) == [
            '__new__',
            '__init__',
            '__repr__',
            'store',
            '_evict',
        ]


class TestConsecutiveSameNameGrouping:
    def test_adjacent_redefinitions_stay_adjacent(self) -> None:
        src = (
            'class C:\n'
            '    def z(self):\n        return 1\n\n'
            '    def z(self):\n        return 2\n\n'
            '    def a(self):\n        return 3\n'
        )
        result = defsort.sort(src)
        first = result.index('return 1')
        second = result.index('return 2')
        moved = result.index('return 3')
        assert moved < first < second

    def test_overload_group_stays_atomic_under_edge(self) -> None:
        original_namespace = _execute_source(_SRC_OVERLOAD_EDGE)
        result = defsort.sort(_SRC_OVERLOAD_EDGE)
        sorted_namespace = _execute_source(result)
        stubs = result.index('@overload')
        impl = result.index('def foo(self, x, cb=helper)')
        helper = result.index('def helper')
        zulu = result.index('def zulu')
        tree = ast.parse(result)
        cls = cast('ast.ClassDef', tree.body[1])
        method_names = [method.name for method in cls.body if isinstance(method, ast.FunctionDef)]

        assert 'C' in original_namespace
        assert 'C' in sorted_namespace
        assert result != _SRC_OVERLOAD_EDGE
        assert helper < stubs < impl < zulu
        assert method_names == ['helper', 'foo', 'foo', 'foo', 'zulu']
        assert defsort.sort(result) == result


class TestDecoratorBindingIdentity:
    def test_earlier_sibling_decorator_keeps_edge(self) -> None:
        src = (
            'class Cls:\n'
            '    def deco(fn):\n'
            '        return fn\n'
            '\n'
            '    @deco\n'
            '    def alpha(self):\n'
            '        return 1\n'
        )
        options = EngineOptions(policy=SortPolicy(method_order='dependency'))
        result = sort_file(src, options=options)
        assert not result.skipped
        assert result.new_source.index('def deco') < result.new_source.index('def alpha')

    def test_repeated_property_binder_events_are_preserved(self) -> None:
        source = (
            'EVENTS = []\n'
            '\n'
            '\n'
            'class property(property):\n'
            '    def deleter(self, fn):\n'
            '        EVENTS.append(("deleter", self.fset is not None))\n'
            '        return super().deleter(fn)\n'
            '\n'
            '    def setter(self, fn):\n'
            '        EVENTS.append(("setter", self.fdel is not None))\n'
            '        return super().setter(fn)\n'
            '\n'
            '\n'
            'class C:\n'
            '    @property\n'
            '    def x(self):\n'
            '        return 1\n'
            '\n'
            '    @x.deleter\n'
            '    def x(self):\n'
            '        pass\n'
            '\n'
            '    @x.setter\n'
            '    def x(self, value):\n'
            '        pass\n'
        )
        original_namespace = _execute_source(source)
        assert original_namespace['EVENTS'] == [('deleter', False), ('setter', True)]
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)
        repeated = sort_file(result.new_source, options=options)
        unchanged_namespace = _execute_source(result.new_source)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == source
        assert unchanged_namespace['EVENTS'] == [('deleter', False), ('setter', True)]
        assert repeated.blocker == result.blocker
        assert repeated.new_source == source

    def test_same_named_later_sibling_default_not_a_dependency(self) -> None:
        src = (
            'def apply():\n'
            "    return 'module'\n"
            '\n'
            '\n'
            'class Cls:\n'
            '    def zeta(self, value=apply):\n'
            '        return value\n'
            '\n'
            '    def apply(self):\n'
            "        return 'method'\n"
        )
        options = EngineOptions(policy=SortPolicy(method_order='alphabetical'))
        result = sort_file(src, options=options)
        assert result.skipped
        assert result.blocker == 'eager_order'
        assert result.new_source == src

    def test_same_named_later_sibling_not_a_dependency(self) -> None:
        src = (
            'def apply(fn):\n'
            "    fn.tag = 'module'\n"
            '    return fn\n'
            '\n'
            '\n'
            'class Cls:\n'
            '    @apply\n'
            '    def zeta(self):\n'
            '        return 1\n'
            '\n'
            '    def apply(self, fn):\n'
            '        return fn\n'
        )
        options = EngineOptions(policy=SortPolicy(method_order='alphabetical'))
        result = sort_file(src, options=options)
        assert result.skipped
        assert result.blocker == 'eager_order'
        assert result.new_source == src

    def test_same_named_setter_decorator_capture_blocked(self) -> None:
        src = (
            'x = property(lambda self: 1)\n'
            '\n'
            '\n'
            'class C:\n'
            '    @x.setter\n'
            '    def x(self, value):\n'
            '        self._value = value\n'
            '\n'
            '    @property\n'
            '    def x(self):\n'
            '        return 2\n'
        )
        original_namespace = _execute_source(src)
        original_class = original_namespace['C']
        assert isinstance(original_class, type)
        original_descriptor = vars(original_class)['x']
        assert isinstance(original_descriptor, property)
        assert original_descriptor.fset is None

        options = EngineOptions(policy=SortPolicy(method_order='alphabetical'))
        result = sort_file(src, options=options)

        assert result.skipped
        assert result.blocker == 'eager_order'
        assert result.new_source == src
        unchanged_namespace = _execute_source(result.new_source)
        unchanged_class = unchanged_namespace['C']
        assert isinstance(unchanged_class, type)
        unchanged_descriptor = vars(unchanged_class)['x']
        assert isinstance(unchanged_descriptor, property)
        assert unchanged_descriptor.fset is None


class TestDefinitionTimeAnchorCrossing:
    def test_init_default_edge_keeps_regular_method_first(self) -> None:
        assert defsort.sort(_SRC_INIT_DEFAULT_EDGE) == _SRC_INIT_DEFAULT_EDGE

    def test_result_is_idempotent(self) -> None:
        once = defsort.sort(_SRC_INIT_DEFAULT_EDGE)
        assert defsort.sort(once) == once


class TestDuplicatePropertyBinders:
    def test_full_sort_blocks_duplicate_binders(self) -> None:
        original_namespace = _execute_source(_SRC_DUP_PROPERTY_BINDERS)
        original_class = original_namespace['C']
        assert isinstance(original_class, type)
        original_descriptor = vars(original_class)['x']
        assert isinstance(original_descriptor, property)
        assert original_descriptor.fset is None

        result = sort_file_methods(
            _SRC_DUP_PROPERTY_BINDERS,
            ast.parse(_SRC_DUP_PROPERTY_BINDERS),
            policy=SortPolicy(banners='ignore'),
        )

        assert result.candidate_source == _SRC_DUP_PROPERTY_BINDERS
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )
        unchanged_namespace = _execute_source(result.candidate_source)
        unchanged_class = unchanged_namespace['C']
        assert isinstance(unchanged_class, type)
        unchanged_descriptor = vars(unchanged_class)['x']
        assert isinstance(unchanged_descriptor, property)
        assert unchanged_descriptor.fset is None

    def test_incremental_sort_blocks_duplicate_binders(self) -> None:
        baseline = """class C:
    @property
    def x(self):
        return 1

    @x.setter
    def x(self, value):
        pass
"""

        result = sort_file_incremental(
            _SRC_DUP_PROPERTY_BINDERS,
            baseline,
            options=EngineOptions(
                policy=SortPolicy(banners='ignore'),
                add_future=False,
                remove_future=False,
            ),
        )

        assert result.new_source == _SRC_DUP_PROPERTY_BINDERS
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_valid_property_family_is_not_blocked(self) -> None:
        source = """class C:
    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, value):
        self._x = value

    @x.deleter
    def x(self):
        del self._x
"""

        result = sort_file_methods(
            source,
            ast.parse(source),
            policy=SortPolicy(banners='ignore'),
        )

        assert result.candidate_source == source
        assert result.class_blockers == ()


class TestFullMethodSortHazards:
    @pytest.mark.parametrize('force', [False, True], ids=['normal', 'forced'])
    @pytest.mark.parametrize(
        ('source', 'unsafe_source'),
        [
            (
                (
                    'class Example:\n'
                    '    def zeta_decorator(function):\n'
                    '        return function\n\n'
                    '    @zeta_decorator\n'
                    '    def alpha(self):\n'
                    '        return 1\n'
                ),
                (
                    'class Example:\n'
                    '    @zeta_decorator\n'
                    '    def alpha(self):\n'
                    '        return 1\n\n'
                    '    def zeta_decorator(function):\n'
                    '        return function\n'
                ),
            ),
            (
                (
                    'class Example:\n'
                    '    def zeta_default():\n'
                    '        return 1\n\n'
                    '    def alpha(self, value=zeta_default()):\n'
                    '        return value\n'
                ),
                (
                    'class Example:\n'
                    '    def alpha(self, value=zeta_default()):\n'
                    '        return value\n\n'
                    '    def zeta_default():\n'
                    '        return 1\n'
                ),
            ),
        ],
        ids=['method-decorator', 'method-default'],
    )
    @pytest.mark.parametrize('allow_reordering', [False, True])
    def test_eager_method_candidate_returns_original_source(
        self,
        source: str,
        unsafe_source: str,
        *,
        force: bool,
        allow_reordering: bool,
    ) -> None:
        _execute_source(source)
        if sys.version_info < (3, 14):
            with pytest.raises(NameError):
                _execute_source(unsafe_source)
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
                evaluation_order='allow-reordering' if allow_reordering else 'preserve',
            ),
            force=force,
        )

        result = sort_file(source, options=options)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.original_source == source
        assert result.new_source == source
        assert len(result.method_sorts) == 1
        method_sort = result.method_sorts[0]
        assert [method.index for method in method_sort.new_order] != [
            method.index for method in method_sort.original_order
        ]
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source
        assert repeated.blocker == 'eager_order'

    @pytest.mark.skipif(
        sys.version_info < (3, 14),
        reason='requires Python 3.14 deferred annotation semantics',
    )
    def test_sibling_method_annotation_add_stringizes_on_python_314(self) -> None:
        source = (
            'class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=True,
        )

        result = sort_file(source, options=options)

        namespace = _execute_source(result.new_source)
        example = namespace['Example']
        assert isinstance(example, type)
        alpha = vars(example)['alpha']
        assert isinstance(alpha, FunctionType)
        assert alpha.__annotations__['value'] == 'zeta_type'
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source

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
    def test_sibling_method_annotation_matrix(
        self,
        *,
        existing_future: bool,
        add_future: bool,
        expected_blocker: Blocker | None,
    ) -> None:
        future = 'from __future__ import annotations\n\n\n' if existing_future else ''
        source = (
            f'{future}class Example:\n'
            '    def zeta_type(self):\n'
            '        return None\n\n'
            '    def alpha(self, value: zeta_type):\n'
            '        return value\n'
        )
        _execute_source(source)
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order='alphabetical'),
            add_future=add_future,
        )

        result = sort_file(source, options=options)

        assert result.blocker == expected_blocker
        if expected_blocker is None:
            assert result.skipped is False
            assert result.new_source.index('def alpha') < result.new_source.index('def zeta_type')
            assert 'from __future__ import annotations' in result.new_source
            _execute_source(result.new_source)
        else:
            assert result.skipped is True
            assert result.new_source == source
        repeated = sort_file(result.new_source, options=options)
        assert repeated.new_source == result.new_source


class TestGetPropertyGroupInfo:
    def test_deleter(self) -> None:
        src = 'class C:\n    @x.deleter\n    def x(self): pass'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _get_property_group_info(func) == ('x', 'deleter')

    def test_property_returns_none(self) -> None:
        src = 'class C:\n    @property\n    def x(self): return 1'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _get_property_group_info(func) is None

    def test_regular_method_returns_none(self) -> None:
        src = 'class C:\n    def m(self): pass'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _get_property_group_info(func) is None

    def test_setter(self) -> None:
        src = 'class C:\n    @x.setter\n    def x(self, val): pass'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _get_property_group_info(func) == ('x', 'setter')


class TestGroupPropertyMethods:
    def test_deleter_with_setter_in_docstring(self) -> None:
        src = (
            'class C:\n'
            '    @property\n'
            '    def x(self):\n'
            '        return self._x\n'
            '\n'
            '    @x.deleter\n'
            '    def x(self):\n'
            '        """Delete x. See also x.setter for setting."""\n'
            '        del self._x\n'
            '\n'
            '    @x.setter\n'
            '    def x(self, val):\n'
            '        self._x = val\n'
        )
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(src, keepends=True), 1),
        )

        groups = _group_property_methods(parsed.methods)

        assert len(groups) == 1
        assert [method.property_kind for method in groups[0]] == ['getter', 'setter', 'deleter']

    def test_mixed_property_and_regular(self) -> None:
        methods = [
            _method('x', 0, prop_group='x'),  # @property
            _method('process', 1),
            _method('x', 2, prop_group='x'),  # @x.setter
        ]
        groups = _group_property_methods(methods)
        # Should have 2 groups: property group for 'x' and single 'process'
        assert len(groups) == 2

    def test_no_properties(self) -> None:
        methods = [_method('alpha', 0), _method('bravo', 1)]
        groups = _group_property_methods(methods)
        assert len(groups) == 2
        assert len(groups[0]) == 1
        assert len(groups[1]) == 1

    def test_property_group_order_enforced(self) -> None:
        methods = [
            _method('x', 0, prop_group='x'),  # @x.setter
            _method('x', 1, prop_group='x'),  # @x.deleter
            _method('x', 2, prop_group='x', is_getter=True),  # @property
        ]
        groups = _group_property_methods(methods)
        assert len(groups) == 1
        group = groups[0]
        # @property (index 2) should be first
        assert group[0].index == 2


class TestInitialMethodGapOwnership:
    def test_adjacent_first_method_comment_moves(self) -> None:
        source = (
            'class C:\n'
            '    # about zeta\n'
            '    def zeta(self):\n'
            '        return 1\n'
            '\n\n'
            '    # about alpha\n'
            '    def alpha(self):\n'
            '        return 2\n'
        )
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

        result = sort_file(source, options=options)

        assert result.new_source == expected

    def test_blank_separated_first_method_comment_stays_fixed(self) -> None:
        source = 'class C:\n    # file note\n\n    def zeta(self):\n        pass\n'
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert parsed.layout.prefix == 'class C:\n    # file note\n\n'
        assert parsed.blocks[0] == '    def zeta(self):\n        pass'
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    def test_both_scopes_reorder_once(self) -> None:
        source = (
            '# about Zulu\n'
            'class Zulu:\n'
            '    # about zeta\n'
            '    def zeta(self):\n'
            '        return 1\n'
            '\n\n'
            '    # about alpha\n'
            '    def alpha(self):\n'
            '        return 2\n'
            '\n\n'
            '# about Alpha\n'
            'class Alpha:\n'
            '    pass\n'
        )
        expected = (
            '# about Alpha\n'
            'class Alpha:\n'
            '    pass\n'
            '\n\n'
            '# about Zulu\n'
            'class Zulu:\n'
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
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        first = sort_file(source, options=options).new_source
        second = sort_file(first, options=options).new_source

        assert first == expected
        assert second == first

    @pytest.mark.parametrize(
        'structural',
        [
            '    """Class documentation."""\n',
            '    attribute = 1\n',
            '    class Nested:\n        pass\n',
            '    import os\n',
            '    Alias: TypeAlias = int\n',
            '    if READY:\n        setup()\n',
            ('    try:\n        setup()\n    except Exception:\n        recover()\n'),
        ],
        ids=['docstring', 'attribute', 'nested-class', 'import', 'type-alias', 'if', 'try'],
    )
    def test_class_structural_statements_stay_fixed(self, structural: str) -> None:
        source = (
            'class C:\n' + structural + '    # about zeta\n    def zeta(self):\n        pass\n'
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert parsed.layout.prefix == 'class C:\n' + structural
        assert parsed.blocks[0].startswith('    # about zeta\n    def zeta')
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    @pytest.mark.parametrize(
        ('source', 'prefix', 'leading'),
        [
            (
                (
                    '@decorate\n'
                    'class C(\n'
                    '    Base,\n'
                    '):\n'
                    '    # about zeta\n'
                    '    def zeta(self):\n'
                    '        pass\n'
                ),
                '@decorate\nclass C(\n    Base,\n):\n',
                '    # about zeta\n',
            ),
            (
                (
                    '@decorate\r\n'
                    'class C(\r\n'
                    '    Base,\r\n'
                    '):\r\n'
                    '    # about zeta\r\n'
                    '    def zeta(self):\r\n'
                    '        pass\r\n'
                ),
                '@decorate\r\nclass C(\r\n    Base,\r\n):\r\n',
                '    # about zeta\r\n',
            ),
            (
                (
                    '@decorate\r'
                    'class C(\r'
                    '    Base,\r'
                    '):\r'
                    '    # about zeta\r'
                    '    def zeta(self):\r'
                    '        pass\r'
                ),
                '@decorate\rclass C(\r    Base,\r):\r',
                '    # about zeta\r',
            ),
            (
                (
                    '@decorate\r\n'
                    'class C(\r'
                    '    Base,\n'
                    '):\r\n'
                    '    # about zeta\r'
                    '    def zeta(self):\n'
                    '        pass\r'
                ),
                '@decorate\r\nclass C(\r    Base,\n):\r\n',
                '    # about zeta\r',
            ),
        ],
        ids=['lf', 'crlf', 'lone-cr', 'mixed'],
    )
    def test_decorated_multiline_class_header_endings(
        self,
        source: str,
        prefix: str,
        leading: str,
    ) -> None:
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert parsed.layout.prefix == prefix
        assert parsed.blocks[0].startswith(leading + '    def zeta')
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    def test_first_method_directive_stays_fixed(self) -> None:
        source = 'class C:\n    # type: ignore\n    def zeta(self):\n        pass\n'
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert parsed.layout.prefix == 'class C:\n    # type: ignore\n'
        assert '# type: ignore' not in parsed.blocks[0]
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    @pytest.mark.parametrize(
        ('source', 'prefix'),
        [
            (
                'class C(Registry["("]):\n    # about zeta\n    def zeta(self):\n        pass\n',
                'class C(Registry["("]):\n',
            ),
            (
                (
                    'class C(\n'
                    '    Base,  # (\n'
                    '):\n'
                    '    # about zeta\n'
                    '    def zeta(self):\n'
                    '        pass\n'
                ),
                'class C(\n    Base,  # (\n):\n',
            ),
        ],
        ids=['string', 'comment'],
    )
    def test_header_brackets_in_string_and_comment(self, source: str, prefix: str) -> None:
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert parsed.layout.prefix == prefix
        assert parsed.blocks[0].startswith('    # about zeta\n    def zeta')
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    @pytest.mark.parametrize(
        ('source', 'banners', 'leading', 'boundary_count'),
        [
            (
                'class C:\n\f    # about zeta\n    def zeta(self):\n        pass\n',
                'ignore',
                '\f    # about zeta\n',
                0,
            ),
            (
                'class C:\n\f    # --------\n\n    def zeta(self):\n        pass\n',
                'barriers',
                '',
                1,
            ),
        ],
        ids=['comment', 'banner'],
    )
    def test_initial_method_form_feed_round_trip(
        self,
        source: str,
        banners: BannerMode,
        leading: str,
        boundary_count: int,
    ) -> None:
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
            banners=banners,
        )
        first = render_layout(parsed.layout, parsed.blocks, [0])
        repeated_cls = ast.parse(first).body[0]
        assert isinstance(repeated_cls, ast.ClassDef)
        repeated = parse_class_methods(
            repeated_cls,
            ClassSlice(split_physical_lines(first, keepends=True), 1),
            banners=banners,
        )

        assert parsed.blocks[0].startswith(leading + '    def zeta')
        assert len(parsed.boundaries) == boundary_count
        assert first == source
        assert render_layout(repeated.layout, repeated.blocks, [0]) == first

    def test_initial_overload_and_property_groups(self) -> None:
        source = (
            'from typing import overload\n'
            '\n\n'
            'class C:\n'
            '    # about zeta\n'
            '    @overload\n'
            '    def zeta(self, value: int) -> int: ...\n'
            '\n\n'
            '    @overload\n'
            '    def zeta(self, value: str) -> str: ...\n'
            '\n\n'
            '    def zeta(self, value: int | str) -> int | str:\n'
            '        return value\n'
            '\n\n'
            '    # about alpha\n'
            '    @property\n'
            '    def alpha(self):\n'
            '        return self._alpha\n'
            '\n\n'
            '    @alpha.setter\n'
            '    def alpha(self, value):\n'
            '        self._alpha = value\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == source

    def test_initial_property_group_binding_change_is_refused(self) -> None:
        source = (
            'value = property(lambda self: None)\n'
            '\n'
            '\n'
            'class C:\n'
            '    # about the setter\n'
            '    @value.setter\n'
            '    def value(self, new_value):\n'
            '        self._value = new_value\n'
            '\n'
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
        original_class = _execute_source(source)['C']
        assert isinstance(original_class, type)
        original_descriptor = vars(original_class)['value']
        assert isinstance(original_descriptor, property)
        assert original_descriptor.fset is None

        result = sort_file(source, options=options)
        repeated = sort_file(result.new_source, options=options)
        unchanged_class = _execute_source(result.new_source)['C']
        assert isinstance(unchanged_class, type)
        unchanged_descriptor = vars(unchanged_class)['value']
        assert isinstance(unchanged_descriptor, property)

        assert result.skipped is True
        assert result.blocker == 'eager_order'
        assert result.new_source == source
        assert '# about the setter\n' in result.new_source
        assert unchanged_descriptor.fset is None
        assert repeated.blocker == result.blocker
        assert repeated.new_source == source


class TestIsPropertyDecorator:
    def test_other_decorator(self) -> None:
        src = 'class C:\n    @staticmethod\n    def m(): pass'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _is_property_decorator(func) is False

    def test_property_decorator(self) -> None:
        src = 'class C:\n    @property\n    def x(self): return 1'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _is_property_decorator(func) is True

    def test_undecorated(self) -> None:
        src = 'class C:\n    def m(self): pass'
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        func = cls.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _is_property_decorator(func) is False


class TestMethodBanners:
    @pytest.mark.parametrize(
        ('mode', 'expected'),
        [
            (
                'barriers',
                (
                    'class C:\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    # ========\n'
                    '    def alpha(self):\n'
                    '        pass'
                ),
            ),
            (
                'strip',
                'class C:\n    def alpha(self):\n        pass\n    def zulu(self):\n        pass',
            ),
        ],
        ids=['barriers', 'strip'],
    )
    def test_adjacent_boundaries_keep_empty_method_section_layout(
        self,
        mode: BannerMode,
        expected: str,
    ) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # --------\n'
            '    # ========\n'
            '    def alpha(self):\n'
            '        pass'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
                banners=mode,
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_banner_barrier_keeps_property_group_and_overloads_in_section(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    @overload\n'
            '    def parse(self, value: int): ...\n'
            '\n'
            '    @overload\n'
            '    def parse(self, value: str): ...\n'
            '\n'
            '    def parse(self, value):\n'
            '        return value\n'
            '\n'
            '    @property\n'
            '    def name(self):\n'
            '        return self._name\n'
            '\n'
            '    @name.setter\n'
            '    def name(self, value):\n'
            '        self._name = value\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        banner = result.new_source.index('    # --------')
        assert result.new_source.index('def zulu') < banner
        assert banner < result.new_source.index('@property')
        assert result.new_source.index('@property') < result.new_source.index('@name.setter')
        assert result.new_source.count('def parse') == 3

    def test_banner_barrier_preserves_unsafe_method_trailing_exactly(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    registry = {}\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        assert result.new_source == src

    def test_banner_barrier_reassembles_exact_raw_bytes(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # ========   \n'
            '    # detail\n'
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
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        assert result.new_source.count('    # ========   \n    # detail\n') == 1
        assert result.new_source.index('def zulu') < result.new_source.index('# ========')
        assert result.new_source.index('def alpha') < result.new_source.index('def bravo')

    @pytest.mark.parametrize(
        ('mode', 'order', 'expected'),
        [
            (
                'barriers',
                'alphabetical',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'barriers',
                'dependency',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # --------\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'barriers',
                'none',
                (
                    'class C:\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # --------\n'
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
                    '    # --------\n'
                    '    def bravo(self):\n'
                    '        pass\n'
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
                    '    # --------\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'ignore',
                'none',
                (
                    'class C:\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # --------\n'
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
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    def alpha(self):\n'
                    '        pass\n'
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
    def test_banner_mode_order_matrix_preserves_exact_layout(
        self,
        mode: BannerMode,
        order: SortScopeOrder,
        expected: str,
    ) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # --------\n'
            '    def bravo(self):\n'
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(definition_order='none', method_order=order, banners=mode),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('mode', 'order', 'expected_names', 'has_banner'),
        [
            ('barriers', 'alphabetical', ['zulu', 'alpha'], True),
            ('ignore', 'alphabetical', ['alpha', 'zulu'], True),
            ('strip', 'alphabetical', ['alpha', 'zulu'], False),
            ('barriers', 'none', ['zulu', 'alpha'], True),
            ('ignore', 'none', ['zulu', 'alpha'], True),
            ('strip', 'none', ['zulu', 'alpha'], False),
        ],
    )
    def test_banner_modes_apply_to_alphabetical_and_none_method_order(
        self,
        mode: BannerMode,
        order: SortScopeOrder,
        expected_names: list[str],
        *,
        has_banner: bool,
    ) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(definition_order='none', method_order=order, banners=mode),
                add_future=False,
            ),
        )

        class_node = ast.parse(result.new_source).body[0]
        assert isinstance(class_node, ast.ClassDef)
        method_names = [
            method.name
            for method in class_node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert method_names == expected_names
        assert ('    # --------\n' in result.new_source) is has_banner

    @pytest.mark.parametrize(
        ('mode', 'alpha_before_zulu'),
        [
            ('barriers', False),
            ('ignore', False),
            ('strip', False),
        ],
    )
    def test_banner_modes_preserve_decorator_binding_identity(
        self,
        mode: BannerMode,
        *,
        alpha_before_zulu: bool,
    ) -> None:
        src = (
            'alpha = lambda fn: fn\n'
            '\n'
            '\n'
            'class C:\n'
            '    @alpha\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='dependency',
                    banners=mode,
                ),
                add_future=False,
            ),
        )

        actual = result.new_source.index('def alpha') < result.new_source.index('def zulu')
        assert actual is alpha_before_zulu

    def test_banner_split_overload_family_blocks_class(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    @overload\n'
            '    def parse(self, value: int): ...\n'
            '\n'
            '    # --------\n'
            '\n'
            '    @overload\n'
            '    def parse(self, value: str): ...\n'
            '\n'
            '    def parse(self, value):\n'
            '        return value\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        assert result.new_source == src
        assert result.blocker is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_banner_split_property_family_blocks_class(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    @property\n'
            '    def value(self):\n'
            '        return self._value\n'
            '\n'
            '    # --------\n'
            '\n'
            '    @value.setter\n'
            '    def value(self, new_value):\n'
            '        self._value = new_value\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )

        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='none',
                    method_order='alphabetical',
                    banners='barriers',
                ),
                add_future=False,
            ),
        )

        assert result.new_source == src
        assert result.blocker is None
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_banner_strip_none_preserves_unrelated_method_bytes(self) -> None:
        banner = '    # --------  \n    # banner detail\t\n'
        src = (
            'class C:\n'
            '    def alpha(self):  \n'
            '        pass\n'
            ' \t\n'
            f'{banner}'
            '\t\n'
            '    def bravo(self):\n'
            '        pass  \n'
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
            ),
        )

        assert result.new_source == src.replace(banner, '')

    def test_banner_strip_preserves_ordinary_block_between_boundaries(self) -> None:
        src = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    # ordinary block\n'
            '\n'
            '    # ========\n'
            '\n'
            '    def bravo(self):\n'
            '        pass\n'
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
            ),
        )

        assert '# --------' not in result.new_source
        assert '# ========' not in result.new_source
        assert result.new_source.count('    # ordinary block\n') == 1

    @pytest.mark.parametrize(
        ('mode', 'expected'),
        [
            (
                'barriers',
                (
                    'class C:\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                    '    # REGION\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def bravo(self):\n'
                    '        pass\n'
                ),
            ),
            (
                'ignore',
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    # REGION\n'
                    '    def bravo(self):\n'
                    '        pass\n'
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
                    '    def bravo(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
        ],
        ids=['barriers', 'ignore', 'strip'],
    )
    def test_custom_banner_pattern_preserves_exact_layout(
        self,
        mode: BannerMode,
        expected: str,
    ) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # REGION\n'
            '    def bravo(self):\n'
            '        pass\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
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

        result = sort_file(source, options=options)

        assert result.new_source == expected
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_form_feed_method_line_keeps_banner_scope(self) -> None:
        src = (
            'class C:\n'
            '\f    def zulu(self):\n'
            '        pass\n'
            '\n'
            '    # --------\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(banners='barriers')))
        assert result.new_source == src


class TestMethodCommentAttachment:
    def test_comment_moves_with_method(self) -> None:
        src = (
            'class C:\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    # comment for a\n'
            '    def a(self):\n'
            '        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        lines = result.new_source.splitlines()
        comment_idx = next(i for i, line in enumerate(lines) if '# comment for a' in line)
        def_a_idx = next(i for i, line in enumerate(lines) if 'def a(' in line)
        assert def_a_idx == comment_idx + 1, (
            f'Comment at line {comment_idx}, def a at line {def_a_idx}'
        )

    def test_multiline_comment_block_moves_with_method(self) -> None:
        src = (
            'class C:\n'
            '    def b(self):\n'
            '        pass\n'
            '\n'
            '    # first line\n'
            '    # second line\n'
            '    def a(self):\n'
            '        pass\n'
        )
        result = sort_file(src, options=EngineOptions(policy=SortPolicy(), add_future=False))
        lines = result.new_source.splitlines()
        first_idx = next(i for i, line in enumerate(lines) if '# first line' in line)
        second_idx = next(i for i, line in enumerate(lines) if '# second line' in line)
        def_a_idx = next(i for i, line in enumerate(lines) if 'def a(' in line)
        assert second_idx == first_idx + 1
        assert def_a_idx == second_idx + 1


class TestMethodGroupKey:
    def test_dunder_repr(self) -> None:
        assert _method_group_key('__repr__', SortPolicy()) == (2, '__repr__')

    def test_dunder_str(self) -> None:
        assert _method_group_key('__str__', SortPolicy()) == (2, '__str__')

    def test_init(self) -> None:
        assert _method_group_key('__init__', SortPolicy()) == (1, '__init__')

    def test_new(self) -> None:
        assert _method_group_key('__new__', SortPolicy()) == (0, '__new__')

    def test_regular_method(self) -> None:
        assert _method_group_key('process', SortPolicy()) == (3, 'process')

    def test_sort_order(self) -> None:
        names = ['process', '__str__', '__init__', '__new__', 'validate']
        policy = SortPolicy()
        sorted_names = sorted(names, key=lambda name: _method_group_key(name, policy))
        assert sorted_names == ['__new__', '__init__', '__str__', 'process', 'validate']


class TestMethodTrailingComments:
    def test_trailing_comment_after_method_preserved(self) -> None:
        src = (
            'class C:\n'
            '    def b(self):\n'
            '        return self.a()\n'
            '    # trailing for b\n'
            '\n'
            '    def a(self):\n'
            '        pass\n'
        )
        result = sort_file(
            src,
            options=EngineOptions(
                policy=SortPolicy(
                    definition_order='alphabetical',
                    method_order='alphabetical',
                ),
                add_future=False,
            ),
        )
        assert '# trailing for b' in result.new_source


class TestMethodTrailingOwnershipGuard:
    def test_barriers_then_strip_removes_only_banner_span(self) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # --------\n'
            '    def omega(self):\n'
            '        pass\n'
        )
        barrier_options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
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
        expected_stripped = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    def omega(self):\n'
            '        pass\n'
        )

        first = sort_file(source, options=barrier_options).new_source
        stripped = sort_file(first, options=strip_options).new_source

        assert first == expected_first
        assert first.count('    # note for zulu\n') == 1
        assert stripped == expected_stripped
        assert stripped == first.replace('    # --------\n', '')
        assert stripped.count('    # note for zulu\n') == 1
        assert '    # --------' not in stripped
        assert sort_file(stripped, options=strip_options).new_source == stripped

    def test_class_footer_stays_opaque_at_final_method_slot(self) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    value = 1\n'
            '    # class footer\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    value = 1\n'
            '    # class footer\n'
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        original = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        result = render_layout(
            original.layout,
            original.blocks,
            [method.index for method in reversed(original.methods)],
        )
        reparsed_cls = ast.parse(result).body[0]
        assert isinstance(reparsed_cls, ast.ClassDef)
        reparsed = parse_class_methods(
            reparsed_cls,
            ClassSlice(split_physical_lines(result, keepends=True), 1),
        )
        zulu = next(method for method in reparsed.methods if method.name == 'zulu')

        assert result == expected
        assert Counter(result.encode()) == Counter(source.encode())
        assert result.endswith('    # class footer\n')
        assert '    # note for zulu' not in reparsed.blocks[zulu.index]
        assert reparsed.layout.suffix == (
            '\n    # note for zulu\n\n    value = 1\n    # class footer\n'
        )

    def test_class_true_eof_stays_opaque_at_final_method_slot(self) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    def alpha(self):\n'
            '        pass'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu'
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        original = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        result = render_layout(
            original.layout,
            original.blocks,
            [method.index for method in reversed(original.methods)],
        )
        reparsed_cls = ast.parse(result).body[0]
        assert isinstance(reparsed_cls, ast.ClassDef)
        reparsed = parse_class_methods(
            reparsed_cls,
            ClassSlice(split_physical_lines(result, keepends=True), 1),
        )
        zulu = next(method for method in reparsed.methods if method.name == 'zulu')

        assert result == expected
        assert Counter(result.encode()) == Counter(source.encode())
        assert not result.endswith(('\n', '\r'))
        assert '    # note for zulu' not in reparsed.blocks[zulu.index]
        assert reparsed.layout.suffix == '\n    # note for zulu'

    def test_mixed_ending_guard_preserves_trailing_owner(self) -> None:
        source = (
            'class C:\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    # note for charlie\n'
            '\n'
            '    def alpha(self):\r'
            '        pass\r'
            '    def zulu(self):\r'
            '        pass\r'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\r'
            '        pass\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    # note for charlie\n'
            '\r'
            '    def zulu(self):\r'
            '        pass\r'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        first = sort_file(source, options=options).new_source
        parsed_cls = ast.parse(first).body[0]
        assert isinstance(parsed_cls, ast.ClassDef)
        parsed = parse_class_methods(
            parsed_cls,
            ClassSlice(split_physical_lines(first, keepends=True), 1),
        )
        charlie = next(method for method in parsed.methods if method.name == 'charlie')

        assert first == expected
        assert parsed.blocks[charlie.index].endswith('    # note for charlie')
        assert Counter(first.encode()) == Counter(source.encode())
        compile(first, '<test>', 'exec')
        assert sort_file(first, options=options).new_source == first

    def test_multiple_owners_reuse_and_relocate_guards(self) -> None:
        source = (
            'class C:\n'
            '    def bravo(self):\n'
            '        pass\n'
            '    # note for bravo\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    def delta(self):\n'
            '        pass\n'
            '    # note for delta\n'
            '\n'
            '    def echo(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def bravo(self):\n'
            '        pass\n'
            '    # note for bravo\n'
            '\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    def delta(self):\n'
            '        pass\n'
            '    # note for delta\n'
            '\n'
            '    def echo(self):\n'
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)
        parsed_cls = ast.parse(result.new_source).body[0]
        assert isinstance(parsed_cls, ast.ClassDef)
        parsed = parse_class_methods(
            parsed_cls,
            ClassSlice(split_physical_lines(result.new_source, keepends=True), 1),
        )
        blocks = {method.name: parsed.blocks[method.index] for method in parsed.methods}

        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')
        assert Counter(result.new_source.encode()) == Counter(source.encode())
        assert '    # note for bravo' in blocks['bravo']
        assert '    # note for delta' in blocks['delta']
        assert sort_file(result.new_source, options=options).new_source == expected

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                (
                    'class C:\n'
                    '    def charlie(self):\n'
                    '        pass\n'
                    '    # note for charlie\n'
                    '     \t\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
                (
                    'class C:\n'
                    '    def alpha(self):\n'
                    '        pass\n'
                    '    def charlie(self):\n'
                    '        pass\n'
                    '    # note for charlie\n'
                    '     \t\n'
                    '    def zulu(self):\n'
                    '        pass\n'
                ),
            ),
            (
                (
                    'class C:\r\n'
                    '    def charlie(self):\r\n'
                    '        pass\r\n'
                    '    # note for charlie\r\n'
                    '     \t\r\n'
                    '    def alpha(self):\r\n'
                    '        pass\r\n'
                    '    def zulu(self):\r\n'
                    '        pass\r\n'
                ),
                (
                    'class C:\r\n'
                    '    def alpha(self):\r\n'
                    '        pass\r\n'
                    '    def charlie(self):\r\n'
                    '        pass\r\n'
                    '    # note for charlie\r\n'
                    '     \t\r\n'
                    '    def zulu(self):\r\n'
                    '        pass\r\n'
                ),
            ),
            (
                (
                    'class C:\r'
                    '    def charlie(self):\r'
                    '        pass\r'
                    '    # note for charlie\r'
                    '     \t\r'
                    '    def alpha(self):\r'
                    '        pass\r'
                    '    def zulu(self):\r'
                    '        pass\r'
                ),
                (
                    'class C:\r'
                    '    def alpha(self):\r'
                    '        pass\r'
                    '    def charlie(self):\r'
                    '        pass\r'
                    '    # note for charlie\r'
                    '     \t\r'
                    '    def zulu(self):\r'
                    '        pass\r'
                ),
            ),
            (
                (
                    'class C:\r\n'
                    '    def charlie(self):\r\n'
                    '        pass\n'
                    '    # note for charlie\r'
                    '     \t\r\n'
                    '    def alpha(self):\r'
                    '        pass\r\n'
                    '    def zulu(self):\n'
                    '        pass'
                ),
                (
                    'class C:\r\n'
                    '    def alpha(self):\r'
                    '        pass\r'
                    '    def charlie(self):\r\n'
                    '        pass\n'
                    '    # note for charlie\r\n'
                    '     \t\r\n'
                    '    def zulu(self):\n'
                    '        pass'
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
                definition_order='none',
                method_order='alphabetical',
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
        ['    #!/usr/bin/env python\n', '    # coding: utf-8\n'],
        ids=['shebang-like', 'encoding-like'],
    )
    def test_protected_looking_trailing_comment_is_idempotent(self, comment: str) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            f'{comment}'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # --------\n'
            '    def omega(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def zulu(self):\n'
            '        pass\n'
            f'{comment}'
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

        first = sort_file(source, options=options).new_source
        second = sort_file(first, options=options).new_source

        assert first == expected
        assert first.count(comment) == 1
        assert second == first
        assert second.count(comment) == 1

    def test_reordered_adjacent_method_comment_keeps_owner(self) -> None:
        source = (
            'class C:\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    # note for charlie\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def zulu(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    def charlie(self):\n'
            '        pass\n'
            '    # note for charlie\n'
            '\n'
            '    def zulu(self):\n'
            '        pass\n'
        )
        options = EngineOptions(
            policy=SortPolicy(
                definition_order='none',
                method_order='alphabetical',
            ),
            add_future=False,
            remove_future=False,
        )

        result = sort_file(source, options=options)
        parsed_cls = ast.parse(result.new_source).body[0]
        assert isinstance(parsed_cls, ast.ClassDef)
        parsed = parse_class_methods(
            parsed_cls,
            ClassSlice(split_physical_lines(result.new_source, keepends=True), 1),
        )
        charlie = next(method for method in parsed.methods if method.name == 'charlie')

        assert result.new_source == expected
        assert '    # note for charlie' in parsed.blocks[charlie.index]
        assert sort_file(result.new_source, options=options).new_source == expected

    def test_reordered_inter_item_comment_keeps_owner(self) -> None:
        source = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    # note for zulu\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    # --------\n'
            '    def omega(self):\n'
            '        pass\n'
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
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        original = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
            banners='barriers',
        )
        zulu_original = next(method for method in original.methods if method.name == 'zulu')

        first = sort_file(source, options=options).new_source
        parsed_cls = ast.parse(first).body[0]
        assert isinstance(parsed_cls, ast.ClassDef)
        parsed = parse_class_methods(
            parsed_cls,
            ClassSlice(split_physical_lines(first, keepends=True), 1),
            banners='barriers',
        )
        zulu = next(method for method in parsed.methods if method.name == 'zulu')

        assert sum(boundary.has_candidate for boundary in original.layout.ownership) == 1
        assert original.layout.ownership[zulu_original.index].has_candidate is True
        assert original.layout.guard_required == frozenset({zulu_original.index})
        assert (
            render_layout(
                original.layout,
                original.blocks,
                [method.index for method in original.methods],
            )
            == source
        )
        assert first == expected
        assert Counter(first.encode()) == Counter(source.encode())
        assert '    # note for zulu' in parsed.blocks[zulu.index]
        assert sort_file(first, options=options).new_source == first


class TestOneMethodBannerParity:
    def test_one_method_class_strips_banner_incrementally(self) -> None:
        src = (
            'class Alpha:\n'
            '    # ------------------------------\n'
            '    # Helpers\n'
            '    # ------------------------------\n'
            '\n'
            '    def solo(self):\n'
            '        pass\n'
        )
        options = EngineOptions(policy=SortPolicy(banners='strip'))
        full = sort_file(src, options=options)
        inc = sort_file_incremental(src, src, options=options)
        assert '# ---' not in full.new_source
        assert inc.new_source == full.new_source


class TestOrderMethodsAlphabetical:
    def test_category_ordering(self) -> None:
        methods = [
            _method('process', 0),
            _method('__repr__', 1),
            _method('__init__', 2),
        ]
        result = _order_methods_alphabetical(methods)
        assert [m.name for m in result] == ['__init__', '__repr__', 'process']

    @pytest.mark.parametrize(
        'source',
        [
            (
                'class Example:\n'
                '    @property\n'
                '    def zeta(self):\n'
                '        return 1\n\n'
                '    @zeta.setter\n'
                '    def zeta(self, value):\n'
                '        pass\n\n'
                '    @property\n'
                '    def alpha(self):\n'
                '        return 2\n'
            ),
            (
                'class Example:\n'
                '    @overload\n'
                '    def zeta(self, value: int) -> int: ...\n\n'
                '    @overload\n'
                '    def zeta(self, value: str) -> str: ...\n\n'
                '    def zeta(self, value):\n'
                '        return value\n\n'
                '    @overload\n'
                '    def alpha(self, value: int) -> int: ...\n\n'
                '    def alpha(self, value):\n'
                '        return value\n'
            ),
        ],
        ids=['property-family', 'overload-family'],
    )
    def test_decorated_method_families_keep_source_order(self, source: str) -> None:
        result = _sort_with_policy(
            source,
            SortPolicy(definition_order='none', method_order='alphabetical'),
        )

        assert result.index('def zeta') < result.index('def alpha')

    def test_eager_groups_keep_source_relative_slots(self) -> None:
        methods = [
            _method('zeta', 0),
            _method('omega', 1),
            _method('alpha', 2),
            _method('beta', 3),
        ]
        methods[0].has_eager_evaluation = True
        methods[2].has_eager_evaluation = True

        result = _order_methods_alphabetical(methods)

        assert [method.name for method in result] == ['zeta', 'beta', 'omega', 'alpha']

    def test_empty_returns_empty(self) -> None:
        assert _order_methods_alphabetical([]) == []

    def test_property_group_kept_together(self) -> None:
        methods = [
            _method('process', 0),
            _method('name', 1, prop_group='name'),  # @property
            _method('name', 2, prop_group='name'),  # @name.setter
            _method('name', 3, prop_group='name'),  # @name.deleter
        ]
        result = _order_methods_alphabetical(methods)
        names = [m.name for m in result]
        # 'name' group (category 3) before 'process' (category 3, alphabetical)
        assert names == ['name', 'name', 'name', 'process']

    def test_simple_alphabetical(self) -> None:
        methods = [
            _method('gamma', 0),
            _method('alpha', 1),
            _method('bravo', 2),
        ]
        result = _order_methods_alphabetical(methods)
        assert [m.name for m in result] == ['alpha', 'bravo', 'gamma']


class TestOrderMethodsByDependency:
    def test_alphabetical_when_no_deps(self) -> None:
        methods = [
            _method('gamma', 0),
            _method('alpha', 1),
            _method('bravo', 2),
        ]
        result = _order_methods_by_dependency(methods)
        assert [m.name for m in result] == ['alpha', 'bravo', 'gamma']

    def test_category_ordering_preserved(self) -> None:
        methods = [
            _method('process', 0),
            _method('__repr__', 1),
            _method('__init__', 2),
        ]
        result = _order_methods_by_dependency(methods)
        assert [m.name for m in result] == ['__init__', '__repr__', 'process']

    def test_cycle_falls_back_to_alphabetical(self) -> None:
        methods = [
            _method('bravo', 0, deps={'alpha'}),
            _method('alpha', 1, deps={'bravo'}),
        ]
        result = _order_methods_by_dependency(methods)
        assert [m.name for m in result] == ['alpha', 'bravo']

    def test_decorator_dep_sorts(self) -> None:
        methods = [
            _method('process', 0, deps={'validator'}),
            _method('validator', 1),
        ]
        result = _order_methods_by_dependency(methods)
        assert [m.name for m in result] == ['validator', 'process']

    def test_dependency_between_cycles_is_preserved(self) -> None:
        methods = [
            _method('alpha', 0, deps={'bravo', 'yankee'}),
            _method('bravo', 1, deps={'alpha'}),
            _method('yankee', 2, deps={'zulu'}),
            _method('zulu', 3, deps={'yankee'}),
        ]

        result = _order_methods_by_dependency(methods)

        names = [method.name for method in result]
        assert names.index('yankee') < names.index('alpha')

    def test_duplicate_method_names_preserved(self) -> None:
        methods = [
            _method('beta', 0),
            _method('process', 1),
            _method('alpha', 2),
            _method('process', 3),
        ]
        result = _order_methods_by_dependency(methods)
        names = [m.name for m in result]
        assert len(result) == 4, f'Expected 4 methods, got {len(result)}: {names}'
        assert names.count('process') == 2

    def test_eager_groups_keep_source_relative_slots(self) -> None:
        methods = [
            _method('zeta', 0),
            _method('omega', 1),
            _method('alpha', 2),
            _method('beta', 3),
        ]
        methods[0].has_eager_evaluation = True
        methods[2].has_eager_evaluation = True

        result = _order_methods_by_dependency(methods)

        assert [method.name for method in result] == ['zeta', 'beta', 'omega', 'alpha']

    def test_empty_returns_empty(self) -> None:
        assert _order_methods_by_dependency([]) == []

    def test_property_group_kept_together(self) -> None:
        methods = [
            _method('process', 0),
            _method('name', 1, prop_group='name'),
            _method('name', 2, prop_group='name'),
        ]
        result = _order_methods_by_dependency(methods)
        names = [m.name for m in result]
        assert names == ['name', 'name', 'process']


class TestParseClassMethods:
    def test_annotation_refs_are_separate_from_eager_dependencies(self) -> None:
        source = (
            'class Example:\n'
            '    def decorator(self): pass\n'
            '    def default(self): pass\n'
            '    def parameter_type(self): pass\n'
            '    def return_type(self): pass\n'
            '    @decorator\n'
            '    def target(\n'
            '        self,\n'
            '        value: parameter_type = default,\n'
            '        own: target = top_level,\n'
            '    ) -> return_type:\n'
            '        pass\n'
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        class_lines = [str(line) for line in source.splitlines(keepends=True)]
        class_slice = ClassSlice(class_lines, 1)

        parsed = parse_class_methods(cls, class_slice)
        target = next(method for method in parsed.methods if method.name == 'target')

        assert target.semantic_deps == {'decorator', 'default'}
        assert target.annotation_refs == {'parameter_type', 'return_type'}

    @pytest.mark.parametrize(
        ('method_source', 'expected'),
        [
            ('    def alpha(self, value=0):\n        pass\n', True),
            ('    def alpha(self, *, value=None):\n        pass\n', True),
            ('    @decorate\n    def alpha(self):\n        pass\n', True),
            ('    async def alpha(self, value=0):\n        pass\n', True),
            ('    def alpha(self):\n        pass\n', False),
        ],
        ids=[
            'positional-default',
            'keyword-only-none-default',
            'decorator',
            'async-default',
            'plain-method',
        ],
    )
    def test_eager_evaluation_metadata(
        self,
        method_source: str,
        expected: bool,
    ) -> None:
        source = 'class Example:\n' + method_source
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(source.splitlines(keepends=True), 1),
        )

        assert parsed.methods[0].has_eager_evaluation is expected

    @pytest.mark.parametrize(
        'source',
        [
            (
                '@decorate\n'
                'class Example(\n'
                '    Base,\n'
                '):\n'
                '    attribute = 1\n'
                '    # fixed leading\n'
                '    @property\n'
                '    def zulu(\n'
                '        self,\n'
                '    ):\n'
                '        return 1\n'
                '\n'
                '    # alpha leading\n'
                '    def alpha(self):\n'
                '        pass\n'
                '    footer = 2\n'
                '    # fixed trailing\n'
            ),
            'class C:\n    def zulu(self):\n        pass\n    def alpha(self):\n        pass',
            (
                'class C:\r\n'
                '    def zulu(self):\r\n'
                '        pass\r\n'
                '\r\n'
                '    def alpha(self):\r\n'
                '        pass\r\n'
            ),
            (
                'class C:\r'
                '    def zulu(self):\r'
                '        pass\r'
                '\r\r'
                '    def alpha(self):\r'
                '        pass'
            ),
            (
                'class C:\r\n'
                '    # fixed leading\n'
                '    def zulu(self):\r'
                '        pass\n'
                ' \t\r\n'
                '\r'
                '\n'
                '    # alpha leading\r\n'
                '    def alpha(self):\n'
                '        pass\r'
                '    # fixed trailing'
            ),
        ],
        ids=[
            'decorated-multiline-header-and-footer',
            'zero-blank-lines-no-final-newline',
            'one-crlf-blank-line-final-newline',
            'two-lone-cr-blank-lines-no-final-newline',
            'mixed-multiple-whitespace-filled-lines',
        ],
    )
    def test_layout_identity_round_trip(self, source: str) -> None:
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        parsed = parse_class_methods(cls, ClassSlice(source.splitlines(keepends=True), 1))

        assert (
            render_layout(
                parsed.layout,
                parsed.blocks,
                [method.index for method in parsed.methods],
            )
            == source
        )

    def test_layout_reuses_prefix_separators_and_suffix_when_reordered(self) -> None:
        source = (
            '@decorate\n'
            'class C(\n'
            '    Base,\n'
            '):\n'
            '    attribute = 1\n'
            '    # class leading\n'
            '    @marker\n'
            '    def zulu(self):\n'
            '        pass\n'
            ' \t\n'
            '    # alpha leading\n'
            '    def alpha(self):\n'
            '        pass\n'
            '    footer = 2\n'
            '    # class trailing\n'
        )
        expected = (
            '@decorate\n'
            'class C(\n'
            '    Base,\n'
            '):\n'
            '    attribute = 1\n'
            '    # alpha leading\n'
            '    def alpha(self):\n'
            '        pass\n'
            ' \t\n'
            '    # class leading\n'
            '    @marker\n'
            '    def zulu(self):\n'
            '        pass\n'
            '    footer = 2\n'
            '    # class trailing\n'
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        class_lines = [str(line) for line in source.splitlines(keepends=True)]
        parsed = parse_class_methods(cls, ClassSlice(class_lines, 1))

        assert parsed.blocks[0].startswith('    # class leading\n    @marker\n')
        assert parsed.methods[0].source == ('    @marker\n    def zulu(self):\n        pass\n')
        assert render_layout(parsed.layout, parsed.blocks, [1, 0]) == expected

    def test_no_method_layout_keeps_the_whole_class_in_prefix(self) -> None:
        source = 'class C:\r\n    attribute = 1\r\n    # trailing'
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        class_lines = [str(line) for line in source.splitlines(keepends=True)]
        parsed = parse_class_methods(cls, ClassSlice(class_lines, 1))

        assert parsed.methods == []
        assert parsed.blocks == {}
        assert render_layout(parsed.layout, parsed.blocks, []) == source

    def test_one_method_layout_owns_terminal_ending_in_class_suffix(self) -> None:
        prefix = (
            '@decorate\r\n'
            'class C(\r\n'
            '    Base,\r\n'
            '):\r\n'
            '    attribute = 1\r\n'
            '    # fixed prefix\r\n'
            '\r\n'
        )
        method_source = (
            '    @wrapper\r\n    def only(\r\n        self,\r\n    ):\r\n        pass\r\n'
        )
        footer = '    footer = 2\r\n    # fixed suffix'
        suffix = '\r\n' + footer
        source = prefix + method_source + footer
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)
        class_lines = [str(line) for line in source.splitlines(keepends=True)]

        parsed = parse_class_methods(cls, ClassSlice(class_lines, 1))

        assert [method.index for method in parsed.methods] == [0]
        assert parsed.methods[0].source == method_source
        assert parsed.layout == SourceLayout(prefix, (), suffix)
        assert parsed.blocks == {0: method_source.removesuffix('\r\n')}
        assert render_layout(parsed.layout, parsed.blocks, [0]) == source

    @pytest.mark.parametrize('coordinates', [False, True])
    def test_optional_body_collection_preserves_other_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
        coordinates: bool,
    ) -> None:
        source = (
            'class C:\n'
            '    def zeta(self):\n        return 1\n\n'
            '    # ----------\n\n'
            '    def beta(self, cb=zeta) -> zeta:\n        return self.zeta()\n'
        )
        tree = ast.parse(source)
        cls = tree.body[0]
        assert isinstance(cls, ast.ClassDef)
        cs = ClassSlice(lines=split_physical_lines(source, keepends=True), start_line=1)
        if coordinates:
            complete = collect_method_coordinates(
                source, tree, banners='barriers', banner_pattern=None
            )[('C', 1)]
        else:
            complete = parse_class_methods(cls, cs, banners='barriers')
        assert complete.methods[1].body_refs == {'zeta'}
        assert complete.methods[1].semantic_deps == {'zeta'}
        assert complete.methods[1].eager_reads == {'zeta'}
        assert complete.methods[1].annotation_refs == {'zeta'}
        assert complete.methods[1].has_eager_evaluation

        def fail_body_collection(*_args: object, **_kwargs: object) -> set[str]:
            pytest.fail('disabled method body was analyzed')

        monkeypatch.setattr(methods_module, 'collect_body_refs', fail_body_collection)
        if coordinates:
            without_bodies = collect_method_coordinates(
                source, tree, banners='barriers', banner_pattern=None, collect_bodies=False
            )[('C', 1)]
        else:
            without_bodies = parse_class_methods(cls, cs, banners='barriers', collect_bodies=False)
        assert without_bodies.methods == [
            replace(method, body_refs=set()) for method in complete.methods
        ]
        assert without_bodies.layout == complete.layout
        assert without_bodies.blocks == complete.blocks
        assert without_bodies.boundaries == complete.boundaries
        assert without_bodies.boundary_layout == complete.boundary_layout

    def test_statement_ownership_scanned_once_for_large_class(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ownership_helper = cast(
            'Callable[..., object]',
            methods_module.statement_owned_lines_by_gap,
        )
        ownership_calls = 0

        def track_ownership(*args: object, **kwargs: object) -> object:
            nonlocal ownership_calls
            ownership_calls += 1
            return ownership_helper(*args, **kwargs)

        monkeypatch.setattr(methods_module, 'statement_owned_lines_by_gap', track_ownership)
        source = 'class C:\n' + ''.join(
            f'    def m{i}(self):\n        return {i}\n\n' for i in range(200)
        )
        cls = ast.parse(source).body[0]
        assert isinstance(cls, ast.ClassDef)

        parsed = parse_class_methods(
            cls,
            ClassSlice(split_physical_lines(source, keepends=True), 1),
        )

        assert len(parsed.methods) == 200
        assert ownership_calls == 1


class TestSortClassMethodsScope:
    def test_alphabetical(self) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
        )
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _sort_class_methods(
            cls,
            src,
            1,
            policy=SortPolicy(
                method_order='alphabetical',
                banners='ignore',
            ),
        )
        assert result.new_source is not None
        assert result.new_source.index('alpha') < result.new_source.index('bravo')

    def test_bare_form_feed_between_methods_is_positional(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        pass\n'
            '\n'
            '\f\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '\f\n'
            '    def zulu(self):\n'
            '        pass\n'
        )
        policy = SortPolicy(banners='ignore')
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _sort_class_methods(
            cls,
            src,
            1,
            policy=policy,
        )
        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')

        second_cls = ast.parse(result.new_source).body[0]
        assert isinstance(second_cls, ast.ClassDef)
        second_result = _sort_class_methods(
            second_cls,
            result.new_source,
            1,
            policy=policy,
        )
        assert second_result.new_source is None

    def test_dependency(self) -> None:
        src = (
            'class Foo:\n'
            '    def alpha(self) -> None:\n'
            '        self.zulu()\n'
            '\n'
            '    def zulu(self) -> None:\n'
            '        pass\n'
        )
        expected = (
            'class Foo:\n'
            '    def zulu(self) -> None:\n'
            '        pass\n'
            '\n'
            '    def alpha(self) -> None:\n'
            '        self.zulu()\n'
        )
        policy = SortPolicy(banners='ignore')
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _sort_class_methods(
            cls,
            src,
            1,
            policy=policy,
        )
        assert result.new_source == expected

        repeated_cls = ast.parse(result.new_source).body[0]
        assert isinstance(repeated_cls, ast.ClassDef)
        repeated = _sort_class_methods(
            repeated_cls,
            result.new_source,
            1,
            policy=policy,
        )
        assert repeated.new_source is None

    def test_method_docstring_form_feed_keeps_spans(self) -> None:
        src = (
            'class C:\n'
            '    def zulu(self):\n'
            '        """Z\fdoc."""\n'
            '\n'
            '    def alpha(self):\n'
            '        pass\n'
        )
        expected = (
            'class C:\n'
            '    def alpha(self):\n'
            '        pass\n'
            '\n'
            '    def zulu(self):\n'
            '        """Z\fdoc."""\n'
        )
        policy = SortPolicy(banners='ignore')
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _sort_class_methods(
            cls,
            src,
            1,
            policy=policy,
        )
        assert result.new_source == expected
        compile(result.new_source, '<test>', 'exec')

        second_cls = ast.parse(result.new_source).body[0]
        assert isinstance(second_cls, ast.ClassDef)
        second_result = _sort_class_methods(
            second_cls,
            result.new_source,
            1,
            policy=policy,
        )
        assert second_result.new_source is None

    def test_none_returns_none(self) -> None:
        src = (
            'class Foo:\n'
            '    def bravo(self) -> None:\n        pass\n\n'
            '    def alpha(self) -> None:\n        pass\n'
        )
        cls = ast.parse(src).body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _sort_class_methods(
            cls,
            src,
            1,
            policy=SortPolicy(
                method_order='none',
                banners='ignore',
            ),
        )
        assert result.new_source is None


class TestSortFileMethods:
    def test_disabled_method_order_fast_path_skips_class_analysis(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = 'class C:\n    def alpha(self): pass\n'

        def fail_class_analysis(*_args: object, **_kwargs: object) -> None:
            pytest.fail('disabled method ordering analyzed a class')

        monkeypatch.setattr(methods_module, '_sort_class_methods', fail_class_analysis)

        result = sort_file_methods(
            source,
            ast.parse(source),
            policy=SortPolicy(method_order='none', banners='ignore'),
        )

        assert result.candidate_source == source
        assert result.method_sorts == ()
        assert result.banner_constraints == ()
        assert result.class_blockers == ()

    def test_reports_blocked_class_without_rewriting_it(self) -> None:
        source = (
            'class C:\n'
            '    def alpha(self): pass\n'
            '    def beta(self): pass\n'
            '    def alpha(self): pass\n'
        )

        result = sort_file_methods(
            source,
            ast.parse(source),
            policy=SortPolicy(banners='ignore'),
        )

        assert result.candidate_source == source
        assert result.class_blockers == (
            ClassBlocker(
                class_name='C',
                occurrence=1,
                blocker='duplicates',
                message='non-consecutive duplicate definitions',
            ),
        )

    def test_returns_method_banner_constraints_from_same_pass(self) -> None:
        source = (
            'class C:\n    def zed(self): pass\n\n    # ----------\n\n    def alpha(self): pass\n'
        )

        result = sort_file_methods(source, ast.parse(source), policy=SortPolicy())

        assert result.candidate_source == source
        assert [item.scope for item in result.banner_constraints] == ['methods']

    def test_rewrites_classes_and_returns_movement(self) -> None:
        source = 'class C:\n    def bravo(self): pass\n    def alpha(self): pass\n'
        tree = ast.parse(source)

        result = sort_file_methods(
            source,
            tree,
            policy=SortPolicy(banners='ignore'),
        )

        assert result.candidate_source.index('alpha') < result.candidate_source.index('bravo')
        assert [item.class_name for item in result.method_sorts] == ['C']
        assert result.banner_constraints == ()
        assert result.class_blockers == ()

    def test_strip_bypasses_fast_path_and_processes_classes(self) -> None:
        banner = '    # --------\n'
        source = f'class C:\n    def alpha(self): pass\n\n{banner}\n    def bravo(self): pass\n'

        result = sort_file_methods(
            source,
            ast.parse(source),
            policy=SortPolicy(method_order='none', banners='strip'),
        )

        assert result.candidate_source == source.replace(banner, '')
        assert result.method_sorts == ()
        assert result.class_blockers == ()


def _execute_source(source: str) -> dict[str, object]:
    namespace: dict[str, object] = {}
    FunctionType(compile(source, '<test>', 'exec'), namespace)()
    return namespace


def _make_methods(names: list[str]) -> list[MethodDefinition]:
    return [
        MethodDefinition(
            index=index,
            name=name,
            start_line=index + 1,
            end_line=index + 1,
            source='',
            semantic_deps=set(),
            section=0,
        )
        for index, name in enumerate(names)
    ]


def _method(
    name: str,
    index: int,
    *,
    deps: set[str] | None = None,
    prop_group: str | None = None,
    is_getter: bool = False,
) -> MethodDefinition:
    kind: PropertyKind | None = None
    if is_getter:
        kind = 'getter'
    return MethodDefinition(
        index=index,
        name=name,
        start_line=index * 10,
        end_line=index * 10 + 5,
        source=f'    def {name}(self): pass',
        semantic_deps=deps or set(),
        property_group=prop_group,
        property_kind=kind,
    )


def _sort_with_policy(source: str, policy: SortPolicy) -> str:
    tree = ast.parse(source)
    cls = cast('ast.ClassDef', tree.body[0])
    result = _sort_class_methods(cls, source, 1, policy=policy)
    return result.new_source if result.new_source is not None else source
