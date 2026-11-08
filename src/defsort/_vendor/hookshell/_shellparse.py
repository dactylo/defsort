"""Parse POSIX shell command text into a structural tree without executing it."""

import re
from dataclasses import dataclass, field
from string import ascii_letters, digits

from ._source import SourceSpan, SourceText, join_sources

_MAX_SUBSTITUTION_DEPTH = 8
NAME_CHARACTERS = frozenset(ascii_letters + digits + '_')
NAME_START = frozenset(ascii_letters + '_')
_OPERATORS = (
    '&>>',
    '<<-',
    '<<<',
    '&&',
    '&>',
    '||',
    ';;',
    '<<',
    '<&',
    '>>',
    '>&',
    '|&',
    ';',
    '|',
    '&',
    '<',
    '>',
    '(',
    ')',
)
_RECOVERY_ANSI_C = 'unsupported ANSI-C quoting'
_RECOVERY_BACKTICK = 'unterminated backtick'
_RECOVERY_EXPANSION = 'unterminated expansion'
_RECOVERY_HEREDOC = 'unterminated heredoc'
_RECOVERY_NESTING = 'substitution nesting limit'
_RECOVERY_QUOTES = 'unbalanced quotes'
_RECOVERY_SUBSTITUTION = 'unterminated substitution'
_SPECIAL_PARAMETERS = frozenset('@*#?-$!0123456789')
_TERMINATORS = frozenset({'&&', '||', ';', ';;', '|', '|&', '&', '\n'})
_WORD_BREAKS = frozenset(' \t\n')

type WordPart = LiteralPart | SubstitutionPart | ExpansionPart | GlobPart
type _Element = Word | Operator | Heredoc | _Comment


@dataclass(frozen=True, slots=True)
class LiteralPart:
    """Represent literal argv text and the quoting that produced it.

    Provenance fields do not participate in equality.

    Attributes:
        text: Text after quote and escape processing.
        quoted: Whether quoting or an escape produced the literal.
        span: Interval in the literal's source view, or `None` without
            provenance.
        source: Mapped view of the decoded text, or `None` without provenance.
    """

    text: str
    quoted: bool
    span: SourceSpan | None = field(default=None, compare=False)
    source: SourceText | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class SubstitutionPart:
    """Represent command substitution with its parsed body and quote context.

    The provenance span does not participate in equality.

    Attributes:
        command: Parsed nested command. An empty tree does not establish an
            empty substitution when `truncated` is true.
        raw: Source spelling of the substitution.
        in_double_quotes: Whether the substitution occurs inside double quotes.
        truncated: Whether the parser omitted the nested tree because its depth
            limit was reached. Consumers must treat its contents as unknown.
        span: Interval covering the substitution spelling, or `None` without
            provenance.
    """

    command: 'Command'
    raw: str
    in_double_quotes: bool
    truncated: bool = False
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ExpansionPart:
    """Represent an expansion or unparsed quoted form with nested substitutions.

    The provenance span does not participate in equality.

    Attributes:
        raw: Source spelling of the expansion or unsupported ANSI-C quoted
            form.
        children: Parsed substitutions nested inside the expansion.
        in_double_quotes: Whether the expansion occurs inside double quotes.
        span: Interval covering the expansion spelling, or `None` without
            provenance.
    """

    raw: str
    children: tuple[SubstitutionPart, ...] = ()
    in_double_quotes: bool = False
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class GlobPart:
    """Represent one unquoted glob character.

    The provenance span does not participate in equality.

    Attributes:
        raw: The unquoted `*`, `?`, or `[` character.
        span: Interval covering that character, or `None` without provenance.
    """

    raw: str
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Word:
    """Represent a shell word before expansion.

    The provenance span does not participate in equality.

    Attributes:
        raw: Original spelling, retaining quotes and escapes.
        parts: Ordered literal, substitution, expansion, and glob components.
        span: Interval covering the word's spelling, or `None` without
            provenance.
    """

    raw: str
    parts: tuple[WordPart, ...]
    span: SourceSpan | None = field(default=None, compare=False)

    @property
    def literal_source(self) -> SourceText | None:
        """Return mapped argv text for a fully literal word with provenance."""
        if any(not isinstance(part, LiteralPart) for part in self.parts):
            return None
        return join_sources(
            [part.source for part in self.parts if isinstance(part, LiteralPart)],
            context='literal',
            separator='',
        )

    @property
    def literal_text(self) -> str | None:
        """Return the argv text of a word made only of literal parts, else `None`."""
        texts: list[str] = []
        for part in self.parts:
            if not isinstance(part, LiteralPart):
                return None
            texts.append(part.text)
        return ''.join(texts)


@dataclass(frozen=True, slots=True)
class Operator:
    """Represent an operator that does not terminate its segment.

    The provenance span does not participate in equality.

    Attributes:
        text: Operator spelling.
        span: Interval covering the operator, or `None` without provenance.
    """

    text: str
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Heredoc:
    """Represent a heredoc body, expansions, and recovery scan ownership.

    Provenance fields do not participate in equality.

    Attributes:
        delimiter: Delimiter text after quote removal.
        quoted: Whether quoting or escaping any delimiter character suppresses
            ordinary body expansion.
        body: Body text after applicable leading-tab removal. Recovery may
            leave it empty when an earlier recovered body owns the same text.
        expansions: Parsed body substitutions, empty for quoted bodies or
            expansions already owned by an earlier recovered body.
        body_inlined: Whether detector recovery parsed the body as following
            segments, so it must not be reparsed as a separate script.
        span: Interval covering the redirect operator and delimiter, or `None`
            without provenance.
        source: Mapped body view, or `None` when provenance is unavailable or
            recovery shares the body text elsewhere.
    """

    delimiter: str
    quoted: bool
    body: str
    expansions: tuple[SubstitutionPart, ...] = ()
    body_inlined: bool = False
    span: SourceSpan | None = field(default=None, compare=False)
    source: SourceText | None = field(default=None, compare=False)


class _CommandState:
    """Mutable state of one command level while its segments are parsed."""

    __slots__ = (
        'elements',
        'finished',
        'nested',
        'paren_depth',
        'pending',
        'spans',
        'terminators',
    )

    def __init__(self, *, nested: bool) -> None:
        """Initialize the segments and pending heredocs for one command level."""
        self.nested = nested
        self.elements: list[list[_Element]] = []
        self.terminators: list[str | None] = []
        self.spans: list[SourceSpan] = []
        self.pending: list[_PendingHeredoc] = []
        self.paren_depth = 0
        self.finished = False


@dataclass(frozen=True, slots=True)
class _Comment:
    """Comment element from `#` to the end of its line."""

    text: str
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Segment:
    """Represent the elements and terminator of one command segment.

    The provenance span does not participate in equality.

    Attributes:
        elements: Ordered word, operator, heredoc, and comment nodes.
        terminator: Separating shell operator or newline, or `None` at the
            command boundary.
        span: Interval through the segment's terminator, excluding drained
            heredoc bodies, or `None` without provenance.
    """

    elements: tuple[_Element, ...]
    terminator: str | None
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Command:
    """Represent a parsed command tree and its recovery reasons.

    The provenance span does not participate in equality.

    Attributes:
        segments: Command segments in source order.
        recovery: Reasons parsing required recovery; empty for a clean parse.
        span: Interval of parsed text, excluding enclosing substitution
            delimiters, or `None` without provenance.
    """

    segments: tuple[Segment, ...]
    recovery: tuple[str, ...] = ()
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(slots=True)
class _PendingHeredoc:
    """Heredoc awaiting its body at the next line feed."""

    elements: list[_Element]
    index: int
    delimiter: str
    quoted: bool
    strip_tabs: bool
    span: SourceSpan


class _WordState:
    """Mutable state while one word's parts are collected."""

    __slots__ = ('ends', 'parts', 'run', 'start', 'starts')

    def __init__(self, start: int) -> None:
        """Begin collecting one word at its raw-text offset."""
        self.start = start
        self.parts: list[WordPart] = []
        self.run: list[str] = []
        self.starts: list[int] = []
        self.ends: list[int] = []


class _Parser:
    """Recursive-descent parser over one command text."""

    def __init__(
        self, source: SourceText, depth: int = 0, *, recover_heredocs: bool = False
    ) -> None:
        """Bind the input and inherited substitution depth."""
        self._source = source
        self._text = source.text
        self._active_body: SourceText | None = None
        self._active_body_start = 0
        self._pos = 0
        self._depth = depth
        self._recovery: list[str] = []
        self._recover_heredocs = recover_heredocs
        self._heredoc_ends: dict[tuple[bool, str], int] | None = None
        self._recovered_body = False
        self._recovered_expansions = False
        self._recovered_tabs = False

    def _at_end(self) -> bool:
        """Return whether the cursor has consumed the input."""
        return self._pos >= len(self._text)

    def _has_heredoc_end(self, pending: _PendingHeredoc) -> bool:
        """Check remaining delimiter lines using one index for this script."""
        if self._heredoc_ends is None:
            self._heredoc_ends = {}
            offset = 0
            for line in self._text.split('\n'):
                if offset == len(self._text):
                    break
                self._heredoc_ends[False, line] = offset
                self._heredoc_ends[True, line.lstrip('\t')] = offset
                offset += len(line) + 1
        return self._heredoc_ends.get((pending.strip_tabs, pending.delimiter), -1) >= self._pos

    def _literal(self, source: SourceText, *, quoted: bool) -> LiteralPart:
        """Attach decoded text and its exact source view to a literal part."""
        return LiteralPart(
            source.text, quoted, span=SourceSpan(source, 0, len(source.text)), source=source
        )

    def _match_operator(self) -> str | None:
        """Find the longest operator at the current cursor."""
        for operator in _OPERATORS:
            if self._text.startswith(operator, self._pos):
                return operator
        return None

    def _read_to_line_end(self) -> str:
        """Consume comment text while leaving its line feed for segment parsing."""
        end = self._text.find('\n', self._pos)
        end = len(self._text) if end < 0 else end
        text = self._text[self._pos : end]
        self._pos = end
        return text

    def _scan_heredoc_expansions(self, body: SourceText) -> tuple[SubstitutionPart, ...]:
        """Scan body expansions with structural parsing at each substitution opener."""
        scanner = _Parser(body, self._depth, recover_heredocs=self._recover_heredocs)
        found: list[SubstitutionPart] = []
        while not scanner._at_end():
            char = scanner._text[scanner._pos]
            if char == '\\':
                following = scanner._text[scanner._pos + 1 : scanner._pos + 2]
                scanner._pos += 2 if following in ('$', '`', '\\', '\n') else 1
            elif scanner._text.startswith('$(', scanner._pos) or char == '`':
                part = scanner._parse_dynamic(in_double_quotes=False)
                if isinstance(part, SubstitutionPart):
                    found.append(part)
            else:
                scanner._pos += 1
        self._recovery.extend(scanner._recovery)
        return tuple(found)

    def _skip_blanks(self) -> None:
        """Skip unquoted spaces, tabs, and continued line feeds."""
        while not self._at_end():
            if self._text[self._pos] in ' \t':
                self._pos += 1
            elif self._text.startswith('\\\n', self._pos):
                self._pos += 2
            else:
                return

    def _span(self, start: int, end: int) -> SourceSpan:
        """Capture an input interval, retaining recovery body ownership."""
        if self._active_body is not None and start >= self._active_body_start:
            return SourceSpan(
                self._active_body, start - self._active_body_start, end - self._active_body_start
            )
        return SourceSpan(self._source, start, end)

    def _parse_brace_expansion(self, *, in_double_quotes: bool) -> ExpansionPart:
        """Retain parameter-expansion text and every nested substitution."""
        start = self._pos
        self._pos += 2
        children: list[SubstitutionPart] = []
        depth = 1
        while not self._at_end():
            char = self._text[self._pos]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    self._pos += 1
                    return ExpansionPart(
                        self._text[start : self._pos],
                        children=tuple(children),
                        in_double_quotes=in_double_quotes,
                        span=self._span(start, self._pos),
                    )
            elif char == '`' or self._text.startswith('$(', self._pos):
                part = self._parse_dynamic(in_double_quotes=in_double_quotes)
                if isinstance(part, SubstitutionPart):
                    children.append(part)
                continue
            self._pos += 1
        self._recovery.append(_RECOVERY_EXPANSION)
        return ExpansionPart(
            self._text[start : self._pos],
            children=tuple(children),
            in_double_quotes=in_double_quotes,
            span=self._span(start, self._pos),
        )

    def _parse_operator(
        self,
        operator: str,
        elements: list[_Element],
        state: _CommandState,
    ) -> None:
        """Record an operator and collect any heredoc delimiter it introduces."""
        if operator == '(':
            state.paren_depth += 1
        elif operator == ')':
            state.paren_depth -= 1
        start = self._pos - len(operator)
        elements.append(Operator(operator, self._span(start, self._pos)))
        if operator in ('<<', '<<-'):
            self._skip_blanks()
            word, quoted = self._parse_word()
            delimiter = _quote_removed_text(word)
            state.pending.append(
                _PendingHeredoc(
                    elements,
                    len(elements),
                    delimiter,
                    quoted,
                    strip_tabs=operator == '<<-',
                    span=self._span(start, self._pos),
                )
            )
            elements.append(Heredoc(delimiter, quoted, '', span=self._span(start, self._pos)))

    def _parse_substitution(self, *, in_double_quotes: bool) -> SubstitutionPart:
        """Parse a nested command until its structural closing parenthesis."""
        start = self._pos
        self._pos += 2
        if self._depth >= _MAX_SUBSTITUTION_DEPTH:
            self._pos = _scan_balanced(self._text, start + 1, '(', ')')
            self._recovery.append(_RECOVERY_NESTING)
            raw = self._text[start : self._pos]
            return SubstitutionPart(
                Command(()),
                raw,
                in_double_quotes,
                truncated=True,
                span=self._span(start, self._pos),
            )
        recovery_start = len(self._recovery)
        self._depth += 1
        segments = self._parse_segments(nested=True)
        self._depth -= 1
        child_recovery = tuple(self._recovery[recovery_start:])
        inner_span = self._span(start + 2, self._pos)
        if self._at_end():
            self._recovery.append(_RECOVERY_SUBSTITUTION)
        else:
            self._pos += 1
        raw = self._text[start : self._pos]
        return SubstitutionPart(
            Command(tuple(segments), child_recovery, span=inner_span),
            raw,
            in_double_quotes,
            span=self._span(start, self._pos),
        )

    def _view(self, start: int, end: int, *, context: str = 'literal') -> SourceText:
        """Capture a cheap identity slice in its enclosing context."""
        span = self._span(start, end)
        return SourceText(self._text[start:end], context, span.source, span.start)

    def _mapped(
        self, text: str, starts: list[int], ends: list[int], *, context: str = 'literal'
    ) -> SourceText:
        """Map retained characters without assigning deleted text to neighbors."""
        if not starts:
            return self._view(self._pos, self._pos, context=context)
        span = self._span(starts[0], ends[-1])
        shift = starts[0] - span.start
        if all(
            start == starts[0] + i and end == start + 1
            for i, (start, end) in enumerate(zip(starts, ends, strict=True))
        ):
            return SourceText(text, context, span.source, span.start)
        return SourceText(
            text,
            context,
            span.source,
            offsets=(*(start - shift for start in starts), ends[-1] - shift),
            end_offsets=tuple(end - shift for end in ends),
        )

    def _flush(self, state: _WordState, *, quoted: bool) -> None:
        """Append a pending literal run with its quoting provenance."""
        if state.run:
            source = self._mapped(''.join(state.run), state.starts, state.ends)
            state.parts.append(self._literal(source, quoted=quoted))
            state.run.clear()
            state.starts.clear()
            state.ends.clear()

    def _nested_substitution(
        self,
        inner: SourceText,
        raw: str,
        span: SourceSpan,
        *,
        in_double_quotes: bool = False,
    ) -> SubstitutionPart:
        """Parse detached substitution text at the next depth or record truncation."""
        if self._depth >= _MAX_SUBSTITUTION_DEPTH:
            self._recovery.append(_RECOVERY_NESTING)
            return SubstitutionPart(Command(()), raw, in_double_quotes, truncated=True, span=span)
        parser = _Parser(inner, self._depth + 1, recover_heredocs=self._recover_heredocs)
        command = parser.parse()
        self._recovery.extend(parser._recovery)
        return SubstitutionPart(command, raw, in_double_quotes, span=span)

    def _parse_ansi_c_quoted(self) -> LiteralPart | ExpansionPart:
        """Recover ANSI-C quote boundaries, retaining only unescaped literal contents."""
        start = self._pos
        self._pos += 2
        content_start = self._pos
        escaped = False
        self._recovery.append(_RECOVERY_ANSI_C)
        while not self._at_end():
            char = self._text[self._pos]
            if char == "'":
                break
            if char == '\\':
                escaped = True
                self._pos = min(self._pos + 2, len(self._text))
            else:
                self._pos += 1
        content_end = self._pos
        if self._at_end():
            self._recovery.append(_RECOVERY_QUOTES)
        else:
            self._pos += 1
        if escaped:
            return ExpansionPart(self._text[start : self._pos], span=self._span(start, self._pos))
        return self._literal(self._view(content_start, content_end), quoted=True)

    def _parse_backtick(self, *, in_double_quotes: bool) -> SubstitutionPart:
        """Parse a backtick region with its inherited quote context."""
        start = self._pos
        close = _scan_backtick(self._text, start + 1)
        if close < 0:
            self._recovery.append(_RECOVERY_BACKTICK)
            close = len(self._text)
            self._pos = close
        else:
            self._pos = close + 1
        # Remove one backtick escape layer before parsing the nested shell text.
        escapes = r'\\([$`\\"])|\\\n' if in_double_quotes else r'\\([$`\\])|\\\n'
        starts: list[int] = []
        ends: list[int] = []
        chunks: list[str] = []
        cursor = start + 1
        for match in re.finditer(escapes, self._text[start + 1 : close]):
            match_start = start + 1 + match.start()
            match_end = start + 1 + match.end()
            chunks.append(self._text[cursor:match_start])
            starts.extend(range(cursor, match_start))
            ends.extend(range(cursor + 1, match_start + 1))
            if match.group(1):
                chunks.append(match.group(1))
                starts.append(match_end - 1)
                ends.append(match_end)
            cursor = match_end
        chunks.append(self._text[cursor:close])
        starts.extend(range(cursor, close))
        ends.extend(range(cursor + 1, close + 1))
        inner = self._mapped(''.join(chunks), starts, ends, context='substitution')
        return self._nested_substitution(
            inner,
            self._text[start : self._pos],
            self._span(start, self._pos),
            in_double_quotes=in_double_quotes,
        )

    def _parse_double_quoted(self, state: _WordState) -> None:
        """Read double-quoted parts with shell escape and substitution rules."""
        self._flush(state, quoted=False)
        self._pos += 1
        quoted_state = _WordState(self._pos)
        emitted = False
        while True:
            if self._at_end():
                self._recovery.append(_RECOVERY_QUOTES)
                break
            char = self._text[self._pos]
            if char == '"':
                self._pos += 1
                break
            if char == '\\':
                if self._text.startswith('\\\n', self._pos):
                    self._pos += 2
                    continue
                following = self._text[self._pos + 1 : self._pos + 2]
                if following in ('$', '`', '"', '\\'):
                    quoted_state.run.append(following)
                    quoted_state.starts.append(self._pos + 1)
                    quoted_state.ends.append(self._pos + 2)
                    self._pos += 2
                    continue
                quoted_state.run.append('\\')
                quoted_state.starts.append(self._pos)
                quoted_state.ends.append(self._pos + 1)
                self._pos += 1
                continue
            if char in '$`':
                self._flush(quoted_state, quoted=True)
                state.parts.extend(quoted_state.parts)
                quoted_state.parts.clear()
                state.parts.append(self._parse_dynamic(in_double_quotes=True))
                emitted = True
                continue
            quoted_state.run.append(char)
            quoted_state.starts.append(self._pos)
            quoted_state.ends.append(self._pos + 1)
            self._pos += 1
        if quoted_state.run:
            self._flush(quoted_state, quoted=True)
            state.parts.extend(quoted_state.parts)
        elif not emitted:
            state.parts.append(
                self._literal(self._view(quoted_state.start, quoted_state.start), quoted=True)
            )

    def _parse_dynamic(self, *, in_double_quotes: bool) -> WordPart:
        """Read the expansion or substitution beginning at the cursor."""
        if self._text[self._pos] == '`':
            return self._parse_backtick(in_double_quotes=in_double_quotes)
        following = self._text[self._pos + 1 : self._pos + 2]
        if following == '(':
            return self._parse_substitution(in_double_quotes=in_double_quotes)
        if following == '{':
            return self._parse_brace_expansion(in_double_quotes=in_double_quotes)
        if following in _SPECIAL_PARAMETERS:
            start = self._pos
            self._pos += 2
            return ExpansionPart(
                f'${following}',
                in_double_quotes=in_double_quotes,
                span=self._span(start, self._pos),
            )
        if following in NAME_START:
            end = self._pos + 1
            while end < len(self._text) and self._text[end] in NAME_CHARACTERS:
                end += 1
            raw = self._text[self._pos : end]
            span = self._span(self._pos, end)
            self._pos = end
            return ExpansionPart(raw, in_double_quotes=in_double_quotes, span=span)
        self._pos += 1
        return self._literal(self._view(self._pos - 1, self._pos), quoted=False)

    def _parse_escape(self, state: _WordState) -> bool:
        """Consume an escape and return whether it quoted a character."""
        if self._text.startswith('\\\n', self._pos):
            self._pos += 2
            return False
        self._flush(state, quoted=False)
        if self._pos + 1 < len(self._text):
            state.parts.append(
                self._literal(self._view(self._pos + 1, self._pos + 2), quoted=True)
            )
            self._pos += 2
            return True
        state.parts.append(self._literal(self._view(self._pos, self._pos + 1), quoted=False))
        self._pos += 1
        return False

    def _parse_single_quoted(self, state: _WordState) -> None:
        """Read literal single-quoted text, recovering an absent closing quote."""
        self._flush(state, quoted=False)
        self._pos += 1
        start = self._pos
        end = self._text.find("'", self._pos)
        if end < 0:
            self._recovery.append(_RECOVERY_QUOTES)
            text = self._text[self._pos :]
            self._pos = len(self._text)
        else:
            text = self._text[self._pos : end]
            self._pos = end + 1
        state.parts.append(self._literal(self._view(start, start + len(text)), quoted=True))

    def _parse_word(self) -> tuple[Word, bool]:
        """Collect word parts without discarding quote and expansion boundaries."""
        state = _WordState(self._pos)
        quoted = False
        while not self._at_end():
            char = self._text[self._pos]
            if char in _WORD_BREAKS or self._match_operator() is not None:
                break
            if char == "'":
                quoted = True
                self._parse_single_quoted(state)
            elif char == '"':
                quoted = True
                self._parse_double_quoted(state)
            elif char == '\\':
                quoted = self._parse_escape(state) or quoted
            elif self._text.startswith("$'", self._pos):
                quoted = True
                self._flush(state, quoted=False)
                state.parts.append(self._parse_ansi_c_quoted())
            elif char in '$`':
                self._flush(state, quoted=False)
                state.parts.append(self._parse_dynamic(in_double_quotes=False))
            elif char in '*?[':
                self._flush(state, quoted=False)
                state.parts.append(GlobPart(char, span=self._span(self._pos, self._pos + 1)))
                self._pos += 1
            elif char in '{}' or (char == '~' and self._pos == state.start):
                self._flush(state, quoted=False)
                state.parts.append(ExpansionPart(char, span=self._span(self._pos, self._pos + 1)))
                self._pos += 1
            else:
                state.run.append(char)
                state.starts.append(self._pos)
                state.ends.append(self._pos + 1)
                self._pos += 1
        self._flush(state, quoted=False)
        return Word(
            self._text[state.start : self._pos],
            tuple(state.parts),
            span=self._span(state.start, self._pos),
        ), quoted

    def _read_heredoc_body(self, pending: _PendingHeredoc) -> SourceText:
        """Consume one heredoc body through its matching delimiter line."""
        start = self._pos
        lines: list[str] = []
        starts: list[int] = []
        ends: list[int] = []
        body_end = start
        while not self._at_end():
            line_start = self._pos
            end = self._text.find('\n', self._pos)
            line = self._text[self._pos :] if end < 0 else self._text[self._pos : end]
            self._pos = len(self._text) if end < 0 else end + 1
            candidate = line.lstrip('\t') if pending.strip_tabs else line
            if candidate == pending.delimiter:
                break
            lines.append(candidate)
            if pending.strip_tabs:
                retained = line_start + len(line) - len(candidate)
                starts.extend(range(retained - start, line_start + len(line) - start))
                ends.extend(range(retained + 1 - start, line_start + len(line) + 1 - start))
                starts.append(line_start + len(line) - start)
                ends.append(self._pos - start)
            body_end = self._pos
        else:
            self._recovery.append(_RECOVERY_HEREDOC)
        raw = self._view(start, body_end, context='heredoc')
        body = ''.join(f'{text}\n' for text in lines)
        if body == raw.text:
            return raw
        if not pending.strip_tabs:
            return SourceText(
                body,
                'decoded heredoc',
                raw,
                offsets=(*range(len(raw.text) + 1), len(raw.text)),
            )
        return SourceText(
            body,
            'decoded heredoc',
            raw,
            offsets=(*starts, body_end - start),
            end_offsets=tuple(ends),
        )

    def _recover_heredoc(self, pending: _PendingHeredoc) -> Heredoc:
        """Leave an unmatched body's script in place and share its tail scans."""
        self._recovery.append(_RECOVERY_HEREDOC)
        if self._active_body is None:
            self._active_body = self._view(self._pos, len(self._text), context='heredoc')
            self._active_body_start = self._pos
        if pending.strip_tabs and not self._recovered_tabs:
            # Preserve <<- semantics even within continued words and substitutions.
            starts = list(range(self._pos))
            ends = list(range(1, self._pos + 1))
            offset = self._pos
            lines: list[str] = []
            for line in self._text[self._pos :].split('\n'):
                stripped = line.lstrip('\t')
                lines.append(stripped)
                retained = offset + len(line) - len(stripped)
                starts.extend(range(retained, offset + len(line)))
                ends.extend(range(retained + 1, offset + len(line) + 1))
                offset += len(line)
                if offset < len(self._text):
                    starts.append(offset)
                    ends.append(offset + 1)
                offset += 1
            tail = '\n'.join(lines)
            original = self._source
            self._source = SourceText(
                self._text[: self._pos] + tail,
                'recovered command',
                original,
                offsets=(*starts, len(self._text)),
                end_offsets=tuple(ends),
            )
            body_start = self._active_body_start
            self._active_body = SourceText(
                tail,
                'decoded heredoc',
                self._active_body,
                offsets=(
                    *(value - body_start for value in starts[self._pos :]),
                    len(self._text) - body_start,
                ),
                end_offsets=tuple(value - body_start for value in ends[self._pos :]),
            )
            self._active_body_start = self._pos
            self._text = self._text[: self._pos] + tail
            self._heredoc_ends = None
            self._recovered_expansions = False
            self._recovered_tabs = True
        body = ''
        expansions: tuple[SubstitutionPart, ...] = ()
        if not self._recovered_body:
            body = self._text[self._pos :]
            self._recovered_body = True
        if not pending.quoted and not self._recovered_expansions:
            expansions = self._scan_heredoc_expansions(
                self._view(self._pos, len(self._text), context='recovery scan')
            )
            self._recovered_expansions = True
        return Heredoc(
            pending.delimiter,
            pending.quoted,
            body,
            expansions,
            body_inlined=True,
            span=pending.span,
            source=self._active_body if body else None,
        )

    def _drain_heredocs(self, state: _CommandState) -> None:
        """Attach pending bodies and their expansion trees to their segments."""
        for pending in state.pending:
            if self._recover_heredocs and not self._has_heredoc_end(pending):
                pending.elements[pending.index] = self._recover_heredoc(pending)
                continue
            body = self._read_heredoc_body(pending)
            expansions = () if pending.quoted else self._scan_heredoc_expansions(body)
            pending.elements[pending.index] = Heredoc(
                pending.delimiter,
                pending.quoted,
                body.text,
                expansions,
                span=pending.span,
                source=body,
            )
        state.pending.clear()

    def _parse_segment(self, state: _CommandState) -> None:
        """Read one segment while preserving words, operators, and heredocs."""
        start = self._pos
        end: int | None = None
        elements: list[_Element] = []
        terminator: str | None = None
        while True:
            self._skip_blanks()
            if self._at_end():
                state.finished = True
                break
            char = self._text[self._pos]
            if char == '\n':
                self._pos += 1
                end = self._pos
                self._drain_heredocs(state)
                terminator = '\n'
                break
            if char == '#':
                comment_start = self._pos
                text = self._read_to_line_end()
                elements.append(_Comment(text, self._span(comment_start, self._pos)))
                continue
            operator = self._match_operator()
            if operator is None:
                word, _ = self._parse_word()
                elements.append(word)
                continue
            if operator == ')' and state.nested and state.paren_depth == 0:
                state.finished = True
                break
            self._pos += len(operator)
            if operator in _TERMINATORS:
                terminator = operator
                break
            self._parse_operator(operator, elements, state)
        state.elements.append(elements)
        state.terminators.append(terminator)
        state.spans.append(self._span(start, self._pos if end is None else end))

    def _parse_segments(self, *, nested: bool) -> list[Segment]:
        """Collect segments until this command level ends."""
        state = _CommandState(nested=nested)
        while not state.finished:
            self._parse_segment(state)
        if state.pending:
            self._drain_heredocs(state)
        return [
            Segment(tuple(elements), terminator, span)
            for elements, terminator, span in zip(
                state.elements,
                state.terminators,
                state.spans,
                strict=True,
            )
            if elements or terminator not in (None, '\n')
        ]

    def parse(self) -> Command:
        """Parse the complete text as a top-level command."""
        segments = self._parse_segments(nested=False)
        return Command(
            tuple(segments), tuple(self._recovery), SourceSpan(self._source, 0, self._pos)
        )


def _quote_removed_text(word: Word) -> str:
    """Return a word's text after quote removal without performing expansions."""
    return ''.join(part.text if isinstance(part, LiteralPart) else part.raw for part in word.parts)


def _scan_backtick(text: str, start: int) -> int:
    """Return the index of the next unescaped backtick at or after `start`, or -1."""
    index = start
    while index < len(text):
        if text[index] == '\\':
            index += 2
            continue
        if text[index] == '`':
            return index
        index += 1
    return -1


def _scan_balanced(text: str, start: int, opener: str, closer: str) -> int:
    """Return the index after the closer matching `opener` at `start`, ignoring quotes."""
    depth = 0
    index = start
    while index < len(text):
        if text.startswith(opener, index):
            depth += 1
            index += len(opener)
            continue
        if text.startswith(closer, index):
            depth -= 1
            index += len(closer)
            if depth == 0:
                return index
            continue
        index += 1
    return len(text)


def parse_source(source: SourceText, *, recover_heredocs: bool = False) -> Command:
    """Parse a mapped shell view while preserving its enclosing source contexts.

    Args:
        source: Shell scan text and its provenance.
        recover_heredocs: Whether unmatched bodies remain following segments
            for conservative detector scans, as in `parse_command`.
    """
    return _Parser(source, recover_heredocs=recover_heredocs).parse()


def parse_command(text: str, *, recover_heredocs: bool = False) -> Command:
    """Parse shell command text into a total command tree.

    Args:
        text: Raw shell command text as the host delivered it.
        recover_heredocs: For conservative detector script scans only, parse
            unmatched heredoc bodies as following segments and share their
            token and expansion tails instead of reparsing nested suffixes.

    Returns:
        The command tree. A parse that needed recovery lists its reasons in
        `Command.recovery`; the tree is never absent.
    """
    return parse_source(SourceText(text), recover_heredocs=recover_heredocs)
