"""Retain source locations across shell text decoding and concatenation."""

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceText:
    """Represent immutable text with an optional mapping into a parent view.

    Mapping arrays use parent-view character offsets. When supplied, `offsets`
    has one entry per character plus the final boundary;
    `end_offsets` has one exclusive end per character.
    Source-resolution operations identify ancestors by object identity.

    Attributes:
        text: Text represented by this view.
        context: Label used to find an enclosing view during span resolution.
        parent: Enclosing source view, or `None` for a root.
        offset: Parent start of an identity slice when `offsets` is absent.
        offsets: Parent starts and final boundary, or `None` for an identity
            slice.
        end_offsets: Exclusive parent ends for discontiguous mappings, or
            `None` to use the following boundary from `offsets`.
    """

    text: str
    context: str = 'command'
    parent: 'SourceText | None' = None
    offset: int = 0
    offsets: tuple[int, ...] | None = None
    end_offsets: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Half-open character interval in one source view."""

    source: SourceText
    start: int
    end: int


def _parent_span(span: SourceSpan) -> SourceSpan | None:
    """Map a span one level toward the original source."""
    source = span.source
    if source.parent is None:
        return None
    if source.offsets is None:
        return SourceSpan(source.parent, source.offset + span.start, source.offset + span.end)
    start = source.offsets[span.start]
    end = (
        source.end_offsets[span.end - 1]
        if source.end_offsets is not None and span.end > span.start
        else source.offsets[span.end]
    )
    return SourceSpan(source.parent, start, end)


def _span_in(span: SourceSpan, ancestor: SourceText) -> SourceSpan:
    """Resolve a span to a known ancestor by identity."""
    while span.source is not ancestor:
        parent = _parent_span(span)
        if parent is None:
            raise ValueError('source view is not a descendant of the requested ancestor')
        span = parent
    return span


def join_sources(
    sources: Collection[SourceText | None], *, context: str, separator: str = ' '
) -> SourceText | None:
    """Join mapped text, anchoring synthetic separators at the preceding end.

    Args:
        sources: Views of the joined arguments or literal parts. Missing
            metadata or unrelated roots yield no source view.
        context: Context name for the resulting scan view.
        separator: Synthetic text between views; every character maps to an
            empty span at the preceding argument's end.

    Returns:
        A view mapped to the nearest shared ancestor, or `None` when provenance
        cannot be established. Empty input has no provenance.
    """
    if not sources or any(source is None for source in sources):
        return None
    views = [source for source in sources if source is not None]
    if len(views) == 1:
        return SourceText(views[0].text, context, views[0])
    ancestor = views[0]
    for view in views[1:]:
        lineage: set[int] = set()
        cursor: SourceText | None = view
        while cursor is not None:
            lineage.add(id(cursor))
            cursor = cursor.parent
        while id(ancestor) not in lineage:
            if ancestor.parent is None:
                return None
            ancestor = ancestor.parent
    starts: list[int] = []
    ends: list[int] = []
    boundary = _span_in(SourceSpan(views[0], 0, 0), ancestor).start
    for index, view in enumerate(views):
        if index:
            starts.extend([boundary] * len(separator))
            ends.extend([boundary] * len(separator))
        for position in range(len(view.text)):
            span = _span_in(SourceSpan(view, position, position + 1), ancestor)
            starts.append(span.start)
            ends.append(span.end)
        boundary = _span_in(SourceSpan(view, len(view.text), len(view.text)), ancestor).start
    return SourceText(
        separator.join(view.text for view in views),
        context,
        ancestor,
        offsets=(*starts, boundary),
        end_offsets=tuple(ends),
    )


def resolve_span(span: SourceSpan, *, context: str) -> SourceSpan | None:
    """Resolve to the nearest view with the requested context, or `None` if absent."""
    while span.source.context != context:
        parent = _parent_span(span)
        if parent is None:
            return None
        span = parent
    return span
