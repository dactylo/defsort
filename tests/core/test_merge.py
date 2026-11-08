from functools import partial

import pytest

from defsort._core._merge import find_new_items, incremental_merge
from defsort._core._ordering import order_dependency
from defsort._types import Blocker, SortPolicy
from tests.core._helpers import alpha_order, def_index, def_is_overload, def_key, make_def


class TestFindNewItems:
    def test_excess_duplicate_positional(self) -> None:
        baseline = [make_def(0, 'foo', 'function')]
        current = [make_def(0, 'foo', 'function'), make_def(1, 'foo', 'function')]
        new = find_new_items(iter(baseline), iter(current), def_key, def_is_overload)
        assert len(new) == 1
        assert new[0] is current[1]

    def test_new_function(self) -> None:
        baseline = [make_def(0, 'alpha', 'function')]
        current = [make_def(0, 'alpha', 'function'), make_def(1, 'bravo', 'function')]
        new = find_new_items(baseline, current, def_key, def_is_overload)
        assert [d.name for d in new] == ['bravo']

    def test_no_new(self) -> None:
        defs = [make_def(0, 'alpha', 'function'), make_def(1, 'bravo', 'function')]
        new = find_new_items(defs, defs, def_key, def_is_overload)
        assert new == []

    def test_overload_never_new(self) -> None:
        baseline = [make_def(0, 'foo', 'function')]
        current = [
            make_def(0, 'foo', 'function'),
            make_def(1, 'foo', 'function'),
        ]
        current[1] = make_def(1, 'foo', 'function', overload=True)
        new = find_new_items(baseline, current, def_key, def_is_overload)
        assert new == []

    def test_same_name_different_kind(self) -> None:
        baseline = [make_def(0, 'Foo', 'class')]
        current = [make_def(0, 'Foo', 'class'), make_def(1, 'Foo', 'function')]
        new = find_new_items(baseline, current, def_key, def_is_overload)
        assert len(new) == 1
        assert new[0].kind == 'function'


class TestIncrementalMerge:
    @pytest.mark.parametrize(
        ('baseline_names', 'current_names', 'expected_indices', 'expected_blocker'),
        [
            ('aac', 'aacb', [0, 1, 3, 2], None),
            ('caa', 'caab', [], 'non_conforming'),
            ('aac', 'caab', [], 'user_reordered'),
            ('aac', 'aacb', [], 'target_incompatible'),
        ],
    )
    def test_duplicate_occurrences_preserve_identity_and_blockers(
        self,
        baseline_names: str,
        current_names: str,
        expected_indices: list[int],
        expected_blocker: Blocker | None,
    ) -> None:
        baseline = [make_def(i + 10, name, 'function') for i, name in enumerate(baseline_names)]
        current = [make_def(i, name, 'function') for i, name in enumerate(current_names)]
        if expected_blocker == 'target_incompatible':
            current[0].dependencies.add('c')

        merged, blocker = incremental_merge(
            current,
            baseline,
            iter([current[-1]]),
            partial(order_dependency, policy=SortPolicy()),
            def_key,
            is_overload_fn=def_is_overload,
            index_fn=def_index,
        )

        assert blocker == expected_blocker
        assert [id(item) for item in merged] == [id(current[i]) for i in expected_indices]
        if blocker is None:
            assert [id(item) for item in merged if item is not current[-1]] == [
                id(item) for item in current[:-1]
            ]

    def test_non_conforming_baseline(self) -> None:
        baseline = [make_def(0, 'b', 'function'), make_def(1, 'a', 'function')]
        current = [
            make_def(0, 'b', 'function'),
            make_def(1, 'a', 'function'),
            make_def(2, 'c', 'function'),
        ]
        new = [make_def(2, 'c', 'function')]
        _, blocker = incremental_merge(
            current,
            baseline,
            new,
            alpha_order,
            def_key,
            is_overload_fn=def_is_overload,
            index_fn=def_index,
        )
        assert blocker == 'non_conforming'

    def test_successful_merge(self) -> None:
        baseline = [make_def(0, 'a', 'function'), make_def(1, 'c', 'function')]
        current = [
            make_def(0, 'a', 'function'),
            make_def(1, 'c', 'function'),
            make_def(2, 'b', 'function'),
        ]
        new = [make_def(2, 'b', 'function')]
        merged, blocker = incremental_merge(
            current,
            baseline,
            new,
            alpha_order,
            def_key,
            is_overload_fn=def_is_overload,
            index_fn=def_index,
        )
        assert blocker is None
        assert [d.name for d in merged] == ['a', 'b', 'c']

    def test_user_reordered(self) -> None:
        baseline = [make_def(0, 'a', 'function'), make_def(1, 'b', 'function')]
        current = [
            make_def(0, 'b', 'function'),
            make_def(1, 'a', 'function'),
            make_def(2, 'c', 'function'),
        ]
        new = [make_def(2, 'c', 'function')]
        _, blocker = incremental_merge(
            current,
            baseline,
            new,
            alpha_order,
            def_key,
            is_overload_fn=def_is_overload,
            index_fn=def_index,
        )
        assert blocker == 'user_reordered'
