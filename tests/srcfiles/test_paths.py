import os
from pathlib import Path

import pytest

from defsort._vendor.srcfiles import (
    ALIAS_CONFLICT,
    group_paths,
    names_agree,
)


def _link_or_skip(source: Path, name: str) -> Path:
    link = source.with_name(name)
    try:
        os.link(source, link)
    except OSError as error:
        pytest.skip(f'hard links unavailable: {error}')
    return link


def _symlink_or_skip(source: Path, name: str) -> Path:
    link = source.with_name(name)
    try:
        link.symlink_to(source)
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    return link


def test_alias_conflict_message() -> None:
    assert ALIAS_CONFLICT == 'names of one file need different results'


def test_case_alias_of_a_hard_link_joins_its_group(tmp_path: Path) -> None:
    a = tmp_path / 'Mod.py'
    a.write_bytes(b'a\n')
    if not (tmp_path / 'mod.py').exists():
        pytest.skip('requires a case-insensitive filesystem')
    hard = _link_or_skip(a, 'link.py')
    assert group_paths([tmp_path / 'mod.py', hard, a]) == [[tmp_path / 'mod.py', hard, a]]


def test_distinct_files_stay_apart_in_caller_order(tmp_path: Path) -> None:
    b = tmp_path / 'b.py'
    a = tmp_path / 'a.py'
    b.write_bytes(b'b\n')
    a.write_bytes(b'a\n')
    assert group_paths([b, a]) == [[b], [a]]


def test_hard_links_and_symlinks_join_the_first_name_in_order(tmp_path: Path) -> None:
    a = tmp_path / 'a.py'
    a.write_bytes(b'a\n')
    other = tmp_path / 'other.py'
    other.write_bytes(b'other\n')
    hard = _link_or_skip(a, 'hard.py')
    soft = _symlink_or_skip(a, 'soft.py')
    assert group_paths([other, hard, a, soft]) == [[other], [hard, a, soft]]


def test_missing_paths_group_by_resolved_path(tmp_path: Path) -> None:
    missing = tmp_path / 'missing.py'
    other = tmp_path / 'other.py'
    assert group_paths([missing, other, tmp_path / 'sub' / '..' / 'missing.py']) == [
        [missing, tmp_path / 'sub' / '..' / 'missing.py'],
        [other],
    ]


@pytest.mark.parametrize(
    ('needed', 'expected'),
    [
        ([b'one', b'one'], True),
        ([b'one', b'two'], False),
        ([b'one', None], False),
        ([None, None], True),
        ([b'one'], True),
    ],
)
def test_names_agree(needed: list[bytes | None], *, expected: bool) -> None:
    assert names_agree(needed) is expected


def test_repeated_operands_and_spellings_form_one_group(tmp_path: Path) -> None:
    a = tmp_path / 'a.py'
    a.write_bytes(b'a\n')
    (tmp_path / 'sub').mkdir()
    spelled = tmp_path / 'sub' / '..' / 'a.py'
    assert group_paths([a, spelled, a]) == [[a, spelled, a]]


def test_resolved_path_joins_successful_stats_with_different_inodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / 'a.py'
    a.write_bytes(b'a\n')
    (tmp_path / 'sub').mkdir()
    spelled = tmp_path / 'sub' / '..' / 'a.py'
    real_stat = Path.stat

    def stat_after_replacement(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        status = real_stat(path, follow_symlinks=follow_symlinks)
        if path == spelled:
            fields = list(status)
            fields[1] += 1
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(Path, 'stat', stat_after_replacement)
    assert group_paths([a, spelled]) == [[a, spelled]]


def test_transient_stat_failure_does_not_split_a_same_path_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / 'a.py'
    a.write_bytes(b'a\n')
    hard = _link_or_skip(a, 'hard.py')
    (tmp_path / 'sub').mkdir()
    flaky = tmp_path / 'sub' / '..' / 'a.py'
    real_stat = Path.stat

    def stat_failing_once(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == flaky:
            raise PermissionError('transient')
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, 'stat', stat_failing_once)
    assert group_paths([hard, flaky, a]) == [[hard, flaky, a]]


def test_zero_inode_keeps_distinct_files_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / 'a.py'
    b = tmp_path / 'b.py'
    a.write_bytes(b'a\n')
    b.write_bytes(b'b\n')
    real_stat = Path.stat

    def stat_without_inode(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        status = real_stat(path, follow_symlinks=follow_symlinks)
        fields = list(status)
        fields[1] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(Path, 'stat', stat_without_inode)
    assert group_paths([a, b, a]) == [[a, a], [b]]
