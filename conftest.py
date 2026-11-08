"""Share Git isolation and distribution fixtures across core and plugin tests.

Pytest loads this module to install automatic Git-configuration isolation.
Importing the shared helpers reads bundled adapter manifests and creates
process-lived temporary support directories, including a synthetic adapter
distribution.
"""

import sys
from pathlib import Path

import pytest

from tests._helpers import BUILTIN_ENTRY_POINTS, isolate_git_config, register_distribution

_isolated_git_config = pytest.fixture(autouse=True)(isolate_git_config)


@pytest.fixture
def distribution_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create and expose a temporary import root for synthetic distributions.

    Args:
        tmp_path: Temporary directory supplied by pytest.
        monkeypatch: Fixture that restores `sys.path` after the test.

    Returns:
        The new directory prepended to `sys.path` for the test.

    Raises:
        OSError: If the distribution directory cannot be created.
    """
    root = tmp_path / 'distributions'
    root.mkdir()
    monkeypatch.setattr(sys, 'path', [str(root), *sys.path])
    return root


@pytest.fixture
def registered_hosts(distribution_root: Path) -> Path:
    """Register every bundled adapter in a synthetic defsort distribution.

    Args:
        distribution_root: Import root in which to write distribution metadata.

    Returns:
        The supplied distribution root with bundled adapters registered.

    Raises:
        OSError: If the distribution metadata cannot be created or written.
    """
    register_distribution(distribution_root, 'defsort', BUILTIN_ENTRY_POINTS)
    return distribution_root
