"""Track commit worktree writes and restore them after a later failure."""

import os
from pathlib import Path

from defsort._vendor.srcfiles import (
    write_in_place,
)

from ._front import (
    Outcome,
    describe_error,
    fail,
)
from ._terminal import (
    path_text,
)


class CommitWrites:
    """Track successful writes so a later failure can restore their bytes."""

    def __init__(self, tool: str, repo_root: str | os.PathLike[str]) -> None:
        """Associate writes with a tool and repository for diagnostics.

        Args:
            tool: Tool name used in diagnostics and recovery writes.
            repo_root: Root against which target names are reported.
        """
        self._tool = tool
        self._repo_root = repo_root
        self._written: list[tuple[Path, bytes, bytes]] = []

    def failed(self, path: Path, error: OSError, *, allow_on_failure: bool) -> Outcome | None:
        """Report a failed write and attempt to restore prior writes when denying.

        Args:
            path: Target whose write failed.
            error: Write failure to describe.
            allow_on_failure: Warn and continue without restoring prior writes.

        Returns:
            The denial outcome, including any restoration failures in its
            reason, or `None` after printing a warning.

        Raises:
            OSError: If diagnostic output or determining a relative diagnostic
                path fails.
            ValueError: If a target and the repository root are on different
                Windows drives.
        """
        name = path_text(os.path.relpath(path, self._repo_root))
        reason = f'{self._tool}: cannot write {name}: {describe_error(error)}'
        if allow_on_failure:
            fail(reason, allow_on_failure=True)
            return None
        not_restored: list[str] = []
        for written_path, original, data in self._written:
            try:
                write_in_place(written_path, original, expected=data, tool=self._tool)
            except OSError as restore_error:
                written_name = path_text(os.path.relpath(written_path, self._repo_root))
                not_restored.append(f'{written_name} ({describe_error(restore_error)})')
        if not_restored:
            reason += '; not restored: ' + '; '.join(not_restored)
        return fail(reason, allow_on_failure=False)

    def record(self, path: Path, original: bytes, data: bytes) -> None:
        """Record the bytes before and after a successful write.

        Args:
            path: Target just written.
            original: Target bytes before the write.
            data: Target bytes after the write.
        """
        self._written.append((path, original, data))
