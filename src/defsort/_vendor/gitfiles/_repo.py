"""Classify Git worktree discovery with exact root paths."""

import enum
from dataclasses import dataclass
from pathlib import Path

from defsort._vendor.hookshell._shell import (
    normalize_msys_path,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
)

from ._run import (
    GitCommandError,
    git_failure_detail,
    run_git,
)


class RepoDiscoveryStatus(enum.Enum):
    """Outcome of checked Git worktree discovery.

    Attributes:
        WORKTREE: A worktree root was discovered.
        NOT_WORKTREE: The path is outside a worktree or inside a bare
            repository.
        ERROR: Git or repository inspection could not establish the state.
    """

    WORKTREE = 'worktree'
    NOT_WORKTREE = 'not-worktree'
    ERROR = 'error'


@dataclass(frozen=True, slots=True)
class RepoDiscoveryResult:
    """Checked discovery outcome with a root only for a worktree.

    Attributes:
        status: Worktree, confirmed non-worktree, or error classification.
        root: Nonempty worktree root for `WORKTREE`; otherwise `None`.
        detail: Failure reason, permitted only for `ERROR`.
    """

    status: RepoDiscoveryStatus
    root: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """Reject inconsistent root and detail combinations.

        Raises:
            ValueError: If fields do not match the discovery status.
        """
        valid_root = (
            bool(self.root) if self.status is RepoDiscoveryStatus.WORKTREE else self.root is None
        )
        if not valid_root:
            raise ValueError('invalid repository discovery result')
        if self.detail is not None and self.status is not RepoDiscoveryStatus.ERROR:
            raise ValueError('invalid repository discovery result detail')


def _git_marker_present(cwd: str) -> bool | None:
    """Return whether cwd or a parent has `.git`, or `None` on error."""
    try:
        path = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_dir():
        return None
    for directory in (path, *path.parents):
        try:
            (directory / '.git').lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return True
    return False


def _classify_failed_toplevel(cwd: str, detail: str) -> RepoDiscoveryResult:
    """Classify a failed toplevel probe with a bare probe and marker check."""
    try:
        bare_result = run_git(
            ['git', 'rev-parse', '--is-bare-repository'], cwd, allowed_returncodes=(0, 128)
        )
    except GitCommandError:
        return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail=detail)
    if bare_result.returncode == 0:
        if bare_result.stdout.strip() == b'true':
            return RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)
        return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail=detail)
    if _git_marker_present(cwd) is False:
        return RepoDiscoveryResult(RepoDiscoveryStatus.NOT_WORKTREE)
    return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail=detail)


def discover_repo_root(cwd: str) -> RepoDiscoveryResult:
    """Classify a directory as a worktree, non-worktree, or discovery error.

    Args:
        cwd: Directory from which Git should inspect the repository.

    Returns:
        The checked status and exact normalized root, when a worktree exists.
    """
    try:
        result = run_git(
            ['git', 'rev-parse', '--show-toplevel'], cwd, allowed_returncodes=(0, 128)
        )
    except GitCommandError as error:
        return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail=error.detail)
    if result.returncode == 0:
        try:
            root = normalize_msys_path(decode_git_path(result.stdout.removesuffix(b'\n')))
        except UnicodeDecodeError as error:
            return RepoDiscoveryResult(RepoDiscoveryStatus.ERROR, detail=error.reason)
        if root:
            return RepoDiscoveryResult(RepoDiscoveryStatus.WORKTREE, root)
        return RepoDiscoveryResult(
            RepoDiscoveryStatus.ERROR, detail='git rev-parse --show-toplevel printed no path'
        )

    return _classify_failed_toplevel(cwd, git_failure_detail(result))
