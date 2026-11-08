import os
import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _listings,
)
from defsort._vendor.gitfiles._listings import (
    _iter_raw_diff_records,
    get_modified_tracked_py_files,
    get_staged_py_info,
    get_tracked_files,
    head_rename_sources,
    is_head_unborn,
    match_tracked_py_files,
)
from defsort._vendor.gitfiles._run import (
    GitCommandError,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
    tracked_query,
)

from ._support import (
    git,
    latin1_decode,
    write_bytes_file,
)


def test_directory_outside_repository_is_not_unborn(tmp_path: Path) -> None:
    assert is_head_unborn(str(tmp_path)) is False


def test_empty_tree_fallback_when_mktree_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str | bytes]] = []

    def run(command: list[str | bytes], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        match command[1:]:
            case ['rev-parse', '--verify', 'HEAD']:
                return subprocess.CompletedProcess(command, 128, b'', b'fatal: no HEAD')
            case ['mktree']:
                raise OSError('cannot launch')
            case ['diff', *_]:
                return subprocess.CompletedProcess(command, 0, b'a.py\0', b'')
            case _:
                pytest.fail(f'unexpected command: {command}')

    monkeypatch.setattr(subprocess, 'run', run)
    assert get_modified_tracked_py_files('/w') == ['a.py']
    assert commands[-1][-1] == '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


def test_failed_diff_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str | bytes]] = []

    def run(command: list[str | bytes], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1:] == ['symbolic-ref', '--quiet', 'HEAD']:
            return subprocess.CompletedProcess(command, 0, b'refs/heads/main\n', b'')
        if command[1:] == ['rev-parse', '--verify', '--quiet', 'HEAD']:
            return subprocess.CompletedProcess(command, 0, b'abc\n', b'')
        if command[1:] == [
            'diff',
            '--raw',
            '-z',
            '-M',
            '--no-relative',
            '--no-abbrev',
            '--diff-filter=R',
            'HEAD',
            '--',
        ]:
            return subprocess.CompletedProcess(command, 128, b'', b'fatal: x\n')
        pytest.fail(f'unexpected command: {command}')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(GitCommandError, match=r'^fatal: x$'):
        head_rename_sources('/w')
    assert len(commands) == 3


def test_head_check_launch_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(GitCommandError, match='git could not be run: Permission denied'):
        get_modified_tracked_py_files('/w')


def test_head_rename_sources_pairs_a_staged_move(repo: Path) -> None:
    (repo / 'old.py').write_text(''.join(f'line_{n} = {n}\n' for n in range(10)), encoding='utf-8')
    git('add', 'old.py', cwd=repo)
    git('commit', '-m', 'add file', cwd=repo)
    git('mv', 'old.py', 'new.py', cwd=repo)
    with (repo / 'new.py').open('a', encoding='utf-8') as output:
        output.write('extra = 10\n')

    assert head_rename_sources(str(repo)) == {'new.py': 'old.py'}


@pytest.mark.parametrize('raw', [b'caf\xc3\xa9.py', b'bad-\xff.py'])
@pytest.mark.parametrize('codec', ['real', 'latin-1'])
def test_listings_decode_names_with_the_git_path_codec(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, codec: str
) -> None:
    decode = decode_git_path if codec == 'real' else latin1_decode
    if codec == 'latin-1':
        monkeypatch.setattr(_listings, 'decode_git_path', latin1_decode)

    def run(command: list[str | bytes], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = command[1:]
        if args[:3] == ['diff', '--cached', '--raw']:
            header = b':100644 100644 ' + b'0' * 40 + b' ' + b'1' * 40 + b' M\0'
            output = header + raw + b'\0'
        elif args[:1] == ['rev-parse']:
            output = b'HEAD\n'
        elif args[:1] in (['diff'], ['ls-files']):
            output = raw + b'\0'
        else:
            pytest.fail(f'unexpected command: {args}')
        return subprocess.CompletedProcess(command, 0, output, b'')

    monkeypatch.setattr(subprocess, 'run', run)

    def exists(_path: Path) -> bool:
        return True

    def is_symlink(_path: Path) -> bool:
        return False

    monkeypatch.setattr(Path, 'exists', exists)
    monkeypatch.setattr(Path, 'is_symlink', is_symlink)
    assert get_staged_py_info('/w') == [(decode(raw), '100644', '1' * 40, '0' * 40, 'M')]
    assert get_modified_tracked_py_files('/w')[0] == decode(raw)
    assert match_tracked_py_files('/w', ['*.py'], repo_root='/w')[0] == decode(raw)


def test_match_directory_and_explicit_path(repo: Path) -> None:
    (repo / 'sub').mkdir()
    (repo / 'sub' / 'inner.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'sub/inner.py', cwd=repo)
    assert match_tracked_py_files(str(repo), ['sub/'], repo_root=str(repo)) == ['sub/inner.py']
    assert match_tracked_py_files(str(repo), ['sub/inner.py'], repo_root=str(repo)) == [
        'sub/inner.py'
    ]


def test_match_pathspecs_filters_missing_symlink_and_non_python(repo: Path) -> None:
    (repo / 'z.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'a.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'link.py').symlink_to(repo / 'a.py')
    (repo / 'readme.md').write_text('text\n', encoding='utf-8')
    git('add', '.', cwd=repo)
    (repo / 'z.py').unlink()

    assert match_tracked_py_files(str(repo), ['*.py'], repo_root=str(repo)) == ['a.py']
    assert match_tracked_py_files(str(repo), [], repo_root=str(repo)) == []


def test_modified_listing_covers_staged_and_unstaged_without_deletes(repo: Path) -> None:
    for name in ('staged.py', 'unstaged.py', 'deleted.py', 'readme.md'):
        (repo / name).write_text('x = 1\n', encoding='utf-8')
    git('add', '.', cwd=repo)
    git('commit', '-m', 'add files', cwd=repo)
    (repo / 'staged.py').write_text('x = 2\n', encoding='utf-8')
    git('add', 'staged.py', cwd=repo)
    (repo / 'unstaged.py').write_text('x = 2\n', encoding='utf-8')
    (repo / 'deleted.py').unlink()
    (repo / 'readme.md').write_text('x = 2\n', encoding='utf-8')

    assert get_modified_tracked_py_files(str(repo)) == ['staged.py', 'unstaged.py']


def test_modified_listing_ignores_rename_settings(repo: Path) -> None:
    (repo / 'old.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'old.py', cwd=repo)
    git('commit', '-m', 'add file', cwd=repo)
    git('mv', 'old.py', 'new.py', cwd=repo)
    git('config', 'diff.renames', 'copies', cwd=repo)

    assert get_modified_tracked_py_files(str(repo)) == ['new.py']


def test_modified_listing_uses_empty_tree_before_first_commit(repo: Path) -> None:
    (repo / 'new.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'new.py', cwd=repo)
    assert get_modified_tracked_py_files(str(repo)) == ['new.py']


def test_no_staged_files_returns_empty(repo: Path) -> None:
    assert get_staged_py_info(str(repo)) == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX bytes-path behavior is unavailable on Windows')
def test_non_utf8_path_round_trips_across_listings(repo: Path) -> None:
    raw = b'bad-\xff.py'
    write_bytes_file(repo, raw, b'x = 1\n')
    git('add', '--', os.fsdecode(raw), cwd=repo)
    decoded = decode_git_path(raw)
    assert get_staged_py_info(str(repo))[0].path == decoded
    assert get_modified_tracked_py_files(str(repo)) == [decoded]
    assert get_tracked_files(str(repo)) == frozenset({decoded})
    assert match_tracked_py_files(str(repo), ['*.py'], repo_root=str(repo)) == [decoded]


@pytest.mark.parametrize('listing', ['staged', 'modified', 'tracked', 'matched'])
def test_listing_failure_preserves_git_detail(
    monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    def fail(command: list[str | bytes], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b'', b'fatal: broken')

    monkeypatch.setattr(subprocess, 'run', fail)

    def invoke() -> object:
        if listing == 'staged':
            return get_staged_py_info('/w')
        if listing == 'modified':
            return get_modified_tracked_py_files('/w')
        if listing == 'tracked':
            return get_tracked_files('/w')
        return match_tracked_py_files('/w', ['*.py'], repo_root='/w')

    with pytest.raises(GitCommandError, match='fatal: broken'):
        invoke()


def test_raw_diff_metadata_uses_surrogateescape_separately_from_path_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = b'module-\xfe.py'
    header = b':100644 100644 ' + b'a' * 40 + b' ' + b'b' * 40 + b' M\xff'
    monkeypatch.setattr(_listings, 'decode_git_path', latin1_decode)

    records = list(_iter_raw_diff_records(header + b'\0' + raw_path + b'\0'))

    assert len(records) == 1
    metadata, paths = records[0]
    assert metadata.endswith('M\udcff')
    assert paths == (latin1_decode(raw_path),)
    assert '\ufffd' not in metadata


def test_raw_diff_rename_decodes_distinct_source_and_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b'old-\xff.py'
    destination = b'new-\xfe.py'
    header = b':100644 100644 ' + b'a' * 40 + b' ' + b'b' * 40 + b' R100'
    decoded: list[bytes] = []

    def decode(raw: bytes) -> str:
        decoded.append(raw)
        return latin1_decode(raw)

    monkeypatch.setattr(_listings, 'decode_git_path', decode)
    records = list(_iter_raw_diff_records(header + b'\0' + source + b'\0' + destination + b'\0'))

    assert len(records) == 1
    _, paths = records[0]
    assert decoded == [source, destination]
    assert paths == (latin1_decode(source), latin1_decode(destination))


def test_rename_settings_do_not_change_sources(repo: Path) -> None:
    (repo / 'old.py').write_text(''.join(f'line_{n} = {n}\n' for n in range(10)), encoding='utf-8')
    (repo / 'copy_source.py').write_text(
        ''.join(f'copy_{n} = {n * 10}\n' for n in range(10)), encoding='utf-8'
    )
    git('add', '.', cwd=repo)
    git('commit', '-m', 'add files', cwd=repo)
    git('mv', 'old.py', 'new.py', cwd=repo)
    with (repo / 'new.py').open('a', encoding='utf-8') as output:
        output.write('extra = 10\n')
    (repo / 'copy.py').write_bytes((repo / 'copy_source.py').read_bytes())
    git('add', 'copy.py', cwd=repo)

    for setting in ('false', 'copies'):
        git('config', 'diff.renames', setting, cwd=repo)
        assert head_rename_sources(str(repo)) == {'new.py': 'old.py'}


def test_repository_with_commit_is_not_unborn(repo: Path) -> None:
    (repo / 'a.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'a.py', cwd=repo)
    git('commit', '-m', 'add file', cwd=repo)
    assert is_head_unborn(str(repo)) is False


def test_repository_without_commits_is_unborn(repo: Path) -> None:
    assert is_head_unborn(str(repo)) is True


def test_staged_listing_filters_modes_and_sorts(repo: Path) -> None:
    (repo / 'z.py').write_text('z = 1\n', encoding='utf-8')
    (repo / 'a.py').write_text('a = 1\n', encoding='utf-8')
    (repo / 'readme.md').write_text('text\n', encoding='utf-8')
    (repo / 'link.py').symlink_to(repo / 'a.py')
    git('add', '.', cwd=repo)

    entries = get_staged_py_info(str(repo))

    assert [entry.path for entry in entries] == ['a.py', 'z.py']
    assert all(entry.mode == '100644' and entry.status == 'A' for entry in entries)
    assert all(entry.blob_hash and set(entry.old_hash) == {'0'} for entry in entries)


def test_staged_listing_reports_real_corrupt_index(repo: Path) -> None:
    (repo / '.git' / 'index').write_bytes(b'broken index')

    with pytest.raises(GitCommandError) as caught:
        get_staged_py_info(str(repo))

    assert caught.value.detail.strip()
    assert str(caught.value) == caught.value.detail


def test_staged_rename_keeps_source_hash_and_status(repo: Path) -> None:
    (repo / 'old.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'old.py', cwd=repo)
    git('commit', '-m', 'add old', cwd=repo)
    old_hash = git('rev-parse', 'HEAD:old.py', cwd=repo).stdout.decode().strip()
    git('mv', 'old.py', 'new.py', cwd=repo)

    entries = get_staged_py_info(str(repo))

    assert len(entries) == 1
    assert entries[0].path == 'new.py'
    assert entries[0].status.startswith('R')
    assert entries[0].old_hash == old_hash


def test_staged_rename_parser_keeps_following_entry(repo: Path) -> None:
    for name in ('old.py', 'other.py'):
        (repo / name).write_text('x = 1\n', encoding='utf-8')
    git('add', '.', cwd=repo)
    git('commit', '-m', 'add files', cwd=repo)
    git('mv', 'old.py', 'new.py', cwd=repo)
    (repo / 'other.py').write_text('x = 2\n', encoding='utf-8')
    git('add', '.', cwd=repo)

    assert [entry.path for entry in get_staged_py_info(str(repo))] == ['new.py', 'other.py']


def test_tracked_listing_uses_bounded_query_and_literal_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str | bytes]] = []

    def run(command: list[str | bytes], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b'a.py\0*.py\0b.txt\0', b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert get_tracked_files('/w') == frozenset({'a.py', '*.py', 'b.txt'})
    assert commands[0][1:] == tracked_query()[1:]


def test_unborn_head_has_no_sources(repo: Path) -> None:
    (repo / 'new.py').write_text('x = 1\n', encoding='utf-8')
    git('add', 'new.py', cwd=repo)
    assert head_rename_sources(str(repo)) == {}


def test_unpaired_files_have_no_source(repo: Path) -> None:
    (repo / 'unpaired.py').write_text(
        ''.join(f'u_{n} = {n}\n' for n in range(10)), encoding='utf-8'
    )
    (repo / 'changed.py').write_text(
        ''.join(f'c_{n} = {n}\n' for n in range(10)), encoding='utf-8'
    )
    git('add', '.', cwd=repo)
    git('commit', '-m', 'add files', cwd=repo)
    (repo / 'unpaired.py').rename(repo / 'untracked.py')
    git('mv', 'changed.py', 'new.py', cwd=repo)
    (repo / 'new.py').write_text('replacement = True\n', encoding='utf-8')

    assert head_rename_sources(str(repo)) == {}
