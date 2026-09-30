"""Detect commit invocations in commands the grammar rejected."""

from collections.abc import Iterator

from defsort._shellparse import (
    Command,
    ExpansionPart,
    Heredoc,
    Segment,
    SubstitutionPart,
    Word,
    parse_command,
)

_EVALUATOR = 'eval'
_GIT_WORDS = frozenset({'git', 'git.exe'})
_INTERPRETER_PREFIX = 'python'
_INTERPRETERS = frozenset({'node', 'perl', 'ruby'})
_SCRIPT_OPTIONS = frozenset({'-c', '-e'})
_SHELLS = frozenset({'bash', 'dash', 'ksh', 'sh', 'zsh'})
_TOKEN_BREAKS = str.maketrans(dict.fromkeys('\'"[](){},;=', ' '))


def _basename(text: str) -> str:
    """Return the lower-cased final path component of a literal word."""
    return text.replace('\\', '/').rsplit('/', 1)[-1].lower()


def _is_interpreter(text: str) -> bool:
    """Return whether a literal word names a listed script interpreter."""
    name = _basename(text).removesuffix('.exe')
    return name in _INTERPRETERS or name.startswith(_INTERPRETER_PREFIX)


def _is_script_option(text: str) -> bool:
    """Return whether a word is a short-option cluster containing `c`."""
    return text.startswith('-') and not text.startswith('--') and 'c' in text[1:]


def _evaluator_scripts(texts: list[str | None]) -> Iterator[str]:
    """Yield the script sources at every listed evaluator position."""
    for start, first in enumerate(texts):
        if first is None:
            continue
        name = _basename(first)
        if name == _EVALUATOR:
            yield ' '.join(text for text in texts[start + 1 :] if text is not None)
        elif name.removesuffix('.exe') in _SHELLS:
            for index, text in enumerate(texts[start + 1 :], start + 1):
                if text is not None and _is_script_option(text):
                    yield from (later for later in texts[index + 1 :] if later is not None)
                    break


def _segment_scripts(segment: Segment) -> Iterator[str]:
    """Yield heredoc bodies and evaluator scripts without parsing them."""
    for element in segment.elements:
        if isinstance(element, Heredoc):
            yield element.body
    texts = [element.literal_text for element in segment.elements if isinstance(element, Word)]
    yield from _evaluator_scripts(texts)


def _segment_substitutions(segment: Segment) -> Iterator[SubstitutionPart]:
    """Yield already-parsed substitutions in words, expansions, and heredocs."""
    for element in segment.elements:
        if isinstance(element, Heredoc):
            yield from element.expansions
        elif isinstance(element, Word):
            for part in element.parts:
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


def _tokens_commit(text: str) -> bool:
    """Return whether quote-stripped tokens hold a git word followed by commit."""
    tokens = text.translate(_TOKEN_BREAKS).split()
    return any(
        is_git_executable(token) and 'commit' in tokens[index + 1 :]
        for index, token in enumerate(tokens)
    )


def _interpreter_commits(texts: list[str | None]) -> bool:
    """Scan the literal script after a listed interpreter's -c or -e option."""
    for index, text in enumerate(texts):
        if text is None or not _is_interpreter(text):
            continue
        for option_index in range(index + 1, len(texts)):
            if texts[option_index] in _SCRIPT_OPTIONS:
                script = texts[option_index + 1] if option_index + 1 < len(texts) else None
                return script is not None and _tokens_commit(script)
    return False


def _segment_commits(segment: Segment) -> bool:
    """Apply direct word and token rules without expanding nested scripts."""
    words = [element for element in segment.elements if isinstance(element, Word)]
    texts = [word.literal_text for word in words]
    for index, text in enumerate(texts):
        if (text is None or is_git_executable(text)) and 'commit' in texts[index + 1 :]:
            return True
    for element in segment.elements:
        if isinstance(element, Heredoc) and _tokens_commit(element.body):
            return True
    return _interpreter_commits(texts)


def detect_commit(command: Command) -> bool:
    """Conservatively detect possible commits in a parsed command tree.

    Args:
        command: Parsed command tree, clean or recovered.

    Returns:
        `True` when a segment, nested substitution, heredoc body, or evaluator
        script matches a commit-detection rule. A depth-truncated substitution
        also returns `True` because its contents could not be inspected.
    """
    pending = [command]
    seen_scripts: set[str] = set()
    while pending:
        for segment in pending.pop().segments:
            if _segment_commits(segment):
                return True
            for part in _segment_substitutions(segment):
                if part.truncated:
                    return True
                pending.append(part.command)
            for script in _segment_scripts(segment):
                if script not in seen_scripts:
                    seen_scripts.add(script)
                    pending.append(parse_command(script))
    return False
