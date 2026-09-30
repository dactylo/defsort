from defsort._types import Definition, DefinitionKind

_SRC_BASELINE = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
_SRC_WITH_NEW = (
    'def alpha():\n    return 1\n\n\n'
    'def bravo():\n    return alpha()\n\n\n'
    'def beta():\n    return alpha()\n'
)


def _alpha_order(  # pyright: ignore[reportUnusedFunction]
    defs: list[Definition],
) -> list[Definition]:
    return sorted(defs, key=lambda d: d.name)


def _def_index(d: Definition) -> int:  # pyright: ignore[reportUnusedFunction]
    return d.index


def _def_is_overload(d: Definition) -> bool:  # pyright: ignore[reportUnusedFunction]
    return d.is_overload


def _def_key(  # pyright: ignore[reportUnusedFunction]
    d: Definition,
) -> tuple[str, DefinitionKind]:
    return (d.name, d.kind)


def _make_def(  # pyright: ignore[reportUnusedFunction]
    index: int,
    name: str,
    kind: DefinitionKind,
    *,
    overload: bool = False,
) -> Definition:
    prefix = '@overload\n' if overload else ''
    return Definition(
        index=index,
        name=name,
        start_line=1,
        end_line=2,
        kind=kind,
        source=f'{prefix}def {name}(): pass\n',
        is_overload=overload,
    )
