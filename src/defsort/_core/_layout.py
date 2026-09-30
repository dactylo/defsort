"""Lossless source layout primitives."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from defsort._source import (
    is_layout_blank,
    join_physical,
    split_physical_lines,
    terminal_line_ending,
)


class LayoutPreservationError(ValueError):
    """Signal that a reorder cannot preserve layout and comment ownership."""


@dataclass(frozen=True, slots=True)
class OwnershipBoundary:
    """Ownership-guard metadata for one exact inter-item separator.

    Attributes:
        terminator_length: Length of the preceding block's physical line
            ending.
        has_candidate: Whether the separator contains a movable blank-line
            guard.
    """

    terminator_length: int
    has_candidate: bool


@dataclass(frozen=True, slots=True)
class SourceLayout:
    """Fixed positional source surrounding movable blocks.

    Attributes:
        prefix: Fixed source before the first movable block.
        separators: Fixed source between adjacent movable blocks.
        suffix: Fixed source after the final movable block.
        ownership: Guard metadata corresponding to `separators`.
        guard_required: Block identities that require a trailing ownership
            guard.
        guard_required_before: Block identities that require a leading
            ownership guard.

    `ownership` is empty or has exactly one entry per separator. Rendering
    preserves every source byte while relocating only recorded guard
    candidates.
    """

    prefix: str
    separators: tuple[str, ...]
    suffix: str
    ownership: tuple[OwnershipBoundary, ...] = ()
    guard_required: frozenset[int] = frozenset()
    guard_required_before: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class _GapParts:
    """Owned and positional parts of one inter-item source gap."""

    trailing: str
    separator: str
    leading: str


def _candidate_span(
    separator: str,
    terminator_length: int,
) -> tuple[int, int] | None:
    """Locate the candidate blank line after a separator terminator."""
    candidate_start = terminator_length
    lines = split_physical_lines(separator[candidate_start:], keepends=True)
    if not lines:
        return None
    candidate = lines[0]
    if not terminal_line_ending(candidate) or not is_layout_blank(candidate):
        return None
    return candidate_start, candidate_start + len(candidate)


def _ends_with_blank_line(separator: str) -> bool:
    """Return whether a separator ends with a terminated layout-blank line."""
    lines = split_physical_lines(separator, keepends=True)
    return bool(lines) and is_layout_blank(lines[-1]) and bool(terminal_line_ending(lines[-1]))


def _has_guard(separator: str, boundary: OwnershipBoundary) -> bool:
    """Return whether a separator contains its recorded ownership guard."""
    return _candidate_span(separator, boundary.terminator_length) is not None


def _candidate_is_available(
    separator: str,
    boundary: OwnershipBoundary,
    *,
    required: bool,
) -> bool:
    """Return whether a boundary candidate can move without losing a guard."""
    if not boundary.has_candidate:
        return False
    if not required:
        return True
    candidate = _candidate_span(separator, boundary.terminator_length)
    if candidate is None:
        return False
    start, end = candidate
    without_candidate = separator[:start] + separator[end:]
    return _has_guard(without_candidate, boundary)


def _insert_candidate_at_seam(before: str, candidate: str, after: str) -> str:
    """Insert one candidate without fusing physical line endings."""
    return join_physical(before, candidate) + after


def _leading_physical_ending(separator: str) -> str:
    """Return the separator's leading physical line ending, if any."""
    if separator.startswith('\r\n'):
        return '\r\n'
    if separator.startswith(('\r', '\n')):
        return separator[0]
    return ''


def _relocate_blank_to_end(
    separator: str,
    boundary: OwnershipBoundary,
    *,
    required: bool,
) -> str | None:
    """Move one interior layout-blank line to the separator end, or None."""
    head = separator[: boundary.terminator_length]
    lines = split_physical_lines(separator[boundary.terminator_length :], keepends=True)
    first_donor = 1 if required else 0
    donor = next(
        (
            index
            for index, line in enumerate(lines)
            if index >= first_donor and is_layout_blank(line) and terminal_line_ending(line)
        ),
        None,
    )
    if donor is None:
        return None
    candidate = lines.pop(donor)
    rebuilt = head
    for line in lines:
        rebuilt = join_physical(rebuilt, line)
    return join_physical(rebuilt, candidate)


def _remove_candidate_at_seam(before: str, after: str) -> str:
    """Remove one candidate without fusing physical line endings."""
    return join_physical(before, after)


def _allocate_leading_guards(
    layout: SourceLayout,
    order: Sequence[int],
    rendered_separators: list[str],
    required_position_set: frozenset[int],
) -> None:
    """Relocate authored blanks before blocks with owned leading text."""
    for position, identity in enumerate(order):
        if position == 0 or identity not in layout.guard_required_before:
            continue
        separator_index = position - 1
        separator = rendered_separators[separator_index]
        if _ends_with_blank_line(separator):
            continue
        boundary = (
            layout.ownership[separator_index]
            if layout.ownership
            else OwnershipBoundary(terminator_length=0, has_candidate=False)
        )
        relocated = _relocate_blank_to_end(
            separator,
            boundary,
            required=separator_index in required_position_set,
        )
        if relocated is not None:
            rendered_separators[separator_index] = relocated
            continue
        donor = next(
            (
                index
                for index, donor_boundary in enumerate(layout.ownership)
                if index != separator_index
                and _candidate_is_available(
                    rendered_separators[index],
                    donor_boundary,
                    required=index in required_position_set,
                )
            ),
            None,
        )
        if donor is None:
            raise LayoutPreservationError('required ownership guard has no candidate')
        donor_span = _candidate_span(
            rendered_separators[donor], layout.ownership[donor].terminator_length
        )
        if donor_span is None:
            raise LayoutPreservationError('required ownership guard has no candidate')
        start, end = donor_span
        candidate = rendered_separators[donor][start:end]
        rendered_separators[donor] = _remove_candidate_at_seam(
            rendered_separators[donor][:start], rendered_separators[donor][end:]
        )
        rendered_separators[separator_index] = join_physical(separator, candidate)


def _relocate_exact_candidate(
    separators: list[str],
    ownership: tuple[OwnershipBoundary, ...],
    donor: int,
    target: int,
) -> None:
    """Move one exact ownership-guard candidate between separators."""
    donor_boundary = ownership[donor]
    candidate = _candidate_span(
        separators[donor],
        donor_boundary.terminator_length,
    )
    if candidate is None:
        raise ValueError('candidate must be one complete layout-blank physical line')
    start, end = candidate
    candidate_source = separators[donor][start:end]
    separators[donor] = _remove_candidate_at_seam(
        separators[donor][:start],
        separators[donor][end:],
    )

    target_offset = ownership[target].terminator_length
    separators[target] = _insert_candidate_at_seam(
        separators[target][:target_offset],
        candidate_source,
        separators[target][target_offset:],
    )


def _validate_ownership_boundary(
    separator: str,
    boundary: OwnershipBoundary,
) -> None:
    """Validate one separator against its ownership metadata."""
    ending = _leading_physical_ending(separator)
    if not ending or boundary.terminator_length != len(ending):
        raise ValueError('terminator length must select one complete physical ending')
    if boundary.has_candidate and not _has_guard(separator, boundary):
        raise ValueError('candidate must be one complete layout-blank physical line')


def _allocate_ownership_guards(
    layout: SourceLayout,
    order: Sequence[int],
) -> list[str]:
    """Allocate recorded ownership guards for one rendered block order."""
    rendered_separators = list(layout.separators)
    if not (layout.ownership or layout.guard_required or layout.guard_required_before):
        return rendered_separators
    if len(layout.ownership) != len(layout.separators):
        raise ValueError('ownership metadata count must match separator count')
    for separator, boundary in zip(
        layout.separators,
        layout.ownership,
        strict=True,
    ):
        _validate_ownership_boundary(separator, boundary)

    required_positions = [
        position
        for position, identity in enumerate(order[:-1])
        if identity in layout.guard_required
    ]
    required_position_set = frozenset(required_positions)
    candidate_positions = [
        position for position, boundary in enumerate(layout.ownership) if boundary.has_candidate
    ]
    missing = [
        position
        for position in required_positions
        if not _has_guard(rendered_separators[position], layout.ownership[position])
    ]
    available = [
        position
        for position in candidate_positions
        if _candidate_is_available(
            rendered_separators[position],
            layout.ownership[position],
            required=position in required_position_set,
        )
    ]
    if len(available) < len(missing):
        raise LayoutPreservationError('required ownership guard has no candidate')
    for target, donor in zip(missing, available, strict=False):
        _relocate_exact_candidate(
            rendered_separators,
            layout.ownership,
            donor,
            target,
        )
    _allocate_leading_guards(
        layout,
        order,
        rendered_separators,
        required_position_set,
    )
    return rendered_separators


def make_ownership_boundary(
    terminator: str,
    separator: str,
    *,
    owns_trailing: bool,
) -> OwnershipBoundary:
    """Build ownership metadata for one exact inter-item separator.

    Args:
        terminator: Exact physical ending detached from the movable block.
        separator: Exact positional source following that ending.
        owns_trailing: Whether the block contains non-layout trailing content.

    Returns:
        Metadata locating the terminator and optional guard candidate.

    Raises:
        ValueError: If the terminator is incomplete or trailing ownership lacks
            a complete layout-blank candidate.
    """
    if terminator not in {'\n', '\r', '\r\n'}:
        raise ValueError('terminator must be one complete physical ending')
    if owns_trailing and _candidate_span(separator, 0) is None:
        raise ValueError('trailing ownership requires one complete layout-blank physical line')
    return OwnershipBoundary(
        terminator_length=len(terminator),
        has_candidate=owns_trailing,
    )


def permute_separators(
    layout: SourceLayout,
    order: Sequence[int],
    pinned: Mapping[int, int],
) -> SourceLayout:
    """Relocate whole separators so pinned render positions keep source bytes.

    Args:
        layout: Layout whose separators are positional.
        order: Block identities in render order.
        pinned: Render separator position -> source separator index.

    Returns:
        A layout with separators (and ownership metadata) permuted; the
        original layout when nothing is pinned or counts do not line up.
    """
    count = len(layout.separators)
    if not pinned or count != max(0, len(order) - 1):
        return layout
    if layout.ownership and len(layout.ownership) != count:
        return layout
    used = set(pinned.values())
    remaining = [index for index in range(count) if index not in used]
    assignment: list[int] = []
    cursor = 0
    for position in range(count):
        source_index = pinned.get(position)
        if source_index is None:
            source_index = remaining[cursor]
            cursor += 1
        assignment.append(source_index)
    separators = tuple(layout.separators[index] for index in assignment)
    ownership = tuple(layout.ownership[index] for index in assignment) if layout.ownership else ()
    return replace(layout, separators=separators, ownership=ownership)


def render_layout(
    layout: SourceLayout,
    blocks: Mapping[int, str],
    order: Sequence[int],
) -> str:
    """Render movable blocks into fixed source positions.

    Args:
        layout: Exact fixed source around the movable positions.
        blocks: Movable source keyed by identity.
        order: Block identities in their requested position order.

    Returns:
        Exact source with the ordered blocks in the fixed layout.

    Raises:
        LayoutPreservationError: If the reorder needs an ownership guard that
            the existing layout cannot supply.
        ValueError: If the layout, blocks, and order violate an internal
            invariant.
    """
    expected_separator_count = max(0, len(order) - 1)
    if len(layout.separators) != expected_separator_count:
        raise ValueError('separator count must be one less than the block count')

    identities = set(order)
    if len(identities) != len(order):
        raise ValueError('order must not contain duplicate identities')
    if any(identity not in blocks for identity in order):
        raise ValueError('order contains an identity without a block')
    if any(identity not in identities for identity in blocks):
        raise ValueError('blocks contains an identity missing from order')

    if any(identity not in blocks for identity in layout.guard_required):
        raise ValueError('guard-required identity missing from blocks')

    rendered_separators = _allocate_ownership_guards(layout, order)

    parts = [layout.prefix]
    for position, identity in enumerate(order):
        parts.append(blocks[identity])
        if position < len(rendered_separators):
            parts.append(rendered_separators[position])
    parts.append(layout.suffix)
    return ''.join(parts)


def split_terminal_line_ending(content: str) -> tuple[str, str]:
    """Detach exactly one terminal physical line ending.

    Args:
        content: Source text to split.

    Returns:
        The unchanged body and its detached terminal line ending.
    """
    ending = terminal_line_ending(content)
    return content.removesuffix(ending), ending


def split_trailing_leading(content: str) -> tuple[str, str]:
    """Split inter-definition content into trailing and leading parts.

    Content before the last blank-line boundary stays as trailing (belongs to
    previous def). Content after becomes leading (belongs to next def). If no
    blank line exists, everything is leading.

    Args:
        content: Exact content between definitions.

    Returns:
        The trailing-owned and leading-owned portions.
    """
    lines = split_physical_lines(content, keepends=True)
    if all(is_layout_blank(line) for line in lines):
        return content, ''
    # Find last layout-blank line
    last_blank = -1
    for i, line in enumerate(lines):
        if is_layout_blank(line):
            last_blank = i
    if last_blank == -1:
        # No blank line -- all content is leading (comment block for next def)
        return '', content
    trailing = ''.join(lines[: last_blank + 1])
    leading = ''.join(lines[last_blank + 1 :])
    return trailing, leading


def split_trailing_owned(content: str) -> tuple[str, str]:
    """Detach positional whitespace-only lines from a trailing owned span.

    Args:
        content: Already classified trailing source text.

    Returns:
        The exact owned text and final positional whitespace-only lines.
    """
    lines = split_physical_lines(content, keepends=True)
    owned_line_count = len(lines)
    while owned_line_count > 0 and is_layout_blank(lines[owned_line_count - 1]):
        owned_line_count -= 1
    return ''.join(lines[:owned_line_count]), ''.join(lines[owned_line_count:])


def split_item_gap(content: str) -> _GapParts:
    """Split an inter-item gap into owned and positional source.

    Args:
        content: Exact source text between adjacent items.

    Returns:
        Trailing ownership, positional separator, and leading ownership.
    """
    trailing, leading = split_trailing_leading(content)
    trailing, separator = split_trailing_owned(trailing)
    return _GapParts(trailing, separator, leading)
