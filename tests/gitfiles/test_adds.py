import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _run,
)
from defsort._vendor.gitfiles._adds import (
    run_pre_commit_adds,
)
from defsort._vendor.gitfiles._listings import (
    get_staged_py_info,
)
from defsort._vendor.hookshell._grammar import (
    CommitPlan,
    PreCommitAdd,
    classify_grammar,
)
from defsort._vendor.hookshell._shellparse import (
    parse_command,
)

from ._support import (
    git,
)


def _add(root: Path, *tokens: str) -> PreCommitAdd:
    return PreCommitAdd(tokens=('git', *tokens), exec_cwd=str(root), git_cwd=str(root))


def test_adds_files_to_index(repo: Path) -> None:
    (repo / 'foo.py').write_text('x = 1\n')
    assert run_pre_commit_adds((_add(repo, 'add', 'foo.py'),)) is None
    assert [item.path for item in get_staged_py_info(str(repo))] == ['foo.py']


def test_empty_adds_launch_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('empty add replay launched Git')

    monkeypatch.setattr(subprocess, 'run', unexpected)
    assert run_pre_commit_adds(()) is None


def test_missing_file_returns_failure(repo: Path) -> None:
    detail = run_pre_commit_adds((_add(repo, 'add', 'missing.py'),))
    assert detail is not None
    assert 'missing.py' in detail


def test_multiple_adds_replay_in_order(repo: Path) -> None:
    (repo / 'a.py').write_text('a = 1\n')
    (repo / 'b.py').write_text('b = 2\n')
    adds = (_add(repo, 'add', 'a.py'), _add(repo, 'add', 'b.py'))
    assert run_pre_commit_adds(iter(adds)) is None
    assert [item.path for item in get_staged_py_info(str(repo))] == ['a.py', 'b.py']


def test_prior_success_is_preserved_and_later_add_skipped(repo: Path) -> None:
    (repo / 'first.py').write_text('first = 1\n')
    (repo / 'later.py').write_text('later = 1\n')
    detail = run_pre_commit_adds(
        (
            _add(repo, 'add', 'first.py'),
            _add(repo, 'add', 'missing.py'),
            _add(repo, 'add', 'later.py'),
        )
    )
    assert detail is not None
    assert [item.path for item in get_staged_py_info(str(repo))] == ['first.py']


def test_replay_launch_failure_names_reason(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(subprocess, 'run', fail)
    assert run_pre_commit_adds((_add(isolated, 'add', 'a.py'),)) == (
        'git could not be run: Permission denied'
    )


def test_replay_missing_git(monkeypatch: pytest.MonkeyPatch, isolated: Path) -> None:
    monkeypatch.setattr(_run, '_git_executable', lambda: None)
    assert run_pre_commit_adds((_add(isolated, 'add', 'a.py'),)) == (
        'git could not be run: Git executable not found on PATH'
    )


def test_replay_reports_an_argument_the_os_cannot_pass(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    detail = run_pre_commit_adds((_add(isolated, 'add', 'a\0b.py'),))
    assert detail is not None
    assert detail.startswith('git could not be run: ')

    def invalid(*_args: object, **_kwargs: object) -> None:
        raise ValueError('invalid subprocess argument')

    monkeypatch.setattr(subprocess, 'run', invalid)
    assert run_pre_commit_adds((_add(isolated, 'add', 'a.py'),)) == (
        'git could not be run: invalid subprocess argument'
    )


def test_replay_stops_at_first_git_failure(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    calls: list[list[str]] = []

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b'', b'first failure\n')

    monkeypatch.setattr(subprocess, 'run', fail)
    assert run_pre_commit_adds(
        (_add(isolated, 'add', 'first.py'), _add(isolated, 'add', 'later.py'))
    ) == ('first failure')
    assert len(calls) == 1
    assert Path(calls[0][0]).is_absolute()
    assert calls[0][1:] == ['add', 'first.py']


def test_replays_literal_argv_and_exec_cwd(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    add = PreCommitAdd(
        tokens=('git', '-C', 'sub', 'add', 'mod.py'),
        exec_cwd=str(isolated),
        git_cwd=str(isolated / 'sub'),
    )
    captured: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(command)
        assert Path(command[0]).is_absolute()
        assert kwargs['cwd'] == add.exec_cwd
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert run_pre_commit_adds((add,)) is None
    assert captured[0][1:] == ['-C', 'sub', 'add', 'mod.py']


def test_replays_parser_tokens_in_execution_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = repo / 'file with spaces.py'
    target.write_text('value = 1\n')
    plan = classify_grammar(
        parse_command('git add -- "file with spaces.py" && git commit -m "test"'), str(repo)
    )
    assert isinstance(plan, CommitPlan)
    add = plan.pre_commit_adds[0]
    calls: list[list[str]] = []
    original_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs == {
            'input': None,
            'capture_output': True,
            'cwd': add.exec_cwd,
            'check': False,
        }
        calls.append(command)
        return original_run(command, cwd=add.exec_cwd, capture_output=True, check=False)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'run', run)
        assert run_pre_commit_adds(plan.pre_commit_adds) is None
    assert len(calls) == 1
    assert Path(calls[0][0]).is_absolute()
    assert calls[0][1:] == list(add.tokens[1:])
    assert git('ls-files', '-z', cwd=repo).stdout == b'file with spaces.py\0'
