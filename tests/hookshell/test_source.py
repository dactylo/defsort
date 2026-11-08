from defsort._vendor.hookshell._source import (
    SourceSpan,
    SourceText,
    join_sources,
    resolve_span,
)


def test_argument_join_keeps_disjoint_locations_and_zero_width_spaces() -> None:
    body = SourceText("'git'\n'commit'", 'heredoc')
    first = SourceText('git', 'literal', body, 1)
    second = SourceText('commit', 'literal', body, 7)
    joined = join_sources((first, second), context='eval')
    assert joined is not None
    assert joined.text == 'git commit'
    assert resolve_span(SourceSpan(joined, 0, 3), context='heredoc') == SourceSpan(body, 1, 4)
    assert resolve_span(SourceSpan(joined, 3, 4), context='heredoc') == SourceSpan(body, 4, 4)
    assert resolve_span(SourceSpan(joined, 4, 10), context='heredoc') == SourceSpan(body, 7, 13)
    assert join_sources((first, None), context='eval') is None


def test_context_resolution_uses_nearest_matching_ancestor() -> None:
    outer = SourceText('outer', 'heredoc')
    inner = SourceText('ute', 'heredoc', outer, 1)
    scan = SourceText('t', 'script', inner, 1)
    assert resolve_span(SourceSpan(scan, 0, 1), context='heredoc') == SourceSpan(inner, 1, 2)


def test_identity_view_resolves_nearest_context() -> None:
    command = SourceText('prefix\nbody\n')
    body = SourceText('body\n', 'heredoc', command, 7)
    nested = SourceText('ody', 'script', body, 1)
    assert resolve_span(SourceSpan(nested, 0, 3), context='heredoc') == SourceSpan(body, 1, 4)
    assert resolve_span(SourceSpan(nested, 0, 3), context='command') == SourceSpan(command, 8, 11)
    assert resolve_span(SourceSpan(command, 0, 1), context='heredoc') is None


def test_join_does_not_conflate_equal_but_unrelated_roots() -> None:
    assert join_sources((SourceText('same'), SourceText('same')), context='eval') is None


def test_nonempty_and_empty_spans_use_distinct_mapping_boundaries() -> None:
    parent = SourceText('a\n\nb', 'heredoc')
    child = SourceText('ab', 'decoded', parent, offsets=(0, 3, 4), end_offsets=(1, 4))
    assert resolve_span(SourceSpan(child, 0, 1), context='heredoc') == SourceSpan(parent, 0, 1)
    assert resolve_span(SourceSpan(child, 1, 1), context='heredoc') == SourceSpan(parent, 3, 3)
