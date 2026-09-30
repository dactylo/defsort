"""Classify a pre-tool shell command into one of three outcomes."""

from dataclasses import dataclass
from typing import Literal

from defsort._detector import detect_commit
from defsort._display._terminal import escape_terminal_text
from defsort._grammar import CommitPlan, NoCommit, Rejection, classify_grammar
from defsort._shellparse import parse_command

_REASON_PREFIX = 'defsort: unsupported commit command'

type _Outcome = Literal['unrelated', 'supported', 'unsupported']


@dataclass(frozen=True, slots=True)
class Classification:
    """Outcome of classifying one command.

    Args:
        outcome: `unrelated`, `supported`, or `unsupported`.
        plan: Resolved commit plan, or `NoCommit`, for a supported command.
        reason: Deny reason for an unsupported command.
    """

    outcome: _Outcome
    plan: CommitPlan | NoCommit | None = None
    reason: str | None = None


def _deny_reason(rejection: Rejection) -> str:
    """Render one sanitized reason for a rejected command."""
    return escape_terminal_text(f'{_REASON_PREFIX}: {rejection.element}; {rejection.hint}')


def classify_command(text: str, command_cwd: str | None) -> Classification:
    """Classify a shell command as unrelated, supported, or unsupported.

    Args:
        text: Shell command text from the hook payload.
        command_cwd: Absolute directory the command executes in, or `None` when
            unknown.

    Returns:
        The classification. The grammar decides first; a rejected command is
        unsupported when the detector finds a commit and unrelated otherwise.
        A committing command with an unknown directory and no absolute route is
        unsupported.
    """
    command = parse_command(text)
    result = classify_grammar(command, command_cwd)
    if isinstance(result, Rejection):
        if detect_commit(command):
            return Classification('unsupported', reason=_deny_reason(result))
        return Classification('unrelated')
    return Classification('supported', plan=result)
