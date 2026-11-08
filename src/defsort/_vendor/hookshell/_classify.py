"""Classify a pre-tool shell command into one of three outcomes."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from ._detector import CommitMatch, find_commit, is_git_executable
from ._forms import ScanBlocker
from ._grammar import (
    HINT_COMMIT,
    CommitPlan,
    NoCommit,
    Rejection,
    classify_grammar,
    has_outer_commit,
)
from ._shellparse import parse_command
from ._source import SourceSpan, resolve_span

_C0_LIMIT = 0x20
_C1_END = 0x9F
_DEL_CODEPOINT = 0x7F
_EXCERPT_LIMIT = 80
_EXPLICIT_COMMIT = 'to commit, use the supported form: [cd DIR &&] [git add ...;] git commit ...'
_FRAGMENT_LIMIT = 40

type _Outcome = Literal['unrelated', 'supported', 'unsupported']


@dataclass(frozen=True, slots=True)
class Classification:
    """Outcome of classifying one command.

    Attributes:
        outcome: `unrelated`, `supported`, or `unsupported`.
        plan: Resolved commit plan, or `NoCommit`, for a supported command.
        reason: Deny reason for an unsupported command.
    """

    outcome: _Outcome
    plan: CommitPlan | NoCommit | None = None
    reason: str | None = None


def _body_defect(blocker: ScanBlocker, fragment: str) -> tuple[str, str]:
    """Distinguish the matched body's defect from another write's defect."""
    if blocker.kind == 'quoting':
        if blocker.body is not None:
            return 'the heredoc delimiter is unquoted', "quote it (<<'EOF') if the body is data"
        return (
            f"another heredoc delimiter '{fragment}' is unquoted",
            "quote that delimiter (<<'EOF') if the body is data",
        )
    if blocker.body is not None:
        return (
            'the heredoc has no eligible file target',
            'write the body to a file with a documented cat or tee form if it is data',
        )
    return (
        f"another heredoc write '{fragment}' has no eligible file target",
        'write that body to a file with a documented cat or tee form if it is data',
    )


def _escape_controls(text: str) -> str:
    """Render C0, DEL, and C1 controls as lowercase hexadecimal escapes."""
    return ''.join(
        f'\\x{codepoint:02x}'
        if codepoint < _C0_LIMIT or _DEL_CODEPOINT <= codepoint <= _C1_END
        else character
        for character in text
        for codepoint in (ord(character),)
    )


def _deny_reason(
    rejection: Rejection, reason_prefix: str, commit_hint: Callable[[], str] | None = None
) -> str:
    """Render a sanitized deny reason for a rejected command."""
    hint = rejection.hint
    if hint == HINT_COMMIT and commit_hint is not None:
        with suppress(Exception):
            hint = commit_hint()
    return _escape_controls(f'{reason_prefix}: {rejection.element}; {hint}')


def _excerpt(span: SourceSpan) -> str:
    """Center a bounded original-line excerpt on the triggering token."""
    text = span.source.text
    line_start = text.rfind('\n', 0, span.start) + 1
    line_end = text.find('\n', span.start)
    if line_end < 0:
        line_end = len(text)
    start = max(line_start, (span.start + span.end) // 2 - _EXCERPT_LIMIT // 2)
    end = min(line_end, start + _EXCERPT_LIMIT)
    start = max(line_start, end - _EXCERPT_LIMIT)
    return (
        ('...' if start > line_start else '') + text[start:end] + ('...' if end < line_end else '')
    )


def _location(match: CommitMatch) -> str:
    """Render mapped evidence without inventing positions for unknown text."""
    if match.kind == 'unknown-substitution':
        return 'command substitution with unknown content (scan depth limit reached)'
    span = match.span
    body = resolve_span(span, context='heredoc') if span else None
    if body is not None:
        line = body.source.text.count('\n', 0, body.start) + 1
        location = f'heredoc body line {line}'
        related = (
            resolve_span(match.related_span, context='heredoc') if match.related_span else None
        )
        if related is not None and related.source is body.source:
            earlier_line = related.source.text.count('\n', 0, related.start) + 1
            if earlier_line != line:
                earlier_text = (
                    match.related_span.source.text[
                        match.related_span.start : match.related_span.end
                    ]
                    if match.related_span
                    else ''
                )
                word = 'git' if is_git_executable(earlier_text) else 'earlier word or expansion'
                location += f' ({word} on line {earlier_line})'
        return f"{location} '{_excerpt(body)}'"
    context = match.contexts[-1].source.context if match.contexts else 'command text'
    if context == 'heredoc':
        context = 'heredoc body'
    original = resolve_span(span, context='command') if span else None
    return f"{context} '{_excerpt(original)}'" if original else context


def _quoting_example(span: SourceSpan | None) -> str:
    """Suggest quotes that keep a leading home reference expandable."""
    word = span.source.text[span.start : span.end] if span else ''
    home, rest = ('~/', word[2:]) if word.startswith('~/') else ('', word)
    example = f'{home}"{rest}"'
    if not rest or any(char in word for char in '"\'\\') or len(example) > _FRAGMENT_LIMIT:
        return '"$TMPDIR/notes.md"'
    return example


def _form_reason(blocker: ScanBlocker, fragment: str) -> tuple[str, str]:
    """Explain unsupported arguments, expansions, and file path forms."""
    if blocker.kind == 'arguments':
        return (
            f"'{fragment}' has unsupported arguments or expansions",
            'use literal arguments in a documented file-writing form if the body is data',
        )
    if blocker.kind == 'unquoted-variable':
        return (
            f"'{fragment}' has an unquoted variable",
            f'quote it, as in {_quoting_example(blocker.span)}, if the body is data',
        )
    if blocker.kind == 'literal-required':
        return (
            f"'{fragment}' needs literal text in this position",
            'use literal text there if the body is data',
        )
    if blocker.kind in {'unknown-target', 'unknown-destination'}:
        return (
            f"write target '{fragment}' does not name a file",
            (
                'add a trailing / if it is a directory, or name the file literally, '
                'if the body is data'
                if blocker.kind == 'unknown-destination'
                else 'name the file literally, as in "$TMPDIR/notes.md", if the body is data'
            ),
        )
    remedy = (
        'use literal text, ~/, or a quoted variable such as "$TMPDIR/notes.md" if the body is data'
    )
    return f"'{fragment}' has unsupported arguments or expansions", remedy


def _scan_reason(match: CommitMatch) -> tuple[str, str]:
    """Choose an actual blocker and its cause-specific remedy."""
    blocker = match.blocker
    if blocker is None:
        cause = (
            'the substitution could not be fully inspected'
            if match.kind == 'unknown-substitution'
            else 'the text matches conservative commit detection'
        )
        return cause, _EXPLICIT_COMMIT
    span = blocker.span
    fragment = (
        span.source.text[span.start : span.end][:_FRAGMENT_LIMIT].rstrip()
        if span
        else 'unknown form'
    )
    if blocker.kind in {'quoting', 'sink'}:
        return _body_defect(blocker, fragment)
    if blocker.kind in {'execution-consumer', 'write-target-collision'}:
        return (
            (f"'{fragment}' can run the written file", _EXPLICIT_COMMIT)
            if blocker.kind == 'execution-consumer'
            else (
                f"write target '{fragment}' can replace a command",
                f'embedded commit text remains scanned; {_EXPLICIT_COMMIT}',
            )
        )
    if blocker.kind in {
        'arguments',
        'unquoted-variable',
        'literal-required',
        'unknown-target',
        'unknown-destination',
        'expansion',
    }:
        return _form_reason(blocker, fragment)
    if blocker.kind == 'structure':
        return (
            f"'{fragment}' contains a pipe or unsupported shell syntax",
            'only documented file-writing forms receive the quoted-body exemption',
        )
    return (
        f"'{fragment}' is not a recognized command",
        (
            'a quoted heredoc body skips scanning only when every command in the chain '
            'is a documented form'
        ),
    )


def _possible_reason(match: CommitMatch, prefix: str) -> str:
    """Render possible evidence separately from proven outer-commit reasons."""
    cause, remedy = _scan_reason(match)
    return _escape_controls(f'{prefix}: {_location(match)}; scanned because {cause}; {remedy}')


def classify_command(
    text: str,
    command_cwd: str | None,
    *,
    reason_prefix: str,
    possible_reason_prefix: str | None = None,
    commit_hint: Callable[[], str] | None = None,
) -> Classification:
    """Classify a shell command as unrelated, supported, or unsupported.

    Args:
        text: Shell command text from the hook payload.
        command_cwd: Absolute directory the command executes in, or `None` when
            unknown.
        reason_prefix: Caller-specific prefix for unsupported-command reasons.
        possible_reason_prefix: Full prefix for possible matches; None selects
            the neutral `possible commit text` prefix.
        commit_hint: Optional callback returning the caller's commit-options
            hint.

    Returns:
        The classification. The grammar decides first; a rejected command is
        unsupported when the detector finds a commit and unrelated otherwise.
        A committing command with an unknown directory and no absolute route is
        unsupported.
    """
    command = parse_command(text)
    result = classify_grammar(command, command_cwd)
    if isinstance(result, Rejection):
        match = find_commit(command)
        if match is not None:
            reason = (
                _deny_reason(result, reason_prefix, commit_hint)
                if has_outer_commit(command)
                else _possible_reason(
                    match,
                    possible_reason_prefix
                    if possible_reason_prefix is not None
                    else 'possible commit text',
                )
            )
            return Classification('unsupported', reason=reason)
        return Classification('unrelated')
    return Classification('supported', plan=result)
