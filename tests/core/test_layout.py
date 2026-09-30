from collections import Counter

import pytest

import defsort._core._layout as layout_module
from defsort._core._layout import (
    OwnershipBoundary,
    SourceLayout,
    _GapParts,
    render_layout,
    split_item_gap,
    split_terminal_line_ending,
    split_trailing_leading,
    split_trailing_owned,
)
from defsort._source import split_physical_lines


def test_make_ownership_boundary_records_exact_candidate() -> None:
    boundary = layout_module.make_ownership_boundary(
        '\r\n',
        ' \t\n# fixed\r',
        owns_trailing=True,
    )

    assert boundary == OwnershipBoundary(terminator_length=2, has_candidate=True)


def test_render_layout_keeps_candidate_when_owner_moves_to_final_slot() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\r\n', '\r# fixed\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 2, 0])

    assert rendered == 'one\n \t\r\ntwo\r# fixed\nzero'


def test_render_layout_keeps_extra_blank_lines_in_authored_separator() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\n\f\r\n# source\n', '\r# target\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 0, 2])

    assert rendered == 'one\n\f\r\n# source\nzero\r \t\n# target\ntwo'


def test_render_layout_moves_candidates_in_source_position_order() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\n', '\r\n\f\r\n', '\r# two\n', '\n# three\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=2, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0, 1}),
    )

    rendered = render_layout(
        layout,
        {0: 'zero', 1: 'one', 2: 'two', 3: 'three', 4: 'four'},
        [2, 3, 0, 1, 4],
    )

    assert rendered == 'two\nthree\r\nzero\r \t\n# two\none\n\f\r\n# three\nfour'


def test_render_layout_moves_multiple_owner_guards() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\n', '\r\n\f\r', '\r# fixed\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=2, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0, 1}),
    )

    rendered = render_layout(
        layout,
        {0: 'zero', 1: 'one', 2: 'two', 3: 'three'},
        [0, 2, 1, 3],
    )

    assert rendered == 'zero\n \t\ntwo\r\none\r\f\r# fixed\nthree'


def test_render_layout_moves_one_exact_guard_to_missing_destination() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\r\n', '\r# fixed\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 0, 2])

    assert rendered == 'one\nzero\r \t\r\n# fixed\ntwo'


def test_render_layout_preserves_identity_order_with_ownership_metadata() -> None:
    layout = SourceLayout(
        prefix='prefix',
        separators=('\n \t\r\n', '\r# fixed\n'),
        suffix='suffix',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [0, 1, 2])

    assert rendered == 'prefixzero\n \t\r\none\r# fixed\ntwosuffix'


def test_render_layout_preserves_two_endings_at_mixed_insertion_seam() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n\n', '\r'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )
    blocks = {0: 'zero', 1: 'one', 2: 'two'}
    original = render_layout(layout, blocks, [0, 1, 2])

    rendered = render_layout(layout, blocks, [1, 0, 2])

    assert rendered == 'one\nzero\n\rtwo'
    assert Counter(rendered.encode()) == Counter(original.encode())
    assert split_physical_lines(rendered, keepends=True) == ['one\n', 'zero\n', '\r', 'two']


def test_render_layout_preserves_two_endings_at_mixed_removal_seam() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\r \t\n\n', '\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )
    blocks = {0: 'zero', 1: 'one', 2: 'two'}
    original = render_layout(layout, blocks, [0, 1, 2])

    rendered = render_layout(layout, blocks, [1, 0, 2])

    assert rendered == 'one\n\rzero\n \t\ntwo'
    assert rendered.removeprefix('one').partition('zero')[0] == '\n\r'
    assert rendered.partition('zero')[2].removesuffix('two') == '\n \t\n'
    assert Counter(rendered.encode()) == Counter(original.encode())
    assert split_physical_lines(rendered, keepends=True) == [
        'one\n',
        '\r',
        'zero\n',
        ' \t\n',
        'two',
    ]


def test_render_layout_rejects_candidate_without_complete_blank_line() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n# not blank\n',),
        suffix='',
        ownership=(OwnershipBoundary(terminator_length=1, has_candidate=True),),
    )

    with pytest.raises(ValueError, match='complete layout-blank physical line'):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


def test_render_layout_rejects_guard_identity_missing_from_blocks() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n',),
        suffix='',
        ownership=(OwnershipBoundary(terminator_length=1, has_candidate=False),),
        guard_required=frozenset({2}),
    )

    with pytest.raises(ValueError, match='guard-required identity missing from blocks'):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


@pytest.mark.parametrize(
    ('layout', 'blocks', 'order', 'message'),
    [
        (
            SourceLayout('', (), ''),
            {0: 'A', 1: 'B'},
            [0, 1],
            'separator count must be one less than the block count',
        ),
        (SourceLayout('', (), ''), {}, [0], 'order contains an identity without a block'),
        (
            SourceLayout('', ('|',), ''),
            {0: 'A'},
            [0, 0],
            'order must not contain duplicate identities',
        ),
        (
            SourceLayout('', (), ''),
            {0: 'A', 1: 'B'},
            [0],
            'blocks contains an identity missing from order',
        ),
    ],
    ids=['separator-count', 'missing-identity', 'duplicate-identity', 'extra-block'],
)
def test_render_layout_rejects_invalid_invariants(
    layout: SourceLayout,
    blocks: dict[int, str],
    order: list[int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        render_layout(layout, blocks, order)


@pytest.mark.parametrize(
    ('separator', 'terminator_length'),
    [
        ('\n', 0),
        ('\n', 2),
        ('\r\n', 1),
    ],
    ids=['zero', 'oversized', 'split-crlf'],
)
def test_render_layout_rejects_invalid_terminator_length(
    separator: str,
    terminator_length: int,
) -> None:
    layout = SourceLayout(
        prefix='',
        separators=(separator,),
        suffix='',
        ownership=(OwnershipBoundary(terminator_length, has_candidate=False),),
    )

    with pytest.raises(ValueError, match='complete physical ending'):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


def test_render_layout_rejects_missing_leading_terminator() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('# fixed\n',),
        suffix='',
        ownership=(OwnershipBoundary(terminator_length=0, has_candidate=False),),
    )

    with pytest.raises(ValueError, match='complete physical ending'):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


def test_render_layout_rejects_ownership_count_mismatch() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n',),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=False),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
    )

    with pytest.raises(ValueError, match='ownership metadata count must match separator count'):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


def test_render_layout_rejects_unsatisfied_guard_without_candidate() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n# fixed\n',),
        suffix='',
        ownership=(OwnershipBoundary(terminator_length=1, has_candidate=False),),
        guard_required=frozenset({0}),
    )

    with pytest.raises(
        layout_module.LayoutPreservationError, match='required ownership guard has no candidate'
    ):
        render_layout(layout, {0: 'zero', 1: 'one'}, [0, 1])


def test_render_layout_reproduces_original_identity_order() -> None:
    layout = SourceLayout(
        prefix='prefix\r\n',
        separators=('\n \t\r\n# fixed\r',),
        suffix='\n# suffix',
    )

    rendered = render_layout(layout, {0: 'first', 1: 'second'}, [0, 1])

    assert rendered == 'prefix\r\nfirst\n \t\r\n# fixed\rsecond\n# suffix'


def test_render_layout_required_donor_keeps_second_guard() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n \t\r\n\f\n', '\r# fixed\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=1, has_candidate=False),
        ),
        guard_required=frozenset({0, 1}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 0, 2])

    assert rendered == 'one\n\f\nzero\r \t\r\n# fixed\ntwo'


def test_render_layout_reuses_candidate_at_destination() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n# fixed\n', '\r\n \t\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=False),
            OwnershipBoundary(terminator_length=2, has_candidate=True),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 0, 2])

    assert rendered == 'one\n# fixed\nzero\r\n \t\ntwo'


def test_render_layout_reuses_ordinary_blank_line_at_destination() -> None:
    layout = SourceLayout(
        prefix='',
        separators=('\n\f\r', '\r\n \t\n# fixed\n'),
        suffix='',
        ownership=(
            OwnershipBoundary(terminator_length=1, has_candidate=True),
            OwnershipBoundary(terminator_length=2, has_candidate=False),
        ),
        guard_required=frozenset({0}),
    )

    rendered = render_layout(layout, {0: 'zero', 1: 'one', 2: 'two'}, [1, 0, 2])

    assert rendered == 'one\n\f\rzero\r\n \t\n# fixed\ntwo'


def test_render_layout_reverses_only_block_placement() -> None:
    layout = SourceLayout(
        prefix='prefix\r\n',
        separators=('\n \t\r\n# fixed\r',),
        suffix='\n# suffix',
    )

    rendered = render_layout(layout, {0: 'first', 1: 'second'}, [1, 0])

    assert rendered == 'prefix\r\nsecond\n \t\r\n# fixed\rfirst\n# suffix'


@pytest.mark.parametrize(
    ('separator', 'expected'),
    [
        ('', 'PABS'),
        ('\t ', 'PA\t BS'),
        ('\r\n', 'PA\r\nBS'),
        ('\r', 'PA\rBS'),
        ('# fixed\r\n', 'PA# fixed\r\nBS'),
    ],
    ids=['empty', 'tabs-and-spaces', 'crlf', 'cr', 'comment'],
)
def test_render_layout_treats_separator_as_opaque(separator: str, expected: str) -> None:
    layout = SourceLayout(prefix='P', separators=(separator,), suffix='S')

    assert render_layout(layout, {0: 'A', 1: 'B'}, [0, 1]) == expected


def test_source_layout_accepts_empty_ownership_metadata() -> None:
    layout = SourceLayout(
        prefix='prefix',
        separators=(),
        suffix='suffix',
        ownership=(),
        guard_required=frozenset(),
    )

    assert render_layout(layout, {}, []) == 'prefixsuffix'


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (' \t\r\n\r', _GapParts('', ' \t\r\n\r', '')),
        ('# trailing\n\n\n# leading\n', _GapParts('# trailing\n', '\n\n', '# leading\n')),
        ('# leading\r\n', _GapParts('', '', '# leading\r\n')),
        (
            '# trailing\r\n \t\r\n# leading\r\n',
            _GapParts('# trailing\r\n', ' \t\r\n', '# leading\r\n'),
        ),
    ],
    ids=['whitespace-only', 'trailing-and-leading', 'leading-only', 'filled-blank-line'],
)
def test_split_item_gap(content: str, expected: _GapParts) -> None:
    assert split_item_gap(content) == expected


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        ('body\n', ('body', '\n')),
        ('body\r\n', ('body', '\r\n')),
        ('body\r', ('body', '\r')),
        ('body', ('body', '')),
        ('', ('', '')),
        (' \t', (' \t', '')),
    ],
    ids=['lf', 'crlf', 'cr', 'unterminated', 'empty', 'whitespace-tail'],
)
def test_split_terminal_line_ending(
    content: str,
    expected: tuple[str, str],
) -> None:
    assert split_terminal_line_ending(content) == expected


def test_split_trailing_leading_keeps_ordinary_characters_in_one_comment() -> None:
    content = '\n# note\x1c\t\n# second\n'
    assert split_trailing_leading(content) == ('\n', '# note\x1c\t\n# second\n')


def test_split_trailing_leading_treats_form_feed_line_as_blank() -> None:
    content = '# tail\n\f\n# lead\n'
    assert split_trailing_leading(content) == ('# tail\n\f\n', '# lead\n')


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        ('', ('', '')),
        (' \t', ('', ' \t')),
        ('# trailing\n\n', ('# trailing\n', '\n')),
        ('# trailing\r\n \t\r\n\r', ('# trailing\r\n', ' \t\r\n\r')),
    ],
    ids=['empty', 'unterminated-whitespace', 'one-blank-line', 'multiple-blank-lines'],
)
def test_split_trailing_owned(
    content: str,
    expected: tuple[str, str],
) -> None:
    assert split_trailing_owned(content) == expected


def test_split_trailing_owned_keeps_ordinary_characters_in_owned_line() -> None:
    assert split_trailing_owned('# own\x1c\t\n') == ('# own\x1c\t\n', '')
