"""Resolve Git and preserve command failure details."""

import os
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path


class GitCommandError(Exception):
    """Report a Git command failure with its actionable cause.

    Attributes:
        detail: Git's first error line or the reason Git could not be run.
    """

    def __init__(self, detail: str) -> None:
        """Record the failure detail as the exception message."""
        super().__init__(detail)
        self.detail = detail


def _windows_git(path: str | None) -> str | None:
    """Select the first regular `git.exe` in absolute Windows PATH entries."""
    for entry in (path or '').split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        candidate = directory / 'git.exe'
        if candidate.is_file():
            return str(candidate)
    return None


def _git_executable() -> str | None:
    """Find Git on the parent process's PATH before each Git launch."""
    if os.name == 'nt':
        return _windows_git(os.environ.get('PATH', ''))

    for entry in os.get_exec_path():
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        candidate = directory / 'git'
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def git_failure_detail(result: subprocess.CompletedProcess[bytes]) -> str:
    """Return Git's first nonblank stderr line, or its exit status when stderr is blank."""
    for line in result.stderr.decode(errors='replace').splitlines():
        if detail := line.strip():
            return detail
    return f'git exited with status {result.returncode}'


def run_git(
    args: Sequence[str | bytes],
    cwd: str | os.PathLike[str] | None,
    *,
    stdin: bytes | None = None,
    allowed_returncodes: Collection[int] | None = (0,),
) -> subprocess.CompletedProcess[bytes]:
    """Run Git and retain the cause of launch or command failures.

    Resolve Git afresh from absolute PATH entries, ignoring empty and relative
    entries. POSIX selects an executable `git`; Windows selects a regular
    `git.exe`. The first element of `args` is only a placeholder.

    Args:
        args: Git command and arguments, including the executable placeholder.
        cwd: Working directory, or `None` to inherit the current directory.
        stdin: Optional bytes to send to Git.
        allowed_returncodes: Status codes the caller can handle as results;
            `None` accepts every completed status.

    Returns:
        The completed process with captured stdout and stderr as bytes.

    Raises:
        GitCommandError: If Git cannot run or returns an unaccepted status.
    """
    executable = _git_executable()
    if executable is None:
        raise GitCommandError('git could not be run: Git executable not found on PATH')
    try:
        result = subprocess.run(  # noqa: S603
            [executable, *args[1:]], input=stdin, capture_output=True, cwd=cwd, check=False
        )
    except OSError as error:
        reason = error.strerror if error.strerror is not None else str(error)
        raise GitCommandError(f'git could not be run: {reason}') from error
    except ValueError as error:
        raise GitCommandError(f'git could not be run: {error}') from error
    if allowed_returncodes is not None and result.returncode not in allowed_returncodes:
        raise GitCommandError(git_failure_detail(result))
    return result
