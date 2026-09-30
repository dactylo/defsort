"""Scope-neutral section banner detection and constraints."""

import ast
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from defsort._core._analysis import DIRECTIVE_PATTERN, ENCODING_PATTERN, get_node_end_line
from defsort._core._layout import split_item_gap, split_trailing_leading, split_trailing_owned
from defsort._source import (
    BANNER_RULE_CHARS,
    is_layout_blank,
    join_physical,
    join_physical_fragments,
    scope_indent_body,
    split_physical_lines,
)
from defsort._types import (
    BannerConstraint,
    BannerMode,
    BoundaryLayoutPart,
    Definition,
    HeldItem,
    MethodDefinition,
    SectionBoundary,
    validate_banner_pattern,
)

_RULE_LINE_PATTERN = re.compile(rf'#[\t ]*([{re.escape(BANNER_RULE_CHARS)}])\1{{3,}}[\t ]*')


@dataclass(frozen=True, slots=True)
class GapClassification:
    """Scope-neutral ownership and boundary classification for one exact gap.

    Attributes:
        trailing: Content owned by the preceding movable item.
        separator: Fixed content between the adjacent movable items.
        leading: Content owned by the following movable item.
        boundaries: Section boundaries found in the gap.
        boundary_layout: Exact fixed and boundary parts in source order.
        next_section: Section assigned to the following movable item.
        fixed_ordinary: Ordinary fixed content retained outside a boundary
            layout.
    """

    trailing: str
    separator: str
    leading: str
    boundaries: tuple[SectionBoundary, ...]
    boundary_layout: tuple[BoundaryLayoutPart, ...]
    next_section: int
    fixed_ordinary: str


class GapLocation(NamedTuple):
    """Absolute start line and indentation for one exact gap.

    Attributes:
        start_line: One-based source line where the gap begins.
        scope_indent: Exact indentation of the containing definition scope.
    """

    start_line: int
    scope_indent: str


@dataclass(frozen=True, slots=True)
class _BannerMatch:
    """Record one recognized banner's source coordinates and text."""

    start: int
    end: int
    start_line: int
    end_line: int
    normalized_text: str


def _banner_boundary_lookups(
    original: Sequence[Definition | MethodDefinition],
    boundaries: Sequence[SectionBoundary],
    target_count: int,
) -> tuple[dict[int, int], list[int]]:
    """Return original-section and target-position boundary lookups."""
    boundary_prefix_counts: list[int] = []
    item_cursor = 0
    for boundary in boundaries:
        while item_cursor < len(original) and original[item_cursor].section < boundary.section:
            item_cursor += 1
        boundary_prefix_counts.append(item_cursor)

    first_boundary_after_section: dict[int, int] = {}
    boundary_cursor = 0
    for item in original:
        while (
            boundary_cursor < len(boundaries)
            and boundaries[boundary_cursor].section <= item.section
        ):
            boundary_cursor += 1
        first_boundary_after_section[item.section] = boundary_cursor

    first_boundary_after_target: list[int] = []
    boundary_cursor = 0
    for target_position in range(target_count):
        while (
            boundary_cursor < len(boundaries)
            and boundary_prefix_counts[boundary_cursor] <= target_position
        ):
            boundary_cursor += 1
        first_boundary_after_target.append(boundary_cursor)
    return first_boundary_after_section, first_boundary_after_target


def _logical_comment(line: str, scope_indent: str) -> str | None:
    """Return one exact-scope logical comment without its line ending."""
    logical = scope_indent_body(line, scope_indent)
    if logical is None or not logical.startswith('#'):
        return None
    return logical


def _ordinary_gap(content: str, section: int) -> GapClassification:
    """Return ordinary ownership for a gap without active boundaries."""
    gap = split_item_gap(content)
    return GapClassification(
        trailing=gap.trailing,
        separator=gap.separator,
        leading=gap.leading,
        boundaries=(),
        boundary_layout=(),
        next_section=section,
        fixed_ordinary='',
    )


def _protected_block(lines: list[str]) -> bool:
    """Return whether a candidate block contains protected comments."""
    return any(
        line.startswith('#!')
        or ENCODING_PATTERN.match(line) is not None
        or DIRECTIVE_PATTERN.search(line) is not None
        for line in lines
    )


def _banner_matches(
    content: str,
    scope_indent: str,
    pattern: re.Pattern[str] | None,
    statement_lines: frozenset[int],
) -> list[_BannerMatch]:
    """Return recognized banner blocks in one scope-relative region."""
    lines = split_physical_lines(content, keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    matches: list[_BannerMatch] = []
    index = 0
    while index < len(lines):
        if index in statement_lines or _logical_comment(lines[index], scope_indent) is None:
            index += 1
            continue
        start = index
        logical_lines: list[str] = []
        while index < len(lines):
            if index in statement_lines:
                break
            logical = _logical_comment(lines[index], scope_indent)
            if logical is None:
                break
            logical_lines.append(logical)
            index += 1
        separated_before = start == 0 or is_layout_blank(lines[start - 1])
        separated_after = index == len(lines) or is_layout_blank(lines[index])
        matched = any(
            _RULE_LINE_PATTERN.fullmatch(line) is not None
            or (pattern is not None and pattern.fullmatch(line) is not None)
            for line in logical_lines
        )
        if (
            separated_before
            and separated_after
            and not _protected_block(logical_lines)
            and matched
        ):
            end_offset = offsets[index - 1] + len(lines[index - 1])
            matches.append(
                _BannerMatch(
                    start=offsets[start],
                    end=end_offset,
                    start_line=start,
                    end_line=index - 1,
                    normalized_text='\n'.join(logical_lines),
                )
            )
    return matches


def _extract_boundary_layout(
    content: str,
    *,
    location: GapLocation,
    first_section: int,
    pattern: re.Pattern[str] | None,
    statement_lines: frozenset[int] = frozenset(),
) -> tuple[str, tuple[BoundaryLayoutPart, ...], str]:
    """Return outer movable text and a lossless fixed boundary layout."""
    matches = _banner_matches(content, location.scope_indent, pattern, statement_lines)
    if not matches:
        return content, (), ''
    before = content[: matches[0].start]
    after = content[matches[-1].end :]
    layout: list[BoundaryLayoutPart] = []
    cursor = matches[0].start
    for section, match in enumerate(matches, start=first_section):
        if cursor < match.start:
            layout.append(content[cursor : match.start])
        layout.append(
            SectionBoundary(
                section=section,
                start_line=location.start_line + match.start_line,
                end_line=location.start_line + match.end_line,
                raw=content[match.start : match.end],
                text=match.normalized_text,
            )
        )
        cursor = match.end
    return before, tuple(layout), after


def _protected_prefix_end(content: str) -> int:
    """Return the character offset after the last directive comment block."""
    lines = split_physical_lines(content, keepends=True)
    last_directive = -1
    for index, line in enumerate(lines):
        if DIRECTIVE_PATTERN.search(line) is not None:
            last_directive = index
    if last_directive < 0:
        return 0

    block_end = last_directive + 1
    while block_end < len(lines) and not is_layout_blank(lines[block_end]):
        block_end += 1
    return sum(len(line) for line in lines[:block_end])


def apply_lossless_strip(
    source: str,
    definitions: Iterable[Definition],
    boundaries: Iterable[SectionBoundary],
) -> str:
    """Apply method source changes and top-level boundary removals exactly.

    Args:
        source: Original source text.
        definitions: Original-order definitions with any method source changes.
        boundaries: Top-level boundaries to remove.

    Returns:
        Source with only the requested definition and boundary replacements.
    """
    source_lines = split_physical_lines(source, keepends=True)
    replacements = [(boundary.start_line, boundary.end_line, '') for boundary in boundaries]
    replacements.extend(
        (definition.start_line, definition.end_line, definition.source)
        for definition in definitions
        if definition.source
        != ''.join(source_lines[definition.start_line - 1 : definition.end_line])
    )
    result_lines = list(source_lines)
    for start_line, end_line, replacement in sorted(
        replacements,
        key=lambda item: item[0],
        reverse=True,
    ):
        result_lines[start_line - 1 : end_line] = split_physical_lines(replacement, keepends=True)
    return join_physical_fragments(result_lines)


def classify_gap(
    content: str,
    *,
    location: GapLocation,
    section: int,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
    statement_lines: frozenset[int] = frozenset(),
) -> GapClassification:
    """Classify one exact inter-item gap without applying scope safety policy.

    Args:
        content: Exact source text between adjacent items.
        location: Absolute start line and exact scope indentation.
        section: Section assigned to the preceding item.
        banner_settings: Banner mode and optional custom pattern.
        statement_lines: Gap-relative lines owned by non-movable statements.

    Returns:
        Exact owned text, fixed layout, boundaries, and section metadata.
    """
    banners, pattern = banner_settings
    if banners == 'ignore':
        return _ordinary_gap(content, section)

    before, boundary_layout, after = _extract_boundary_layout(
        content,
        location=location,
        first_section=section + 1,
        pattern=pattern,
        statement_lines=statement_lines,
    )
    if not boundary_layout:
        return _ordinary_gap(content, section)

    boundaries = tuple(part for part in boundary_layout if isinstance(part, SectionBoundary))
    next_section = boundaries[-1].section
    if banners == 'strip':
        merged = before
        for part in boundary_layout:
            if isinstance(part, str):
                merged = join_physical(merged, part)
        merged = join_physical(merged, after)
        gap = split_item_gap(merged)
        return GapClassification(
            trailing=gap.trailing,
            separator=gap.separator,
            leading=gap.leading,
            boundaries=boundaries,
            boundary_layout=boundary_layout,
            next_section=next_section,
            fixed_ordinary='',
        )

    ordinary_between = ''.join(part for part in boundary_layout if isinstance(part, str))
    trailing, before_separator = split_trailing_owned(before)
    fixed_after, leading = split_trailing_leading(after)
    if fixed_after:
        boundary_layout = (*boundary_layout, fixed_after)
    fixed_boundary = ''.join(
        part.raw if isinstance(part, SectionBoundary) else part for part in boundary_layout
    )
    return GapClassification(
        trailing=trailing,
        separator=before_separator + fixed_boundary,
        leading=leading,
        boundaries=boundaries,
        boundary_layout=boundary_layout,
        next_section=next_section,
        fixed_ordinary=ordinary_between + after,
    )


def classify_initial_gap(
    content: str,
    *,
    content_start_line: int,
    scope_indent: str,
    banner_settings: tuple[BannerMode, re.Pattern[str] | None],
) -> GapClassification:
    """Classify the exact gap before the first item in a scope.

    Args:
        content: Exact source between the structural prefix and first item.
        content_start_line: Absolute line where `content` starts.
        scope_indent: Exact indentation for comments in this scope.
        banner_settings: Banner mode and optional custom pattern.

    Returns:
        Initial fixed bytes in `trailing`, the remaining positional
        `separator`, owned `leading`, and unchanged boundary metadata.
    """
    gap = classify_gap(
        content,
        location=GapLocation(content_start_line, scope_indent),
        section=0,
        banner_settings=banner_settings,
    )
    if DIRECTIVE_PATTERN.search(gap.leading) is not None:
        return replace(
            gap,
            trailing=gap.trailing + gap.separator + gap.leading,
            separator='',
            leading='',
        )

    protected_end = _protected_prefix_end(gap.separator)
    if protected_end == 0:
        return gap
    return replace(
        gap,
        trailing=gap.trailing + gap.separator[:protected_end],
        separator=gap.separator[protected_end:],
    )


def collect_banner_constraints[T: (Definition, MethodDefinition)](
    original: Sequence[T],
    unconstrained: Sequence[T],
    boundaries: Sequence[SectionBoundary],
    *,
    class_name: str | None = None,
) -> list[BannerConstraint]:
    """Return every original-side versus unconstrained-side crossing.

    Args:
        original: Items in current source order.
        unconstrained: Items in the global target order.
        boundaries: Scope boundaries in source order.
        class_name: Owning class for method scope, or `None` for definitions.

    Returns:
        One constraint for each crossed boundary.
    """
    target_positions = {item.index: position for position, item in enumerate(unconstrained)}
    seen: Counter[str] = Counter()
    occurrences: dict[int, int] = {}
    for item in original:
        seen[item.name] += 1
        occurrences[item.index] = seen[item.name]

    first_boundary_after_section, first_boundary_after_target = _banner_boundary_lookups(
        original,
        boundaries,
        len(unconstrained),
    )

    held_by_boundary = [list[HeldItem]() for _ in boundaries]
    for item in original:
        original_boundary = first_boundary_after_section[item.section]
        target_boundary = first_boundary_after_target[target_positions[item.index]]
        start = min(original_boundary, target_boundary)
        end = max(original_boundary, target_boundary)
        direction = 'before' if target_boundary < original_boundary else 'after'
        for boundary_index in range(start, end):
            held_by_boundary[boundary_index].append(
                HeldItem(
                    name=item.name,
                    kind='method' if isinstance(item, MethodDefinition) else item.kind,
                    occurrence=occurrences[item.index],
                    line=item.start_line,
                    direction=direction,
                )
            )

    constraints: list[BannerConstraint] = []
    for boundary_index, boundary in enumerate(boundaries):
        held = held_by_boundary[boundary_index]
        if held:
            constraints.append(
                BannerConstraint(
                    scope='methods' if class_name is not None else 'definitions',
                    class_name=class_name,
                    boundary=boundary,
                    held=tuple(held),
                )
            )
    return constraints


def compile_banner_pattern(pattern: str | None) -> re.Pattern[str] | None:
    """Compile one configured banner pattern for runtime matching.

    Args:
        pattern: Regular expression from the supported bounded subset, or
            `None`.

    Returns:
        The compiled regular expression, or `None`.

    Raises:
        TypeError: If `pattern` is neither a string nor `None`.
        ValueError: If the pattern does not compile or uses unsupported syntax.
            Compilation failures chain the underlying `re.error`.
    """
    validate_banner_pattern(pattern)
    return re.compile(pattern) if pattern is not None else None


def order_within_sections[T: Definition | MethodDefinition](
    items: Iterable[T],
    order_fn: Callable[[list[T]], list[T]],
) -> list[T]:
    """Order items independently within each source section.

    Args:
        items: Section-tagged items in source order.
        order_fn: Existing ordering function applied to each section.

    Returns:
        Ordered items without crossings between sections.
    """
    sections: dict[int, list[T]] = {}
    for item in items:
        sections.setdefault(item.section, []).append(item)
    return [item for section in sections.values() for item in order_fn(section)]


def remove_boundary_lines(
    source: str,
    boundaries: Iterable[SectionBoundary],
    *,
    source_start_line: int = 1,
) -> str:
    """Remove exact boundary line spans without changing other source bytes.

    Args:
        source: Source containing the recognized boundaries.
        boundaries: Absolute boundary line spans to remove.
        source_start_line: Absolute line number of the first source line.

    Returns:
        Source with only the recognized boundary lines removed.
    """
    lines = split_physical_lines(source, keepends=True)
    for boundary in sorted(boundaries, key=lambda item: item.start_line, reverse=True):
        start = boundary.start_line - source_start_line
        end = boundary.end_line - source_start_line + 1
        del lines[start:end]
    return join_physical_fragments(lines)


def statement_owned_lines_by_gap(
    body: Iterable[ast.stmt],
    gaps: Sequence[tuple[int, int]],
    *,
    movable_types: tuple[type[ast.AST], ...],
) -> tuple[frozenset[int], ...]:
    """Return statement-owned physical line indices for ordered gaps.

    Args:
        body: Direct statements of the owning scope.
        gaps: Ordered absolute first and last lines for each gap.
        movable_types: Scope item types that are not gap statements.

    Returns:
        One immutable set of 0-based gap-relative line indices per gap.
    """
    owned_by_gap = [set[int]() for _ in gaps]
    gap_cursor = 0
    for node in body:
        if isinstance(node, movable_types):
            continue
        node_start = node.lineno
        node_end = get_node_end_line(node)
        while gap_cursor < len(gaps) and gaps[gap_cursor][1] < node_start:
            gap_cursor += 1

        overlap_cursor = gap_cursor
        while overlap_cursor < len(gaps):
            gap_start, gap_end = gaps[overlap_cursor]
            if gap_start > node_end:
                break
            overlap_start = max(node_start, gap_start)
            overlap_end = min(node_end, gap_end)
            if overlap_start <= overlap_end:
                owned_by_gap[overlap_cursor].update(
                    line - gap_start for line in range(overlap_start, overlap_end + 1)
                )
            overlap_cursor += 1
    return tuple(frozenset(owned) for owned in owned_by_gap)
