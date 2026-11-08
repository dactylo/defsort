import os
from pathlib import Path

import pytest

from defsort._vendor.srcfiles import (
    KeyMatch,
    SpellingIndex,
    _keys,
    decode_git_path,
    encode_git_path,
    repository_key,
    tracked_paths,
    tracked_query,
)

_WINDOWS_CASES = [True] if os.name == 'nt' else [True, False]


def _hard_link_if_missing(source: Path, target: Path) -> None:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)


def test_case_variant_matches_gits_spelling(tmp_path: Path) -> None:
    tracked = tmp_path / 'Thing.py'
    tracked.touch()
    selected = tmp_path / 'THING.py'
    _hard_link_if_missing(tracked, selected)
    assert SpellingIndex({'Thing.py'}).match(selected, str(tmp_path), 'THING.py') == KeyMatch(
        'matched', 'Thing.py'
    )


@pytest.mark.parametrize('windows', _WINDOWS_CASES)
def test_git_path_round_trips_every_byte(
    monkeypatch: pytest.MonkeyPatch, *, windows: bool
) -> None:
    monkeypatch.setattr(_keys, '_WINDOWS', windows)
    for byte in range(256):
        raw = bytes([byte]) + b'.py'
        assert encode_git_path(decode_git_path(raw)) == raw


@pytest.mark.parametrize('windows', _WINDOWS_CASES)
@pytest.mark.parametrize(
    'raw',
    [
        '\u00e9'.encode(),
        '\u65e5'.encode(),
        '\U0001f600'.encode(),
        b'\xc3\x28',
        b'\xe6\x97',
        b'\xc0\xaf',
    ],
)
def test_git_path_round_trips_multibyte_sequences(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, *, windows: bool
) -> None:
    monkeypatch.setattr(_keys, '_WINDOWS', windows)
    assert encode_git_path(decode_git_path(raw)) == raw


def test_has_variant_reports_only_another_spelling() -> None:
    assert SpellingIndex({'Thing.py', 'thing.py'}).has_variant('Thing.py')
    assert not SpellingIndex({'Thing.py'}).has_variant('Thing.py')


def test_identity_failure_makes_key_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked = tmp_path / 'Thing.py'
    tracked.touch()
    selected = tmp_path / 'THING.py'
    _hard_link_if_missing(tracked, selected)
    real_stat = Path.stat

    def deny_candidate(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == tracked:
            raise PermissionError('denied')
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, 'stat', deny_candidate)
    assert SpellingIndex({'Thing.py'}).match(selected, str(tmp_path), 'THING.py') == KeyMatch(
        'unavailable'
    )


def test_repository_key_falls_back_to_path_for_unknown_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'a.py'
    path.touch()
    actual_stat = Path.stat

    def unknown_inode(candidate: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = list(actual_stat(candidate, follow_symlinks=follow_symlinks))
        result[1] = 0
        return os.stat_result(result)

    monkeypatch.setattr(Path, 'stat', unknown_inode)
    assert repository_key(path, str(tmp_path)) == 'a.py'


def test_repository_key_matches_root_through_symlinked_ancestor(tmp_path: Path) -> None:
    root = tmp_path / 'real' / 'repo'
    root.mkdir(parents=True)
    path = root / 'a.py'
    path.touch()
    alias = tmp_path / 'alias'
    alias.symlink_to(root.parent, target_is_directory=True)
    assert repository_key(path, str(alias / 'repo')) == 'a.py'


def test_repository_key_rejects_outside_and_missing_files(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside.py'
    outside.touch()
    assert repository_key(outside, str(root)) is None
    assert repository_key(root / 'missing.py', str(root)) is None


def test_repository_key_uses_the_path_below_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    path = root / 'dir' / 'file.py'
    path.parent.mkdir(parents=True)
    path.touch()
    assert repository_key(path, str(root)) == 'dir/file.py'


def test_same_fold_but_distinct_file_is_untracked(tmp_path: Path) -> None:
    tracked = tmp_path / 'Thing.py'
    tracked.touch()
    selected = tmp_path / 'thing.py'
    if selected.exists():
        pytest.skip('needs a case-sensitive filesystem')
    selected.touch()
    assert SpellingIndex({'Thing.py'}).match(selected, str(tmp_path), 'thing.py') == KeyMatch(
        'untracked'
    )


def test_tracked_paths_keep_undecodable_names() -> None:
    encoded = b'bad-\xff.py'
    paths = tracked_paths(encoded + b'\0good.py\0')
    assert 'good.py' in paths
    assert {path.encode('utf-8', 'surrogateescape') for path in paths} == {encoded, b'good.py'}


def test_tracked_query_and_paths() -> None:
    assert tracked_query() == ['git', 'ls-files', '-z', '--']
    assert tracked_paths(b'a.py\0dir/b.py\0') == frozenset({'a.py', 'dir/b.py'})
    assert tracked_paths(b'') == frozenset()


@pytest.mark.parametrize('selected_key', ['Pkg/Thing.py', 'PKG/THING.py'])
def test_two_tracked_spellings_make_key_unavailable(tmp_path: Path, selected_key: str) -> None:
    first = tmp_path / 'Pkg' / 'Thing.py'
    first.parent.mkdir()
    first.touch()
    second = tmp_path / 'pkg' / 'thing.py'
    _hard_link_if_missing(first, second)
    selected = tmp_path / selected_key
    _hard_link_if_missing(first, selected)
    assert SpellingIndex({'Pkg/Thing.py', 'pkg/thing.py'}).match(
        selected, str(tmp_path), selected_key
    ) == KeyMatch('unavailable')


def test_unique_exact_key_does_not_check_file_identity(tmp_path: Path) -> None:
    assert SpellingIndex({'gone.py'}).match(tmp_path / 'gone.py', str(tmp_path), 'gone.py') == (
        KeyMatch('matched', 'gone.py')
    )


def test_windows_codec_uses_utf8_surrogateescape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_keys, '_WINDOWS', True)
    assert decode_git_path(b'bad-\xff.py') == 'bad-\udcff.py'
    assert encode_git_path('bad-\udcff.py') == b'bad-\xff.py'
