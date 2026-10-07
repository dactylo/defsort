from defsort._core._guard import (
    ReorderGuardParams,
    _count_reorder_moves,
    _ReorderGuardResult,
    build_guard_params,
    evaluate_reorder_guard,
)
from defsort._core._parse import parse_definitions
from defsort._types import Definition


class TestBuildGuardParams:
    def test_both_thresholds(self) -> None:
        source = 'def alpha(): ...\ndef beta(): ...\n'
        result = build_guard_params(source, max_reorder_count=5, max_reorder_percent=50)
        assert result is not None
        assert result.max_reorder_count == 5
        assert result.max_reorder_percent == 50

    def test_returns_none_for_no_definitions(self) -> None:
        source = 'import os\nx = 1\n'
        result = build_guard_params(source, max_reorder_count=3, max_reorder_percent=None)
        assert result is None

    def test_returns_none_for_syntax_error(self) -> None:
        source = 'def alpha(\n'
        result = build_guard_params(source, max_reorder_count=3, max_reorder_percent=None)
        assert result is None

    def test_returns_params_for_valid_source(self) -> None:
        source = 'def alpha(): ...\ndef beta(): ...\n'
        result = build_guard_params(source, max_reorder_count=3, max_reorder_percent=None)
        assert result is not None
        assert len(result.baseline_defs) == 2
        assert result.max_reorder_count == 3
        assert result.max_reorder_percent is None


class TestCountReorderMoves:
    def test_all_move_when_reversed(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        sorted_src = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (2, 2)

    def test_classes_matched(self) -> None:
        baseline = 'class A:\n    pass\n\n\nclass B:\n    pass\n'
        sorted_src = 'class B:\n    pass\n\n\nclass A:\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (2, 2)

    def test_deleted_definition_excluded(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n'
        sorted_src = 'def a():\n    pass\n\n\ndef c():\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (0, 2)

    def test_empty_baseline(self) -> None:
        assert _count_reorder_moves((), _parse_defs('def a():\n    pass\n')) == (0, 0)

    def test_initial_comment_does_not_change_move_count(self) -> None:
        baseline = 'def zeta():\n    pass\n\n\ndef alpha():\n    pass\n'
        sorted_source = 'def alpha():\n    pass\n\n\ndef zeta():\n    pass\n'
        baseline_with_comment = '# about zeta\n' + baseline
        sorted_with_comment = 'def alpha():\n    pass\n\n\n# about zeta\ndef zeta():\n    pass\n'

        without_comment = _count_reorder_moves(
            _parse_defs(baseline),
            _parse_defs(sorted_source),
        )
        with_comment = _count_reorder_moves(
            _parse_defs(baseline_with_comment),
            _parse_defs(sorted_with_comment),
        )

        assert without_comment == (2, 2)
        assert with_comment == without_comment

    def test_new_def_does_not_affect_existing_count(self) -> None:
        baseline = 'def b():\n    pass\n\n\ndef c():\n    pass\n'
        sorted_src = 'def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (0, 2)

    def test_new_definition_excluded(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        sorted_src = 'def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (0, 2)

    def test_no_moves_when_already_sorted(self) -> None:
        source = 'def a():\n    pass\n\n\ndef b():\n    return a()\n'
        defs = _parse_defs(source)
        assert _count_reorder_moves(defs, defs) == (0, 2)

    def test_overloads_matched_by_occurrence(self) -> None:
        baseline = (
            'from typing import overload\n\n\n'
            '@overload\ndef f(x: int) -> int: ...\n\n\n'
            '@overload\ndef f(x: str) -> str: ...\n\n\n'
            'def f(x: int | str) -> int | str:\n    return x\n\n\n'
            'def g():\n    pass\n'
        )
        defs = _parse_defs(baseline)
        assert _count_reorder_moves(defs, defs) == (0, 4)

    def test_partial_reorder(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n'
        sorted_src = 'def c():\n    pass\n\n\ndef a():\n    pass\n\n\ndef b():\n    pass\n'
        assert _count_reorder_moves(_parse_defs(baseline), _parse_defs(sorted_src)) == (3, 3)

    def test_syntax_error_baseline(self) -> None:
        broken_defs = _parse_defs('def broken(\n')
        assert broken_defs == ()
        sorted_defs = _parse_defs('def a():\n    pass\n')
        assert _count_reorder_moves(broken_defs, sorted_defs) == (0, 0)


class TestEvaluateReorderGuard:
    def test_both_thresholds_or_logic(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        sorted_src = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = _parse_defs(baseline)
        sorted_defs = _parse_defs(sorted_src)
        params = ReorderGuardParams(
            baseline_defs=bl_defs,
            max_reorder_count=1,
            max_reorder_percent=50,
        )
        result = evaluate_reorder_guard(params, sorted_defs)
        assert result.triggered is True
        assert len(result.reasons) == 2

    def test_disabled_params_returns_not_triggered(self) -> None:
        defs = _parse_defs('def a():\n    pass\n')
        params = ReorderGuardParams(baseline_defs=defs)
        result = evaluate_reorder_guard(params, defs)
        assert result.triggered is False

    def test_none_params_returns_not_triggered(self) -> None:
        defs = _parse_defs('def a():\n    pass\n')
        result = evaluate_reorder_guard(None, defs)
        assert result.triggered is False

    def test_over_count_threshold_triggered(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        sorted_src = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = _parse_defs(baseline)
        sorted_defs = _parse_defs(sorted_src)
        params = ReorderGuardParams(
            baseline_defs=bl_defs,
            max_reorder_count=1,
        )
        result = evaluate_reorder_guard(params, sorted_defs)
        assert result.triggered is True
        assert 'max-reorder-count = 1' in result.reasons

    def test_over_percent_threshold_triggered(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        sorted_src = 'def b():\n    pass\n\n\ndef a():\n    pass\n'
        bl_defs = _parse_defs(baseline)
        sorted_defs = _parse_defs(sorted_src)
        params = ReorderGuardParams(
            baseline_defs=bl_defs,
            max_reorder_percent=50,
        )
        result = evaluate_reorder_guard(params, sorted_defs)
        assert result.triggered is True
        assert 'max-reorder-percent = 50' in result.reasons

    def test_under_threshold_not_triggered(self) -> None:
        baseline = 'def a():\n    pass\n\n\ndef b():\n    pass\n'
        bl_defs = _parse_defs(baseline)
        sorted_defs = _parse_defs(baseline)  # same order
        params = ReorderGuardParams(
            baseline_defs=bl_defs,
            max_reorder_count=5,
        )
        result = evaluate_reorder_guard(params, sorted_defs)
        assert result.triggered is False
        assert result.moved == 0


class TestReorderGuardResult:
    def test_message_format(self) -> None:
        result = _ReorderGuardResult(moved=5, total=8, reasons=('max-reorder-percent = 60',))
        assert '5 of 8' in result.message
        assert 'max-reorder-percent = 60' in result.message

    def test_not_triggered_when_no_reasons(self) -> None:
        result = _ReorderGuardResult(moved=0, total=2)
        assert result.triggered is False

    def test_triggered_when_reasons(self) -> None:
        result = _ReorderGuardResult(moved=2, total=3, reasons=('max-reorder-count = 1',))
        assert result.triggered is True


def _parse_defs(source: str) -> tuple[Definition, ...]:
    return tuple(parse_definitions(source).definitions)
