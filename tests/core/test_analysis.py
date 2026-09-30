import ast
import re
import textwrap
from collections.abc import Hashable, Iterator
from types import FunctionType, MappingProxyType
from typing import cast

import pytest

import defsort._core._analysis as analysis
from defsort._core import sort_file
from defsort._core._analysis import (
    DIRECTIVE_PATTERN,
    ENCODING_PATTERN,
    SAFE_TRAILING_PATTERN,
    _analyze_class_deps,
    _analyze_signature_deps,
    _collect_all_names,
    _get_import_names,
    _get_with_names,
    _is_generator_function,
    _names_from_target,
    analyze_annotation_refs,
    analyze_dependencies,
    analyze_eager_refs,
    collect_body_refs,
    collect_name_references,
    extract_postamble_names,
    find_cycles,
    get_bound_names,
    has_future_annotations_import,
    is_constant_name,
    kendall_tau,
    module_contains_annotations,
)
from defsort._core._parse import parse_definitions
from defsort._types import EngineOptions, SortPolicy

_BODY_REF_SCOPE_CASES: dict[str, tuple[str, set[str]]] = {
    # Nesting depth: a binding in a grandchild belongs to the grandchild.
    'grandchild_binding': (
        """
def a(self):
    def inner():
        def deeper():
            self = object()
            return self.c
        return self.b
    return inner
""",
        {'b'},
    ),
    'async_grandchild_binding': (
        """
def a(self):
    async def inner():
        async def deeper():
            self = object()
            return self.c
        return self.b
    return inner
""",
        {'b'},
    ),
    'grandchild_loop_target': (
        """
def a(self):
    def inner():
        def deeper():
            for self in ():
                pass
        return self.b
    return inner
""",
        {'b'},
    ),
    'grandchild_match_capture': (
        """
def a(self):
    def inner():
        def deeper():
            match ():
                case self:
                    pass
        return self.b
    return inner
""",
        {'b'},
    ),
    'grandchild_comprehension_walrus': (
        """
def a(self):
    def inner():
        def deeper():
            return [(self := object()) for _ in ()]
        return self.b
    return inner
""",
        {'b'},
    ),
    'nested_class_method_keeps_closure_ref': (
        """
def a(self):
    def inner():
        class Handler:
            def run(self):
                return self.c
        return self.b(Handler)
    return inner
""",
        {'b'},
    ),
    # Class bodies bind, and are boundaries for the functions around them.
    'class_body_reads_receiver': (
        """
def a(self):
    class C:
        x = self.b
    return C
""",
        {'b'},
    ),
    'class_body_binding_excludes_class_body': (
        """
def a(self):
    class C:
        self = 1
        x = self.b
    return self.c
""",
        {'c'},
    ),
    'class_body_binding_keeps_function_ref': (
        """
def a(self):
    def inner():
        class C:
            self = 1
        return self.b
    return inner
""",
        {'b'},
    ),
    # Outer parts evaluate in the enclosing scope even when the scope is excluded.
    'nested_annotations_evaluate_outer': (
        """
def a(self):
    def inner(x: self.b) -> self.c:
        return x
    return inner
""",
        {'b', 'c'},
    ),
    'decorator_on_excluded_scope_counts': (
        """
def a(self):
    @self.b
    def inner(self, event):
        return event
    return inner
""",
        {'b'},
    ),
    'walrus_in_nested_default_binds_outer': (
        """
def a(self):
    def inner(x=[(self := object()) for _ in ()]):
        return self.b
    return inner
""",
        {'b'},
    ),
    'first_iterable_evaluates_outer': (
        """
def a(self):
    return [None for self in self.b()]
""",
        {'b'},
    ),
    'comprehension_walrus_binds_function': (
        """
def a(self):
    def inner():
        return [((self := object()), self.b) for _ in ()]
    return self.c
""",
        {'c'},
    ),
    # Declarations are whole-scope facts, resolved before bindings.
    'nonlocal_declaration_only': (
        """
def a(self):
    def inner():
        nonlocal self
        return self.b
    return inner
""",
        {'b'},
    ),
    'nonlocal_with_assignment': (
        """
def a(self):
    def inner():
        nonlocal self
        self = self.b
        return self.c
    return inner
""",
        {'b', 'c'},
    ),
    'global_declaration_only': (
        """
def a(self):
    def inner():
        global self
        return self.b
    return self.c
""",
        {'c'},
    ),
    'global_with_assignment': (
        """
def a(self):
    def inner():
        global self
        self = 1
        return self.b
    return self.c
""",
        {'c'},
    ),
    # Every binding form excludes its own scope and nothing else.
    'del_excludes_scope_only': (
        """
def a(self):
    def inner():
        del self
        return self.b
    return self.c
""",
        {'c'},
    ),
    'loop_target_excludes_scope_only': (
        """
def a(self):
    def inner():
        for self in ():
            pass
        return self.b
    return self.c
""",
        {'c'},
    ),
    'except_target_excludes_scope_only': (
        """
def a(self):
    def inner():
        try:
            pass
        except Exception as self:
            return self.b
    return self.c
""",
        {'c'},
    ),
    'with_target_excludes_scope_only': (
        """
def a(self):
    def inner():
        with open('f') as self:
            return self.b
    return self.c
""",
        {'c'},
    ),
    'import_alias_excludes_scope_only': (
        """
def a(self):
    def inner():
        import os as self
        return self.b
    return self.c
""",
        {'c'},
    ),
    'match_capture_excludes_scope_only': (
        """
def a(self):
    def inner():
        match ():
            case self:
                return self.b
    return self.c
""",
        {'c'},
    ),
    'lambda_param_excludes_lambda_only': (
        """
def a(self):
    f = lambda self: self.b
    return self.c
""",
        {'c'},
    ),
    'nested_def_named_receiver': (
        """
def a(self):
    def inner():
        def self():
            pass
        return self.b
    return self.c
""",
        {'c'},
    ),
}


class TestAggregateScore:
    def test_sums_within_section_discordant_and_total_pairs(self) -> None:
        aggregate_score = getattr(analysis, 'aggregate_score', None)

        assert callable(aggregate_score)
        assert aggregate_score([[1, 0], [0, 1, 2]]) == 0.5


class TestAnalyzeAnnotationRefs:
    def test_async_function_annotations(self) -> None:
        node = _parse_stmt('async def f(x: Alpha) -> Bravo: pass')
        assert isinstance(node, ast.AsyncFunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha', 'Bravo'}

    def test_class_annassign(self) -> None:
        node = _parse_stmt('class Foo:\n    x: Alpha = 1')
        assert isinstance(node, ast.ClassDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha'}

    def test_class_annotations_under_control_flow(self) -> None:
        src = (
            'class Alpha[T: Ignored]:\n'
            '    if condition:\n'
            '        first: Later\n'
            '    try:\n'
            '        second: Later\n'
            '    except Exception:\n'
            '        pass\n'
            '    match subject:\n'
            '        case _:\n'
            '            third: Later\n'
            '    type Alias = Ignored\n'
            '    def method(self):\n'
            '        local: Ignored\n'
        )
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)

        assert analyze_annotation_refs(node) == {'Later'}

    def test_class_method_annotations(self) -> None:
        src = 'class Foo:\n    def m(self, x: Alpha) -> Bravo: pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha', 'Bravo'}

    def test_class_nested_class_recursive(self) -> None:
        src = 'class Outer:\n    class Inner:\n        def m(self) -> Alpha: pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha'}

    def test_class_self_referential_not_excluded(self) -> None:
        src = 'class Node:\n    def next(self) -> Node: pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Node'}

    def test_class_unannotated_assign_ignored(self) -> None:
        node = _parse_stmt('class Foo:\n    x = helper()')
        assert isinstance(node, ast.ClassDef)
        refs = analyze_annotation_refs(node)
        assert refs == set()

    def test_function_kwarg_annotation(self) -> None:
        node = _parse_stmt('def f(**kw: Alpha): pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha'}

    def test_function_no_annotations(self) -> None:
        node = _parse_stmt('def f(x, y): pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == set()

    def test_function_param_and_return(self) -> None:
        node = _parse_stmt('def f(x: Alpha) -> Bravo: pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha', 'Bravo'}

    def test_function_posonly_annotation(self) -> None:
        node = _parse_stmt('def f(x: Alpha, /): pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha'}

    def test_function_self_name_not_excluded(self) -> None:
        node = _parse_stmt('def f(x: f) -> f: pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'f'}

    def test_function_vararg_annotation(self) -> None:
        node = _parse_stmt('def f(*args: Alpha): pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == {'Alpha'}

    def test_no_decorators_or_defaults_scanned(self) -> None:
        node = _parse_stmt('@wrapper\ndef f(x=default_val): pass')
        assert isinstance(node, ast.FunctionDef)
        refs = analyze_annotation_refs(node)
        assert refs == set()


class TestAnalyzeDependenciesClass:
    def test_async_method_body_not_scanned(self) -> None:
        src = 'class Foo:\n    async def m(self):\n        return helper()'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'helper'})
        assert deps == set()

    def test_async_method_signature_scanned(self) -> None:
        src = 'class Foo:\n    async def m(self, x: Alpha): pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Alpha'})
        assert deps == {'Alpha'}

    def test_base_class_creates_dep(self) -> None:
        node = _parse_stmt('class Derived(Base): pass')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Base'})
        assert deps == {'Base'}

    def test_called_sibling_body_creates_top_level_dependency(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        return zeta_compute()\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)
        eager_refs = analyze_eager_refs(node)

        deps = analyze_dependencies(node, {'Alpha', 'zeta_compute'}, eager_refs=eager_refs)

        assert deps == {'zeta_compute'}

    def test_class_level_assignment_scanned(self) -> None:
        node = _parse_stmt('class Foo:\n    x = helper()')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'helper'})
        assert deps == {'helper'}

    def test_class_level_expression_scanned(self) -> None:
        node = _parse_stmt('class Foo:\n    register(Foo)')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'register'})
        assert deps == {'register'}

    def test_decorator_creates_dep(self) -> None:
        node = _parse_stmt('@decorator\nclass Foo: pass')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'decorator'})
        assert deps == {'decorator'}

    def test_generic_class_bound_dependency(self) -> None:
        src = 'class C[T: Bound]:\n    pass\n'
        tree = ast.parse(src)
        cls = tree.body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _analyze_class_deps(cls, {'Bound', 'C'}, 'C')
        assert result == {'Bound'}

    def test_generic_method_bound_dependency(self) -> None:
        src = 'class A:\n    def m[T: Bound](self):\n        pass\n'
        tree = ast.parse(src)
        cls = tree.body[0]
        assert isinstance(cls, ast.ClassDef)
        result = _analyze_class_deps(cls, {'A', 'Bound'}, 'A')
        assert result == {'Bound'}

    def test_keyword_arg_creates_dep(self) -> None:
        node = _parse_stmt('class Foo(metaclass=ABCMeta): pass')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'ABCMeta'})
        assert deps == {'ABCMeta'}

    def test_method_body_not_scanned(self) -> None:
        src = 'class Foo:\n    def m(self):\n        return helper()'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'helper'})
        assert deps == set()

    def test_method_signature_annotation_scanned(self) -> None:
        src = 'class Foo:\n    def m(self, x: Alpha) -> Bravo: pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Alpha', 'Bravo'})
        assert deps == {'Alpha', 'Bravo'}

    def test_method_signature_decorator_scanned(self) -> None:
        src = 'class Foo:\n    @wrapper\n    def m(self): pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'wrapper'})
        assert deps == {'wrapper'}

    def test_method_signature_default_scanned(self) -> None:
        src = 'class Foo:\n    def m(self, x=default_val): pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'default_val'})
        assert deps == {'default_val'}

    def test_multiple_bases(self) -> None:
        node = _parse_stmt('class C(Alpha, Bravo): pass')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Alpha', 'Bravo', 'Charlie'})
        assert deps == {'Alpha', 'Bravo'}

    def test_nested_class_method_body_not_scanned(self) -> None:
        src = 'class Outer:\n    class Inner:\n        def m(self):\n            return helper()'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'helper'})
        assert deps == set()

    def test_nested_class_precomputed_eager_refs_are_analyzed_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        node = _parse_stmt(
            'class Outer:\n'
            '    class Inner:\n'
            '        def helper():\n'
            '            return zeta_compute()\n'
            '        value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)
        analyzed: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = []
        original_analyze_eager_refs = analysis.analyze_eager_refs

        def spy_analyze_eager_refs(
            definition: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> set[str]:
            analyzed.append(definition)
            return original_analyze_eager_refs(definition)

        monkeypatch.setattr(analysis, 'analyze_eager_refs', spy_analyze_eager_refs)
        eager_refs = analysis.analyze_eager_refs(node)

        deps = analysis.analyze_dependencies(
            node,
            {'Outer', 'zeta_compute'},
            eager_refs=eager_refs,
        )

        assert deps == {'zeta_compute'}
        assert analyzed == [node]

    def test_nested_class_recursive(self) -> None:
        src = 'class Outer:\n    class Inner(Base): pass'
        node = _parse_stmt(src)
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Base'})
        assert deps == {'Base'}

    def test_self_dep_excluded(self) -> None:
        node = _parse_stmt('class Foo(Foo): pass')
        assert isinstance(node, ast.ClassDef)
        deps = analyze_dependencies(node, {'Foo'})
        assert deps == set()

    def test_uncalled_method_body_still_creates_no_dependency(self) -> None:
        node = _parse_stmt('class Alpha:\n    def helper():\n        return zeta_compute()\n')
        assert isinstance(node, ast.ClassDef)
        eager_refs = analyze_eager_refs(node)

        assert (
            analyze_dependencies(
                node,
                {'Alpha', 'zeta_compute'},
                eager_refs=eager_refs,
            )
            == set()
        )


class TestAnalyzeDependenciesFunction:
    def test_annotation_creates_dep(self) -> None:
        node = _parse_stmt('def f(x: Alpha) -> Bravo: pass')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'Alpha', 'Bravo'})
        assert deps == {'Alpha', 'Bravo'}

    def test_async_def_same_as_def(self) -> None:
        node = _parse_stmt('async def f():\n    return alpha()')
        assert isinstance(node, ast.AsyncFunctionDef)
        deps = analyze_dependencies(node, {'alpha'})
        assert deps == {'alpha'}

    def test_body_scanned_catches_closures(self) -> None:
        node = _parse_stmt('def f():\n    return alpha() + bravo')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'alpha', 'bravo', 'charlie'})
        assert deps == {'alpha', 'bravo'}

    def test_decorator_creates_dep(self) -> None:
        node = _parse_stmt('@wrapper\ndef f(): pass')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'wrapper'})
        assert deps == {'wrapper'}

    def test_default_arg_creates_dep(self) -> None:
        node = _parse_stmt('def f(x=default_val): pass')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'default_val'})
        assert deps == {'default_val'}

    def test_generic_function_bound_dependency(self) -> None:
        src = 'def f[T: Bound]():\n    pass\n'
        tree = ast.parse(src)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        result = analyze_dependencies(func, {'Bound', 'f'})
        assert result == {'Bound'}

    def test_no_deps_returns_empty(self) -> None:
        node = _parse_stmt('def f():\n    return 42')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'alpha', 'bravo'})
        assert deps == set()

    def test_only_load_context(self) -> None:
        node = _parse_stmt('def f():\n    alpha = 1\n    return alpha')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'alpha'})
        # alpha is both stored and loaded; the Load usage creates a dep
        assert deps == {'alpha'}

    def test_self_dep_excluded(self) -> None:
        node = _parse_stmt('def f():\n    return f()')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'f'})
        assert deps == set()

    def test_store_only_no_dep(self) -> None:
        node = _parse_stmt('def f():\n    alpha = 1')
        assert isinstance(node, ast.FunctionDef)
        deps = analyze_dependencies(node, {'alpha'})
        assert deps == set()


class TestAnalyzeEagerRefs:
    def test_called_body_executes_nested_definition_eager_parts_only(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        def nested(value=compute()):\n'
            '            return deferred()\n'
            '        return nested\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        refs = analyze_eager_refs(node)
        assert 'compute' in refs
        assert 'deferred' not in refs

    def test_called_sibling_body_is_eager(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n    def helper():\n        return compute()\n    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert analyze_eager_refs(node) == {'compute', 'helper'}

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                (
                    '@decorate(factory())\n'
                    'def alpha(value=default(), *, other=keyword()):\n'
                    '    return body()\n'
                ),
                {'decorate', 'factory', 'default', 'keyword'},
            ),
            (
                (
                    '@decorate\n'
                    'class Alpha(base(), metaclass=meta()):\n'
                    '    value = make()\n'
                    '    def method(self, value=method_default()):\n'
                    '        return body()\n'
                ),
                {'decorate', 'base', 'meta', 'make', 'method_default'},
            ),
            (
                'def alpha(value: annotation = lambda: deferred()):\n    return body()\n',
                {'deferred'},
            ),
        ],
    )
    def test_collects_eager_reads(self, source: str, expected: set[str]) -> None:
        node = _parse_stmt(source)
        assert isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))

        assert analyze_eager_refs(node) == expected

    @pytest.mark.parametrize(
        'definition',
        [
            'async def helper():\n        return compute()',
            'def helper():\n        yield compute()',
        ],
        ids=['async', 'generator'],
    )
    def test_deferred_sibling_function_body_stays_lazy(self, definition: str) -> None:
        node = _parse_stmt(f'class Alpha:\n    {definition}\n    value = helper()\n')
        assert isinstance(node, ast.ClassDef)

        assert 'compute' not in analyze_eager_refs(node)

    def test_duplicate_sibling_names_union_called_body_reads(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        return first()\n'
            '    def helper():\n'
            '        return second()\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert {'first', 'second'} <= analyze_eager_refs(node)

    @pytest.mark.parametrize(
        'call_site',
        [
            'alias = helper\n    value = alias()',
            'value = Alpha.helper()',
            'value = getattr(Alpha, "helper")()',
        ],
        ids=['alias', 'attribute', 'computed'],
    )
    def test_indirect_sibling_call_does_not_expand_body(self, call_site: str) -> None:
        node = _parse_stmt(
            f'class Alpha:\n    def helper():\n        return compute()\n    {call_site}\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert 'compute' not in analyze_eager_refs(node)

    def test_nested_class_does_not_use_outer_sibling_map(self) -> None:
        node = _parse_stmt(
            'class Outer:\n'
            '    def helper():\n'
            '        return leaked()\n'
            '    class Inner:\n'
            '        value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert 'leaked' not in analyze_eager_refs(node)

    def test_nested_class_uses_its_own_sibling_map(self) -> None:
        node = _parse_stmt(
            'class Outer:\n'
            '    class Inner:\n'
            '        def helper():\n'
            '            return inner_dependency()\n'
            '        value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert 'inner_dependency' in analyze_eager_refs(node)

    def test_nested_default_yield_makes_called_outer_function_lazy(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        def nested(value=(yield zeta_compute())):\n'
            '            return value\n'
            '        return nested\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert analyze_eager_refs(node) == {'helper'}

    def test_nested_yield_does_not_make_called_outer_function_a_generator(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        def deferred():\n'
            '            yield later()\n'
            '        return compute()\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        refs = analyze_eager_refs(node)
        assert 'compute' in refs
        assert 'later' not in refs

    def test_repeated_sibling_calls_deduplicate_body_reads(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def helper():\n'
            '        return compute()\n'
            '    first = helper()\n'
            '    second = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert analyze_eager_refs(node) == {'compute', 'helper'}

    def test_staticmethod_body_is_eager_when_class_suite_calls_it(self) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    @staticmethod\n'
            '    def helper():\n'
            '        return compute()\n'
            '    value = helper()\n'
        )
        assert isinstance(node, ast.ClassDef)

        assert {'compute', 'helper', 'staticmethod'} <= analyze_eager_refs(node)

    def test_uncalled_sibling_body_stays_lazy(self) -> None:
        node = _parse_stmt('class Alpha:\n    def helper():\n        return compute()\n')
        assert isinstance(node, ast.ClassDef)

        assert analyze_eager_refs(node) == set()

    def test_uncalled_sibling_is_not_generator_classified(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        node = _parse_stmt(
            'class Alpha:\n'
            '    def called():\n'
            '        return compute()\n'
            '    def uncalled():\n'
            '        return deferred()\n'
            '    first = called()\n'
            '    second = called()\n'
        )
        assert isinstance(node, ast.ClassDef)
        functions = {
            statement.name: statement
            for statement in node.body
            if isinstance(statement, ast.FunctionDef)
        }
        classified: list[ast.FunctionDef] = []
        original_is_generator_function = _is_generator_function

        def spy_is_generator_function(function: ast.FunctionDef) -> bool:
            classified.append(function)
            return original_is_generator_function(function)

        monkeypatch.setattr(analysis, '_is_generator_function', spy_is_generator_function)

        assert analysis.analyze_eager_refs(node) == {'called', 'compute'}
        assert classified == [functions['called']]


class TestAnalyzeSignatureDeps:
    def test_annotation_on_kwarg(self) -> None:
        src = 'def f(**kw: Alpha): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'Alpha'}, exclude='Cls')
        assert refs == {'Alpha'}

    def test_annotation_on_vararg(self) -> None:
        src = 'def f(*args: Alpha): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'Alpha'}, exclude='Cls')
        assert refs == {'Alpha'}

    def test_async_def_signature_scanned(self) -> None:
        src = 'async def m(self, x: Alpha) -> Bravo: pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.AsyncFunctionDef)
        refs = _analyze_signature_deps(func, {'Alpha', 'Bravo'}, exclude='Cls')
        assert refs == {'Alpha', 'Bravo'}

    def test_body_not_scanned(self) -> None:
        src = 'def m(self):\n    return helper()'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'helper'}, exclude='Cls')
        assert refs == set()

    def test_decorator_scanned(self) -> None:
        src = '@wrapper\ndef m(self): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'wrapper'}, exclude='Cls')
        assert refs == {'wrapper'}

    def test_default_arg_scanned(self) -> None:
        src = 'def m(self, x=default_val): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'default_val'}, exclude='Cls')
        assert refs == {'default_val'}

    def test_excludes_self_name(self) -> None:
        src = '@Cls\ndef m(self): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'Cls'}, exclude='Cls')
        assert refs == set()

    def test_kw_default_scanned(self) -> None:
        src = 'def m(self, *, x=default_val): pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'default_val'}, exclude='Cls')
        assert refs == {'default_val'}

    def test_param_annotation_scanned(self) -> None:
        src = 'def m(self, x: Alpha) -> Bravo: pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'Alpha', 'Bravo'}, exclude='Cls')
        assert refs == {'Alpha', 'Bravo'}

    def test_return_annotation_scanned(self) -> None:
        src = 'def m(self) -> RetType: pass'
        func = _parse_stmt(src)
        assert isinstance(func, ast.FunctionDef)
        refs = _analyze_signature_deps(func, {'RetType'}, exclude='Cls')
        assert refs == {'RetType'}


class TestBoundedEagerExpansion:
    def test_bare_sibling_decorator_body_is_eager(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class alpha:\n'
            '    def deco(fn):\n'
            '        print(zed())\n'
            '        return fn\n'
            '\n'
            '    @deco\n'
            '    def m(self):\n'
            '        return 1\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('def zed') < result.new_source.index('class alpha')

    def test_class_bound_consumer_name_stays_deferred(self) -> None:
        src = (
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    list = len\n'
            '    ITEMS = list(gen())\n'
        )
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' not in refs

    def test_consumed_sibling_generator_body_is_eager(self) -> None:
        src = (
            'def zed():\n'
            '    return [1, 2]\n'
            '\n'
            '\n'
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    VALUES = list(gen())\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('def zed') < result.new_source.index('class alpha')

    def test_generator_expression_deferred_element_stays_lazy(self) -> None:
        src = (
            'def zed():\n'
            '    return [1]\n'
            '\n'
            '\n'
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    X = ([y for y in gen()] for x in ())\n'
        )
        node = ast.parse(src).body[1]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' not in refs

    def test_generator_expression_first_iterator_is_eager(self) -> None:
        src = (
            'class alpha:\n    def make():\n        return zed()\n\n    X = (x for x in make())\n'
        )
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' in refs

    def test_later_comprehension_iterators_are_consumed(self) -> None:
        src = (
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    PAIRS = [x for a in (1,) for x in gen()]\n'
        )
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' in refs

    def test_preamble_shadowed_consumer_stays_deferred(self) -> None:
        src = 'class alpha:\n    def gen():\n        yield from zed()\n\n    ITEMS = list(gen())\n'
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(
            node,
            module_shadowed=frozenset({'list'}),
        )
        assert 'zed' not in refs

    def test_rebinding_after_call_is_still_builtin(self) -> None:
        src = (
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    ITEMS = list(gen())\n'
            '    list = len\n'
        )
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' in refs

    def test_unconsumed_sibling_generator_stays_lazy(self) -> None:
        src = (
            'class alpha:\n'
            '    def gen():\n'
            '        yield from zed()\n'
            '\n'
            '    MADE = gen()\n'
            '\n'
            '\n'
            'def zed():\n'
            '    return [1]\n'
        )
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.ClassDef)
        refs = analyze_eager_refs(node)
        assert 'zed' not in refs


class TestClassCreationProtocols:
    def test_global_hook_binding_remains_external_read(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls):\n'
            '        global zed\n'
            '        cls.tag = zed()\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        parsed = parse_definitions(src)
        assert 'zed' in parsed.definitions[-1].eager_refs

    def test_hook_local_consumer_stays_deferred_in_hook_class(self) -> None:
        src = (
            'def zed():\n'
            "    raise AssertionError('not consumed')\n"
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls):\n'
            '        list = lambda values: ()\n'
            '        class Inner:\n'
            '            def gen():\n'
            '                yield from zed()\n'
            '            values = list(gen())\n'
            '        cls.inner = Inner\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        FunctionType(compile(src, '<test>', 'exec'), {})()
        parsed = parse_definitions(src)
        assert 'zed' not in parsed.definitions[-1].eager_refs

    def test_hook_parameters_and_locals_are_not_module_reads(self) -> None:
        src = (
            'def cls():\n'
            '    return None\n'
            '\n'
            '\n'
            'def kwargs():\n'
            '    return None\n'
            '\n'
            '\n'
            'def local():\n'
            '    return None\n'
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls, **kwargs):\n'
            '        local = cls\n'
            '        cls.options = kwargs\n'
            '        cls.saved = local\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        parsed = parse_definitions(src)
        assert {'cls', 'kwargs', 'local'}.isdisjoint(parsed.definitions[-1].eager_refs)

    def test_init_subclass_reads_are_eager_at_subclass(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls, **kwargs):\n'
            '        super().__init_subclass__(**kwargs)\n'
            '        cls.tag = zed()\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('def zed') < result.new_source.index('class alpha')

    def test_metaclass_new_reads_are_eager(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class Meta(type):\n'
            '    def __new__(mcs, name, bases, ns):\n'
            "        ns['tag'] = zed()\n"
            '        return super().__new__(mcs, name, bases, ns)\n'
            '\n'
            '\n'
            'class alpha(metaclass=Meta):\n'
            '    pass\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('def zed') < result.new_source.index('class alpha')

    def test_module_shadowed_consumer_stays_deferred_in_hook_class(self) -> None:
        src = (
            'def list(values):\n'
            '    return ()\n'
            '\n'
            '\n'
            'def zed():\n'
            "    raise AssertionError('not consumed')\n"
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls, **kwargs):\n'
            '        class Inner:\n'
            '            def gen():\n'
            '                yield from zed()\n'
            '            values = list(gen())\n'
            '        cls.inner = Inner\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        FunctionType(compile(src, '<test>', 'exec'), {})()
        parsed = parse_definitions(src)
        assert 'zed' not in parsed.definitions[-1].eager_refs

    def test_nonlocal_hook_binding_remains_external_read(self) -> None:
        provider_src = (
            'def outer():\n'
            '    def zed():\n'
            "        return 'z'\n"
            '    class Base:\n'
            '        def __init_subclass__(cls):\n'
            '            nonlocal zed\n'
            '            cls.tag = zed()\n'
            '    return Base\n'
        )
        outer = ast.parse(provider_src).body[0]
        assert isinstance(outer, ast.FunctionDef)
        base = outer.body[1]
        assert isinstance(base, ast.ClassDef)
        trigger = ast.parse('class alpha(Base):\n    pass\n').body[0]
        assert isinstance(trigger, ast.ClassDef)

        refs = analyze_eager_refs(trigger, module_classes={'Base': base})

        assert 'zed' in refs

    def test_rebound_provider_is_invalidated(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class Base:\n'
            '    def __init_subclass__(cls, **kwargs):\n'
            '        cls.tag = zed()\n'
            '\n'
            '\n'
            'Base = dict\n'
            '\n'
            '\n'
            'class alpha(Base):\n'
            '    pass\n'
        )
        parsed = parse_definitions(src)
        assert parsed.definitions[-1].name == 'alpha'
        assert 'zed' not in parsed.definitions[-1].eager_refs

    def test_set_name_reads_are_eager_at_owner(self) -> None:
        src = (
            'def zed():\n'
            "    return 'z'\n"
            '\n'
            '\n'
            'class Descr:\n'
            '    def __set_name__(self, owner, name):\n'
            '        self.tag = zed()\n'
            '\n'
            '\n'
            'class alpha:\n'
            '    field = Descr()\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('def zed') < result.new_source.index('class alpha')


class TestCollectAllNamesFromRoot:
    def test_annotation_names(self) -> None:
        node = _parse_stmt('def f(x: MyType) -> RetType: pass')
        names = _collect_all_names(node)
        assert 'MyType' in names
        assert 'RetType' in names

    def test_assignment_target_not_collected(self) -> None:
        node = _parse_stmt('x = 1')
        assert _collect_all_names(node) == set()

    def test_decorator_names(self) -> None:
        node = _parse_stmt('@wrapper\ndef f(): pass')
        assert 'wrapper' in _collect_all_names(node)

    def test_default_arg_names(self) -> None:
        node = _parse_stmt('def f(x=default_val): pass')
        assert 'default_val' in _collect_all_names(node)

    def test_empty_function(self) -> None:
        node = _parse_stmt('def f(): pass')
        assert _collect_all_names(node) == set()

    def test_excludes_store_context(self) -> None:
        node = _parse_stmt('def f():\n    x = alpha\n    return x')
        names = _collect_all_names(node)
        assert 'alpha' in names
        assert 'x' in names  # x is also loaded in the return

    def test_simple_function_body(self) -> None:
        node = _parse_stmt('def f():\n    return alpha + bravo')
        assert _collect_all_names(node) == {'alpha', 'bravo'}


class TestCollectBodyRefs:
    def _refs(self, method_src: str, members: set[str]) -> set[str]:
        tree = ast.parse(f'class C:\n{textwrap.indent(method_src, "    ")}')
        cls = cast('ast.ClassDef', tree.body[0])
        func = cast('ast.FunctionDef', cls.body[0])
        return collect_body_refs(func, members, 'C')

    def test_alias_and_getattr_ignored(self) -> None:
        src = 'def a(self):\n    x = self\n    x.b()\n    getattr(self, "b")()\n    return self.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_bare_load_counts(self) -> None:
        assert self._refs('def a(self):\n    return self.b', {'a', 'b'}) == {'b'}

    def test_call_reference_counts(self) -> None:
        assert self._refs('def a(self):\n    self.b()', {'a', 'b'}) == {'b'}

    def test_class_attribute_in_instance_method_ignored(self) -> None:
        src = 'def a(self):\n    C.b()\n    return self.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_classmethod_receiver(self) -> None:
        src = '@classmethod\ndef a(cls):\n    cls.b()'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_comprehension_rebinding_excluded(self) -> None:
        src = 'def a(self):\n    x = [self.b for self in ()]\n    return (x, self.c)'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_grandchild_generic_class_type_param_scope_ownership(self) -> None:
        src = (
            'def a(self):\n'
            '    def parent():\n'
            '        class Child[self: self.c]:\n'
            '            value = self.x\n'
            '        return self.b\n'
            '    parent()\n'
            '    return self.d'
        )
        assert self._refs(src, {'a', 'b', 'c', 'd', 'x'}) == {'b', 'c', 'd'}

    def test_grandchild_generic_function_type_param_scope_ownership(self) -> None:
        src = (
            'def a(self):\n'
            '    def parent():\n'
            '        def child[self: self.c]():\n'
            '            return self.x\n'
            '        return self.b\n'
            '    parent()\n'
            '    return self.d'
        )
        assert self._refs(src, {'a', 'b', 'c', 'd', 'x'}) == {'b', 'c', 'd'}

    def test_nested_class_type_param_rebinding_excludes_inner(self) -> None:
        src = 'def a(self):\n    class Inner[self: self.c]:\n        x = self.b\n    return Inner'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_nested_class_type_params_evaluate_in_outer_scope(self) -> None:
        src = 'def a(self):\n    class C[T: self.b]:\n        pass\n    return self.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'b', 'c'}

    def test_nested_default_evaluates_in_outer_scope(self) -> None:
        src = 'def a(self):\n    def inner(self=self.b):\n        return self\n    return inner'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_nested_function_type_param_rebinding_excludes_inner(self) -> None:
        src = (
            'def a(self):\n    def inner[self: self.c]():\n        return self.b\n    return inner'
        )
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_nested_scope_counts(self) -> None:
        src = 'def a(self):\n    def inner():\n        return self.b\n    return inner'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_nested_type_params_evaluate_in_outer_scope(self) -> None:
        src = 'def a(self):\n    def inner[T](x: T) -> T:\n        return self.b\n    return inner'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_nested_walrus_rebinding_excluded(self) -> None:
        src = (
            'def a(self):\n'
            '    def inner():\n'
            '        return (self := object()) and self.b\n'
            '    return self.c'
        )
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_rebinding_scope_excluded(self) -> None:
        src = 'def a(self):\n    def inner(self):\n        return self.b\n    return self.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_renamed_receiver(self) -> None:
        assert self._refs('def a(this):\n    this.b()', {'a', 'b'}) == {'b'}

    def test_scanner_uses_project_child_iterator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_iterator = ast.iter_child_nodes
        calls = 0

        def counting_iterator(node: ast.AST) -> Iterator[ast.AST]:
            nonlocal calls
            calls += 1
            return real_iterator(node)

        source = (
            'def a(self):\n'
            '    def inner(value=self.b):\n'
            '        return value\n'
            '    values = [self.c for item in ()]\n'
            '    return values, self.d\n'
        )

        with monkeypatch.context() as context:
            context.setattr(ast, 'iter_child_nodes', counting_iterator)
            refs = self._refs(source, {'a', 'b', 'c', 'd'})

        assert refs == {'b', 'c', 'd'}
        assert calls == 0

    def test_scope_kind_uses_exact_type_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = ast.parse(
            'def sync(): pass\n'
            'async def async_(): pass\n'
            'class C: pass\n'
            'lambda_ = lambda: None\n'
            'list_ = [item for item in ()]\n'
            'set_ = {item for item in ()}\n'
            'dict_ = {item: item for item in ()}\n'
            'generator_ = (item for item in ())\n'
            'plain = None\n'
        )
        values = [
            statement.value for statement in tree.body[3:] if isinstance(statement, ast.Assign)
        ]
        nodes = [*tree.body[:3], *values]
        expected = [
            'function',
            'function',
            'class',
            'lambda',
            'list_comprehension',
            'set_comprehension',
            'dict_comprehension',
            'generator_expression',
            None,
        ]
        calls = 0
        real_isinstance = isinstance

        def counting_isinstance(
            obj: object,
            class_or_tuple: type[object] | tuple[type[object], ...],
        ) -> bool:
            nonlocal calls
            calls += 1
            return real_isinstance(obj, class_or_tuple)

        monkeypatch.setattr(analysis, 'isinstance', counting_isinstance, raising=False)

        assert [analysis._body_ref_scope_kind(node) for node in nodes] == expected
        assert calls == 0

    @pytest.mark.parametrize(
        ('source', 'expected'),
        _BODY_REF_SCOPE_CASES.values(),
        ids=list(_BODY_REF_SCOPE_CASES),
    )
    def test_scope_matrix(self, source: str, expected: set[str]) -> None:
        assert self._refs(source, {'a', 'b', 'c'}) == expected

    def test_self_reference_excluded(self) -> None:
        src = 'def a(self):\n    self.a()\n    return self.b'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_staticmethod_bare_name_ignored(self) -> None:
        src = '@staticmethod\ndef a():\n    b()\n    return C.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_staticmethod_class_attribute_counts(self) -> None:
        src = '@staticmethod\ndef a():\n    C.b()'
        assert self._refs(src, {'a', 'b'}) == {'b'}

    def test_store_does_not_count(self) -> None:
        src = 'def a(self):\n    self.b = 1\n    return self.c'
        assert self._refs(src, {'a', 'b', 'c'}) == {'c'}

    def test_zero_arg_method_class_attribute_ignored(self) -> None:
        assert self._refs('def a():\n    C.b()', {'a', 'b'}) == set()


class TestCollectNameReferencesFromRoot:
    def test_empty_candidates(self) -> None:
        node = _parse_stmt('def f():\n    return alpha')
        result = collect_name_references(node, set(), exclude='f')
        assert result == set()

    def test_excludes_non_candidates(self) -> None:
        node = _parse_stmt('def f():\n    return unknown + alpha')
        result = collect_name_references(node, {'alpha'}, exclude='f')
        assert result == {'alpha'}

    def test_excludes_self(self) -> None:
        node = _parse_stmt('def f():\n    return f()')
        result = collect_name_references(node, {'f'}, exclude='f')
        assert result == set()

    def test_filters_by_candidates(self) -> None:
        node = _parse_stmt('def f():\n    return alpha + bravo + charlie')
        result = collect_name_references(node, {'alpha', 'bravo'}, exclude='f')
        assert result == {'alpha', 'bravo'}

    def test_only_load_context(self) -> None:
        node = _parse_stmt('def f():\n    alpha = 1\n    return alpha')
        result = collect_name_references(node, {'alpha'}, exclude='f')
        assert result == {'alpha'}  # the return loads alpha


class TestDirectivePattern:
    @pytest.fixture
    def pattern(self) -> re.Pattern[str]:
        return DIRECTIVE_PATTERN

    def test_case_insensitive(self, pattern: re.Pattern[str]) -> None:
        assert pattern.search('# NOQA')
        assert pattern.search('# Type: Ignore')
        assert pattern.search('# PRAGMA: NO COVER')

    @pytest.mark.parametrize(
        'comment',
        [
            '# noqa',
            '# noqa: E501',
            '# type: ignore',
            '# type: ignore[override]',
            '# pyright: ignore[reportGeneralIssues]',
            '# pylint: disable=W0611',
            '# flake8: noqa',
            '# fmt: off',
            '# fmt: on',
            '# fmt: skip',
            '# yapf: disable',
            '# yapf: enable',
            '# pragma: no cover',
        ],
    )
    def test_matches_directives(self, pattern: re.Pattern[str], comment: str) -> None:
        assert pattern.search(comment), f'Expected match for: {comment}'

    @pytest.mark.parametrize(
        'comment',
        [
            '# ruff: noqa',
            '# ruff:noqa',
            '# ruff: noqa: E501',
            '# pyright: strict',
            '# pyright: basic',
            '# pyright: standard',
            '# pyright: off',
        ],
    )
    def test_matches_extended_directives(
        self,
        pattern: re.Pattern[str],
        comment: str,
    ) -> None:
        assert pattern.search(comment)

    def test_rejects_directive_keyword_mid_sentence(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.search('# this has noqa in the middle of a word')

    @pytest.mark.parametrize(
        'comment',
        [
            '# ruff: noquality',
            '# ruff is a tool',
            '# pyright: strictly speaking',
            '# pyright rocks',
            '# about noqa handling',
        ],
    )
    def test_rejects_extended_near_misses(
        self,
        pattern: re.Pattern[str],
        comment: str,
    ) -> None:
        assert not pattern.search(comment)

    def test_rejects_fixme(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.search('# FIXME: broken')

    def test_rejects_note(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.search('# NOTE: important')

    def test_rejects_plain_comment(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.search('# a regular comment')

    def test_rejects_todo(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.search('# TODO: fix this')


class TestEncodingPattern:
    @pytest.fixture
    def pattern(self) -> re.Pattern[str]:
        return ENCODING_PATTERN

    @pytest.mark.parametrize(
        'line',
        [
            '# -*- coding: utf-8 -*-',
            '# coding=utf-8',
            '# vim: set fileencoding=utf-8 :',
        ],
    )
    def test_matches(self, pattern: re.Pattern[str], line: str) -> None:
        assert pattern.match(line), f'Expected match for: {line!r}'

    def test_rejects_plain_comment(self, pattern: re.Pattern[str]) -> None:
        assert not pattern.match('# a regular comment')


class TestExtractPostambleNames:
    def test_annotated_assignment_without_value(self) -> None:
        postamble = 'x: int\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'x'}

    def test_assignment(self) -> None:
        postamble = 'WidgetList = list[int]\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'WidgetList'}

    def test_async_function_body_excluded(self) -> None:
        postamble = 'async def worker():\n    data = await fetch()\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'worker'}

    def test_class_body_attrs_excluded(self) -> None:
        postamble = 'class Config:\n    x = 1\n    y = 2\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'Config'}

    def test_del_binding(self) -> None:
        postamble = 'del CLEANUP\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'CLEANUP'}

    def test_empty_postamble(self) -> None:
        tree = ast.parse('')
        assert extract_postamble_names(tree.body) == set()

    def test_except_handler(self) -> None:
        postamble = 'try:\n    pass\nexcept Exception as e:\n    pass\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'e'}

    def test_for_target(self) -> None:
        postamble = 'for item in items:\n    pass\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'item'}

    def test_function_body_locals_excluded(self) -> None:
        postamble = 'def helper():\n    local_var = 1\n    result = compute()\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'helper'}

    def test_function_def(self) -> None:
        postamble = 'def helper(): pass\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'helper'}

    def test_import(self) -> None:
        postamble = 'from typing import Protocol\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'Protocol'}

    def test_import_as(self) -> None:
        postamble = 'import os.path as osp\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'osp'}

    def test_multiple_names(self) -> None:
        postamble = 'x = 1\ny = 2\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'x', 'y'}

    def test_nested_in_if(self) -> None:
        postamble = 'if True:\n    helper = lambda: None\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'helper'}

    def test_walrus_operator(self) -> None:
        postamble = 'if (result := compute()):\n    pass\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'result'}

    def test_with_binding(self) -> None:
        postamble = 'with open("f") as fp:\n    pass\n'
        tree = ast.parse(postamble)
        assert extract_postamble_names(tree.body) == {'fp'}


class TestFastWalker:
    def test_collect_all_names_matches_walk(self) -> None:
        source = (
            'def foo(x: int) -> str:\n'
            '    return bar(x) + baz\n\n\n'
            'def bar(y):\n'
            '    return str(y)\n\n\n'
            'class Qux:\n'
            '    val: int = foo(1)\n'
        )
        tree = ast.parse(source)
        for node in tree.body:
            walk_result = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            }
            fast_result = _collect_all_names(node)
            assert fast_result == walk_result, f'mismatch at {node}'

    def test_collect_name_references_matches_walk(self) -> None:
        source = (
            'def foo(x: int) -> str:\n'
            '    return bar(x) + baz\n\n\n'
            'def bar(y):\n'
            '    return str(y)\n\n\n'
            'def baz():\n'
            '    return 42\n'
        )
        tree = ast.parse(source)
        names = {'foo', 'bar', 'baz'}
        for node in tree.body:
            assert isinstance(node, ast.FunctionDef)
            walk_result = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name)
                and isinstance(child.ctx, ast.Load)
                and child.id in names
                and child.id != node.name
            }
            fast_result = collect_name_references(node, names, node.name)
            assert fast_result == walk_result, f'mismatch at {node.name}'


class TestFindCycles:
    def test_cycle_branching_scc_all_members(self) -> None:
        dependencies = {
            'alpha': {'bravo', 'charlie'},
            'bravo': {'alpha'},
            'charlie': {'bravo'},
        }
        result = find_cycles(dependencies)
        assert result == [frozenset({'alpha', 'bravo', 'charlie'})]

    def test_cycle_diamond_scc(self) -> None:
        dependencies = {
            'alpha': {'bravo', 'charlie'},
            'bravo': {'delta'},
            'charlie': {'delta'},
            'delta': {'alpha'},
        }
        result = find_cycles(dependencies)
        assert result == [frozenset({'alpha', 'bravo', 'charlie', 'delta'})]

    def test_deep_chain_is_acyclic(self) -> None:
        dependencies = {f'node_{index:04d}': {f'node_{index + 1:04d}'} for index in range(1_499)}
        dependencies['node_1499'] = set[str]()

        assert find_cycles(dependencies) == []

    def test_mixed_cycle_and_non_cycle(self) -> None:
        dependencies = {
            'alpha': {'bravo'},
            'bravo': {'alpha'},  # cycle: alpha <-> bravo
            'charlie': {'alpha'},  # not in cycle
        }
        result = find_cycles(dependencies)
        assert result == [frozenset({'alpha', 'bravo'})]

    def test_no_cycles(self) -> None:
        dependencies = {'alpha': set[str](), 'bravo': {'alpha'}}
        assert find_cycles(dependencies) == []

    def test_non_cycle_nodes_excluded(self) -> None:
        dependencies = {
            'alpha': {'bravo'},
            'bravo': {'alpha'},
            'charlie': set[str](),
            'delta': {'charlie'},
        }
        result = find_cycles(dependencies)
        cycle_members = {member for cycle in result for member in cycle}
        assert 'charlie' not in cycle_members
        assert 'delta' not in cycle_members

    @pytest.mark.parametrize('read_only', [False, True])
    def test_none_node_preserves_all_cycle_members(self, *, read_only: bool) -> None:
        dependencies: dict[Hashable, set[Hashable]] = {
            'alpha': {None},
            None: {'beta'},
            'beta': {'alpha'},
        }
        expected = [frozenset({'alpha', None, 'beta'})]

        if read_only:
            assert find_cycles(MappingProxyType(dependencies)) == expected
        else:
            assert find_cycles(dependencies) == expected

    def test_self_dependency_ignored(self) -> None:
        dependencies = {'alpha': {'alpha'}}
        assert find_cycles(dependencies) == []

    def test_simple_two_node_cycle(self) -> None:
        dependencies = {'alpha': {'bravo'}, 'bravo': {'alpha'}}
        assert find_cycles(dependencies) == [frozenset({'alpha', 'bravo'})]

    def test_three_node_cycle(self) -> None:
        dependencies = {
            'alpha': {'bravo'},
            'bravo': {'charlie'},
            'charlie': {'alpha'},
        }
        assert find_cycles(dependencies) == [frozenset({'alpha', 'bravo', 'charlie'})]

    def test_tuple_nodes_preserve_cycle_members(self) -> None:
        dependencies = {
            ('alpha', 1): {('beta', 2)},
            ('beta', 2): {('alpha', 1)},
        }

        assert find_cycles(dependencies) == [frozenset({('alpha', 1), ('beta', 2)})]

    def test_two_cycles_with_dependency_remain_distinct(self) -> None:
        dependencies = {
            'alpha': {'bravo', 'charlie'},
            'bravo': {'alpha'},
            'charlie': {'delta'},
            'delta': {'charlie'},
        }

        result = find_cycles(dependencies)

        assert set(result) == {
            frozenset({'alpha', 'bravo'}),
            frozenset({'charlie', 'delta'}),
        }


class TestGetBoundNames:
    def test_annotated_assign(self) -> None:
        assert get_bound_names(_parse_stmt('x: int = 1')) == ['x']

    def test_assign(self) -> None:
        assert get_bound_names(_parse_stmt('x = 1')) == ['x']

    def test_async_for(self) -> None:
        node = _parse_stmt('async def f():\n    async for x in items: pass')
        assert isinstance(node, ast.AsyncFunctionDef)
        inner = node.body[0]
        assert get_bound_names(inner) == ['x']

    def test_async_function_def(self) -> None:
        assert get_bound_names(_parse_stmt('async def foo(): pass')) == ['foo']

    def test_async_with(self) -> None:
        node = _parse_stmt('async def f():\n    async with ctx() as c: pass')
        assert isinstance(node, ast.AsyncFunctionDef)
        inner = node.body[0]
        assert get_bound_names(inner) == ['c']

    def test_augmented_assign(self) -> None:
        assert get_bound_names(_parse_stmt('x += 1')) == ['x']

    def test_class_def(self) -> None:
        assert get_bound_names(_parse_stmt('class Foo: pass')) == ['Foo']

    def test_delete(self) -> None:
        assert get_bound_names(_parse_stmt('del x')) == ['x']

    def test_except_handler(self) -> None:
        node = _parse_stmt('try:\n    pass\nexcept ValueError as e:\n    pass')
        assert isinstance(node, ast.Try)
        handler = node.handlers[0]
        assert get_bound_names(handler) == ['e']

    def test_for_loop(self) -> None:
        assert get_bound_names(_parse_stmt('for x in items: pass')) == ['x']

    def test_function_def(self) -> None:
        assert get_bound_names(_parse_stmt('def foo(): pass')) == ['foo']

    def test_global_declaration(self) -> None:
        assert get_bound_names(_parse_stmt('global x')) == []

    def test_import(self) -> None:
        assert get_bound_names(_parse_stmt('import os')) == ['os']

    def test_import_from(self) -> None:
        assert get_bound_names(_parse_stmt('from os import path')) == ['path']

    def test_match_as(self) -> None:
        node = _parse_stmt('match x:\n    case int() as n:\n        pass')
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchAs)
        assert get_bound_names(pattern) == ['n']

    def test_match_as_wildcard(self) -> None:
        node = _parse_stmt('match x:\n    case _:\n        pass')
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchAs)
        assert get_bound_names(pattern) == []

    def test_match_mapping_no_rest(self) -> None:
        node = ast.parse('match x:\n    case {"a": v}:\n        pass\n').body[0]
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchMapping)
        result = get_bound_names(pattern)
        assert result == []

    def test_match_mapping_rest(self) -> None:
        node = ast.parse('match x:\n    case {**rest}:\n        pass\n').body[0]
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchMapping)
        result = get_bound_names(pattern)
        assert result == ['rest']

    def test_match_star(self) -> None:
        node = _parse_stmt('match x:\n    case [first, *rest]:\n        pass')
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchSequence)
        star = pattern.patterns[1]
        assert isinstance(star, ast.MatchStar)
        assert get_bound_names(star) == ['rest']

    def test_match_star_unnamed(self) -> None:
        node = _parse_stmt('match x:\n    case [first, *_]:\n        pass')
        assert isinstance(node, ast.Match)
        pattern = node.cases[0].pattern
        assert isinstance(pattern, ast.MatchSequence)
        star = pattern.patterns[1]
        assert isinstance(star, ast.MatchStar)
        assert get_bound_names(star) == []

    def test_multi_assign(self) -> None:
        result = get_bound_names(_parse_stmt('x = y = 1'))
        assert 'x' in result
        assert 'y' in result

    def test_non_binding(self) -> None:
        assert get_bound_names(_parse_stmt('print("hello")')) == []

    def test_nonlocal_declaration(self) -> None:
        node = _parse_stmt('def f():\n    nonlocal x')
        assert isinstance(node, ast.FunctionDef)
        assert get_bound_names(node.body[0]) == []

    def test_type_alias_bound_name(self) -> None:
        node = ast.parse('type Point = tuple[int, int]\n').body[0]
        result = get_bound_names(node)
        assert result == ['Point']

    def test_walrus_operator(self) -> None:
        node = _parse_stmt('(x := 10)')
        assert isinstance(node, ast.Expr)
        assert get_bound_names(node.value) == ['x']

    def test_with_statement(self) -> None:
        assert get_bound_names(_parse_stmt('with open("f") as fh: pass')) == ['fh']


class TestGetImportNames:
    def test_aliased_import(self) -> None:
        node = _parse_stmt('import numpy as np')
        assert isinstance(node, ast.Import)
        assert _get_import_names(node) == ['np']

    def test_dotted_import(self) -> None:
        node = _parse_stmt('import os.path')
        assert isinstance(node, ast.Import)
        assert _get_import_names(node) == ['os']

    def test_from_import(self) -> None:
        node = _parse_stmt('from os import path')
        assert isinstance(node, ast.ImportFrom)
        assert _get_import_names(node) == ['path']

    def test_from_import_alias(self) -> None:
        node = _parse_stmt('from os import path as p')
        assert isinstance(node, ast.ImportFrom)
        assert _get_import_names(node) == ['p']

    def test_multiple_from_imports(self) -> None:
        node = _parse_stmt('from os import path, getcwd')
        assert isinstance(node, ast.ImportFrom)
        assert _get_import_names(node) == ['path', 'getcwd']

    def test_simple_import(self) -> None:
        node = _parse_stmt('import os')
        assert isinstance(node, ast.Import)
        assert _get_import_names(node) == ['os']


class TestGetWithNames:
    def test_multiple_items(self) -> None:
        node = _parse_stmt('with open("a") as a, open("b") as b: pass')
        assert isinstance(node, ast.With)
        assert _get_with_names(node) == ['a', 'b']

    def test_no_as_clause(self) -> None:
        node = _parse_stmt('with open("f"): pass')
        assert isinstance(node, ast.With)
        assert _get_with_names(node) == []

    def test_simple_with(self) -> None:
        node = _parse_stmt('with open("f") as fh: pass')
        assert isinstance(node, ast.With)
        assert _get_with_names(node) == ['fh']


class TestHasFutureAnnotationsImport:
    def test_combined_import(self) -> None:
        src = 'from __future__ import annotations, generator_stop\n'
        assert has_future_annotations_import(src) is True

    def test_future_import_top_level_only(self) -> None:
        source = 'def nested():\n    pass\n'
        tree = ast.parse(source)
        nested = tree.body[0]
        assert isinstance(nested, ast.FunctionDef)
        nested.body.append(
            ast.ImportFrom(
                module='__future__',
                names=[ast.alias(name='annotations')],
                level=0,
            )
        )

        assert has_future_annotations_import(source, _tree=tree) is False

    def test_no_import(self) -> None:
        src = 'def alpha(): pass\n'
        assert has_future_annotations_import(src) is False

    def test_standalone_import(self) -> None:
        src = 'from __future__ import annotations\n'
        assert has_future_annotations_import(src) is True

    def test_syntax_error_returns_false(self) -> None:
        assert has_future_annotations_import('def alpha(\n') is False

    def test_with_parentheses(self) -> None:
        src = 'from __future__ import (annotations)\n'
        assert has_future_annotations_import(src) is True


class TestIsConstantName:
    @pytest.mark.parametrize(
        'name',
        ['MAX_SIZE', 'A', 'HTTP_404', 'MY_CONST_2', 'AB'],
    )
    def test_matches(self, name: str) -> None:
        assert is_constant_name(name) is True

    @pytest.mark.parametrize(
        'name',
        ['lowercase', 'camelCase', '_LEADING', 'Mixed_Case', 'a', '_'],
    )
    def test_rejects(self, name: str) -> None:
        assert is_constant_name(name) is False


class TestKendallTau:
    def test_identical(self) -> None:
        assert kendall_tau([0, 1, 2, 3]) == 1.0

    def test_reversed(self) -> None:
        assert kendall_tau([3, 2, 1, 0]) == -1.0

    def test_single_swap(self) -> None:
        # [0,1,2,3] with 2,3 swapped -> [0,1,3,2]: 1 discordant pair out of 6
        result = kendall_tau([0, 1, 3, 2])
        assert abs(result - (2 / 3)) < 1e-9

    def test_two_elements_reversed(self) -> None:
        assert kendall_tau([1, 0]) == -1.0

    def test_two_elements_sorted(self) -> None:
        assert kendall_tau([0, 1]) == 1.0


class TestModuleContainsAnnotations:
    @pytest.mark.parametrize(
        'source',
        [
            'value: int = 1\n',
            'class Example:\n    value: int\n',
            'class Outer:\n    class Inner:\n        value: int\n',
            'def outer():\n    local: int = 1\n',
            'def func(value: int, /, other: str = "") -> bytes:\n    pass\n',
            'def func(*values: int, flag: bool, **kwargs: str):\n    pass\n',
            'async def func(value: int):\n    pass\n',
        ],
    )
    def test_detects_annotations_at_any_depth(self, source: str) -> None:
        assert module_contains_annotations(ast.parse(source)) is True

    @pytest.mark.parametrize(
        'source',
        [
            '',
            'value = 1\n',
            'def func(value=1):\n    return value\n',
            'class Example:\n    value = 1\n',
        ],
    )
    def test_rejects_annotation_free_modules(self, source: str) -> None:
        assert module_contains_annotations(ast.parse(source)) is False

    def test_rejects_pep_695_type_alias_only(self) -> None:
        assert module_contains_annotations(ast.parse('type Alias = Later\n')) is False

    def test_rejects_type_parameter_default_only(self) -> None:
        source = 'def func[T = Later]():\n    pass\n'

        assert module_contains_annotations(ast.parse(source)) is False


class TestNamesFromTarget:
    def test_list_unpack(self) -> None:
        node = _parse_stmt('[a, b] = [1, 2]')
        assert isinstance(node, ast.Assign)
        assert _names_from_target(node.targets[0]) == ['a', 'b']

    def test_nested_unpack(self) -> None:
        node = _parse_stmt('a, (b, c) = 1, (2, 3)')
        assert isinstance(node, ast.Assign)
        assert _names_from_target(node.targets[0]) == ['a', 'b', 'c']

    def test_non_name_target(self) -> None:
        node = _parse_stmt('obj.attr = 1')
        assert isinstance(node, ast.Assign)
        assert _names_from_target(node.targets[0]) == []

    def test_simple_name(self) -> None:
        node = _parse_stmt('x = 1')
        assert isinstance(node, ast.Assign)
        assert _names_from_target(node.targets[0]) == ['x']

    def test_starred(self) -> None:
        node = _parse_stmt('first, *rest = items')
        assert isinstance(node, ast.Assign)
        result = _names_from_target(node.targets[0])
        assert 'first' in result
        assert 'rest' in result

    def test_tuple_unpack(self) -> None:
        node = _parse_stmt('a, b = 1, 2')
        assert isinstance(node, ast.Assign)
        assert _names_from_target(node.targets[0]) == ['a', 'b']


class TestSafeTrailingPattern:
    @pytest.fixture
    def pattern(self) -> re.Pattern[str]:
        return SAFE_TRAILING_PATTERN

    @pytest.mark.parametrize(
        'line',
        [
            '',  # empty
            '   ',  # whitespace only
            '# a comment',
            '  # indented comment',
            '\t# tabbed comment',
        ],
    )
    def test_matches_safe(self, pattern: re.Pattern[str], line: str) -> None:
        assert pattern.match(line), f'Expected match for: {line!r}'

    @pytest.mark.parametrize(
        'line',
        [
            'registry = {}',
            'x = 1',
            'print("hello")',
        ],
    )
    def test_rejects_code(self, pattern: re.Pattern[str], line: str) -> None:
        assert not pattern.match(line), f'Expected no match for: {line!r}'


class TestTypeParamDefaults:
    def test_class_type_param_default_orders_dependency(self) -> None:
        src = 'class alpha[T = zed]:\n    pass\n\n\nclass zed:\n    pass\n'
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('class zed') < result.new_source.index('class alpha')

    def test_method_type_param_default_orders_dependency(self) -> None:
        src = (
            'class alpha:\n'
            '    def m[T = zed](self) -> None:\n'
            '        return None\n'
            '\n'
            '\n'
            'class zed:\n'
            '    pass\n'
        )
        options = EngineOptions(policy=SortPolicy())
        result = sort_file(src, options=options)
        assert result.new_source.index('class zed') < result.new_source.index('class alpha')


def _parse_stmt(code: str) -> ast.AST:
    return ast.parse(code).body[0]
