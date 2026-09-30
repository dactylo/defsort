import itertools
import random

import pytest

import defsort
from defsort import DefsortConfig
from defsort._types import (
    BANNER_MODE_VALUES,
    SORT_SCOPE_ORDER_VALUES,
)

_NORMAL_CASES = 8
_NORMAL_SEED = 0xDEF507
_SLOW_CASES = 1024
_SLOW_SEED = 0xBA66E25

_SORT_OPTIONS = tuple(
    DefsortConfig(
        definition_order=definition_order,
        method_order=method_order,
        banners=banners,
    )
    for definition_order, method_order, banners in itertools.product(
        SORT_SCOPE_ORDER_VALUES,
        SORT_SCOPE_ORDER_VALUES,
        BANNER_MODE_VALUES,
    )
)


class _DeterministicRandom(random.Random):
    pass


def _banner(rng: random.Random, label: str, *, indent: str = '') -> str:
    rule = rng.choice(('-', '=', '*', '#', '~')) * rng.randint(4, 12)
    if rng.choice((False, True)):
        lines = (f'# {rule}', f'# {label}', f'# {rule}')
    else:
        lines = (f'# {rule}',)
    return '\n'.join(f'{indent}{line}' for line in lines)


def _class_block(rng: random.Random, case: int) -> str:
    method_groups = [
        (
            '    @property\n'
            '    def value(self) -> int:\n'
            '        return self._value\n'
            '\n'
            '    @value.setter\n'
            '    def value(self, value: int) -> None:\n'
            '        self._value = value'
        ),
        (
            '    @overload\n'
            '    def convert(self, value: int) -> int: ...\n'
            '\n'
            '    @overload\n'
            '    def convert(self, value: str) -> str: ...\n'
            '\n'
            '    def convert(self, value: object) -> object:\n'
            '        return value'
        ),
        (f'    def __init__(self) -> None:\n        self._value = {case}'),
        ('    def __str__(self) -> str:\n        return str(self._value)'),
        ('    async def alpha(self) -> int:\n        return self._value'),
        ('    def zulu(self) -> int:\n        return self._value'),
    ]
    rng.shuffle(method_groups)
    split = rng.randint(2, len(method_groups) - 2)
    first = '\n\n'.join(method_groups[:split])
    second = '\n\n'.join(method_groups[split:])
    banner = _banner(rng, f'Methods {case}', indent='    ')
    body = f'{first}\n\n{banner}\n\n{second}'
    return f'class Widget{case}:\n{body}'


def _function_block(rng: random.Random, case: int, index: int) -> str:
    prefix = 'async ' if rng.randrange(4) == 0 else ''
    body = f'return {case}' if index == 0 else f'return helper_{case}_{index - 1}()'
    return f'{prefix}def helper_{case}_{index}():\n    {body}'


def _module(rng: random.Random, case: int) -> str:
    definitions = [_function_block(rng, case, index) for index in range(4)]
    definitions.extend(
        (
            (
                '@overload\n'
                f'def parse_{case}(value: int) -> int: ...\n'
                '\n'
                '@overload\n'
                f'def parse_{case}(value: str) -> str: ...\n'
                '\n'
                f'def parse_{case}(value: object) -> object:\n'
                '    return value'
            ),
            _class_block(rng, case),
        )
    )
    rng.shuffle(definitions)
    first_split = rng.randint(1, 2)
    second_split = rng.randint(3, len(definitions) - 1)
    sections = (
        definitions[:first_split],
        definitions[first_split:second_split],
        definitions[second_split:],
    )
    blocks: list[str] = []
    for section_index, section in enumerate(sections):
        if section_index:
            blocks.append(_banner(rng, f'Section {case}.{section_index}'))
        blocks.extend(section)
    return 'from typing import overload\n\n\n' + '\n\n'.join(blocks) + '\n'


def _assert_idempotent(*, cases: int, seed: int) -> None:
    rng = _DeterministicRandom(seed)
    for case in range(cases):
        source = _module(rng, case)
        for options in _SORT_OPTIONS:
            once = defsort.sort(source, config=options)
            twice = defsort.sort(once, config=options)
            assert twice == once, (
                f'idempotency failed for case={case}, seed={seed}, options={options!r}'
            )


def test_banner_modes_are_idempotent() -> None:
    _assert_idempotent(cases=_NORMAL_CASES, seed=_NORMAL_SEED)


@pytest.mark.slow
def test_banner_modes_are_idempotent_large_corpus() -> None:
    _assert_idempotent(cases=_SLOW_CASES, seed=_SLOW_SEED)
