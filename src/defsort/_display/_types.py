"""Type definitions owned by the display subsystem."""

import enum
from typing import Literal, NamedTuple, TypedDict

from defsort._types import Blocker, JsonStatus, OrderKind

MovementRecord = TypedDict(
    'MovementRecord',
    {'name': str, 'from': int, 'to': int},
)
MethodMovementRecord = TypedDict(
    'MethodMovementRecord',
    {'class': str, 'movements': list[MovementRecord]},
)
ClassBlockerRecord = TypedDict(
    'ClassBlockerRecord',
    {'class': str, 'occurrence': int, 'blocker': Blocker},
)


class CliMode(enum.Enum):
    """CLI output mode, with `SUMMARY` as the default.

    The `-w`, `-p`, and `-d` flags select the alternative modes.

    Attributes:
        SUMMARY: Print a compact summary table.
        WRITE: Replace files with sorted source.
        PREVIEW: Print movement tables.
        DIFF: Print unified source diffs.
    """

    SUMMARY = 'summary'
    WRITE = 'write'
    PREVIEW = 'preview'
    DIFF = 'diff'


class CountRecord(TypedDict):
    """JSON movement count for one definition category.

    Attributes:
        moved: Number of items whose positions changed.
        total: Total number of items considered.
    """

    moved: int
    total: int


class MovementPositions(NamedTuple):
    """Original positions and stable indices for moved definitions.

    Attributes:
        old_by_index: Original position keyed by stable definition index.
        moved_indices: Stable indices that occupy new positions.
    """

    old_by_index: dict[int, int]
    moved_indices: list[int]


class MovementRow(NamedTuple):
    """One row of a definition or method movement table.

    Attributes:
        old_name: Name formerly occupying the row.
        new_name: Name proposed for the row.
        movement: Upward or downward distance marker, or an empty string.
        new_idx: Stable index of `new_name`.
        old_idx: Stable index of `old_name`.
    """

    old_name: str
    new_name: str
    movement: str  # e.g. '^2', 'v1', ''
    new_idx: int
    old_idx: int


class SummaryRow(NamedTuple):
    """One row of the CLI summary output.

    Attributes:
        path: Display path of the processed file.
        defs: Number of top-level definitions.
        methods: Number of sortable methods.
        moved: Total number of items that moved.
        total: Total number of sortable items.
        changed: Whether sorting changed the source.
    """

    path: str
    defs: int
    methods: int
    moved: int
    total: int
    changed: bool


class _BannerRecord(TypedDict):
    """JSON banner details owned by a constraint record."""

    line: int
    text: str


class _HeldItemRecord(TypedDict):
    """JSON details for an item held across a banner."""

    name: str
    kind: Literal['class', 'function', 'method']
    occurrence: int
    line: int
    direction: Literal['before', 'after']


class BannerConstraintRecord(TypedDict):
    """JSON representation of one banner constraint.

    Attributes:
        scope: Whether the constraint governs definitions or methods.
        class_name: Containing class for method constraints, otherwise `None`.
        banner: Banner that constrains the items.
        held: Items retained on their original side of the banner.
    """

    scope: Literal['definitions', 'methods']
    class_name: str | None
    banner: _BannerRecord
    held: list[_HeldItemRecord]


class JsonFileRecord(TypedDict):
    """Complete JSON output record for one processed file.

    Attributes:
        path: Display path of the processed file.
        status: File-processing outcome.
        warning: Optional human-readable diagnostic.
        order: Detected source order, or `None` when detection is unavailable.
        dep_score: Dependency-order score, or `None` when detection is
            unavailable.
        alpha_score: Alphabetical-order score, or `None` when detection is
            unavailable.
        definitions: Top-level definition movement counts.
        methods: Class-method movement counts.
        movements: Top-level definition movements.
        method_movements: Per-class method movements.
        banner_constraints: Banner constraints affecting proposed order.
        class_blockers: Classes whose method sorting was blocked.
    """

    path: str
    status: JsonStatus
    warning: str | None
    order: OrderKind | None
    dep_score: float | None
    alpha_score: float | None
    definitions: CountRecord
    methods: CountRecord
    movements: list[MovementRecord]
    method_movements: list[MethodMovementRecord]
    banner_constraints: list[BannerConstraintRecord]
    class_blockers: list[ClassBlockerRecord]
