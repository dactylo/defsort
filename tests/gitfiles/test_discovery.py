import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from defsort._vendor.gitfiles import (
    _discovery,
)
from defsort._vendor.gitfiles._discovery import (
    _SKIP_DIRS,
    _checked_private_attribute,
    _filter_private,
    collect_file_groups,
    collect_files,
)
from defsort._vendor.hookfront._terminal import (
    path_text,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
)

from ._support import (
    git,
    latin1_encode,
)


def _add_marker_repositories(parent: Path, count: int) -> None:
    for index in range(count):
        root = parent / f'repo{index}'
        (root / '.git').mkdir(parents=True)
        (root / 'module.py').write_text('x = 1\n', encoding='utf-8')


def _record_walk(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], list[Path]]:
    tops: list[Path] = []
    visited: list[Path] = []
    real_walk = os.walk

    def recording_walk(
        top: Path,
        *,
        topdown: bool = True,
        followlinks: bool = False,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        tops.append(top)
        for root, dirnames, filenames in real_walk(top, topdown=topdown, followlinks=followlinks):
            visited.append(Path(root))
            yield root, dirnames, filenames

    monkeypatch.setattr(_discovery.os, 'walk', recording_walk)
    return tops, visited


def test_aliases_share_a_group_in_first_seen_order(isolated: Path) -> None:
    a = isolated / 'a.py'
    a.write_text('x = 1\n', encoding='utf-8')
    b = isolated / 'b.py'
    os.link(a, b)
    other = isolated / 'other.py'
    other.write_text('y = 2\n', encoding='utf-8')
    assert collect_file_groups([str(b), str(other), str(a)], tool='tool') == [[b, a], [other]]


def test_case_alias_dedups_to_first(isolated: Path) -> None:
    target = isolated / 'CaseFile.py'
    target.write_text('x = 1\n', encoding='utf-8')
    alias = isolated / 'casefile.py'
    if not alias.exists():
        pytest.skip('requires a case-insensitive filesystem')
    result = collect_files([str(target), str(alias)], tool='tool')
    assert result == [target]


def test_contains_expected_entries() -> None:
    critical = {'.git', '__pycache__', '.venv', 'build', 'dist'}
    assert critical <= _SKIP_DIRS


def test_dedup_by_resolved_path(isolated: Path) -> None:
    f = isolated / 'a.py'
    f.write_text('x = 1\n', encoding='utf-8')
    result = collect_files([str(f), str(f)], tool='tool')
    assert len(result) == 1


def test_directory_excludes_git_ignored_nested_worktree(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    nested = isolated / 'nested'
    nested.mkdir()
    git('init', '-q', cwd=nested)
    inside = nested / 'inside.py'
    inside.write_text('x = 1\n', encoding='utf-8')
    (isolated / '.gitignore').write_text('nested/\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == []


def test_directory_excludes_git_ignored_python(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    ignored = isolated / 'ignored.py'
    ignored.write_text('x = 2\n', encoding='utf-8')
    (isolated / '.gitignore').write_text('ignored.py\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [kept]


def test_directory_expansion(isolated: Path) -> None:
    d = isolated / 'pkg'
    d.mkdir()
    (d / 'a.py').write_text('x = 1\n', encoding='utf-8')
    (d / 'b.py').write_text('x = 1\n', encoding='utf-8')
    (d / 'c.txt').write_text('not python\n', encoding='utf-8')
    result = collect_files([str(d)], tool='tool')
    assert result == [d / 'a.py', d / 'b.py']


@pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows CreateProcess lookup')
def test_directory_ignores_inert_git_exe_in_parent_cwd(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_git = shutil.which('git')
    assert trusted_git is not None
    trusted_git = str(Path(trusted_git).absolute())
    git('init', '-q', cwd=isolated)
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    (isolated / 'ignored.py').write_text('x = 2\n', encoding='utf-8')
    (isolated / '.gitignore').write_text('ignored.py\n', encoding='utf-8')
    # Finish all Git setup before introducing an invalid, inert executable.
    decoy = isolated / 'git.exe'
    decoy.write_bytes(b'not an executable\n')
    monkeypatch.chdir(isolated)
    monkeypatch.setenv('PATH', str(Path(trusted_git).parent))
    monkeypatch.delenv('NoDefaultCurrentDirectoryInExePath', raising=False)

    assert collect_files([str(isolated)], tool='tool') == [kept]
    assert decoy.read_bytes() == b'not an executable\n'


def test_directory_keeps_tracked_ignored_python(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    source = isolated / 'tracked.py'
    source.write_text('x = 1\n', encoding='utf-8')
    git('add', 'tracked.py', cwd=isolated)
    (isolated / '.gitignore').write_text('tracked.py\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [source]


def test_directory_keeps_untracked_nonignored_python(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    source = isolated / 'untracked.py'
    source.write_text('x = 1\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [source]


@pytest.mark.parametrize('operand', ['.', 'sub/..', '../alias'])
def test_directory_operand_spellings_preserve_git_exclusions(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, operand: str
) -> None:
    repository = isolated / 'project'
    repository.mkdir()
    git('init', '-q', cwd=repository)
    (repository / 'sub').mkdir()
    (repository / '.gitignore').write_text('ignored.py\n', encoding='utf-8')
    for name in ('kept.py', 'ignored.py', 'sub/kept.py', 'sub/ignored.py'):
        (repository / name).write_text('x = 1\n', encoding='utf-8')
    if operand == '../alias':
        try:
            (isolated / 'alias').symlink_to(repository, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'symlinks unavailable: {error}')
    monkeypatch.chdir(repository)

    assert collect_files([operand], tool='tool') == [
        Path(operand) / 'kept.py',
        Path(operand) / 'sub' / 'kept.py',
    ]


def test_directory_recurses_into_nested_git_worktree(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    top = isolated / 'top.py'
    top.write_text('x = 1\n', encoding='utf-8')
    nested = isolated / 'nested'
    nested.mkdir()
    git('init', '-q', cwd=nested)
    inside = nested / 'inside.py'
    inside.write_text('x = 2\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [inside, top]


def test_distinct_files_kept_in_order(isolated: Path) -> None:
    a = isolated / 'a.py'
    b = isolated / 'b.py'
    a.write_text('x = 1\n', encoding='utf-8')
    b.write_text('x = 2\n', encoding='utf-8')
    assert collect_files([str(b), str(a)], tool='tool') == [b, a]


def test_empty_lists_launch_no_label_check(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('Git must not run for empty paths')

    monkeypatch.setattr(subprocess, 'run', fail_run)

    assert _discovery.private_paths(str(isolated), [], tool='tool') == (frozenset(), None)
    assert _filter_private(isolated, [], tool='tool') == ([], None)


def test_empty_result(isolated: Path) -> None:
    d = isolated / 'empty'
    d.mkdir()
    result = collect_files([str(d)], tool='tool')
    assert result == []


def test_explicit_git_ignored_python_is_included(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    ignored = isolated / 'ignored.py'
    ignored.write_text('x = 1\n', encoding='utf-8')
    (isolated / '.gitignore').write_text('ignored.py\n', encoding='utf-8')

    assert collect_files([str(ignored)], tool='tool') == [ignored]


def test_folder_labelled_by_its_own_path_keeps_its_files(repo: Path) -> None:
    (repo / '.gitattributes').write_text('folder private\n')
    folder = repo / 'folder'
    folder.mkdir()
    tracked = folder / 'tracked.py'
    tracked.write_text('x = 1\n')
    git('add', 'folder/tracked.py', cwd=repo)
    untracked = folder / 'untracked.py'
    untracked.write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [tracked, untracked]
    assert collect_files([str(folder)], tool='tool') == [tracked, untracked]


def test_force_tracked_file_under_ignored_directory_is_kept(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ign/\n', encoding='utf-8')
    ignored = isolated / 'ign'
    ignored.mkdir()
    tracked = ignored / 't.py'
    tracked.write_text('x = 1\n', encoding='utf-8')
    (ignored / 'u.py').write_text('x = 2\n', encoding='utf-8')
    git('add', '-f', 'ign/t.py', cwd=isolated)

    assert collect_files([str(isolated)], tool='tool') == [tracked]


@pytest.mark.parametrize('failure', ['error-status', 'oserror'])
def test_git_failure_falls_back_to_filesystem(
    failure: str,
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = isolated / 'source.py'
    source.write_text('x = 1\n', encoding='utf-8')

    def fail_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if failure == 'oserror':
            raise OSError
        return subprocess.CompletedProcess([], 128, b'source.py\0', b'fatal error')

    monkeypatch.setattr(subprocess, 'run', fail_git)

    assert collect_files([str(isolated)], tool='tool') == [source]
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize(
    ('git_stderr', 'detail'),
    [
        (
            b"fatal: detected dubious ownership in repository at '/x'\nhint\n",
            "fatal: detected dubious ownership in repository at '/x'",
        ),
        (b'', 'git exited with status 128'),
        (b'fatal: bad \x1b[31mname\n', 'fatal: bad \\x1b[31mname'),
    ],
)
def test_git_failure_inside_worktree_warns(
    git_stderr: bytes,
    detail: str,
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (isolated / '.git').mkdir()
    source = isolated / 'source.py'
    source.write_text('x = 1\n', encoding='utf-8')

    calls: list[list[str]] = []

    def fail_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, b'', git_stderr)

    monkeypatch.setattr(subprocess, 'run', fail_git)

    assert collect_files([str(isolated)], tool='tool') == [source]
    assert all('check-attr' not in args for args in calls)
    assert capsys.readouterr().err == (
        f'warning: cannot apply Git exclusions in {path_text(isolated)}: {detail}; '
        'keeping every discovered Python file\n'
    )


def test_git_filter_uses_absolute_executable(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = isolated / 'space name.py'
    source.write_text('pass\n')
    commands: list[list[str]] = []

    def capture_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert Path(args[0]).is_absolute()
        assert kwargs['cwd'] == isolated
        commands.append(args[1:])
        if args[1] == 'ls-files':
            assert kwargs['input'] is None
            return subprocess.CompletedProcess(args, 0, b'', b'')
        assert kwargs['input'] == b'space name.py\0'
        return subprocess.CompletedProcess(args, 0, b'space name.py\0', b'')

    monkeypatch.setattr(subprocess, 'run', capture_run)
    assert collect_files([str(isolated)], tool='tool') == []
    assert commands == [
        ['ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--directory'],
        ['check-ignore', '-z', '--stdin'],
    ]


def test_global_config_names_the_attribute(
    repo: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = isolated / 'global-config'
    config.write_text('[tool]\n\tprivateAttribute = unpublished\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    (repo / '.gitattributes').write_text('a.py unpublished\nb.py private\n')
    (repo / 'a.py').write_text('x = 1\n')
    (repo / 'b.py').write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [repo / 'b.py']


def test_hardlink_alias_dedups(isolated: Path) -> None:
    a = isolated / 'a.py'
    a.write_text('x = 1\n', encoding='utf-8')
    b = isolated / 'b.py'
    os.link(a, b)
    assert collect_files([str(a), str(b)], tool='tool') == [a]


def test_ignored_directories_decode_with_the_git_path_codec(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / '.gitignore').write_text('ignored/\n', encoding='utf-8')
    ignored = repo / 'ignored'
    ignored.mkdir()
    (ignored / 'x.py').write_text('x = 1\n', encoding='utf-8')
    decoded: list[bytes] = []

    def record_decode(raw: bytes) -> str:
        decoded.append(raw)
        return decode_git_path(raw)

    monkeypatch.setattr(_discovery, 'decode_git_path', record_decode)

    assert collect_files([str(repo)], tool='tool') == []
    assert b'ignored/' in decoded


@pytest.mark.parametrize('inside_worktree', [False, True])
def test_invalid_argument_keeps_files(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    inside_worktree: bool,
) -> None:
    repo = isolated
    if inside_worktree:
        git('init', '-q', cwd=repo)
    (repo / 'source.py').write_text('x = 1\n')

    def invalid_run(*_args: object, **_kwargs: object) -> None:
        raise ValueError('invalid subprocess argument')

    monkeypatch.setattr(subprocess, 'run', invalid_run)

    assert collect_files([str(repo)], tool='tool') == [repo / 'source.py']
    warning = (
        f'warning: cannot apply Git exclusions in {path_text(repo)}: '
        'git could not be run: invalid subprocess argument; '
        'keeping every discovered Python file\n'
    )
    assert capsys.readouterr().err == (warning if inside_worktree else '')


def test_invalid_name_warns_once_and_uses_private(
    repo: Path,
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _checked_private_attribute.cache_clear()
    config = isolated / 'global-config'
    config.write_text('[tool]\n\tprivateAttribute = a b\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    (repo / '.gitattributes').write_text('b.py private\n')
    a = repo / 'a.py'
    a.write_text('x = 1\n')
    (repo / 'b.py').write_text('x = 1\n')
    nested = repo / 'nested'
    nested.mkdir()
    git('init', '-q', cwd=nested)
    c = nested / 'c.py'
    c.write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [a, c]
    assert capsys.readouterr().err == (
        "warning: invalid git config tool.privateAttribute 'a b': "
        "use letters, digits, '.', '_', or '-', not starting with '-' or 'builtin_'; "
        "using 'private'\n"
    )


def test_label_failure_inside_worktree_warns_and_keeps_unignored_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / '.gitignore').write_text('ignored.py\n')
    (repo / '.gitattributes').write_text('secret.py private\n')
    keep = repo / 'keep.py'
    keep.write_text('x = 1\n')
    (repo / 'ignored.py').write_text('x = 1\n')
    secret = repo / 'secret.py'
    secret.write_text('x = 1\n')

    def fail_labels(
        _directory: str | Path, _paths: list[bytes], *, tool: str
    ) -> tuple[frozenset[bytes], str]:
        assert tool == 'tool'
        return frozenset(), 'fatal: check-attr failed'

    monkeypatch.setattr(_discovery, '_labelled_private', fail_labels)

    assert collect_files([str(repo)], tool='tool') == [keep, secret]
    assert capsys.readouterr().err == (
        f'warning: cannot apply private labels in {path_text(repo)}: '
        'fatal: check-attr failed; keeping every file Git does not ignore\n'
    )


def test_label_failure_outside_worktree_is_silent(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = isolated / 'source.py'
    source.write_text('x = 1\n')

    def keep_files(_directory: Path, files: list[Path]) -> tuple[list[Path], None]:
        return files, None

    def fail_labels(
        _directory: str | Path, _paths: list[bytes], *, tool: str
    ) -> tuple[frozenset[bytes], str]:
        assert tool == 'tool'
        return frozenset(), 'fatal: check-attr failed'

    monkeypatch.setattr(_discovery, '_filter_git_ignored', keep_files)
    monkeypatch.setattr(_discovery, '_labelled_private', fail_labels)

    assert collect_files([str(isolated)], tool='tool') == [source]
    assert capsys.readouterr().err == ''


def test_missing_file_included(isolated: Path) -> None:
    missing = isolated / 'gone.py'
    result = collect_files([str(missing)], tool='tool')
    assert result == [missing]


def test_missing_git_falls_back_to_filesystem(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = isolated / 'source.py'
    source.write_text('x = 1\n', encoding='utf-8')

    monkeypatch.setenv('PATH', str(isolated))

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail('No executable should be launched when Git cannot be resolved')

    monkeypatch.setattr(subprocess, 'run', unexpected_run)

    assert collect_files([str(isolated)], tool='tool') == [source]
    assert capsys.readouterr().err == ''


def test_missing_git_inside_worktree_warns(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (isolated / '.git').mkdir()
    source = isolated / 'source.py'
    source.write_text('x = 1\n', encoding='utf-8')
    monkeypatch.setenv('PATH', str(isolated))

    assert collect_files([str(isolated)], tool='tool') == [source]
    assert capsys.readouterr().err == (
        f'warning: cannot apply Git exclusions in {path_text(isolated)}: '
        'git could not be run: Git executable not found on PATH; '
        'keeping every discovered Python file\n'
    )


def test_missing_paths_dedup_lexically(isolated: Path) -> None:
    missing = isolated / 'missing.py'
    other = isolated / 'other.py'
    result = collect_files([str(missing), str(missing), str(other)], tool='tool')
    assert result == [missing, other]


def test_named_folder_with_labelled_files_selects_nothing(repo: Path) -> None:
    drafts = repo / 'drafts'
    drafts.mkdir()
    (drafts / '.gitattributes').write_text('* private\n')
    (drafts / 'p.py').write_text('x = 1\n')

    assert collect_files([str(drafts)], tool='tool') == []


def test_named_nested_worktree_uses_its_own_labels(repo: Path) -> None:
    (repo / '.gitattributes').write_text('vendor/** private\n')
    nested = repo / 'vendor' / 'nested'
    nested.mkdir(parents=True)
    git('init', '-q', cwd=nested)
    (nested / '.gitattributes').write_text('hidden.py private\n')
    (nested / 'hidden.py').write_text('x = 1\n')
    inner = nested / 'inner.py'
    inner.write_text('x = 1\n')

    assert collect_files([str(nested)], tool='tool') == [inner]


def test_named_private_file_is_still_processed(repo: Path) -> None:
    (repo / '.gitattributes').write_text('secret.py private\n')
    secret = repo / 'secret.py'
    secret.write_text('x = 1\n')

    assert collect_files([str(secret)], tool='tool') == [secret]


def test_names_with_spaces_and_non_ascii_letters_are_labelled(repo: Path) -> None:
    (repo / '.gitattributes').write_text('notes/** private\n')
    notes = repo / 'notes'
    notes.mkdir()
    (notes / 'my notes \u00e9.py').write_text('x = 1\n')
    kept = repo / 'keep me.py'
    kept.write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [kept]


def test_negated_file_inside_ignored_directory_stays_excluded(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ign/\n!ign/keep.py\n', encoding='utf-8')
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    ignored = isolated / 'ign'
    ignored.mkdir()
    (ignored / 'keep.py').write_text('x = 2\n', encoding='utf-8')
    (ignored / 'other.py').write_text('x = 3\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [kept]


def test_nested_git_failures_warn_once_per_root_in_processing_order(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (isolated / '.git').mkdir()
    outer_file = isolated / 'outer.py'
    outer_file.write_text('x = 1\n', encoding='utf-8')
    nested_roots = [isolated / 'a', isolated / 'c', isolated / 'c' / 'd']
    nested_files: list[Path] = []
    for root in nested_roots:
        (root / '.git').mkdir(parents=True)
        source = root / 'inside.py'
        source.write_text('x = 2\n', encoding='utf-8')
        nested_files.append(source)

    calls: list[tuple[Path, str]] = []

    def fail_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        root = kwargs['cwd']
        assert isinstance(root, Path)
        calls.append((root, args[1]))
        detail = f'{root.name} {args[1]} failure'.encode()
        return subprocess.CompletedProcess(args, 128, b'', detail)

    monkeypatch.setattr(subprocess, 'run', fail_git)

    roots = [isolated, *nested_roots]
    assert collect_files([str(isolated)], tool='tool') == sorted([outer_file, *nested_files])
    assert calls == [(root, command) for root in roots for command in ('ls-files', 'check-ignore')]
    assert capsys.readouterr().err.splitlines() == [
        f'warning: cannot apply Git exclusions in {path_text(root)}: '
        f'{root.name} check-ignore failure; keeping every discovered Python file'
        for root in roots
    ]


def test_nested_git_worktree_uses_own_tracked_state(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    nested = isolated / 'nested'
    nested.mkdir()
    git('init', '-q', cwd=nested)
    tracked = nested / 'tracked.py'
    tracked.write_text('x = 1\n', encoding='utf-8')
    git('add', 'tracked.py', cwd=nested)
    ignored = nested / 'ignored.py'
    ignored.write_text('x = 2\n', encoding='utf-8')
    (nested / '.gitignore').write_text('*.py\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [tracked]


def test_nested_worktree_ownership_follows_path_components(isolated: Path) -> None:
    git('init', '-q', cwd=isolated)
    for name in ('a', 'ab', 'ab/inner'):
        (isolated / name).mkdir()
        git('init', '-q', cwd=isolated / name)
    (isolated / 'a' / '.gitignore').write_text('hidden.py\n', encoding='utf-8')
    (isolated / 'ab' / '.gitignore').write_text('generated.py\n', encoding='utf-8')
    for name in ('a/hidden.py', 'ab/hidden.py', 'ab/generated.py', 'ab/inner/generated.py'):
        (isolated / name).write_text('x = 1\n', encoding='utf-8')

    assert collect_files([str(isolated)], tool='tool') == [
        isolated / 'ab' / 'hidden.py',
        isolated / 'ab' / 'inner' / 'generated.py',
    ]


def test_non_py_existing_silently_dropped(isolated: Path) -> None:
    f = isolated / 'readme.txt'
    f.write_text('hi', encoding='utf-8')
    result = collect_files([str(f)], tool='tool')
    assert result == []


def test_non_py_missing_included(isolated: Path) -> None:
    f = isolated / 'gone.txt'
    result = collect_files([str(f)], tool='tool')
    assert result == [f]


def test_operand_inside_ignored_directory_yields_nothing(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ign/\n', encoding='utf-8')
    deep = isolated / 'ign' / 'deep'
    deep.mkdir(parents=True)
    (deep / 'a.py').write_text('x = 1\n', encoding='utf-8')

    assert collect_files([str(deep)], tool='tool') == []
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('codec', ['real', 'latin-1'])
def test_private_paths_encode_git_names(
    repo: Path, codec: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_name = b'bad-\xff.py'
    (repo / '.gitattributes').write_bytes(raw_name + b' private\n')
    name = decode_git_path(raw_name) if codec == 'real' else raw_name.decode('latin-1')
    if codec == 'latin-1':
        monkeypatch.setattr(_discovery, 'encode_git_path', latin1_encode, raising=False)

    assert _discovery.private_paths(str(repo), [name], tool='tool') == (frozenset({name}), None)


def _record_git_calls(
    monkeypatch: pytest.MonkeyPatch,
    fail: str | None = None,
) -> list[tuple[Path, str]]:
    calls: list[tuple[Path, str]] = []
    real_run = cast('Callable[..., subprocess.CompletedProcess[bytes]]', subprocess.run)

    def recording_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        cwd = kwargs['cwd']
        assert isinstance(cwd, Path)
        calls.append((cwd, args[1]))
        if args[1] == fail:
            return subprocess.CompletedProcess(args, 128, b'', f'fatal: {fail} failed'.encode())
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recording_run)
    return calls


def test_ignored_nested_repositories_add_no_git_calls_or_walking(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trees: list[tuple[Path, Path]] = []
    for count in (1, 5):
        tree = isolated / f'tree{count}'
        tree.mkdir()
        git('init', '-q', cwd=tree)
        (tree / '.gitignore').write_text('ignored/\n', encoding='utf-8')
        tracked = tree / 'tracked.py'
        tracked.write_text('x = 1\n', encoding='utf-8')
        git('add', 'tracked.py', cwd=tree)
        _add_marker_repositories(tree / 'ignored', count)
        trees.append((tree, tracked))
    calls = _record_git_calls(monkeypatch)
    _tops, visited = _record_walk(monkeypatch)

    call_counts: list[int] = []
    for tree, tracked in trees:
        calls.clear()
        assert collect_files([str(tree)], tool='tool') == [tracked]
        call_counts.append(len(calls))

    assert call_counts[0] == call_counts[1]
    assert isolated / 'tree1' in visited
    assert not [
        path for tree, _ in trees for path in visited if path.is_relative_to(tree / 'ignored')
    ]


def test_listed_directory_stays_pruned_when_check_ignore_fails(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ignored/\n', encoding='utf-8')
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    (isolated / 'ignored').mkdir()
    (isolated / 'ignored' / 'hidden.py').write_text('x = 2\n', encoding='utf-8')
    _record_git_calls(monkeypatch, fail='check-ignore')

    assert collect_files([str(isolated)], tool='tool') == [kept]
    assert capsys.readouterr().err == (
        f'warning: cannot apply Git exclusions in {path_text(isolated)}: '
        'fatal: check-ignore failed; keeping every discovered Python file\n'
    )


def test_listing_failure_keeps_check_ignore_results(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ignored/\nskipped.py\n', encoding='utf-8')
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    (isolated / 'skipped.py').write_text('x = 2\n', encoding='utf-8')
    (isolated / 'ignored').mkdir()
    (isolated / 'ignored' / 'hidden.py').write_text('x = 3\n', encoding='utf-8')
    assert collect_files([str(isolated)], tool='tool') == [kept]
    calls = _record_git_calls(monkeypatch, fail='ls-files')

    assert collect_files([str(isolated)], tool='tool') == [kept]
    assert (isolated, 'ls-files') in calls
    assert capsys.readouterr().err == ''


def test_nested_worktree_excluded_by_check_ignore_is_not_walked(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('skipped/\n', encoding='utf-8')
    kept = isolated / 'kept.py'
    kept.write_text('x = 1\n', encoding='utf-8')
    skipped = isolated / 'skipped'
    (skipped / '.git').mkdir(parents=True)
    (skipped / 'sub').mkdir()
    (skipped / 'sub' / 'hidden.py').write_text('x = 2\n', encoding='utf-8')
    # A failed listing leaves skipped/ for the parent's check-ignore pass to exclude.
    calls = _record_git_calls(monkeypatch, fail='ls-files')
    tops, visited = _record_walk(monkeypatch)

    assert collect_files([str(isolated)], tool='tool') == [kept]
    assert skipped not in tops
    assert skipped / 'sub' not in visited
    assert {cwd for cwd, _ in calls} == {isolated}


def test_nested_worktree_prunes_with_its_own_exclusions(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trees: list[tuple[Path, list[Path]]] = []
    for count in (1, 5):
        tree = isolated / f'tree{count}'
        tree.mkdir()
        git('init', '-q', cwd=tree)
        top = tree / 'top.py'
        top.write_text('x = 1\n', encoding='utf-8')
        nested = tree / 'nested'
        nested.mkdir()
        git('init', '-q', cwd=nested)
        (nested / '.gitignore').write_text('cache/\n', encoding='utf-8')
        inside = nested / 'inside.py'
        inside.write_text('x = 2\n', encoding='utf-8')
        _add_marker_repositories(nested / 'cache', count)
        trees.append((tree, [inside, top]))
    calls = _record_git_calls(monkeypatch)
    _tops, visited = _record_walk(monkeypatch)

    call_counts: list[int] = []
    for tree, expected in trees:
        calls.clear()
        assert collect_files([str(tree)], tool='tool') == expected
        call_counts.append(len(calls))

    assert call_counts[0] == call_counts[1]
    assert not [
        path
        for tree, _ in trees
        for path in visited
        if path.is_relative_to(tree / 'nested' / 'cache')
    ]


def test_owner_with_only_pruned_files_makes_no_check_ignore_call(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git('init', '-q', cwd=isolated)
    (isolated / '.gitignore').write_text('ignored/\n', encoding='utf-8')
    (isolated / 'ignored').mkdir()
    (isolated / 'ignored' / 'hidden.py').write_text('x = 1\n', encoding='utf-8')
    calls = _record_git_calls(monkeypatch, fail='check-ignore')

    assert collect_files([str(isolated)], tool='tool') == []
    assert calls == [(isolated, 'ls-files')]
    assert capsys.readouterr().err == ''


def test_prunes_skipped_directories_before_descent(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def controlled_walk(
        root: Path,
        *,
        topdown: bool,
        followlinks: bool,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        assert root == isolated
        assert topdown is True
        assert followlinks is False
        dirnames = ['.git', 'retained']
        yield str(isolated), dirnames, []
        assert dirnames == ['retained']
        yield str(isolated / 'retained'), [], ['kept.py']

    monkeypatch.setattr(_discovery.os, 'walk', controlled_walk)

    assert collect_files([str(isolated)], tool='tool') == [isolated / 'retained' / 'kept.py']


def test_recursive_discovery_with_explicit_alias(isolated: Path) -> None:
    target = isolated / 'CaseFile.py'
    target.write_text('x = 1\n', encoding='utf-8')
    alias = isolated / 'casefile.py'
    if not alias.exists():
        pytest.skip('requires a case-insensitive filesystem')
    result = collect_files([str(isolated), str(alias)], tool='tool')
    assert [p.name for p in result] == ['CaseFile.py']


def test_repository_config_names_the_attribute(repo: Path) -> None:
    git('config', 'tool.privateAttribute', 'unpublished', cwd=repo)
    (repo / '.gitattributes').write_text('a.py unpublished\nb.py private\n')
    (repo / 'a.py').write_text('x = 1\n')
    (repo / 'b.py').write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [repo / 'b.py']


def test_root_pattern_labels_a_named_subfolder(repo: Path) -> None:
    (repo / '.gitattributes').write_text('drafts/** private\n')
    drafts = repo / 'drafts'
    drafts.mkdir()
    (drafts / 'p.py').write_text('x = 1\n')

    assert collect_files([str(drafts)], tool='tool') == []


def test_scan_skips_labelled_files_folders_and_worktree_roots(repo: Path) -> None:
    (repo / '.gitattributes').write_text('secret.py private\nvendor/** private\n')
    (repo / 'keep.py').write_text('x = 1\n')
    (repo / 'secret.py').write_text('x = 1\n')
    drafts = repo / 'drafts'
    drafts.mkdir()
    (drafts / '.gitattributes').write_text('* private\n')
    (drafts / 'p.py').write_text('x = 1\n')
    nested = repo / 'vendor' / 'nested'
    nested.mkdir(parents=True)
    git('init', '-q', cwd=nested)
    (nested / 'inner.py').write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [repo / 'keep.py']


def test_single_py_file(isolated: Path) -> None:
    f = isolated / 'a.py'
    f.write_text('x = 1\n', encoding='utf-8')
    result = collect_files([str(f)], tool='tool')
    assert result == [f]


def test_skip_dirs(isolated: Path) -> None:
    d = isolated / 'proj'
    d.mkdir()
    (d / 'good.py').write_text('x = 1\n', encoding='utf-8')
    cache = d / '__pycache__'
    cache.mkdir()
    (cache / 'bad.py').write_text('x = 1\n', encoding='utf-8')
    venv = d / '.venv'
    venv.mkdir()
    (venv / 'bad.py').write_text('x = 1\n', encoding='utf-8')
    result = collect_files([str(d)], tool='tool')
    assert len(result) == 1
    assert result[0].name == 'good.py'


def test_skip_dirs_are_pruned_before_file_inspection(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = isolated / 'proj'
    project.mkdir()
    good = project / 'good.py'
    good.write_text('x = 1\n', encoding='utf-8')
    skipped = project / '.venv'
    skipped.mkdir()
    (skipped / 'bad.py').write_text('x = 1\n', encoding='utf-8')
    inspected: list[Path] = []
    original_is_symlink = Path.is_symlink

    def record_is_symlink(path: Path) -> bool:
        inspected.append(path)
        return original_is_symlink(path)

    monkeypatch.setattr(Path, 'is_symlink', record_is_symlink)

    assert collect_files([str(project)], tool='tool') == [good]
    assert all('.venv' not in path.parts for path in inspected)


def test_skips_symlinked_py_files_during_directory_recursion(isolated: Path) -> None:
    target_dir = isolated / 'external'
    target_dir.mkdir()
    target = target_dir / 'external.py'
    target.write_text('def ext():\n    """External."""\n')

    project = isolated / 'project'
    project.mkdir()
    real = project / 'real.py'
    real.write_text('def foo():\n    """Short."""\n')
    link = project / 'linked.py'
    link.symlink_to(target)

    files = collect_files([str(project)], tool='tool')
    names = [f.name for f in files]
    assert 'real.py' in names
    assert 'linked.py' not in names


def test_symlink_alias_dedups(isolated: Path) -> None:
    a = isolated / 'a.py'
    a.write_text('x = 1\n', encoding='utf-8')
    link = isolated / 'link.py'
    link.symlink_to(a)
    assert collect_files([str(a), str(link)], tool='tool') == [a]


def test_symlink_explicit_processed(isolated: Path) -> None:
    target = isolated / 'target.py'
    target.write_text('x = 1\n', encoding='utf-8')
    link = isolated / 'link.py'
    link.symlink_to(target)
    result = collect_files([str(link)], tool='tool')
    assert result == [link]


def test_symlink_in_directory_skipped(isolated: Path) -> None:
    d = isolated / 'pkg'
    d.mkdir()
    real = d / 'real.py'
    real.write_text('x = 1\n', encoding='utf-8')
    link = d / 'link.py'
    link.symlink_to(real)
    result = collect_files([str(d)], tool='tool')
    assert len(result) == 1
    assert result[0].name == 'real.py'


def test_untrusted_repository_keeps_files_and_warns(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = repo / 'a.py'
    source.write_text('x = 1\n', encoding='utf-8')
    monkeypatch.setenv('GIT_TEST_ASSUME_DIFFERENT_OWNER', '1')

    assert collect_files([str(repo)], tool='tool') == [source]
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('warning: cannot apply Git exclusions in ')
    assert 'fatal: detected dubious ownership in repository at ' in lines[0]


def test_whitespace_in_the_name_is_not_trimmed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _checked_private_attribute.cache_clear()
    git('config', 'tool.privateAttribute', ' private', cwd=repo)
    (repo / '.gitattributes').write_text('b.py private\n')
    a = repo / 'a.py'
    a.write_text('x = 1\n')
    (repo / 'b.py').write_text('x = 1\n')

    assert collect_files([str(repo)], tool='tool') == [a]
    assert capsys.readouterr().err == (
        "warning: invalid git config tool.privateAttribute ' private': "
        "use letters, digits, '.', '_', or '-', not starting with '-' or 'builtin_'; "
        "using 'private'\n"
    )


def test_zero_inode_keeps_distinct_files(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = isolated / 'a.py'
    b = isolated / 'b.py'
    a.write_text('x = 1\n', encoding='utf-8')
    b.write_text('x = 2\n', encoding='utf-8')
    real_stat = Path.stat

    def zero_inode_stat(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = real_stat(path, follow_symlinks=follow_symlinks)
        values = list(result)
        values[1] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, 'stat', zero_inode_stat)

    assert collect_files([str(a), str(b)], tool='tool') == [a, b]
