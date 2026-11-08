"""Validate literal file-writing chains before exempting quoted data bodies."""

import re
from dataclasses import dataclass

from ._shellparse import Command, ExpansionPart, Heredoc, LiteralPart, Operator, Word
from ._source import SourceSpan

_BUILTINS = frozenset(
    {'basename', 'cd', 'chmod', 'dirname', 'echo', 'printf', 'pwd', 'realpath', 'true'}
)
_CLOSERS = frozenset({'then', 'elif', 'else', 'fi', 'do', 'done', '}', ')'})
_DYNAMIC_NAMES = frozenset(
    {
        'ARGC',
        'BASHPID',
        'COLUMNS',
        'COMPREPLY',
        'COPROC',
        'DIRSTACK',
        'EPOCHREALTIME',
        'EPOCHSECONDS',
        'ERRNO',
        'FUNCNAME',
        'HISTCMD',
        'LINENO',
        'LINES',
        'MAPFILE',
        'MATCH',
        'MBEGIN',
        'MEND',
        'OPTARG',
        'OPTIND',
        'PIPESTATUS',
        'RANDOM',
        'REPLY',
        'SECONDS',
        'SRANDOM',
        'TTYIDLE',
    }
)
_DYNAMIC_PREFIXES = ('BASH_', 'COMP_', 'READLINE_', 'TRY_BLOCK_', 'ZSH_')
_EXECUTION_CONSUMERS = frozenset(
    {
        '.',
        'source',
        'exec',
        'eval',
        'sh',
        'bash',
        'dash',
        'ksh',
        'zsh',
        'python',
        'node',
        'perl',
        'ruby',
        'make',
        'npm',
        'pytest',
        'git',
    }
)
# Stands in for an expansion in argv text: non-empty, and never a leading '-' or '+'.
_EXPANDED = 'x'
_FLAGS = {
    'mkdir': 'p',
    'ls': 'aAdFhlnprRSt1',
    'wc': 'clmw',
    'head': '',
    'tail': '',
    'cat': 'benstuv',
    'tee': 'ai',
    'touch': 'acm',
    'cp': 'fip',
    'mv': 'fi',
    'rm': 'firR',
    'grep': 'EFivnqlc',
    'diff': 'u',
    'sort': 'nru',
}
_MINIMUM_OPERANDS = {'mkdir': 1, 'tee': 1, 'touch': 1, 'cp': 2, 'mv': 2, 'rm': 1}
_PAIR = 2
_REFERENCE = re.compile(r'\$(?:([A-Z][A-Z0-9_]*)|\{([A-Z][A-Z0-9_]*)\})')
_SEPARATORS = frozenset({'&&', '||', ';', '\n'})
_VALUES = {'mkdir': {'m'}, 'head': {'n', 'c'}, 'tail': {'n', 'c'}, 'grep': {'e'}}

type _Token = Word | Operator | Heredoc


@dataclass(frozen=True, slots=True)
class ScanBlocker:
    """Reason a body remains scanned, with optional local body ownership.

    Attributes:
        kind: One of sink, quoting, arguments, structure, execution-consumer,
            unknown-form, write-target-collision, unknown-target,
            unknown-destination, unquoted-variable, expansion, or
            literal-required.
        span: Source evidence for the defect, or None without metadata.
        body: Affected original body; None means a command-wide defect.
    """

    kind: str
    span: SourceSpan | None
    body: Heredoc | None = None


@dataclass(frozen=True, slots=True)
class _DataWriteAnalysis:
    """Validated body nodes and reasons withholding the chained exemption."""

    exempt_bodies: tuple[Heredoc, ...]
    blockers: tuple[ScanBlocker, ...]


@dataclass(frozen=True, slots=True)
class _Simple:
    """One simple command's possible writes, bodies, and at most one command defect."""

    name: str
    targets: list[tuple[Word, str]]
    bodies: list[Heredoc]
    local: list[ScanBlocker]
    defect: ScanBlocker | None
    sink: bool


class _Structure:
    """Iterative restricted grammar over the parser's original token nodes."""

    def __init__(self, tokens: list[_Token]) -> None:
        """Initialize an explicit grammar stack, avoiding Python recursion."""
        self._tokens = tokens
        self._index = 0
        self._tasks: list[tuple[str, frozenset[str]]] = [('list', frozenset())]
        self._commands: list[list[_Token]] = []

    def _peek(self) -> str:
        """Read the next grammatical spelling or an empty end marker."""
        return _key(self._tokens[self._index]) if self._index < len(self._tokens) else ''

    def _newlines(self) -> None:
        """Discard only optional line breaks in the current grammar position."""
        while self._peek() == '\n':
            self._index += 1

    def _schedule(self, tasks: list[tuple[str, frozenset[str]]]) -> None:
        """Queue grammar continuations in their source execution order."""
        self._tasks.extend(reversed(tasks))

    def _conditional(self) -> None:
        """Queue a nonempty condition and body, followed by branch selection."""
        self._schedule(
            [
                ('list', frozenset({'then'})),
                ('expect', frozenset({'then'})),
                ('list', frozenset({'elif', 'else', 'fi'})),
                ('if-rest', frozenset()),
            ]
        )

    def _command(self) -> bool:
        """Read one simple command or open a supported compound command."""
        key = self._peek()
        if self._index == len(self._tokens) or key in _CLOSERS | _SEPARATORS:
            return False
        if key in {'(', '{'}:
            self._index += 1
            closer = ')' if key == '(' else '}'
            self._schedule([('list', frozenset({closer})), ('expect', frozenset({closer}))])
        elif key == 'if':
            self._index += 1
            self._conditional()
        elif key in {'while', 'until'}:
            self._index += 1
            self._schedule(
                [
                    ('list', frozenset({'do'})),
                    ('expect', frozenset({'do'})),
                    ('list', frozenset({'done'})),
                    ('expect', frozenset({'done'})),
                ]
            )
        else:
            start = self._index
            while self._index < len(self._tokens):
                token = self._tokens[self._index]
                if isinstance(token, Operator) and token.text in _SEPARATORS | {'(', ')'}:
                    break
                self._index += 1
            if self._index == start:
                return False
            self._commands.append(self._tokens[start : self._index])
        return True

    def _if_rest(self) -> bool:
        """Consume a conditional branch delimiter and queue its remainder."""
        key = self._peek()
        self._index += 1
        if key == 'fi':
            return True
        if key == 'else':
            self._schedule([('list', frozenset({'fi'})), ('expect', frozenset({'fi'}))])
            return True
        if key == 'elif':
            self._conditional()
            return True
        return False

    def _tail(self, stops: frozenset[str]) -> bool:
        """Require a separator between commands and before keyword closers."""
        key = self._peek()
        if self._index == len(self._tokens) or (key == ')' and key in stops):
            return True
        if key not in _SEPARATORS:
            return False
        self._index += 1
        self._newlines()
        if key in {';', '\n'} and (self._index == len(self._tokens) or self._peek() in stops):
            return True
        self._schedule([('command', frozenset()), ('tail', stops)])
        return True

    def parse(self) -> tuple[list[list[_Token]], SourceSpan | None, bool]:
        """Parse the restricted structure and retain commands collected before failure.

        Returns:
            The collected simple commands, a span at the stopping position when
            available, and whether the complete structure is valid.
            The span is diagnostic evidence only when validity is false.
        """
        valid = True
        while self._tasks and valid:
            task, stops = self._tasks.pop()
            if task == 'list':
                self._newlines()
                self._schedule([('command', frozenset()), ('tail', stops)])
            elif task == 'command':
                valid = self._command()
            elif task == 'tail':
                valid = self._tail(stops)
            elif task == 'expect':
                valid = self._peek() in stops
                if valid:
                    self._index += 1
            else:
                valid = self._if_rest()
        valid = valid and self._index == len(self._tokens)
        token = self._tokens[min(self._index, len(self._tokens) - 1)] if self._tokens else None
        return self._commands, token.span if token else None, valid


def _adjacent_descriptor(word: Word, operator: Operator) -> bool:
    """Distinguish an unquoted IO number from a separated numeric argument."""
    return (
        re.fullmatch('[0-9]+', word.raw) is not None
        and word.span is not None
        and operator.span is not None
        and word.span.source is operator.span.source
        and word.span.end == operator.span.start
    )


def _argv_text(word: Word) -> str:
    """Remove quotes, standing in for expansions, so validators see leading dashes."""
    return ''.join(
        part.text if isinstance(part, LiteralPart) else _EXPANDED for part in word.parts
    )


def _basename(text: str) -> str | None:
    """Extract a conservative leaf without filesystem or PATH inspection."""
    leaf = text.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    return leaf.casefold() if leaf not in {'', '.', '..', '-'} else None


def _command_span(tokens: list[_Token]) -> SourceSpan | None:
    """Describe the simple command including arguments but excluding its body."""
    first, last = tokens[0].span, tokens[-1].span
    if first is None or last is None or first.source is not last.source:
        return None
    return SourceSpan(first.source, first.start, last.end)


def _form_failure(name: str) -> str:
    """Name the blocker for a command whose argv fails, or can never pass, its form."""
    if name in _EXECUTION_CONSUMERS or name.startswith('python'):
        return 'execution-consumer'
    return 'arguments' if name in _FLAGS or name in _BUILTINS else 'unknown-form'


def _has_operand(args: list[Word]) -> bool:
    """Report whether argv names a file, counting every word after `--`."""
    for index, word in enumerate(args):
        text = _argv_text(word)
        if text == '--':
            return index + 1 < len(args)
        if not text.startswith('-'):
            return True
    return False


def _is_literal(word: Word) -> bool:
    """Exclude shell expansions even when the parser stores literal parts."""
    if word.literal_text is None:
        return False
    first = next(
        (part for part in word.parts if isinstance(part, LiteralPart) and part.text), None
    )
    # Zsh expands an unquoted leading =command to a PATH executable.
    return first is None or first.quoted or not first.text.startswith('=')


def _key(token: _Token | None) -> str:
    """Read operators and unquoted reserved spellings without unquoting words."""
    if isinstance(token, Operator):
        return token.text
    # The permissive parser represents bare braces as expansions; only these
    # exact unquoted tokens are reserved in grammatical positions.
    if isinstance(token, Word) and token.raw in {token.literal_text, '{', '}'}:
        return token.raw
    return ''


def _leaf(word: Word) -> str | None:
    """Return the literal basename after the last expansion, if any."""
    if word.literal_text is not None:
        return _basename(word.literal_text)
    tail: list[str] = []
    for part in reversed(word.parts):
        if not isinstance(part, LiteralPart):
            break
        tail.append(part.text)
    text = ''.join(reversed(tail)).replace('\\', '/').rstrip('/')
    return _basename(text) if '/' in text else None


def _literal_required(name: str, args: list[Word]) -> Word | None:
    """Find the first expanding word in an option, value, mode, format, or pattern position."""
    texts = [_argv_text(word) for word in args]
    readable = {index for index, text in enumerate(texts) if text.startswith('-')}
    index = 0
    while index < len(texts) and texts[index].startswith('-') and texts[index] != '--':
        if texts[index][1:] in _VALUES.get(name, set()):
            index += 1
            readable.add(index)
        index += 1
    if index < len(texts) and texts[index] == '--':
        index += 1
    if name in {'chmod', 'printf'}:
        readable.add(0)
    if name == 'grep' and '-e' not in texts[:index]:
        readable.add(index)
    return next(
        (args[i] for i in sorted(readable) if i < len(args) and not _is_literal(args[i])), None
    )


def _local_sink_blockers(
    bodies: list[Heredoc], owned: list[ScanBlocker], defect: ScanBlocker | None, *, sink: bool
) -> list[ScanBlocker]:
    """Give each body its own defect first, else its command's one defect."""
    local: list[ScanBlocker] = []
    for body in bodies:
        if not sink:
            local.append(ScanBlocker('sink', body.span, body))
        elif (
            defect is not None
            and body.quoted
            and not any(blocker.body is body for blocker in owned)
        ):
            local.append(ScanBlocker(defect.kind, defect.span, body))
    return local


def _options(name: str, args: list[Word]) -> tuple[list[Word], dict[str, str]] | None:
    """Parse explicit short flags and separate values before literal operands."""
    index = 0
    values: dict[str, str] = {}
    ended = False
    while index < len(args):
        text = _argv_text(args[index])
        if text == '--':
            index += 1
            ended = True
            break
        if not text.startswith('-'):
            break
        option = text.removeprefix('-')
        if option in _VALUES.get(name, set()):
            if option in values or index + 1 == len(args):
                return None
            index += 1
            values[option] = _argv_text(args[index])
        elif not option or any(flag not in _FLAGS[name] for flag in option):
            return None
        index += 1
    operands = args[index:]
    if not ended and any(_argv_text(word).startswith('-') for word in operands):
        return None
    return operands, values


def _path_form(name: str, args: list[str]) -> bool:
    """Validate literal path-oriented builtins and no-options utilities."""
    if name == 'chmod':
        return (
            len(args) >= _PAIR
            and (
                re.fullmatch(r'[0-7]{3,4}', args[0]) is not None or args[0] in {'+x', 'u+x', 'a+x'}
            )
            and all(arg and not arg.startswith('-') for arg in args[1:])
        )
    if name == 'basename':
        return (
            len(args) in {1, 2} and bool(args[0]) and all(not arg.startswith('-') for arg in args)
        )
    counts = {'dirname': {1}, 'realpath': {1}}
    return len(args) in counts.get(name, set()) and all(
        arg and not arg.startswith('-') for arg in args
    )


def _builtin_form(name: str, args: list[str]) -> bool:
    """Validate builtins and the no-options path utilities by exact form."""
    if name in {'cd', 'pwd'}:
        if args and args[0] in {'-L', '-P'}:
            args = args[1:]
        if name == 'pwd':
            return not args
        return len(args) == 1 and bool(args[0]) and not args[0].startswith(('-', '+'))
    if name == 'true':
        return not args
    if name == 'echo':
        if args and args[0] == '-n':
            args = args[1:]
        return not args or not args[0].startswith('-')
    if name == 'printf':
        return (
            bool(args)
            and not args[0].startswith('-')
            and re.fullmatch(r'(?:[^%\\]|%[s%]|\\n)*', args[0]) is not None
        )
    return _path_form(name, args)


def _shared_blockers(blockers: list[ScanBlocker]) -> tuple[ScanBlocker, ...]:
    """Expose local defects to every other body denied by whole-chain validation."""
    shared = {(blocker.kind, id(blocker.span)) for blocker in blockers if blocker.body is None}
    for blocker in tuple(blockers):
        key = blocker.kind, id(blocker.span)
        if blocker.body is not None and key not in shared:
            blockers.append(ScanBlocker(blocker.kind, blocker.span))
            shared.add(key)
    blockers.sort(key=lambda blocker: blocker.span.start if blocker.span else -1)
    return tuple(blockers)


def _target_defect(targets: list[tuple[Word, str]], names: set[str]) -> ScanBlocker | None:
    """Return the first write, in source order, that is unnamed or names a command."""
    for target, unnamed in sorted(
        targets, key=lambda item: item[0].span.start if item[0].span else -1
    ):
        leaf = _leaf(target)
        if leaf is None:
            return ScanBlocker(unnamed, target.span)
        if leaf in names:
            return ScanBlocker('write-target-collision', target.span)
    return None


def _tokens(command: Command) -> tuple[list[_Token], list[Heredoc]]:
    """Flatten command syntax while retaining the original body nodes."""
    tokens: list[_Token] = []
    all_bodies: list[Heredoc] = []
    for segment in command.segments:
        for element in segment.elements:
            if isinstance(element, (Word, Operator, Heredoc)):
                tokens.append(element)
                if isinstance(element, Heredoc):
                    all_bodies.append(element)
        if segment.terminator is not None:
            span = segment.span
            if span is not None:
                span = SourceSpan(span.source, span.end - len(segment.terminator), span.end)
            tokens.append(Operator(segment.terminator, span))
    return tokens, all_bodies


def _utility_form(name: str, operands: list[Word], values: dict[str, str]) -> bool:
    """Check operand cardinality and the allowed value-option languages."""
    paths = operands[1:] if name == 'grep' and 'e' not in values else operands
    if any(not _argv_text(word) for word in paths) or len(operands) < _MINIMUM_OPERANDS.get(
        name, 0
    ):
        return False
    if name == 'mkdir' and 'm' in values:
        return re.fullmatch('[0-7]+', values['m']) is not None
    if name in {'head', 'tail'}:
        return len(values) <= 1 and all(
            re.fullmatch('[0-9]+', value) is not None and bool(value.strip('0'))
            for value in values.values()
        )
    if name == 'grep':
        return len(operands) >= (1 if 'e' in values else 2)
    if name == 'diff':
        return len(operands) == _PAIR
    return True


def _form(name: str, args: list[Word]) -> tuple[bool, list[Word], Word | None]:
    """Validate one documented argv form and collect its possible writes."""
    if name not in _FLAGS:
        valid = _builtin_form(name, [_argv_text(word) for word in args])
        return valid, args[1:] if valid and name == 'chmod' else [], None
    parsed = _options(name, args)
    if parsed is None:
        return False, [], None
    operands, values = parsed
    if not _utility_form(name, operands, values):
        return False, [], None
    # cp/mv destinations may be directories: every source leaf is a possible
    # written leaf too. Earlier writes are retained across moves and removals.
    writes = operands if name in {'tee', 'touch', 'cp', 'mv'} else []
    destination = operands[-1] if name in {'cp', 'mv'} else None
    if destination is not None and _argv_text(destination).endswith('/'):
        # A directory destination adds only the source leaves, which are already listed.
        return True, writes[:-1], None
    return True, writes, destination


def _word_kind(word: Word) -> str:
    """Classify a word as literal, path, unquoted-variable, or expansion."""
    if word.literal_text is not None:
        return 'literal' if _is_literal(word) else 'expansion'
    parts = [part for part in word.parts if not (isinstance(part, LiteralPart) and not part.text)]
    first = parts[0]
    if isinstance(first, LiteralPart) and not first.quoted and first.text.startswith('='):
        return 'expansion'
    unquoted = False
    for index, part in enumerate(parts):
        following = parts[index + 1] if index + 1 < len(parts) else None
        if isinstance(part, LiteralPart):
            continue
        if not isinstance(part, ExpansionPart) or part.children:
            return 'expansion'
        slash_next = following is None or (
            isinstance(following, LiteralPart) and following.text.startswith('/')
        )
        if part.raw == '~' and index == 0:
            if following is not None and (
                not slash_next or (isinstance(following, LiteralPart) and following.quoted)
            ):
                return 'expansion'
            continue
        match = _REFERENCE.fullmatch(part.raw)
        name = match and (match[1] or match[2])
        if (
            not name
            or name in _DYNAMIC_NAMES
            or name.startswith(_DYNAMIC_PREFIXES)
            or not slash_next
        ):
            return 'expansion'
        unquoted = unquoted or not part.in_double_quotes
    return 'unquoted-variable' if unquoted else 'path'


def _ordered_defect(
    name: str, words: list[Word], files: list[Word], span: SourceSpan | None
) -> ScanBlocker | None:
    """Return the first defect, in an order where each remedy can succeed."""
    if not words:
        return ScanBlocker('arguments', span)
    if _word_kind(words[0]) != 'literal':
        return ScanBlocker('literal-required', words[0].span)
    if words[0].raw != words[0].literal_text:
        return ScanBlocker('unknown-form', span)
    failure = _form_failure(name)
    if failure != 'arguments':
        return ScanBlocker(failure, span)
    required = _literal_required(name, words[1:])
    if required is not None:
        return ScanBlocker('literal-required', required.span)
    operands = sorted([*words[1:], *files], key=lambda word: word.span.start if word.span else -1)
    return next(
        (
            ScanBlocker(kind, word.span)
            for word in operands
            if (kind := _word_kind(word)) in {'unquoted-variable', 'expansion'}
        ),
        None,
    )


def _redirections(
    tokens: list[_Token],
) -> tuple[list[Word], list[Word], list[tuple[Word, str]], list[Heredoc], list[ScanBlocker]]:
    """Extract argv and simple file redirections, retaining local sink defects."""
    words: list[Word] = []
    files: list[Word] = []
    targets: list[tuple[Word, str]] = []
    bodies: list[Heredoc] = []
    blockers: list[ScanBlocker] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if isinstance(token, Word):
            words.append(token)
            continue
        if not isinstance(token, Operator) or token.text not in {'<', '>', '>>', '<<', '<<-'}:
            blockers.append(ScanBlocker('structure', token.span))
            continue
        descriptor = None
        if words and _adjacent_descriptor(words[-1], token):
            descriptor = words.pop().raw
        target = tokens[index] if index < len(tokens) else None
        if token.text in {'<<', '<<-'} and isinstance(target, Heredoc):
            bodies.append(target)
            if descriptor is not None and descriptor.strip('0'):
                blockers.append(ScanBlocker('sink', token.span, target))
            index += 1
        elif token.text in {'<', '>', '>>'} and isinstance(target, Word):
            files.append(target)
            if _word_kind(target) == 'literal' and not target.literal_text:
                blockers.append(ScanBlocker('arguments', target.span))
            if token.text != '<':
                targets.append((target, descriptor.lstrip('0') if descriptor is not None else '1'))
            index += 1
        else:
            blockers.append(ScanBlocker('structure', token.span))
    return words, files, targets, bodies, blockers


def _analyze_simple(tokens: list[_Token]) -> _Simple:
    """Validate one simple command, keeping at most one command defect."""
    words, files, outputs, bodies, found = _redirections(tokens)
    name = words[0].raw if words else ''
    span = _command_span(tokens)
    targets = [(target, 'unknown-target') for target, _ in outputs]
    defect = next((blocker for blocker in found if blocker.body is None), None)
    defect = defect or _ordered_defect(name, words, files, span)
    if defect is None:
        valid, writes, destination = _form(name, words[1:])
        targets.extend(
            (word, 'unknown-destination' if word is destination else 'unknown-target')
            for word in writes
        )
        if not valid:
            defect = ScanBlocker('arguments', span)
    sink = (name == 'cat' and any(descriptor == '1' for _, descriptor in outputs)) or (
        name == 'tee' and _has_operand(words[1:])
    )
    local = [blocker for blocker in found if blocker.body is not None]
    return _Simple(name, targets, bodies, local, defect, sink)


def analyze_data_writes(command: Command) -> _DataWriteAnalysis:
    """Find quoted data bodies in a wholly validated literal command structure.

    The analysis never reads the filesystem. Callers retain the historical
    single-segment exemption independently; this result grants only the chained
    exemption, using original body node identities.

    Returns:
        An analysis whose `exempt_bodies` contains eligible original heredoc
        nodes only when the entire chain passes validation, and whose
        `blockers` explains withheld exemptions. No heredocs yields empty
        tuples for both fields.
    """
    tokens, all_bodies = _tokens(command)
    if not all_bodies:
        return _DataWriteAnalysis((), ())
    blockers = [ScanBlocker('quoting', body.span, body) for body in all_bodies if not body.quoted]
    structure = _Structure(tokens)
    commands, failure_span, valid = structure.parse()
    if command.recovery or not valid:
        blockers.append(ScanBlocker('structure', failure_span or command.span))
    analyzed = [_analyze_simple(simple) for simple in commands]
    names = {simple.name.casefold() for simple in analyzed}
    if any(simple.name == 'mv' for simple in analyzed):
        names.update({'cp', 'rm'})
    bodies: list[Heredoc] = []
    for simple in analyzed:
        defect = simple.defect or _target_defect(simple.targets, names)
        blockers.extend(simple.local)
        if defect is not None:
            blockers.append(defect)
        blockers.extend(
            _local_sink_blockers(simple.bodies, simple.local, defect, sink=simple.sink)
        )
        bodies.extend(simple.bodies)
    return _DataWriteAnalysis(() if blockers else tuple(bodies), _shared_blockers(blockers))
