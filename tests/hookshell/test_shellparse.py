import pytest

from defsort._vendor.hookshell._shellparse import (
    Command,
    ExpansionPart,
    GlobPart,
    Heredoc,
    LiteralPart,
    Operator,
    SubstitutionPart,
    Word,
    _Comment,
    parse_command,
    parse_source,
)
from defsort._vendor.hookshell._source import (
    SourceSpan,
    SourceText,
    join_sources,
    resolve_span,
)


class TestSourceMapping:
    def test_added_newline_in_unterminated_body_has_zero_width_source(self) -> None:
        command = parse_command('cat <<EOF\nbody')
        body = next(e for e in command.segments[0].elements if isinstance(e, Heredoc))
        assert body.source is not None
        assert body.source.text == 'body\n'
        span = resolve_span(SourceSpan(body.source, 4, 5), context='heredoc')
        assert span is not None
        assert span.start == span.end == 4

    def test_evaluator_join_parses_both_arguments_at_original_body_lines(self) -> None:
        source = SourceText("'git'\n'commit'", context='heredoc')
        words = [
            element
            for segment in parse_source(source).segments
            for element in segment.elements
            if isinstance(element, Word)
        ]
        joined = join_sources([word.literal_source for word in words], context='eval')
        assert joined is not None
        reparsed = parse_source(joined)
        for word, expected_line in zip(reparsed.segments[0].elements, (1, 2), strict=True):
            assert isinstance(word, Word)
            assert word.span is not None
            span = resolve_span(word.span, context='heredoc')
            assert span is not None
            assert span.source.text[: span.start].count('\n') + 1 == expected_line
            assert span.source.text[span.start : span.end] == word.literal_text

    def test_hand_built_word_has_no_invented_source(self) -> None:
        word = Word('git', (LiteralPart('git', quoted=False),))
        assert word.literal_text == 'git'
        assert word.literal_source is None
        assert word.span is None

    def test_late_recovery_tab_removal_keeps_shared_body_ownership(self) -> None:
        text = "cat <<'MISSING'\nfirst\ncat <<-ALSO_MISSING\n\tgit commit"
        command = parse_command(text, recover_heredocs=True)
        first_body = command.segments[0].elements[-1]
        later_body = command.segments[2].elements[-1]
        assert isinstance(first_body, Heredoc)
        assert isinstance(later_body, Heredoc)
        assert first_body.body == 'first\ncat <<-ALSO_MISSING\n\tgit commit'
        assert later_body.body == ''
        word = command.segments[-1].elements[-1]
        assert isinstance(word, Word)
        assert word.span is not None
        span = resolve_span(word.span, context='heredoc')
        assert span is not None
        assert span.source is first_body.source
        assert span.source.text[: span.start].count('\n') + 1 == 3
        assert span.source.text[span.start : span.end] == 'commit'
        original = resolve_span(word.span, context='command')
        assert original is not None
        assert original.source.text == text
        assert text[original.start : original.end] == 'commit'

    @pytest.mark.parametrize('strip_tabs', [False, True])
    def test_recovered_words_keep_body_locations(self, *, strip_tabs: bool) -> None:
        operator = '<<-' if strip_tabs else '<<'
        text = f'cat {operator}EOF\n\tfirst\n\tgit commit'
        command = parse_command(text, recover_heredocs=True)
        body = next(e for e in command.segments[0].elements if isinstance(e, Heredoc))
        word = command.segments[-1].elements[-1]
        assert isinstance(word, Word)
        assert word.span is not None
        span = resolve_span(word.span, context='heredoc')
        assert span is not None
        assert span.source.text[: span.start].count('\n') + 1 == 2
        assert span.source.text[span.start : span.end] == 'commit'
        root = resolve_span(word.span, context='command')
        assert root is not None
        assert root.source.text == text
        assert text[root.start : root.end] == 'commit'
        assert body.body_inlined

    @pytest.mark.parametrize('text', ["'gi't", 'gi\\\nt', '"gi\\\nt"'])
    def test_literal_decoding_preserves_character_positions(self, text: str) -> None:
        word = _word(text)
        assert word.literal_source is not None
        resolved = resolve_span(SourceSpan(word.literal_source, 2, 3), context='command')
        assert resolved is not None
        assert text[resolved.start : resolved.end] == 't'
        assert word == Word(text, word.parts, span=None)

    def test_repeated_heredocs_keep_distinct_body_occurrences(self) -> None:
        text = "cat <<'EOF'; cat <<'EOF'\ngit commit\nEOF\ngit commit\nEOF\n"
        bodies = [
            element
            for segment in parse_command(text).segments
            for element in segment.elements
            if isinstance(element, Heredoc)
        ]
        assert len(bodies) == 2
        first_start = len("cat <<'EOF'; cat <<'EOF'\n")
        second_start = first_start + len('git commit\nEOF\n')
        for body, start in zip(bodies, (first_start, second_start), strict=True):
            assert body.source is not None
            resolved = resolve_span(SourceSpan(body.source, 0, 10), context='command')
            assert resolved is not None
            assert resolved.start == start
            assert text[resolved.start : resolved.end] == 'git commit'
        assert bodies[0] == bodies[1]

    def test_tabs_and_backtick_decoding_resolve_body_local_line(self) -> None:
        command = parse_command('cat <<-EOF\n\tfirst\n\t`gi\\\nt commit`\nEOF\n')
        body = next(e for e in command.segments[0].elements if isinstance(e, Heredoc))
        part = body.expansions[0]
        word = part.command.segments[0].elements[1]
        assert isinstance(word, Word)
        assert word.span is not None
        resolved = resolve_span(word.span, context='heredoc')
        assert resolved is not None
        assert resolved.source.text[: resolved.start].count('\n') + 1 == 3
        assert resolved.source.text[resolved.start : resolved.end] == 'commit'


class TestLexicalRules:
    def test_backslash_escapes_next_character_outside_quotes(self) -> None:
        assert _word('a\\ b').parts == (
            LiteralPart('a', quoted=False),
            LiteralPart(' ', quoted=True),
            LiteralPart('b', quoted=False),
        )

    def test_backslash_line_feed_continues_the_line(self) -> None:
        assert _words('git commit -m "x" \\\n    -- a.py') == [
            'git',
            'commit',
            '-m',
            'x',
            '--',
            'a.py',
        ]
        assert _words('gi\\\nt commit') == ['git', 'commit']

    def test_carriage_return_is_literal(self) -> None:
        assert _word('"x\ry"').literal_text == 'x\ry'
        assert _words('git commit\r-m x') == ['git', 'commit\r-m', 'x']

    def test_comment_starts_only_at_word_start(self) -> None:
        command = parse_command('ls a#b # git commit\nls')
        first = command.segments[0].elements
        assert [type(element) for element in first] == [Word, Word, _Comment]
        assert first[2] == _Comment('# git commit')
        assert first[1] == Word('a#b', (LiteralPart('a#b', quoted=False),))

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('"\\$x"', '$x'),
            ('"\\`x"', '`x'),
            ('"\\"x"', '"x'),
            ('"\\\\x"', '\\x'),
            ('"\\nx"', '\\nx'),
        ],
    )
    def test_double_quoted_backslash_escapes(self, text: str, expected: str) -> None:
        assert _word(text).literal_text == expected

    def test_double_quotes_keep_text_literal_except_dynamic_characters(self) -> None:
        word = _word('"a $B c"')
        assert word.parts == (
            LiteralPart('a ', quoted=True),
            ExpansionPart('$B', in_double_quotes=True),
            LiteralPart(' c', quoted=True),
        )

    def test_empty_quotes_join_literal_runs(self) -> None:
        word = _word('g""it')
        assert word.literal_text == 'git'
        assert word.parts == (
            LiteralPart('g', quoted=False),
            LiteralPart('', quoted=True),
            LiteralPart('it', quoted=False),
        )

    def test_empty_word_keeps_one_empty_part(self) -> None:
        assert _word('""').parts == (LiteralPart('', quoted=True),)
        assert _word("''").parts == (LiteralPart('', quoted=True),)

    @pytest.mark.parametrize(
        ('text', 'quoted'),
        [
            ('"$TMPDIR/x"', True),
            ('"${TMPDIR}/x"', True),
            ('"a $B"', True),
            ('$TMPDIR/x', False),
            ('${TMPDIR}/x', False),
            ('~/x', False),
        ],
    )
    def test_expansion_records_double_quotes(self, text: str, *, quoted: bool) -> None:
        part = next(part for part in _word(text).parts if isinstance(part, ExpansionPart))
        assert part.in_double_quotes is quoted

    def test_operator_characters_never_join_a_word(self) -> None:
        command = parse_command('a>b')
        elements = command.segments[0].elements
        assert elements == (
            Word('a', (LiteralPart('a', quoted=False),)),
            Operator('>'),
            Word('b', (LiteralPart('b', quoted=False),)),
        )

    def test_single_quotes_are_literal(self) -> None:
        word = _word("'$(git commit) `x` \\'")
        assert word.parts == (LiteralPart('$(git commit) `x` \\', quoted=True),)

    def test_whitespace_separates_words(self) -> None:
        assert _words('git  commit\t-m x') == ['git', 'commit', '-m', 'x']


class TestTree:
    @pytest.mark.parametrize('body', [r'g\\it commit -m x', r'git com\\mit -m x'])
    @pytest.mark.parametrize('quoted', [False, True])
    @pytest.mark.parametrize('closed', [False, True])
    def test_backtick_escape_layer_preserves_command_and_provenance(
        self, body: str, *, quoted: bool, closed: bool
    ) -> None:
        raw = '`' + body + ('`' if closed else '')
        text = ('"' + raw + ('"' if closed else '')) if quoted else raw
        command = parse_command(text)
        word = command.segments[0].elements[0]
        assert isinstance(word, Word)
        part = word.parts[0]
        assert isinstance(part, SubstitutionPart)
        assert part.raw == raw
        assert part.in_double_quotes == quoted
        assert [
            element.literal_text
            for element in part.command.segments[0].elements
            if isinstance(element, Word)
        ] == ['git', 'commit', '-m', 'x']
        assert (not command.recovery) == closed
        if not closed:
            assert command.recovery[0] == 'unterminated backtick'

    def test_backtick_escaped_backticks_form_a_nested_substitution(self) -> None:
        part = _word(r'`echo \`git commit -m x\``').parts[0]
        assert isinstance(part, SubstitutionPart)
        word = part.command.segments[0].elements[1]
        assert isinstance(word, Word)
        nested = word.parts[0]
        assert isinstance(nested, SubstitutionPart)
        assert nested.raw == '`git commit -m x`'
        assert not nested.command.recovery

    def test_backtick_escaped_dollar_starts_a_nested_expansion(self) -> None:
        part = _word(r'`echo \$VALUE`').parts[0]
        assert isinstance(part, SubstitutionPart)
        word = part.command.segments[0].elements[1]
        assert isinstance(word, Word)
        assert word.parts == (ExpansionPart('$VALUE'),)

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [(r'`echo \"x\"`', '"x"'), (r'"`echo \"x\"`"', 'x')],
    )
    def test_backtick_quote_escape_depends_on_outer_quotes(self, text: str, expected: str) -> None:
        part = _word(text).parts[0]
        assert isinstance(part, SubstitutionPart)
        word = part.command.segments[0].elements[1]
        assert isinstance(word, Word)
        assert word.literal_text == expected

    def test_backtick_substitution(self) -> None:
        part = _word('`git commit -m x`').parts[0]
        assert isinstance(part, SubstitutionPart)
        assert part.raw == '`git commit -m x`'
        assert [
            element.literal_text
            for element in part.command.segments[0].elements
            if isinstance(element, Word)
        ] == ['git', 'commit', '-m', 'x']

    def test_bare_dollar_is_literal(self) -> None:
        assert _word('a$').literal_text == 'a$'

    def test_clean_tree_has_no_recovery(self) -> None:
        assert not parse_command('git commit -m x').recovery

    def test_empty_segment_with_operator_terminator_is_kept(self) -> None:
        command = parse_command('a ; ; b')
        assert [segment.elements for segment in command.segments][1] == ()

    def test_escaped_heredoc_dollar_is_not_an_expansion(self) -> None:
        command = parse_command('cat <<EOF\n\\$(git commit -m x)\nEOF')
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert heredoc.expansions == ()

    def test_expansion_retains_nested_substitution_children(self) -> None:
        part = _word('"${VALUE:-$(git commit -m x)}"').parts[0]
        assert isinstance(part, ExpansionPart)
        assert len(part.children) == 1
        assert part.children[0].raw == '$(git commit -m x)'
        assert part.children[0].in_double_quotes

        part = _word('${VALUE:-$(git commit -m x)}').parts[0]
        assert isinstance(part, ExpansionPart)
        assert not part.children[0].in_double_quotes

    def test_heredoc_body_follows_the_operator_line(self) -> None:
        command = parse_command('cat <<EOF && echo after\nbody\nEOF\necho done')
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert heredoc.body == 'body\n'
        after = command.segments[1].elements[1]
        assert isinstance(after, Word)
        assert after.literal_text == 'after'

    def test_heredoc_dash_strips_tabs(self) -> None:
        command = parse_command('cat <<-EOF\n\tbody\n\tEOF')
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc('EOF', quoted=False, body='body\n')

    def test_heredoc_expansion_uses_structural_boundaries(self) -> None:
        command = parse_command("cat <<EOF\n'$(printf ')'; git commit -m x)'\nEOF")
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert len(heredoc.expansions) == 1
        part = heredoc.expansions[0]
        assert part.raw == "$(printf ')'; git commit -m x)"
        assert len(part.command.segments) == 2
        assert not command.recovery

    def test_heredoc_inside_substitution(self) -> None:
        part = _word('"$(cat <<\'EOF\'\nfeat: x\n\nbody\nEOF\n)"').parts[0]
        assert isinstance(part, SubstitutionPart)
        assert part.in_double_quotes
        segment = part.command.segments[0]
        assert segment.terminator == '\n'
        assert segment.elements[2] == Heredoc('EOF', quoted=True, body='feat: x\n\nbody\n')

    def test_heredoc_with_continued_unquoted_delimiter(self) -> None:
        command = parse_command("cat <<EO\\\nF\n'$(printf expanded)'\nEOF\n")
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert heredoc.delimiter == 'EOF'
        assert not heredoc.quoted
        assert heredoc.body == "'$(printf expanded)'\n"
        assert [part.raw for part in heredoc.expansions] == ['$(printf expanded)']
        assert not command.recovery

    def test_heredoc_with_double_quoted_dynamic_delimiter(self) -> None:
        command = parse_command('cat <<"$EOF"\n$(git commit -m x)\n$EOF\n')
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc('$EOF', quoted=True, body='$(git commit -m x)\n')
        assert not command.recovery

    def test_heredoc_with_escaped_delimiter(self) -> None:
        command = parse_command('cat <<E\\OF\n$(printf hidden)\nEOF\n')
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc('EOF', quoted=True, body='$(printf hidden)\n')
        assert not command.recovery

    def test_heredoc_with_quoted_delimiter(self) -> None:
        command = parse_command("cat <<'EOF'\nline one\n$(git commit)\nEOF\necho done")
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc('EOF', quoted=True, body='line one\n$(git commit)\n')
        following = command.segments[1].elements[0]
        assert isinstance(following, Word)
        assert following.literal_text == 'echo'

    def test_heredoc_with_unquoted_delimiter_records_expansions(self) -> None:
        command = parse_command("cat <<EOF\n'$(git commit -m x)' `true`\nEOF")
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert not heredoc.quoted
        assert [part.raw for part in heredoc.expansions] == ['$(git commit -m x)', '`true`']

    def test_heredoc_with_unquoted_dynamic_delimiter(self) -> None:
        command = parse_command('cat <<$EOF\n$(git commit -m x)\n$EOF\n')
        heredoc = command.segments[0].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert heredoc.delimiter == '$EOF'
        assert not heredoc.quoted
        assert [part.raw for part in heredoc.expansions] == ['$(git commit -m x)']
        assert not command.recovery

    def test_line_feed_terminates_and_blank_lines_collapse(self) -> None:
        command = parse_command('\na\n\n\nb\n')
        assert [segment.terminator for segment in command.segments] == ['\n', '\n']
        assert [segment.elements[0] for segment in command.segments] == [
            Word('a', (LiteralPart('a', quoted=False),)),
            Word('b', (LiteralPart('b', quoted=False),)),
        ]

    def test_parentheses_inside_substitution_balance(self) -> None:
        part = _word('$((1+2))').parts[0]
        assert isinstance(part, SubstitutionPart)
        assert part.raw == '$((1+2))'
        assert part.command.segments[0].elements[0] == Operator('(')

    def test_quoted_bare_dollar_delimiter_suppresses_body_expansion(self) -> None:
        command = parse_command('cat <<"$"\n$(git commit)\n$')
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc('$', quoted=True, body='$(git commit)\n')

    def test_quoted_glob_characters_are_literal(self) -> None:
        assert _word("'*.py'").literal_text == '*.py'

    def test_substitution_closes_by_parsing_not_counting(self) -> None:
        word = _word('"$(printf "%s" "$(printf ")")"; sh -c \'git commit -m x\')"')
        part = word.parts[0]
        assert isinstance(part, SubstitutionPart)
        assert [segment.terminator for segment in part.command.segments] == [';', None]
        inner = part.command.segments[0].elements[2]
        assert isinstance(inner, Word)
        inner_part = inner.parts[0]
        assert isinstance(inner_part, SubstitutionPart)
        assert inner_part.command.segments[0].elements[1] == Word(
            '")"', (LiteralPart(')', quoted=True),)
        )

    def test_substitution_inside_double_quotes_records_quote_context(self) -> None:
        word = _word('"$(git log -1)"')
        assert len(word.parts) == 1
        part = word.parts[0]
        assert isinstance(part, SubstitutionPart)
        assert part.in_double_quotes
        assert part.raw == '$(git log -1)'
        nested = part.command.segments[0].elements
        assert [element.literal_text for element in nested if isinstance(element, Word)] == [
            'git',
            'log',
            '-1',
        ]

    @pytest.mark.parametrize('terminator', ['&&', '||', ';', ';;', '|', '|&', '&'])
    def test_terminators_end_segments(self, terminator: str) -> None:
        command = parse_command(f'a {terminator} b')
        assert [segment.terminator for segment in command.segments] == [terminator, None]

    @pytest.mark.parametrize(
        'operator', ['(', ')', '<', '>', '>>', '<<<', '<&', '>&', '&>', '&>>']
    )
    def test_non_terminating_operators_are_elements(self, operator: str) -> None:
        command = parse_command(f'a {operator} b')
        assert len(command.segments) == 1
        assert Operator(operator) in command.segments[0].elements

    @pytest.mark.parametrize('text', ['$NAME', '$1', '$@', '${NAME}', '~', '{a,b}'])
    def test_expansions(self, text: str) -> None:
        assert any(isinstance(part, ExpansionPart) for part in _word(text).parts)

    @pytest.mark.parametrize('text', ['*.py', 'a?', '[abc]'])
    def test_globs(self, text: str) -> None:
        assert any(isinstance(part, GlobPart) for part in _word(text).parts)

    @pytest.mark.parametrize(
        ('syntax', 'delimiter', 'quoted'),
        [
            ("'EOF'", 'EOF', True),
            ('"$"', '$', True),
            ('"$EOF"', '$EOF', True),
            ('"$(name)"', '$(name)', True),
            ('EOF', 'EOF', False),
        ],
    )
    def test_heredoc_delimiter_quote_removal(
        self,
        syntax: str,
        delimiter: str,
        *,
        quoted: bool,
    ) -> None:
        command = parse_command(f'cat <<{syntax}\nbody\n{delimiter}')
        heredoc = command.segments[0].elements[2]
        assert heredoc == Heredoc(delimiter, quoted, body='body\n')
        assert not command.recovery

    def test_unquoted_substitution_records_no_quote_context(self) -> None:
        part = _word('$(x)').parts[0]
        assert isinstance(part, SubstitutionPart)
        assert not part.in_double_quotes


class TestRecovery:
    def test_ansi_c_opener_inside_double_quotes_is_literal(self) -> None:
        text = '"$\'commit\'"'
        assert not parse_command(text).recovery
        assert _word(text).literal_text == "$'commit'"

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [("$'git'", 'git'), ("com$'mit'", 'commit'), ("$'-a'", '-a')],
    )
    def test_ansi_c_quotes_recover_known_literal_text(self, text: str, expected: str) -> None:
        command = parse_command(text)
        assert command.recovery == ('unsupported ANSI-C quoting',)
        assert _word(text).literal_text == expected

    def test_detector_heredoc_recovery_shares_overlapping_tail_scans(self) -> None:
        body = "cat <<'SECOND'\ncat <<THIRD\n'$(true)'\n"
        command = parse_command(f"cat <<'FIRST'\n{body}", recover_heredocs=True)
        heredocs = [
            element
            for segment in command.segments
            for element in segment.elements
            if isinstance(element, Heredoc)
        ]
        assert [heredoc.body_inlined for heredoc in heredocs] == [True, True, True]
        assert [heredoc.body for heredoc in heredocs] == [body, '', '']
        assert [len(heredoc.expansions) for heredoc in heredocs] == [0, 0, 1]
        assert command.recovery == ('unterminated heredoc',) * 3

    def test_detector_recovery_does_not_use_an_earlier_delimiter(self) -> None:
        command = parse_command('EOF\ncat <<EOF\ntrue\n', recover_heredocs=True)
        heredoc = command.segments[1].elements[2]
        assert isinstance(heredoc, Heredoc)
        assert heredoc.body_inlined
        assert command.segments[2].elements == (
            Word('true', (LiteralPart('true', quoted=False),)),
        )

    @pytest.mark.parametrize(
        'text',
        [
            "cat <<'EOF'\nbody\nEOF\n",
            'cat <<-EOF\n\tbody\n\tEOF\n',
            'cat <<FIRST <<SECOND\none\nFIRST\ntwo\nSECOND\n',
            'cat <<EOF; true\nbody\nEOF\n',
            "cat <<''\nbody\n\n",
        ],
    )
    def test_detector_recovery_preserves_terminated_heredocs(self, text: str) -> None:
        assert parse_command(text, recover_heredocs=True) == parse_command(text)

    @pytest.mark.parametrize(
        'text', [r"$'com\x6dit'", r"$'a\'b'", r"$'a\\'", r"$'$(git commit)\n'"]
    )
    def test_escaped_ansi_c_quotes_remain_one_opaque_word(self, text: str) -> None:
        command = parse_command(text + ' after')
        assert command.recovery == ('unsupported ANSI-C quoting',)
        words = command.segments[0].elements
        assert len(words) == 2
        assert words[0] == Word(text, (ExpansionPart(text),))
        assert isinstance(words[1], Word)
        assert words[1].literal_text == 'after'

    def test_nesting_limit_marks_the_deepest_substitution_truncated(self) -> None:
        text = 'echo ' + '$(echo ' * 9 + 'deep' + ')' * 9
        command = parse_command(text)
        assert command.recovery == ('substitution nesting limit',)
        word = command.segments[0].elements[1]
        for depth in range(9):
            assert isinstance(word, Word)
            part = word.parts[0]
            assert isinstance(part, SubstitutionPart)
            assert part.truncated == (depth == 8)
            if part.truncated:
                break
            word = part.command.segments[0].elements[1]

    def test_parser_is_total_on_arbitrary_input(self) -> None:
        for text in ['', '"', "'", '$(', '`', '<<', '${', ')', '&&&', '\\']:
            command = parse_command(text)
            assert isinstance(command, Command)

    def test_recovery_keeps_words_distinct(self) -> None:
        command = parse_command('g""it com""mit -m "x\ry')
        assert [
            element.literal_text
            for element in command.segments[0].elements
            if isinstance(element, Word)
        ] == ['git', 'commit', '-m', 'x\ry']

    @pytest.mark.parametrize(
        ('text', 'reason'),
        [
            ("git commit -m 'unterminated", 'unbalanced quotes'),
            ('git commit -m "unterminated', 'unbalanced quotes'),
            ('git commit -m "$(echo x"', 'unbalanced quotes'),
            ('git commit -m $(echo x', 'unterminated substitution'),
            ('git commit -m `echo x', 'unterminated backtick'),
            ('cat <<EOF\nbody', 'unterminated heredoc'),
            ('echo ${X', 'unterminated expansion'),
        ],
    )
    def test_recovery_reasons(self, text: str, reason: str) -> None:
        command = parse_command(text)
        assert command.recovery
        assert command.recovery[0] == reason

    def test_substitution_command_keeps_its_own_recovery(self) -> None:
        command = parse_command("echo $(echo 'x)")
        part = command.segments[0].elements[1]
        assert isinstance(part, Word)
        substitution = part.parts[0]
        assert isinstance(substitution, SubstitutionPart)
        assert substitution.command.recovery == ('unbalanced quotes',)
        assert substitution.command.recovery
        assert command.recovery == ('unbalanced quotes', 'unterminated substitution')

    def test_unterminated_ansi_c_quotes_record_both_reasons(self) -> None:
        command = parse_command("git $'commit")
        assert command.recovery == ('unsupported ANSI-C quoting', 'unbalanced quotes')

    def test_unterminated_quote_takes_the_rest_as_literal(self) -> None:
        command = parse_command("git commit -m 'x; git push")
        elements = command.segments[0].elements
        assert len(command.segments) == 1
        assert elements[-1] == Word("'x; git push", (LiteralPart('x; git push', quoted=True),))


def _word(text: str) -> Word:
    command = parse_command(text)
    element = command.segments[0].elements[0]
    assert isinstance(element, Word)
    return element


def _words(text: str) -> list[str | None]:
    command = parse_command(text)
    assert len(command.segments) == 1
    return [
        element.literal_text
        for element in command.segments[0].elements
        if isinstance(element, Word)
    ]
