import os
from pathlib import Path

import pytest

from ._support import (
    git,
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_ATTR_NOSYSTEM', '1')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test User')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.invalid')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test User')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.invalid')
    for name in (
        'GIT_CONFIG',
        'GIT_CONFIG_COUNT',
        'GIT_CONFIG_KEY_0',
        'GIT_CONFIG_VALUE_0',
        'GIT_CONFIG_PARAMETERS',
        'GIT_DIR',
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def repo(isolated: Path) -> Path:
    git('init', '-q', cwd=isolated)
    return isolated
