"""Large-reorder guard: measure how many existing definitions would move."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from defsort._core._parse import parse_definitions
from defsort._types import Definition, DefinitionKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ReorderGuardParams:
    """Per-file reorder guard inputs.

    Attributes:
        baseline_defs: Existing definitions whose movement is measured.
        max_reorder_count: Maximum permitted moved definitions, or `None`.
        max_reorder_percent: Maximum permitted moved percentage, or `None`.
    """

    baseline_defs: tuple[Definition, ...]
    max_reorder_count: int | None = None
    max_reorder_percent: int | None = None

    @property
    def _enabled(self) -> bool:
        """Return whether at least one threshold is configured."""
        return self.max_reorder_count is not None or self.max_reorder_percent is not None


@dataclass(frozen=True, slots=True)
class _ReorderGuardResult:
    """Outcome of evaluating the reorder guard."""

    moved: int
    total: int
    reasons: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        """Return a human-readable summary of the guard outcome.

        Returns:
            The movement count and exceeded thresholds.
        """
        return (
            f'would move {self.moved} of {self.total} definitions; '
            f'threshold: {"; ".join(self.reasons)}'
        )

    @property
    def triggered(self) -> bool:
        """Return whether at least one threshold was exceeded.

        Returns:
            `True` when the reorder guard was triggered.
        """
        return bool(self.reasons)


def _build_keyed_sequence(
    defs: Sequence[Definition],
) -> list[tuple[str, DefinitionKind, int]]:
    """Build keyed sequence with occurrence counters for identity matching."""
    counts: Counter[tuple[str, DefinitionKind]] = Counter()
    result: list[tuple[str, DefinitionKind, int]] = []
    for d in defs:
        key = (d.name, d.kind)
        counts[key] += 1
        result.append((d.name, d.kind, counts[key]))
    return result


def _count_reorder_moves(
    baseline_defs: Sequence[Definition],
    sorted_defs: Sequence[Definition],
) -> tuple[int, int]:
    """Count existing definitions that changed position after sorting."""
    if not baseline_defs or not sorted_defs:
        return (0, 0)

    bl_keys = _build_keyed_sequence(baseline_defs)
    sorted_keys = _build_keyed_sequence(sorted_defs)

    common = set(bl_keys) & set(sorted_keys)
    if not common:
        return (0, 0)

    bl_filtered = [k for k in bl_keys if k in common]
    sorted_filtered = [k for k in sorted_keys if k in common]

    total = len(bl_filtered)
    moved = sum(1 for a, b in zip(bl_filtered, sorted_filtered, strict=True) if a != b)
    return (moved, total)


def build_guard_params(
    baseline_content: str,
    *,
    max_reorder_count: int | None,
    max_reorder_percent: int | None,
) -> ReorderGuardParams | None:
    """Parse baseline and construct guard params.

    Args:
        baseline_content: Baseline Python source to measure against.
        max_reorder_count: Maximum permitted number of moved definitions.
        max_reorder_percent: Maximum permitted percentage of moved definitions.

    Returns:
        Guard parameters, or `None` when the baseline has no parseable
        definitions.
    """
    bl_defs = parse_definitions(baseline_content).definitions
    if not bl_defs:
        return None
    return ReorderGuardParams(
        baseline_defs=tuple(bl_defs),
        max_reorder_count=max_reorder_count,
        max_reorder_percent=max_reorder_percent,
    )


def evaluate_reorder_guard(
    params: ReorderGuardParams | None,
    sorted_defs: Sequence[Definition],
) -> _ReorderGuardResult:
    """Evaluate the reorder guard, returning whether thresholds are exceeded.

    Args:
        params: Guard configuration with baseline definitions and thresholds.
            None means guard disabled.
        sorted_defs: The proposed sorted definitions to evaluate.

    Returns:
        Movement totals and any exceeded-threshold reasons.
    """
    if params is None or not params._enabled:  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        return _ReorderGuardResult(0, 0)

    moved, total = _count_reorder_moves(params.baseline_defs, sorted_defs)
    if total == 0:
        return _ReorderGuardResult(0, 0)

    reasons: list[str] = []
    if params.max_reorder_count is not None and moved > params.max_reorder_count:
        reasons.append(f'max-reorder-count = {params.max_reorder_count}')
    if params.max_reorder_percent is not None and moved * 100 > params.max_reorder_percent * total:
        reasons.append(f'max-reorder-percent = {params.max_reorder_percent}')

    return _ReorderGuardResult(moved, total, tuple(reasons))
