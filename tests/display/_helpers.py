from defsort._types import Definition, SortResult


def _make_def(  # pyright: ignore[reportUnusedFunction]
    index: int, name: str
) -> Definition:
    return Definition(
        index=index,
        name=name,
        start_line=index * 3,
        end_line=index * 3 + 1,
        kind='function',
        source=f'def {name}(): pass',
        trailing='',
    )


def _make_result(  # pyright: ignore[reportUnusedFunction]
    *, changed: bool = True
) -> SortResult:
    orig = [
        Definition(
            index=0,
            name='bravo',
            start_line=1,
            end_line=2,
            kind='function',
            source='def bravo():\n    pass\n',
        ),
        Definition(
            index=1,
            name='alpha',
            start_line=5,
            end_line=6,
            kind='function',
            source='def alpha():\n    pass\n',
        ),
    ]
    new = [
        Definition(
            index=1,
            name='alpha',
            start_line=5,
            end_line=6,
            kind='function',
            source='def alpha():\n    pass\n',
        ),
        Definition(
            index=0,
            name='bravo',
            start_line=1,
            end_line=2,
            kind='function',
            source='def bravo():\n    pass\n',
        ),
    ]
    if not changed:
        new = list(orig)
    return SortResult(
        original_source='def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
        new_source='def alpha():\n    pass\n\n\ndef bravo():\n    pass\n'
        if changed
        else 'def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
        original_order=orig,
        new_order=new,
        warning=None,
        blocking_code=[],
        skipped=False,
    )
