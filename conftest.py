"""Share Git isolation and distribution fixtures across core and plugin tests."""

import sys
from pathlib import Path

import pytest

from tests._helpers import BUILTIN_ENTRY_POINTS, isolate_git_config, register_distribution

_isolated_git_config = pytest.fixture(autouse=True)(isolate_git_config)


@pytest.fixture
def distribution_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary import root for synthetic package distributions."""
    root = tmp_path / 'distributions'
    root.mkdir()
    monkeypatch.setattr(sys, 'path', [str(root), *sys.path])
    return root


@pytest.fixture
def registered_hosts(distribution_root: Path) -> Path:
    """Register every bundled adapter from its plugin-local manifest."""
    register_distribution(distribution_root, 'defsort', BUILTIN_ENTRY_POINTS)
    return distribution_root
