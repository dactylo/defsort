import itertools
from typing import Any, cast

import pytest

import defsort
from defsort import DefsortConfig
from defsort._types import (
    BANNER_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
)

# Exercise every pair of ordering, evaluation, and banner policy values.
_AXIS_VALUES: dict[str, list[object]] = {
    'definition_order': list(SORT_SCOPE_ORDER_VALUES),
    'method_order': list(SORT_SCOPE_ORDER_VALUES),
    'banners': list(BANNER_MODE_VALUES),
    'evaluation_order': list(EVALUATION_ORDER_VALUES),
    'method_bodies': list(METHOD_BODIES_VALUES),
    'method_visibility': list(METHOD_VISIBILITY_VALUES),
    'method_dunders': list(METHOD_DUNDERS_VALUES),
    'main': list(MAIN_PLACEMENT_VALUES),
    'tie_break': list(TIE_BREAK_VALUES),
}

_INCREMENTAL_ROUND_TRIP_OPTIONS = [
    DefsortConfig(),
    DefsortConfig(method_bodies='across-categories'),
    DefsortConfig(banners='ignore'),
    DefsortConfig(tie_break='source'),
    DefsortConfig(evaluation_order='allow-reordering'),
]

# Genuine pairwise coverage: every value of every axis meets every value of
# every other axis at least once. The dynamic kwargs are an untyped boundary;
# the cast keeps pyright strict satisfied without loosening DefsortConfig.
_PAIRWISE: list[DefsortConfig] = [
    DefsortConfig(**cast('dict[str, Any]', {a: va, b: vb}))
    for (a, values_a), (b, values_b) in itertools.combinations(_AXIS_VALUES.items(), 2)
    for va in values_a
    for vb in values_b
]

_SRC_KITCHEN_SINK = """from typing import overload


class Cache:
    def store(self, key, value):
        self._evict_if_full()
        self._data[key] = value

    @property
    def size(self):
        return len(self._data)

    @size.setter
    def size(self, value):
        raise AttributeError('read-only')

    # ----------

    @overload
    def get(self, key: str) -> object: ...

    @overload
    def get(self, key: bytes) -> object: ...

    def get(self, key):
        return self._data.get(key)

    def __init__(self, limit):
        self._data = {}
        self._limit = self._validate(limit)

    def _evict_if_full(self):
        if self._overflowing():
            self._data.pop(next(iter(self._data)))

    def __repr__(self):
        return f'Cache(size={self.size})'

    def _overflowing(self):
        return len(self._data) >= self._limit

    def __new__(cls, limit):
        return super().__new__(cls)

    def _validate(self, limit):
        return max(1, limit)


def main():
    run(Cache(10))


def run(cache):
    cache.store('a', 1)
"""

_TARGETED_CASES = [
    # Spec-named interaction cases plus a three-axis banner combination.
    DefsortConfig(
        method_visibility='public-first',
        method_bodies='within-categories',
    ),
    DefsortConfig(
        tie_break='source',
        method_bodies='across-categories',
    ),
    DefsortConfig(
        banners='ignore',
        method_bodies='across-categories',
        tie_break='source',
    ),
    DefsortConfig(
        definition_order='none',
        method_order='none',
    ),
]


@pytest.mark.parametrize('options', _INCREMENTAL_ROUND_TRIP_OPTIONS, ids=repr)
def test_incremental_round_trip_unchanged(options: DefsortConfig) -> None:
    baseline = defsort.sort(_SRC_KITCHEN_SINK, config=options)
    result = defsort.check(baseline, baseline=baseline, config=options)
    assert result.status == 'unchanged'


def test_kitchen_sink_exercises_sorting() -> None:
    result = defsort.check(_SRC_KITCHEN_SINK)
    assert result.status == 'changed'
    assert result.class_blockers == ()


# True all-pairs coverage necessarily includes valid but ineffective combinations.
# Ignore expected ineffective-option warnings in this engine interaction sweep.
@pytest.mark.filterwarnings(
    r'ignore:\[tool\.defsort\] .* has no effect under the resolved strategies:UserWarning'
)
@pytest.mark.parametrize(
    'options',
    list(dict.fromkeys([DefsortConfig(), *_PAIRWISE, *_TARGETED_CASES])),
    ids=repr,
)
def test_sort_is_idempotent(options: DefsortConfig) -> None:
    once = defsort.sort(_SRC_KITCHEN_SINK, config=options)
    assert defsort.sort(once, config=options) == once
