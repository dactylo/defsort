"""AST analysis primitives for names, dependencies, and cycles."""

import ast
import re
from collections.abc import Hashable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, cast

BLOCKING_CODE_PREVIEW_LEN = 60

_BodyRefScopeKind = Literal[
    'class',
    'dict_comprehension',
    'function',
    'generator_expression',
    'lambda',
    'list_comprehension',
    'set_comprehension',
]

_BODY_REF_SCOPE_KINDS: dict[type[ast.AST], _BodyRefScopeKind] = {
    ast.AsyncFunctionDef: 'function',
    ast.ClassDef: 'class',
    ast.DictComp: 'dict_comprehension',
    ast.FunctionDef: 'function',
    ast.GeneratorExp: 'generator_expression',
    ast.Lambda: 'lambda',
    ast.ListComp: 'list_comprehension',
    ast.SetComp: 'set_comprehension',
}

DIRECTIVE_PATTERN = re.compile(
    r'#\s*(?:'
    r'noqa\b|type:\s*ignore|pyright:\s*ignore|pylint:|flake8:|'
    r'ruff:\s*noqa\b|pyright:\s*(?:strict|basic|standard|off)\b|'
    r'fmt:\s*(?:off|on|skip)|yapf:\s*(?:disable|enable)|'
    r'pragma:\s*no\s*cover'
    r')',
    re.IGNORECASE,
)

_EAGER_GENERATOR_CONSUMERS = frozenset(
    {
        'all',
        'any',
        'dict',
        'frozenset',
        'list',
        'max',
        'min',
        'next',
        'set',
        'sorted',
        'sum',
        'tuple',
    }
)

ENCODING_LINE_LIMIT = 2

ENCODING_PATTERN = re.compile(r'^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)')

MIN_DEFS_FOR_DETECTION = 2

MIN_METHODS_FOR_SORT = 2

_MIN_PAIRS_FOR_TAU = 2

SAFE_TRAILING_PATTERN = re.compile(r'^(\s*#.*|\s*)$')

_UPPER_SNAKE_CASE_PATTERN = re.compile(r'^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$')


@dataclass(frozen=True, slots=True)
class _BodyRefScopeParts:
    """Evaluation and binding regions for one body-reference scope."""

    parameters: tuple[ast.AST, ...]
    outer: tuple[ast.AST, ...]
    inner: tuple[ast.AST, ...]
    binding_roots: tuple[ast.AST, ...]
    captures_comprehension_walruses: bool
    is_comprehension: bool


@dataclass(slots=True)
class _EagerRefCollector:
    """Collect eager refs within one active class-sibling context."""

    sibling_functions: dict[str, tuple[ast.FunctionDef, ...]] | None = None
    expanded: set[int] | None = None
    sibling_bound: set[str] | None = None
    shadowed_names: frozenset[str] = frozenset()

    def _collect_class(self, current: ast.ClassDef) -> set[str]:
        """Collect class outer parts and suite reads in the right contexts."""
        outer_roots: list[ast.AST] = [
            *current.decorator_list,
            *current.bases,
            *(keyword.value for keyword in current.keywords),
        ]
        refs = {name for root in outer_roots for name in self._collect(root)}
        sibling_lists: dict[str, list[ast.FunctionDef]] = {}
        for statement in current.body:
            if isinstance(statement, ast.FunctionDef):
                sibling_lists.setdefault(statement.name, []).append(statement)
        class_collector = _EagerRefCollector(
            sibling_functions={
                name: tuple(functions) for name, functions in sibling_lists.items()
            },
            expanded=set(),
            sibling_bound=set(),
            shadowed_names=self.shadowed_names,
        )
        for statement in current.body:
            refs.update(class_collector._collect(statement))
            if class_collector.sibling_bound is not None:
                class_collector.sibling_bound.update(get_bound_names(statement))
        return refs

    def _expand_sibling(self, name: str, *, consume_generators: bool) -> set[str]:
        """Expand one directly referenced sibling function body, once."""
        if self.sibling_functions is None or self.expanded is None:
            return set()
        refs: set[str] = set()
        for function in self.sibling_functions.get(name, ()):
            function_id = id(function)
            if function_id in self.expanded:
                continue
            if _is_generator_function(function) and not consume_generators:
                continue
            self.expanded.add(function_id)
            function_collector = _EagerRefCollector()
            for statement in function.body:
                refs.update(function_collector._collect(statement))
        return refs

    def _collect_consumed_iterators(self, current: ast.AST) -> set[str]:
        """Collect sibling generators consumed directly by iteration syntax."""
        if (
            isinstance(current, ast.For)
            and isinstance(current.iter, ast.Call)
            and isinstance(current.iter.func, ast.Name)
        ):
            return self._expand_sibling(current.iter.func.id, consume_generators=True)
        if not isinstance(current, (ast.DictComp, ast.ListComp, ast.SetComp)):
            return set()
        refs: set[str] = set()
        for generator in current.generators:
            if isinstance(generator.iter, ast.Call) and isinstance(generator.iter.func, ast.Name):
                refs.update(self._expand_sibling(generator.iter.func.id, consume_generators=True))
        return refs

    def _collect_expanded_call(self, current: ast.AST) -> set[str]:
        """Collect one level of directly called or consumed sibling bodies."""
        if not isinstance(current, ast.Call) or not isinstance(current.func, ast.Name):
            return set()
        refs = self._expand_sibling(current.func.id, consume_generators=False)
        if (
            self.sibling_functions is not None
            and current.func.id in _EAGER_GENERATOR_CONSUMERS
            and current.func.id not in (self.sibling_bound or set())
            and current.func.id not in self.shadowed_names
        ):
            for argument in current.args:
                if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name):
                    refs.update(self._expand_sibling(argument.func.id, consume_generators=True))
        return refs

    def _collect(self, current: ast.AST) -> set[str]:
        """Collect eager references from an AST subtree."""
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots: list[ast.AST] = [*current.decorator_list, *current.args.defaults]
            roots.extend(default for default in current.args.kw_defaults if default is not None)
            refs = {name for root in roots for name in self._collect(root)}
            for decorator in current.decorator_list:
                if isinstance(decorator, ast.Name):
                    refs.update(self._expand_sibling(decorator.id, consume_generators=False))
            return refs
        if isinstance(current, ast.ClassDef):
            return self._collect_class(current)
        if isinstance(current, ast.AnnAssign):
            roots = [current.target]
            if current.value is not None:
                roots.append(current.value)
            return {name for root in roots for name in self._collect(root)}
        if isinstance(current, ast.TypeAlias):
            return set()
        if isinstance(current, ast.Name):
            return {current.id} if isinstance(current.ctx, ast.Load) else set()

        children = (
            (current.generators[0].iter,)
            if isinstance(current, ast.GeneratorExp)
            else iter_child_nodes(current)
        )
        refs = {name for child in children for name in self._collect(child)}
        refs.update(self._collect_consumed_iterators(current))
        refs.update(self._collect_expanded_call(current))
        return refs


@dataclass
class _TarjanFrame[NodeT: Hashable]:
    """Explicit DFS frame for Tarjan's algorithm."""

    node: NodeT
    successors: Iterator[NodeT]


def _body_ref_arguments(args: ast.arguments) -> tuple[ast.arg, ...]:
    """Return parameters in binding order for a function-like scope."""
    parameters = [*args.posonlyargs, *args.args]
    if args.vararg is not None:
        parameters.append(args.vararg)
    parameters.extend(args.kwonlyargs)
    if args.kwarg is not None:
        parameters.append(args.kwarg)
    return tuple(parameters)


def _body_ref_receiver(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: str,
) -> str | None:
    """Return the literal receiver name used for body references."""
    if any(
        isinstance(decorator, ast.Name) and decorator.id == 'staticmethod'
        for decorator in func.decorator_list
    ):
        return class_name
    positional = (*func.args.posonlyargs, *func.args.args)
    if not positional:
        return None
    return positional[0].arg


def _body_ref_scope_kind(node: ast.AST) -> _BodyRefScopeKind | None:
    """Classify body-reference scopes through the single boundary authority."""
    return _BODY_REF_SCOPE_KINDS.get(type(node))


def _body_ref_type_param_expressions(
    type_params: Iterable[ast.AST],
) -> tuple[ast.AST, ...]:
    """Return only the outer-evaluated expressions of type parameters."""
    return tuple(child for type_param in type_params for child in ast.iter_child_nodes(type_param))


def _body_ref_type_param_name(node: ast.AST) -> str | None:
    """Return a type-parameter binding name when the node has one."""
    if isinstance(node, (ast.ParamSpec, ast.TypeVar, ast.TypeVarTuple)):
        return node.name
    return None


def _body_ref_type_params(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[ast.AST, ...]:
    """Return type-parameter nodes from a definition."""
    return tuple(node.type_params)


def _collect_eager_refs(
    node: ast.AST,
    *,
    module_shadowed: frozenset[str] = frozenset(),
) -> set[str]:
    """Collect eager reads, including bounded class-local call expansion."""
    return _EagerRefCollector(shadowed_names=module_shadowed)._collect(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        node
    )


def _count_inversions(values: list[int]) -> int:
    """Count inversions using merge sort. O(n log n)."""
    work = values.copy()
    scratch = [0] * len(work)

    def _sort_count(lo: int, hi: int) -> int:
        if hi - lo <= 1:
            return 0
        mid = (lo + hi) // 2
        inversions = _sort_count(lo, mid) + _sort_count(mid, hi)
        i, j, k = lo, mid, lo
        while i < mid and j < hi:
            if work[i] <= work[j]:
                scratch[k] = work[i]
                i += 1
            else:
                scratch[k] = work[j]
                inversions += mid - i
                j += 1
            k += 1
        while i < mid:
            scratch[k] = work[i]
            i += 1
            k += 1
        while j < hi:
            scratch[k] = work[j]
            j += 1
            k += 1
        work[lo:hi] = scratch[lo:hi]
        return inversions

    return _sort_count(0, len(work))


def _get_import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Get names bound by an import statement."""
    if isinstance(node, ast.Import):
        return [a.asname or a.name.split('.')[0] for a in node.names]
    return [a.asname or a.name for a in node.names]


def _iter_function_annotation_nodes(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[ast.expr]:
    """Yield every parameter and return annotation on a function."""
    for argument in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs):
        if argument.annotation is not None:
            yield argument.annotation
    if func.args.vararg is not None and func.args.vararg.annotation is not None:
        yield func.args.vararg.annotation
    if func.args.kwarg is not None and func.args.kwarg.annotation is not None:
        yield func.args.kwarg.annotation
    if func.returns is not None:
        yield func.returns


def _body_ref_scope_parts(
    scope: ast.AST,
    kind: _BodyRefScopeKind,
) -> _BodyRefScopeParts:
    """Split a classified scope into outer, inner, and binding regions."""
    if kind == 'function':
        func = cast('ast.FunctionDef | ast.AsyncFunctionDef', scope)
        defaults = (*func.args.defaults, *(d for d in func.args.kw_defaults if d is not None))
        type_params = _body_ref_type_params(func)
        outer = (
            *func.decorator_list,
            *defaults,
            *_iter_function_annotation_nodes(func),
            *_body_ref_type_param_expressions(type_params),
        )
        function_inner = tuple(func.body)
        return _BodyRefScopeParts(
            parameters=(*_body_ref_arguments(func.args), *type_params),
            outer=outer,
            inner=function_inner,
            binding_roots=function_inner,
            captures_comprehension_walruses=True,
            is_comprehension=False,
        )
    if kind == 'lambda':
        lambda_node = cast('ast.Lambda', scope)
        defaults = (
            *lambda_node.args.defaults,
            *(d for d in lambda_node.args.kw_defaults if d is not None),
        )
        lambda_inner = (lambda_node.body,)
        return _BodyRefScopeParts(
            parameters=_body_ref_arguments(lambda_node.args),
            outer=defaults,
            inner=lambda_inner,
            binding_roots=lambda_inner,
            captures_comprehension_walruses=True,
            is_comprehension=False,
        )
    if kind == 'class':
        cls = cast('ast.ClassDef', scope)
        type_params = _body_ref_type_params(cls)
        outer = (
            *cls.decorator_list,
            *cls.bases,
            *(keyword.value for keyword in cls.keywords),
            *_body_ref_type_param_expressions(type_params),
        )
        class_inner = tuple(cls.body)
        return _BodyRefScopeParts(
            parameters=type_params,
            outer=outer,
            inner=class_inner,
            binding_roots=class_inner,
            captures_comprehension_walruses=False,
            is_comprehension=False,
        )

    comprehension = cast(
        'ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp',
        scope,
    )
    if not comprehension.generators:
        return _BodyRefScopeParts(
            parameters=(),
            outer=(),
            inner=(),
            binding_roots=(),
            captures_comprehension_walruses=False,
            is_comprehension=True,
        )
    first, *remaining = comprehension.generators
    comprehension_inner: list[ast.AST] = [first.target, *first.ifs]
    for generator in remaining:
        comprehension_inner.extend((generator.iter, generator.target, *generator.ifs))
    if kind == 'dict_comprehension':
        dictionary = cast('ast.DictComp', comprehension)
        comprehension_inner.extend((dictionary.key, dictionary.value))
    else:
        comprehension_inner.append(
            cast(
                'ast.ListComp | ast.SetComp | ast.GeneratorExp',
                comprehension,
            ).elt
        )
    return _BodyRefScopeParts(
        parameters=(),
        outer=(first.iter,),
        inner=tuple(comprehension_inner),
        binding_roots=tuple(generator.target for generator in comprehension.generators),
        captures_comprehension_walruses=False,
        is_comprehension=True,
    )


def _names_from_target(target: ast.AST) -> list[str]:
    """Recursively extract names from assignment targets."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _names_from_target(elt)]
    if isinstance(target, ast.Starred):
        return _names_from_target(target.value)
    return []


def _get_with_names(node: ast.With | ast.AsyncWith) -> list[str]:
    """Get names bound by a with statement."""
    names: list[str] = []
    for item in node.items:
        if item.optional_vars:
            names.extend(_names_from_target(item.optional_vars))
    return names


def _pop_scc[NodeT: Hashable](
    stack: list[NodeT], on_stack: set[NodeT], root: NodeT
) -> frozenset[NodeT]:
    """Pop and return an SCC rooted at *root* from Tarjan's stack."""
    scc: set[NodeT] = set()
    while True:
        w = stack.pop()
        on_stack.remove(w)
        scc.add(w)
        if w == root:
            break
    return frozenset(scc)


def _tarjan_sccs[NodeT: Hashable](
    deps: dict[NodeT, set[NodeT]],
) -> list[frozenset[NodeT]]:
    """Return all strongly connected components using Tarjan's algorithm."""
    index_counter: int = 0
    stack: list[NodeT] = []
    on_stack: set[NodeT] = set()
    indices: dict[NodeT, int] = {}
    lowlinks: dict[NodeT, int] = {}
    result: list[frozenset[NodeT]] = []
    frames: list[_TarjanFrame[NodeT]] = []

    def push(node: NodeT) -> None:
        nonlocal index_counter
        indices[node] = index_counter
        lowlinks[node] = index_counter
        index_counter += 1
        stack.append(node)
        on_stack.add(node)
        successors = (successor for successor in deps.get(node, set()) if successor in deps)
        frames.append(_TarjanFrame(node, successors))

    for root in deps:
        if root in indices:
            continue
        push(root)
        while frames:
            frame = frames[-1]
            try:
                successor = next(frame.successors)
            except StopIteration:
                frames.pop()
                if frames:
                    parent = frames[-1].node
                    lowlinks[parent] = min(lowlinks[parent], lowlinks[frame.node])
                if lowlinks[frame.node] == indices[frame.node]:
                    result.append(_pop_scc(stack, on_stack, frame.node))
                continue
            if successor not in indices:
                push(successor)
            elif successor in on_stack:
                lowlinks[frame.node] = min(lowlinks[frame.node], indices[successor])

    return result


def aggregate_score(section_ranks: Iterable[list[int]]) -> float | None:
    """Aggregate Kendall tau scores without cross-section comparisons.

    Args:
        section_ranks: Rank permutations for independently comparable sections.

    Returns:
        The pair-weighted score, or `None` when no section has a comparable
        pair.
    """
    discordant = 0
    total_pairs = 0
    for ranks in section_ranks:
        pairs = len(ranks) * (len(ranks) - 1) // 2
        if pairs == 0:
            continue
        discordant += _count_inversions(ranks)
        total_pairs += pairs
    if total_pairs == 0:
        return None
    return (total_pairs - 2 * discordant) / total_pairs


def compute_ranks(actual: Iterable[int], target: Iterable[int]) -> list[int]:
    """Build rank array for kendall_tau comparison.

    Maps actual ordering into ranks relative to target ordering.
    Each element in actual gets assigned its position in target.

    Args:
        actual: The actual order of definition indices.
        target: The target order of definition indices.

    Returns:
        The actual indices expressed as positions in the target order.
    """
    pos = {idx: i for i, idx in enumerate(target)}
    return [pos[idx] for idx in actual]


def decorated_start_line(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    """Get the starting line of a definition, including decorators.

    Args:
        node: A class, function, or async function AST node.

    Returns:
        The first decorator line, or the definition line when undecorated.
    """
    if node.decorator_list:
        return min(node.lineno, *(d.lineno for d in node.decorator_list))
    return node.lineno


def find_cycles[NodeT: Hashable](
    dependencies: Mapping[NodeT, set[NodeT]],
) -> list[frozenset[NodeT]]:
    """Find the distinct dependency cycles in a directed graph.

    Uses Tarjan's algorithm to find Strongly Connected Components.
    Any SCC with more than one member indicates a cycle. Self-dependencies are
    stripped before analysis.

    Args:
        dependencies: Adjacency map from each node to its dependencies.

    Returns:
        The multi-node strongly connected components as distinct sets.
    """
    deps = {node: successors - {node} for node, successors in dependencies.items()}
    return [scc for scc in _tarjan_sccs(deps) if len(scc) > 1]


def get_bound_names(node: ast.AST) -> list[str]:  # noqa: C901, PLR0911
    """Get names bound or deleted directly by a single AST node.

    Args:
        node: AST node whose binding or deletion targets should be collected.

    Returns:
        Names bound or deleted directly by the node.
    """
    if isinstance(node, (ast.Assign, ast.Delete)):
        return [n for t in node.targets for n in _names_from_target(t)]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        return _names_from_target(node.target)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _get_import_names(node)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _get_with_names(node)
    if isinstance(node, ast.NamedExpr):
        return [node.target.id]
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchAs) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchStar) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    if isinstance(node, ast.TypeAlias):
        return [node.name.id]
    return []


def get_node_end_line(node: ast.stmt) -> int:
    """Get the last line of an AST statement node.

    Args:
        node: Statement whose final line is needed.

    Returns:
        The inclusive final source line.
    """
    return node.end_lineno or node.lineno


def is_constant_name(name: str) -> bool:
    """Check if a name follows UPPER_SNAKE_CASE convention.

    Args:
        name: Identifier to check.

    Returns:
        `True` when the name follows the convention.
    """
    return bool(_UPPER_SNAKE_CASE_PATTERN.match(name))


def is_overload_decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function or method has the `@overload` decorator.

    Args:
        node: Function or method definition to inspect.

    Returns:
        `True` when an overload decorator is present.
    """
    return any(
        (isinstance(dec, ast.Name) and dec.id == 'overload')
        or (isinstance(dec, ast.Attribute) and dec.attr == 'overload')
        for dec in node.decorator_list
    )


def iter_child_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield direct child AST nodes by inspecting node.__dict__.

    This is CPython-specific traversal used by defsort to match the
    implementation behavior of a file's AST as Python sees it.  See
    docs/platform.md for rationale.

    Args:
        node: AST node whose direct children should be visited.

    Yields:
        Each direct child AST node.
    """
    for value in node.__dict__.values():
        if isinstance(value, ast.AST):
            yield value
        elif isinstance(value, list):
            for item in cast('list[object]', value):
                if isinstance(item, ast.AST):
                    yield item


def _collect_all_names(node: ast.AST) -> set[str]:
    """Collect all name references in Load context from an AST subtree."""
    result: set[str] = set()
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Name):
            if isinstance(cur.ctx, ast.Load):
                result.add(cur.id)
            continue  # Name nodes have no AST children worth visiting
        stack.extend(iter_child_nodes(cur))
    return result


def _collect_func_annotation_refs(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    """Collect all annotation name references from a function signature."""
    return {
        name
        for annotation in _iter_function_annotation_nodes(func)
        for name in _collect_all_names(annotation)
    }


def _collect_class_suite_annotation_refs(node: ast.AST) -> set[str]:
    """Collect annotations reachable in an executable class suite."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _collect_func_annotation_refs(node)
    if isinstance(node, ast.ClassDef):
        return {
            name
            for statement in node.body
            for name in _collect_class_suite_annotation_refs(statement)
        }
    if isinstance(node, ast.AnnAssign):
        return _collect_all_names(node.annotation)
    if isinstance(node, ast.TypeAlias):
        return set()
    return {
        name
        for child in iter_child_nodes(node)
        for name in _collect_class_suite_annotation_refs(child)
    }


def _collect_class_annotation_refs(cls: ast.ClassDef) -> set[str]:
    """Collect annotation name references from an executable class suite."""
    return {
        name for statement in cls.body for name in _collect_class_suite_annotation_refs(statement)
    }


def _is_generator_function(func: ast.FunctionDef) -> bool:
    """Return whether `func` yields in its own function scope."""
    stack: list[ast.AST] = list(func.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(_body_ref_scope_parts(current, 'function').outer)
            continue
        if isinstance(current, ast.ClassDef):
            stack.extend(_body_ref_scope_parts(current, 'class').outer)
            continue
        if isinstance(current, ast.Lambda):
            stack.extend(_body_ref_scope_parts(current, 'lambda').outer)
            continue
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        stack.extend(iter_child_nodes(current))
    return False


def _iter_body_ref_binding_surface(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield one scope's complete own binding surface."""
    root_kind = _body_ref_scope_kind(scope)
    if root_kind is None:
        return
    root_parts = _body_ref_scope_parts(scope, root_kind)
    yield from root_parts.parameters

    stack = [(node, False) for node in root_parts.binding_roots]
    while stack:
        node, walrus_only = stack.pop()
        nested_kind = _body_ref_scope_kind(node)
        if nested_kind is not None:
            nested_parts = _body_ref_scope_parts(node, nested_kind)
            if walrus_only:
                if nested_parts.is_comprehension:
                    stack.extend((child, True) for child in nested_parts.outer)
                    stack.extend((child, True) for child in nested_parts.inner)
                continue

            yield node
            stack.extend((child, False) for child in nested_parts.outer)
            if nested_parts.is_comprehension and root_parts.captures_comprehension_walruses:
                stack.extend((child, True) for child in nested_parts.inner)
            continue

        if not walrus_only or isinstance(node, ast.NamedExpr):
            yield node
        stack.extend((child, walrus_only) for child in iter_child_nodes(node))


def _body_ref_scope_bindings(
    scope: ast.AST,
) -> tuple[set[str], set[str], set[str]]:
    """Return local, global, and nonlocal names for one binding surface."""
    surface = tuple(_iter_body_ref_binding_surface(scope))
    global_names: set[str] = set()
    local_names: set[str] = set()
    nonlocal_names: set[str] = set()
    for node in surface:
        if isinstance(node, ast.Global):
            global_names.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            nonlocal_names.update(node.names)
        elif isinstance(node, ast.arg):
            local_names.add(node.arg)
        else:
            type_param_name = _body_ref_type_param_name(node)
            if type_param_name is not None:
                local_names.add(type_param_name)
            if isinstance(node, (ast.Name, ast.Tuple, ast.List, ast.Starred)) and isinstance(
                node.ctx, ast.Store
            ):
                local_names.update(_names_from_target(node))
            local_names.update(get_bound_names(node))
    local_names.difference_update(global_names | nonlocal_names)
    return local_names, global_names, nonlocal_names


def _body_ref_scope_keeps_receiver(
    scope: ast.AST,
    receiver: str,
) -> bool:
    """Decide receiver ownership only after materializing the whole surface."""
    local_names, global_names, nonlocal_names = _body_ref_scope_bindings(scope)
    if receiver in global_names:
        return False
    if receiver in nonlocal_names:
        return True
    return receiver not in local_names


def _class_hook_body_refs(
    cls: ast.ClassDef | None,
    hook_name: str,
    *,
    module_shadowed: frozenset[str],
) -> set[str]:
    """Collect eager reads from one directly defined class hook body."""
    if cls is None:
        return set()
    hook: ast.FunctionDef | None = None
    for statement in cls.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and statement.value is None
            and isinstance(statement.target, ast.Name)
            and statement.target.id == hook_name
        ):
            continue
        if hook_name not in get_bound_names(statement):
            continue
        hook = (
            statement
            if isinstance(statement, ast.FunctionDef) and statement.name == hook_name
            else None
        )
    if hook is None:
        return set()
    local_names, _, nonlocal_names = _body_ref_scope_bindings(hook)
    collector = _EagerRefCollector(shadowed_names=module_shadowed | local_names | nonlocal_names)
    refs = {
        name
        for statement in hook.body
        for name in collector._collect(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
            statement
        )
    }
    return refs - local_names


def _class_protocol_refs(
    cls: ast.ClassDef,
    module_classes: Mapping[str, ast.ClassDef],
    *,
    module_shadowed: frozenset[str],
) -> set[str]:
    """Collect eager reads from supported local class-creation protocols."""
    refs: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            refs.update(
                _class_hook_body_refs(
                    module_classes.get(base.id),
                    '__init_subclass__',
                    module_shadowed=module_shadowed,
                )
            )
    for keyword in cls.keywords:
        if keyword.arg == 'metaclass' and isinstance(keyword.value, ast.Name):
            refs.update(
                _class_hook_body_refs(
                    module_classes.get(keyword.value.id),
                    '__new__',
                    module_shadowed=module_shadowed,
                )
            )
    for statement in cls.body:
        value: ast.expr | None = None
        if isinstance(statement, (ast.AnnAssign, ast.Assign)):
            value = statement.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in module_classes
        ):
            refs.update(
                _class_hook_body_refs(
                    module_classes[value.func.id],
                    '__set_name__',
                    module_shadowed=module_shadowed,
                )
            )
    return refs


def _scan_body_ref_nodes(
    nodes: Iterable[ast.AST],
    receiver: str,
    member_names: set[str],
) -> set[str]:
    """Scan receiver loads while respecting each nested scope's surface."""
    references: set[str] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        scope_kind = _body_ref_scope_kind(node)
        if scope_kind is not None:
            parts = _body_ref_scope_parts(node, scope_kind)
            stack.extend(parts.outer)
            if _body_ref_scope_keeps_receiver(node, receiver):
                stack.extend(parts.inner)
            continue

        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == receiver
            and node.attr in member_names
        ):
            references.add(node.attr)
        stack.extend(iter_child_nodes(node))
    return references


def analyze_annotation_refs(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    """Analyze annotation-only name references of a definition.

    Collects ALL name references from annotations without filtering against a
    candidate set. Self-referential annotations are included because they are
    genuine forward references (e.g., a class using its own name in method
    annotations).

    Args:
        node: The definition AST node.

    Returns:
        Names referenced by the definition's annotations.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _collect_func_annotation_refs(node)
    return _collect_class_annotation_refs(node)


def analyze_eager_refs(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module_classes: Mapping[str, ast.ClassDef] | None = None,
    module_shadowed: frozenset[str] = frozenset(),
) -> set[str]:
    """Collect direct name reads from definition-time expressions.

    Args:
        node: Definition whose eager expressions should be scanned.
        module_classes: Earlier local classes visible at the definition.
        module_shadowed: Names already bound in the containing module.

    Returns:
        Names loaded while Python creates the definition.
    """
    refs = _collect_eager_refs(node, module_shadowed=module_shadowed)
    if isinstance(node, ast.ClassDef) and module_classes:
        refs |= _class_protocol_refs(
            node,
            module_classes,
            module_shadowed=module_shadowed,
        )
    return refs


def collect_body_refs(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    member_names: set[str],
    class_name: str,
) -> set[str]:
    """Collect literal sibling-member loads through a method's receiver.

    Args:
        func: Method whose body should be scanned.
        member_names: Names defined as members of the containing class.
        class_name: Name of the containing class for staticmethod access.

    Returns:
        Referenced sibling member names, excluding the method itself.
    """
    receiver = _body_ref_receiver(func, class_name)
    if receiver is None:
        return set()
    return _scan_body_ref_nodes(func.body, receiver, member_names - {func.name})


def collect_const_def_lines(
    tree: ast.Module,
    candidates: set[str],
) -> dict[str, int]:
    """Map each candidate constant to the line where it is assigned.

    Args:
        tree: The full source AST.
        candidates: UPPER_SNAKE_CASE names to look for.

    Returns:
        Candidate names mapped to their assignment lines.
    """
    const_def_lines: dict[str, int] = {}
    stack: list[ast.AST] = [tree]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Assign) and len(cur.targets) == 1:
            target = cur.targets[0]
            if isinstance(target, ast.Name) and target.id in candidates:
                const_def_lines[target.id] = cur.lineno
        stack.extend(iter_child_nodes(cur))
    return const_def_lines


def collect_name_references(node: ast.AST, names: set[str], exclude: str) -> set[str]:
    """Collect name references filtered by candidate set, excluding self.

    Args:
        node: AST subtree to search.
        names: Candidate names to look for.
        exclude: Name to exclude (typically the definition itself).

    Returns:
        Referenced candidate names, excluding `exclude`.
    """
    result: set[str] = set()
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Name):
            if isinstance(cur.ctx, ast.Load) and cur.id in names and cur.id != exclude:
                result.add(cur.id)
            continue
        stack.extend(iter_child_nodes(cur))
    return result


def _analyze_type_param_deps(
    type_params: Iterable[ast.AST],
    names: set[str],
    exclude: str,
) -> set[str]:
    """Collect name references from PEP 695/696 type parameter bounds and defaults."""
    deps: set[str] = set()
    for tp in type_params:
        if isinstance(tp, ast.TypeVar) and tp.bound is not None:
            deps.update(collect_name_references(tp.bound, names, exclude))
        if (
            isinstance(tp, (ast.ParamSpec, ast.TypeVar, ast.TypeVarTuple))
            and tp.default_value is not None
        ):
            deps.update(collect_name_references(tp.default_value, names, exclude))
    return deps


def _analyze_signature_deps(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    names: set[str],
    exclude: str,
) -> set[str]:
    """Collect references from a function's signature only."""
    nodes: list[ast.AST] = list(func.decorator_list)
    nodes.extend(func.args.defaults)
    nodes.extend(d for d in func.args.kw_defaults if d)
    nodes.extend(_iter_function_annotation_nodes(func))
    deps: set[str] = set()
    for node in nodes:
        deps.update(collect_name_references(node, names, exclude))
    type_params = func.type_params
    if type_params:
        deps.update(_analyze_type_param_deps(type_params, names, exclude))
    return deps


def _analyze_class_deps(
    node: ast.ClassDef,
    all_names: set[str],
    exclude: str,
) -> set[str]:
    """Analyze dependencies in a class."""
    deps: set[str] = set()
    for base in node.bases:
        deps.update(collect_name_references(base, all_names, exclude))
    for keyword in node.keywords:
        deps.update(collect_name_references(keyword.value, all_names, exclude))
    for decorator in node.decorator_list:
        deps.update(collect_name_references(decorator, all_names, exclude))
    type_params = node.type_params
    if type_params:
        deps.update(_analyze_type_param_deps(type_params, all_names, exclude))
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            deps.update(_analyze_signature_deps(stmt, all_names, exclude))
        elif isinstance(stmt, ast.ClassDef):
            deps.update(_analyze_class_deps(stmt, all_names, exclude))
        else:
            deps.update(collect_name_references(stmt, all_names, exclude))
    return deps


def extract_postamble_names(stmts: Iterable[ast.stmt]) -> set[str]:
    """Extract names bound or deleted by postamble AST statements.

    Walks nested statements, including conditional and loop bodies, but does
    not descend into function or class bodies. Definition names themselves are
    included.

    Args:
        stmts: Parsed AST statements from the postamble.

    Returns:
        Names bound or deleted by the postamble statements.
    """
    names: set[str] = set()
    stack: list[ast.AST] = list(stmts)
    while stack:
        cur = stack.pop()
        names.update(get_bound_names(cur))
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # Name captured; body creates a new scope, don't descend
        stack.extend(iter_child_nodes(cur))
    return names


def has_future_annotations_import(source: str, *, _tree: ast.Module | None = None) -> bool:
    """Check if source contains `from __future__ import annotations`.

    Detects standalone, combined, and parenthesized forms.

    Args:
        source: The Python source code.
        _tree: Optional pre-parsed AST that must correspond to `source`.

    Returns:
        `True` when the future import is present. Returns `False` when it is
        absent or parsing the source raises `SyntaxError`.
    """
    if _tree is not None:
        tree = _tree
    else:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == '__future__'
        and any(alias.name == 'annotations' for alias in node.names)
        for node in tree.body
    )


def analyze_dependencies(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    all_names: set[str],
    *,
    eager_refs: set[str] | None = None,
    preamble_shadowed: set[str] | None = None,
) -> set[str]:
    """Analyze all dependencies of a definition.

    For functions, scan the entire node to retain conservative closure and
    early-reference relationships. For classes, scan definition-time components
    and the precomputed bounded eager reads.

    Args:
        node: The definition AST node.
        all_names: Set of all top-level definition names in scope.
        eager_refs: Precomputed definition-time reads for a class node.
            Ignored for function nodes.
        preamble_shadowed: Names whose original class-scope reads resolve to a
            preceding module binding instead of a movable definition.

    Returns:
        Referenced names that are movable top-level definitions.
    """
    if not isinstance(node, ast.ClassDef):
        return collect_name_references(node, all_names, node.name)
    deps = _analyze_class_deps(node, all_names, node.name) - (preamble_shadowed or set())
    class_eager_refs = analyze_eager_refs(node) if eager_refs is None else eager_refs
    deps.update(((class_eager_refs - (preamble_shadowed or set())) & all_names) - {node.name})
    return deps


def kendall_tau(ranks: list[int]) -> float:
    """Compute Kendall tau-a rank correlation against the natural order.

    Compares the given permutation against [0, 1, 2, ..., n-1].
    Returns 1.0 for identical ordering, -1.0 for fully reversed.

    Args:
        ranks: A permutation of integers representing the actual positions
            relative to a target ordering.

    Returns:
        Kendall tau-a correlation against the natural order.
    """
    n = len(ranks)
    if n < _MIN_PAIRS_FOR_TAU:
        return 1.0
    total = n * (n - 1) // 2
    discordant = _count_inversions(ranks)
    concordant = total - discordant
    return (concordant - discordant) / total


def module_contains_annotations(tree: ast.Module) -> bool:
    """Return whether a module contains annotations affected by the future import.

    Args:
        tree: Parsed module to inspect.

    Returns:
        `True` for any annotated assignment or function signature annotation.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _iter_function_annotation_nodes(node)
        ):
            return True
    return False
