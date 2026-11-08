"""Replay parsed Git adds preceding a commit."""

from collections.abc import Iterable

from defsort._vendor.hookshell._grammar import (
    PreCommitAdd,
)

from ._run import (
    GitCommandError,
    run_git,
)


def run_pre_commit_adds(adds: Iterable[PreCommitAdd]) -> str | None:
    """Replay adds and return the first Git failure detail, if any.

    Args:
        adds: Parsed Git add commands in execution order.

    Returns:
        `None` after every add succeeds, or the first failure detail.
    """
    for add in adds:
        try:
            run_git(add.tokens, add.exec_cwd)
        except GitCommandError as error:
            return error.detail
    return None
