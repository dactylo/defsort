"""Resolve Git through explicit PATH entries before launching a subprocess."""

import errno
import os
from collections.abc import Mapping
from pathlib import Path


def _find_executable(command: str, paths: list[str], cwd: Path) -> str:
    """Return the first eligible absolute candidate without resolving symlinks."""
    for directory in paths:
        candidate = (cwd / directory / command).absolute()
        if os.access(candidate, os.F_OK | os.X_OK) and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(errno.ENOENT, 'Git executable not found on PATH', command)


def _resolve_windows_git(command: str) -> str:
    """Select only an exact exe from the parent PATH and working directory."""
    path = os.environ.get('PATH')
    if not path:
        raise FileNotFoundError(errno.ENOENT, 'Git executable not found on PATH', command)
    filename = command if command.lower().endswith('.exe') else f'{command}.exe'
    return _find_executable(filename, path.split(os.pathsep), Path.cwd())


def resolve_git_executable(
    command: str = 'git',
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return an absolute Git executable selected only from explicit PATH entries.

    Windows uses the parent PATH and cwd and checks only the exact exe name,
    without PATHEXT expansion or implicit directory searches. POSIX uses the
    child PATH and cwd, including its standard default when PATH is absent.
    Explicit wrappers and symlinks retain their selected filenames.

    Args:
        command: The existing accepted `git` or `git.exe` invocation spelling.
        cwd: Subprocess cwd; relative POSIX PATH entries are based here.
        env: Subprocess environment; used for POSIX PATH lookup only.

    Raises:
        FileNotFoundError: No eligible executable exists on the selected PATH.
    """
    if os.name == 'nt':
        return _resolve_windows_git(command)
    child_cwd = Path(cwd).absolute() if cwd is not None else Path.cwd()
    return _find_executable(command, os.get_exec_path(env), child_cwd)
