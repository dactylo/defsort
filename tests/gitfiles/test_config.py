import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _config,
)
from defsort._vendor.gitfiles._config import (
    _git_config_bool,
    is_hook_enabled,
)
from defsort._vendor.gitfiles._run import (
    GitCommandError,
)

from ._support import (
    git,
)


def test_boolean_launch_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError('no git')

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(GitCommandError, match='git could not be run: no git'):
        _git_config_bool('test.key')


@pytest.mark.parametrize(('raw', 'expected'), [(b'true\n', True), (b'false\n', False)])
def test_boolean_result(monkeypatch: pytest.MonkeyPatch, raw: bytes, *, expected: bool) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert Path(command[0]).is_absolute()
        assert command[1:] == ['config', '--type=bool', '--get', 'test.key']
        return subprocess.CompletedProcess(command, 0, raw, b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert _git_config_bool('test.key') is expected


def test_broken_config_raises(repo: Path) -> None:
    config = repo / '.git' / 'config'
    config.write_bytes(config.read_bytes() + b'\n[broken\n')
    with pytest.raises(GitCommandError) as caught:
        is_hook_enabled(str(repo), tool='tool')
    assert caught.value.detail


def test_hook_launch_failure_raises(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, 'missing executable')

    monkeypatch.setattr(subprocess, 'run', missing)
    with pytest.raises(GitCommandError, match='git could not be run: missing executable'):
        is_hook_enabled(str(isolated), tool='tool')


def test_hook_setting_uses_explicit_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, str | None]] = []

    def configured(key: str, cwd: str | None = None) -> bool | None:
        observed.append((key, cwd))
        return False

    monkeypatch.setattr(_config, '_git_config_bool', configured)
    assert is_hook_enabled('/repo', tool='tool') is False
    assert observed == [('tool.hook', '/repo')]


def test_malformed_hook_value_raises(repo: Path) -> None:
    git('config', 'tool.hook', 'flase', cwd=repo)
    with pytest.raises(GitCommandError) as caught:
        is_hook_enabled(str(repo), tool='tool')
    assert caught.value.detail


def test_non_utf8_hook_value_raises(repo: Path) -> None:
    config = repo / '.git' / 'config'
    config.write_bytes(config.read_bytes() + b'\n[tool]\n hook = \xff\n')
    with pytest.raises(GitCommandError) as caught:
        is_hook_enabled(str(repo), tool='tool')
    assert caught.value.detail


def test_reads_absent_boolean_from_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    assert _git_config_bool('test.nonexistent') is None


@pytest.mark.parametrize('explicit_cwd', [False, True])
@pytest.mark.parametrize('value', [False, True])
def test_reads_boolean_from_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, explicit_cwd: bool, value: bool
) -> None:
    git('config', 'test.key', str(value).lower(), cwd=repo)
    monkeypatch.chdir(repo.parent if explicit_cwd else repo)
    assert _git_config_bool('test.key', cwd=str(repo) if explicit_cwd else None) is value


@pytest.mark.parametrize(('value', 'expected'), [(False, False), (True, True), (None, True)])
def test_hook_setting_uses_callers_tool_name(
    monkeypatch: pytest.MonkeyPatch, *, value: bool | None, expected: bool
) -> None:
    observed: list[tuple[str, str | None]] = []

    def configured(key: str, cwd: str | None = None) -> bool | None:
        observed.append((key, cwd))
        return value

    monkeypatch.setattr(_config, '_git_config_bool', configured)
    assert is_hook_enabled(tool='tool') is expected
    assert observed == [('tool.hook', None)]


@pytest.mark.parametrize(('value', 'expected'), [('false', False), ('true', True)])
def test_real_hook_setting(repo: Path, value: str, *, expected: bool) -> None:
    git('config', 'tool.hook', value, cwd=repo)
    assert is_hook_enabled(str(repo), tool='tool') is expected


def test_unset_boolean_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b'', b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert _git_config_bool('test.key') is None


def test_unset_hook_defaults_enabled(repo: Path) -> None:
    assert is_hook_enabled(str(repo), tool='tool') is True
