"""Failures of the in-place writer."""

from pathlib import Path
from typing import override


class FileChangedError(OSError):
    """A target the writer left alone because it changed or vanished after the caller read it.

    `filename` names the target. `str()` gives only the reason, because callers
    name the path themselves.
    """

    def __init__(self, path: Path, *, removed: bool = False) -> None:
        """Describe why the target was not written.

        Args:
            path: The target the caller read.
            removed: Whether the target no longer exists, rather than holding
                other bytes.
        """
        reason = 'no longer exists' if removed else 'changed since it was read'
        super().__init__(None, f'{reason}; not written', str(path))

    @override
    def __str__(self) -> str:
        """Return the failure reason without repeating the target path."""
        return str(self.strerror)


class RestoreError(OSError):
    """A failed write that may have left the target with neither version intact.

    Either restoring the original after a write error failed, or closing the
    file failed, so the new content may not have been stored. `__cause__` is
    the write or close error, and `errno` is its number. `recovery_path` names
    the file that holds the original bytes, or is `None` when they could not be
    saved. `str()` states each error once and where the original is, without
    the target's path, which callers name themselves.
    """

    def __init__(self, error_number: int | None, message: str, recovery_path: Path | None) -> None:
        """Record the failure.

        Args:
            error_number: The write or close error's number, if it has one.
            message: The complete description, including where the original is.
            recovery_path: The recovery file, or `None` when none was saved.
        """
        super().__init__(error_number, message)
        self.recovery_path = recovery_path

    @override
    def __str__(self) -> str:
        """Return the failure reason without repeating the target path."""
        return str(self.strerror)
