"""Detect commit invocations in commands the grammar rejected."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from ._forms import ScanBlocker, analyze_data_writes
from ._shellparse import (
    Command,
    ExpansionPart,
    Heredoc,
    Operator,
    Segment,
    SubstitutionPart,
    Word,
    parse_source,
)
from ._source import SourceSpan, SourceText, join_sources, resolve_span

_EVALUATOR = 'eval'
_GIT_WORDS = frozenset({'git', 'git.exe'})
_INTERPRETER_PREFIX = 'python'
_INTERPRETERS = frozenset({'node', 'perl', 'ruby'})
_SCRIPT_OPTIONS = frozenset({'-c', '-e'})
_SHELLS = frozenset({'bash', 'dash', 'ksh', 'sh', 'zsh'})
_TOKEN_BREAKS = str.maketrans(dict.fromkeys('\'"[](){},;=', ' '))


@dataclass(frozen=True, slots=True)
class CommitMatch:
    """Evidence for a conservative match, which need not prove execution.

    Attributes:
        kind: Word pair, token pair, or unknown substitution.
        span: Triggering commit token, absent for unknown or unmapped text.
        related_span: Earlier Git word or expansion, when mapped.
        contexts: Enclosing scan frames, outermost first.
        blocker: Applicable original data-write defect, if any.
    """

    kind: str
    span: SourceSpan | None
    related_span: SourceSpan | None
    contexts: tuple[SourceSpan, ...]
    blocker: ScanBlocker | None


@dataclass(frozen=True, slots=True)
class _Script:
    """Reparse work retaining optional transformed source metadata."""

    text: str
    source: SourceText | None
    arguments: tuple[Word, ...] = ()


@dataclass(frozen=True, slots=True)
class _Work:
    """Pending iterative scan with inherited provenance and blocker."""

    item: Segment | SubstitutionPart | _Script
    contexts: tuple[SourceSpan, ...] = ()
    blocker: ScanBlocker | None = None


@dataclass(slots=True)
class _Scan:
    """Iterative scan scope with source-relative evidence and pending work."""

    pending: list[_Work]
    source: SourceText
    exempt: set[int]
    blockers: tuple[ScanBlocker, ...]
    script: str | None = None
    match: CommitMatch | None = None


def _basename(text: str) -> str:
    """Return the lower-cased final path component of a literal word."""
    return text.replace('\\', '/').rsplit('/', 1)[-1].lower()


def _frame(source: SourceText | None, context: str) -> SourceSpan:
    """Name a scan context even when a legacy tree has no source metadata."""
    view = SourceText(source.text, context, source) if source else SourceText('', context)
    return SourceSpan(view, 0, len(view.text))


def _is_data_sink(segment: Segment) -> bool:
    """Identify a plain cat or tee segment that writes its input to a file."""
    elements = segment.elements
    if (
        not elements
        or not isinstance(elements[0], Word)
        or elements[0].literal_text not in {'cat', 'tee'}
        or segment.terminator in {'|', '|&'}
    ):
        return False

    writes_file = False
    for index, element in enumerate(elements):
        previous = elements[index - 1] if index else None
        if isinstance(element, Operator):
            if element.text not in {'<<', '<<-', '>', '>>'}:
                return False
            if element.text in {'>', '>>'}:
                if index + 1 >= len(elements) or not isinstance(elements[index + 1], Word):
                    return False
                writes_file = True
        elif (
            elements[0].literal_text == 'tee'
            and index > 0
            and isinstance(element, Word)
            and not (isinstance(previous, Operator) and previous.text in {'>', '>>'})
            and element.literal_text is not None
            and not element.literal_text.startswith('-')
        ):
            writes_file = True
    return writes_file


def _is_interpreter(text: str) -> bool:
    """Return whether a literal word names a listed script interpreter."""
    name = _basename(text).removesuffix('.exe')
    return name in _INTERPRETERS or name.startswith(_INTERPRETER_PREFIX)


def _is_script_option(text: str) -> bool:
    """Return whether a word is a short-option cluster containing `c`."""
    return text.startswith('-') and not text.startswith('--') and 'c' in text[1:]


def _match_position(match: CommitMatch, source: SourceText | None) -> int | None:
    """Resolve candidate ordering without fabricating unknown token spans."""
    if source is None:
        return None
    spans = (match.span,) if match.span is not None else tuple(reversed(match.contexts))
    for span in spans:
        resolved = resolve_span(span, context=source.context)
        if resolved is not None and resolved.source is source:
            return resolved.start
    return None


def _consider(scan: _Scan, match: CommitMatch) -> None:
    """Retain the earliest mapped candidate, with traversal-order fallback."""
    if scan.match is None:
        scan.match = match
        return
    previous = _match_position(scan.match, scan.source)
    current = _match_position(match, scan.source)
    if previous is not None and current is not None and current < previous:
        scan.match = match


def _ordering_source(command: Command) -> SourceText:
    """Compare recovered roots and sibling body views in their shared source."""
    source = command.span.source if command.span else SourceText('', 'unmapped root')
    # Recovered roots and heredoc evidence can be sibling views. Compare in
    # their original source, retaining the body views for displayed locations.
    while source.context == 'recovered command' and source.parent is not None:
        source = source.parent
    return source


def _rebase_span(
    span: SourceSpan | None,
    root: SourceText,
    source: SourceText | None,
    views: dict[int, SourceText],
) -> SourceSpan | None:
    """Attach cached relative evidence to one occurrence's source views."""
    if span is None or source is None:
        return None
    views[id(root)] = source
    trail: list[SourceText] = []
    cursor = span.source
    while id(cursor) not in views:
        trail.append(cursor)
        if cursor.parent is None:
            return None
        cursor = cursor.parent
    parent = views[id(cursor)]
    for view in reversed(trail):
        parent = SourceText(
            view.text,
            view.context,
            parent,
            view.offset,
            view.offsets,
            view.end_offsets,
        )
        views[id(view)] = parent
    return SourceSpan(views[id(span.source)], span.start, span.end)


def _cached_match(root: SourceText, match: CommitMatch, work: _Work, item: _Script) -> CommitMatch:
    """Map memoized script evidence and attach occurrence-specific context."""
    source = item.source
    contexts = work.contexts
    if item.arguments:
        source = join_sources(
            [word.literal_source for word in item.arguments], context='eval argument'
        )
        contexts = (*contexts[:-1], _frame(source, 'eval argument'))
    views: dict[int, SourceText] = {}
    frames = tuple(
        _rebase_span(frame, root, source, views) or _frame(None, frame.source.context)
        for frame in match.contexts
    )
    return CommitMatch(
        match.kind,
        _rebase_span(match.span, root, source, views),
        _rebase_span(match.related_span, root, source, views),
        (*contexts, *frames),
        work.blocker,
    )


def _script_work(text: str, source: SourceText | None, context: str, work: _Work) -> _Work:
    """Queue a script while retaining its enclosing execution context."""
    return _Work(_Script(text, source), (*work.contexts, _frame(source, context)), work.blocker)


def _evaluator_at(
    words: list[Word], texts: list[str | None], start: int, work: _Work
) -> Iterator[_Work]:
    """Yield scripts selected by the evaluator rule at one literal word."""
    first = texts[start]
    if first is None:
        return
    name = _basename(first)
    if name == _EVALUATOR:
        arguments = [
            words[index] for index in range(start + 1, len(words)) if texts[index] is not None
        ]
        text = ' '.join(
            texts[index] or ''
            for index in range(start + 1, len(words))
            if texts[index] is not None
        )
        yield _Work(
            _Script(text, None, tuple(arguments)),
            (*work.contexts, _frame(None, 'eval argument')),
            work.blocker,
        )
        return
    if name.removesuffix('.exe') not in _SHELLS:
        return
    for index, text in enumerate(texts[start + 1 :], start + 1):
        if text is not None and _is_script_option(text):
            for script_index in range(index + 1, len(words)):
                script = texts[script_index]
                if script is not None:
                    yield _script_work(
                        script, words[script_index].literal_source, 'shell -c script', work
                    )
            return


def _substitution_work(item: SubstitutionPart, work: _Work) -> tuple[SourceSpan, ...]:
    """Retain substitution location separately from uncertain token evidence."""
    source = item.span
    view = (
        SourceText(
            source.source.text[source.start : source.end],
            'substitution',
            source.source,
            source.start,
        )
        if source
        else None
    )
    return (*work.contexts, _frame(view, 'command substitution'))


def _scan_substitution(scan: _Scan, item: SubstitutionPart, work: _Work) -> None:
    """Record uncertainty or queue all nested substitution segments."""
    contexts = _substitution_work(item, work)
    if item.truncated:
        _consider(scan, CommitMatch('unknown-substitution', None, None, contexts, work.blocker))
    else:
        scan.pending.extend(
            _Work(segment, contexts, work.blocker) for segment in reversed(item.command.segments)
        )


def _word_span(word: Word) -> SourceSpan | None:
    """Prefer decoded token metadata to surrounding shell quotes."""
    source = word.literal_source
    return SourceSpan(source, 0, len(source.text)) if source else word.span


def _word_substitutions(word: Word) -> Iterator[SubstitutionPart]:
    """Yield every substitution nested anywhere inside a word."""
    for part in word.parts:
        if isinstance(part, SubstitutionPart):
            yield part
        elif isinstance(part, ExpansionPart):
            yield from part.children


def is_git_executable(text: str) -> bool:
    r"""Return whether a literal word names Git by its executable basename.

    Args:
        text: Literal text of one shell word, possibly path-qualified with
            POSIX or Windows separators.

    Returns:
        `True` for `git` or `git.exe` in any letter case after the last path
        separator, so `/usr/bin/git`, `./git`, and `C:\Git\cmd\GIT.EXE` all
        match. A word carrying an expansion has no literal text and is never
        passed here.
    """
    return _basename(text) in _GIT_WORDS


def _token_match(text: str, source: SourceText | None, work: _Work) -> CommitMatch | None:
    """Return evidence for a Git token followed by commit, or `None`."""
    earlier: tuple[int, int] | None = None
    for token in re.finditer(r'\S+', text.translate(_TOKEN_BREAKS)):
        if earlier is not None and token.group() == 'commit':
            return CommitMatch(
                'token-pair',
                SourceSpan(source, token.start(), token.end()) if source else None,
                SourceSpan(source, *earlier) if source else None,
                work.contexts,
                work.blocker,
            )
        if earlier is None and is_git_executable(token.group()):
            earlier = token.span()
    return None


def _interpreter_match(words: list[Word], work: _Work) -> CommitMatch | None:
    """Scan the literal script after a listed interpreter's -c or -e option."""
    texts = [word.literal_text for word in words]
    for index, text in enumerate(texts):
        if text is None or not _is_interpreter(text):
            continue
        for option_index in range(index + 1, len(texts)):
            if texts[option_index] in _SCRIPT_OPTIONS:
                script = texts[option_index + 1] if option_index + 1 < len(texts) else None
                if script is None:
                    return None
                source = words[option_index + 1].literal_source
                nested = _script_work(script, source, 'inline interpreter script', work)
                return _token_match(script, source, nested)
    return None


def _word_match(words: list[Word], work: _Work) -> CommitMatch | None:
    """Locate a literal commit after Git or an unknown executable word."""
    texts = [word.literal_text for word in words]
    for index, text in enumerate(texts):
        if (text is None or is_git_executable(text)) and 'commit' in texts[index + 1 :]:
            commit_index = texts.index('commit', index + 1)
            contexts = work.contexts
            if any(
                literal is not None and _basename(literal) == _EVALUATOR
                for literal in texts[:index]
            ):
                source = join_sources(
                    [word.literal_source for word in words[index : commit_index + 1]],
                    context='eval argument',
                )
                contexts = (*contexts, _frame(source, 'eval argument'))
            return CommitMatch(
                'word-pair',
                _word_span(words[commit_index]),
                _word_span(words[index]),
                contexts,
                work.blocker,
            )
    return None


def _segment_matches(
    segment: Segment,
    pending: list[_Work],
    work: _Work,
    *,
    exempt: set[int],
    blockers: tuple[ScanBlocker, ...],
) -> Iterator[CommitMatch]:
    """Apply direct detector rules and queue nested work for one segment."""
    words = [element for element in segment.elements if isinstance(element, Word)]
    match = _word_match(words, work)
    if match is not None:
        yield match
    nested: list[_Work] = []
    for element in segment.elements:
        if isinstance(element, Word):
            nested.extend(
                _Work(part, work.contexts, work.blocker) for part in _word_substitutions(element)
            )
        elif isinstance(element, Heredoc) and id(element) not in exempt:
            blocker = next((blocker for blocker in blockers if blocker.body is element), None)
            blocker = (
                blocker
                or work.blocker
                or next((blocker for blocker in blockers if blocker.body is None), None)
            )
            body_work = _Work(
                _Script(element.body, element.source),
                (*work.contexts, _frame(element.source, 'heredoc')),
                blocker,
            )
            match = _token_match(element.body, element.source, body_work)
            if match is not None:
                yield match
            nested.extend(_Work(part, body_work.contexts, blocker) for part in element.expansions)
            if not element.body_inlined:
                nested.append(body_work)
    match = _interpreter_match(words, work)
    if match is not None:
        yield match
    texts = [word.literal_text for word in words]
    for start in range(len(words)):
        nested.extend(_evaluator_at(words, texts, start, work))
    pending.extend(reversed(nested))


def find_commit(command: Command) -> CommitMatch | None:
    """Return the first conservative commit match with available source evidence.

    Quoted data bodies are exempt in the historical single-segment form or a
    wholly validated literal file-writing chain.

    Args:
        command: Parsed command tree, clean or recovered.

    Returns:
        Evidence when a segment, substitution, body, or script matches, or
        None.
        A match does not establish that Git will execute.
    """
    sink = (
        command.segments[0]
        if len(command.segments) == 1 and _is_data_sink(command.segments[0])
        else None
    )
    analysis = analyze_data_writes(command)
    exempt: set[int] = (
        {id(body) for body in sink.elements if isinstance(body, Heredoc) and body.quoted}
        if sink is not None
        else {id(body) for body in analysis.exempt_bodies}
        if len(command.segments) > 1
        else set()
    )
    scans = [
        _Scan(
            [_Work(segment) for segment in reversed(command.segments)],
            _ordering_source(command),
            exempt,
            analysis.blockers,
        )
    ]
    scripts: dict[str, tuple[SourceText, CommitMatch | None]] = {}
    active: set[str] = set()
    while scans:
        scan = scans[-1]
        if not scan.pending:
            scans.pop()
            if scan.script is None:
                return scan.match
            scripts[scan.script] = (scan.source, scan.match)
            active.remove(scan.script)
            continue
        work = scan.pending.pop()
        item = work.item
        if isinstance(item, _Script):
            if item.text in scripts:
                root, match = scripts[item.text]
                if match is not None:
                    _consider(scan, _cached_match(root, match, work, item))
            elif item.text not in active:
                active.add(item.text)
                source = SourceText(item.text, 'script root')
                parsed = parse_source(source, recover_heredocs=True)
                scan.pending.append(work)
                scans.append(
                    _Scan(
                        [_Work(segment) for segment in reversed(parsed.segments)],
                        source,
                        set(),
                        (),
                        item.text,
                    )
                )
        elif isinstance(item, SubstitutionPart):
            _scan_substitution(scan, item, work)
        # Only original body identities can receive the quoted-heredoc exemption.
        else:
            for match in _segment_matches(
                item, scan.pending, work, exempt=scan.exempt, blockers=scan.blockers
            ):
                _consider(scan, match)
    return None
