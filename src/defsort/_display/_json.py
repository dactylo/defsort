"""JSON output formatting."""

import json

from defsort._display._movement import count_moved_split, movement_positions
from defsort._display._types import (
    BannerConstraintRecord,
    ClassBlockerRecord,
    CountRecord,
    JsonFileRecord,
    MethodMovementRecord,
    MovementRecord,
)
from defsort._types import BannerConstraint, JsonStatus, OrderKind, SortResult


def _banner_constraint_record(constraint: BannerConstraint) -> BannerConstraintRecord:
    """Serialize one structured banner constraint."""
    return {
        'scope': constraint.scope,
        'class_name': constraint.class_name,
        'banner': {
            'line': constraint.boundary.start_line,
            'text': constraint.boundary.text,
        },
        'held': [
            {
                'name': item.name,
                'kind': item.kind,
                'occurrence': item.occurrence,
                'line': item.line,
                'direction': item.direction,
            }
            for item in constraint.held
        ],
    }


def build_json_record(
    path: str,
    status: JsonStatus,
    *,
    warning: str | None = None,
    result: SortResult | None = None,
    detect_result: tuple[OrderKind, float, float] | None = None,
) -> JsonFileRecord:
    """Build a single file record for the unified JSON schema.

    Args:
        path: Display path for the selected file.
        status: JsonStatus literal string value.
        warning: Human-readable error, skip, guard, or partial-outcome note.
        result: SortResult when the file was successfully parsed.
        detect_result: Original-order classification and scores, when
            available.

    Returns:
        The version-1 JSON record for the file.
    """
    if detect_result is not None:
        order: OrderKind | None = detect_result[0]
        dep_score: float | None = detect_result[1]
        alpha_score: float | None = detect_result[2]
    else:
        order = None
        dep_score = None
        alpha_score = None

    if result is not None:
        defs_moved, meth_moved = count_moved_split(result)
        definitions: CountRecord = {
            'moved': defs_moved,
            'total': len(result.original_order),
        }
        methods: CountRecord = {'moved': meth_moved, 'total': result.total_methods}
        banner_constraints = [
            _banner_constraint_record(constraint) for constraint in result.banner_constraints
        ]
        class_blockers: list[ClassBlockerRecord] = [
            {
                'class': class_blocker.class_name,
                'occurrence': class_blocker.occurrence,
                'blocker': class_blocker.blocker,
            }
            for class_blocker in result.class_blockers
        ]
    else:
        definitions = {'moved': 0, 'total': 0}
        methods = {'moved': 0, 'total': 0}
        banner_constraints = []
        class_blockers = []

    movements: list[MovementRecord] = []
    method_movements: list[MethodMovementRecord] = []
    if result is not None and (status == 'changed' or result.guarded):
        positions = movement_positions(result.original_order, result.new_order)
        moved_indices = set(positions.moved_indices)
        for new_pos, defn in enumerate(result.new_order):
            if defn.index in moved_indices:
                old_pos = positions.old_by_index[defn.index]
                movements.append({'name': defn.name, 'from': old_pos, 'to': new_pos})
        for info in result.method_sorts:
            positions = movement_positions(info.original_order, info.new_order)
            moved_indices = set(positions.moved_indices)
            class_mvs: list[MovementRecord] = []
            for new_pos, method in enumerate(info.new_order):
                if method.index in moved_indices:
                    old_pos = positions.old_by_index[method.index]
                    class_mvs.append({'name': method.name, 'from': old_pos, 'to': new_pos})
            if class_mvs:
                method_movements.append({'class': info.class_name, 'movements': class_mvs})

    return {
        'path': path,
        'status': status,
        'warning': warning,
        'order': order,
        'dep_score': dep_score,
        'alpha_score': alpha_score,
        'definitions': definitions,
        'methods': methods,
        'movements': movements,
        'method_movements': method_movements,
        'banner_constraints': banner_constraints,
        'class_blockers': class_blockers,
    }


def format_json(records: list[JsonFileRecord]) -> str:
    """Serialize unified JSON output.

    Args:
        records: File records to include.

    Returns:
        The indented version-1 JSON document.
    """
    return json.dumps({'version': 1, 'files': records}, indent=2)
