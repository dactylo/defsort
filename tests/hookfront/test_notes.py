from pathlib import PureWindowsPath

import pytest

from defsort._vendor.hookfront import (
    _terminal,
)
from defsort._vendor.hookfront._notes import (
    OUTSIDE_REPOSITORY,
    git_unavailable_message,
    git_unavailable_reason,
    post_tool_note,
    sync_note,
    syntax_reason,
    unstaged_reason,
)


@pytest.mark.parametrize(
    ('incremental', 'guard', 'reason', 'message'),
    [
        (
            True,
            False,
            'incremental mode cannot use Git: fatal: x',
            (
                'incremental mode cannot use Git: fatal: x; '
                'pass --no-incremental to process whole files'
            ),
        ),
        (
            False,
            True,
            'the reorder guard cannot use Git: fatal: x',
            'the reorder guard cannot use Git: fatal: x',
        ),
        (
            True,
            True,
            'incremental mode and the reorder guard cannot use Git: fatal: x',
            'incremental mode and the reorder guard cannot use Git: fatal: x',
        ),
    ],
)
def test_git_unavailable_wording(
    *, incremental: bool, guard: bool, reason: str, message: str
) -> None:
    assert git_unavailable_reason('fatal: x', incremental=incremental, guard=guard) == reason
    assert git_unavailable_message('fatal: x', incremental=incremental, guard=guard) == message


def test_post_tool_note_escapes_controls_once() -> None:
    assert post_tool_note('tool', 'a\x1b.py', OUTSIDE_REPOSITORY) == (
        'tool: PostToolUse skipped a\\x1b.py: not in a Git repository'
    )


def test_sync_note_displays_native_windows_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_terminal, 'PurePath', PureWindowsPath)
    assert unstaged_reason([r'pkg\m.py'], of=2) == 'pkg/m.py has unstaged changes'
    assert sync_note('tool', [r'pkg\m.py'], 'it has unstaged changes', result='wrapped') == (
        'tool: did not update pkg/m.py in the working tree: it has unstaged changes; '
        'the commit has the wrapped version'
    )


def test_sync_note_uses_shared_worktree_wording() -> None:
    assert sync_note('tool', ['m.py'], unstaged_reason(['m.py'], of=1), result='sorted') == (
        'tool: did not update m.py in the working tree: it has unstaged changes; '
        'the commit has the sorted version'
    )
    assert sync_note('tool', ['a\x1b.py'], 'blocked\x07', result='wrapped') == (
        'tool: did not update a\\x1b.py in the working tree: blocked\\x07; '
        'the commit has the wrapped version'
    )


def test_syntax_reason_uses_line_only_when_known() -> None:
    assert syntax_reason(SyntaxError('invalid syntax', ('m.py', 3, 1, 'x'))) == (
        'syntax error: invalid syntax (line 3)'
    )
    assert syntax_reason(SyntaxError('too deep')) == 'syntax error: too deep'


@pytest.mark.parametrize(
    ('names', 'expected'),
    [
        (['A.py'], 'A.py has unstaged changes'),
        (['a.py', 'A.py'], 'a.py, A.py have unstaged changes'),
    ],
)
def test_unstaged_reason_for_one_name_of_many_or_multiple_names(
    names: list[str], expected: str
) -> None:
    assert unstaged_reason(names, of=2) == expected
