"""Read Git booleans and a tool's hook setting."""

from ._run import (
    run_git,
)


def _git_config_bool(key: str, cwd: str | None = None) -> bool | None:
    """Read a Git boolean, returning `None` only when the key is unset."""
    result = run_git(
        ['git', 'config', '--type=bool', '--get', key], cwd, allowed_returncodes=(0, 1)
    )
    if result.returncode == 1:
        return None
    return result.stdout.decode(errors='replace').strip() == 'true'


def is_hook_enabled(cwd: str | None = None, *, tool: str) -> bool:
    """Read `<tool>.hook`, defaulting to enabled when it is unset.

    Args:
        cwd: Directory in which to read the configuration, or the current
            directory.
        tool: Calling tool's Git configuration namespace.

    Raises:
        GitCommandError: Git could not read a valid hook setting.
    """
    configured = _git_config_bool(f'{tool}.hook', cwd=cwd)
    return configured is not False
