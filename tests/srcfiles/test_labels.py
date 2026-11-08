import os
import subprocess
from pathlib import Path

import pytest

from defsort._vendor.srcfiles import (
    DEFAULT_PRIVATE_ATTRIBUTE,
    label_query,
    labelled_paths,
    private_attribute,
)

_INVALID_NAMES = (
    '-private',
    'a b',
    'a:b',
    'a/b',
    'a=b',
    '\u00e9',
    '',
    '!x',
    ' private',
    'builtin_private',
)
_VALID_NAMES = ('private', 'my.private', 'my_private', 'my-private', '9lives')


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_ATTR_NOSYSTEM', '1')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    for name in (
        'GIT_CONFIG',
        'GIT_CONFIG_COUNT',
        'GIT_CONFIG_KEY_0',
        'GIT_CONFIG_VALUE_0',
        'GIT_CONFIG_PARAMETERS',
        'GIT_DIR',
    ):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    return root


def _labelled(repo: Path, paths: list[bytes], attribute: str) -> frozenset[bytes]:
    command, stdin = label_query(attribute, paths)
    result = subprocess.run(command, cwd=repo, input=stdin, capture_output=True, check=True)
    return labelled_paths(result.stdout, attribute)


def test_global_attributes_file_labels_paths(repo: Path, tmp_path: Path) -> None:
    attributes = tmp_path / 'xdg' / 'git' / 'attributes'
    attributes.parent.mkdir(parents=True)
    attributes.write_text('notes/** private\n')
    assert _labelled(repo, [b'notes/x.py', b'src/x.py'], 'private') == {b'notes/x.py'}


@pytest.mark.parametrize('name', _INVALID_NAMES)
def test_invalid_names_are_rejected_with_the_rule(name: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"^use letters, digits, '\.', '_', or '-', not starting with '-' or 'builtin_'$",
    ):
        private_attribute(name)


def test_label_query_builds_the_check_attr_command() -> None:
    assert label_query('private', [b'a.py', b'b c.py']) == (
        ['git', 'check-attr', '-z', '--stdin', 'private'],
        b'a.py\0b c.py\0',
    )


def test_label_query_rejects_an_invalid_name() -> None:
    with pytest.raises(ValueError, match=r'^use letters, digits,'):
        label_query('-private', [b'a.py'])


def test_only_set_values_are_labelled() -> None:
    output = b'a\0private\0set\0b\0private\0unset\0c\0private\0unspecified\0d\0private\0yes\0'
    assert labelled_paths(output, 'private') == {b'a'}
    assert labelled_paths(b'', 'private') == frozenset()


def test_unconfigured_name_is_private() -> None:
    assert private_attribute(None) == DEFAULT_PRIVATE_ATTRIBUTE == 'private'


@pytest.mark.parametrize('name', _VALID_NAMES)
def test_valid_names_label_paths_in_git(repo: Path, name: str) -> None:
    (repo / '.gitattributes').write_text(f'x.py {name}\n')
    assert private_attribute(name) == name
    assert _labelled(repo, [b'x.py'], name) == {b'x.py'}


@pytest.mark.parametrize('output', [b'a\0private\0set', b'a\0private\0', b'a\0other\0set\0'])
def test_malformed_output_is_rejected(output: bytes) -> None:
    with pytest.raises(ValueError, match=r'^malformed git check-attr output$'):
        labelled_paths(output, 'private')


@pytest.mark.parametrize(
    ('declarations', 'attribute', 'path', 'expected'),
    [
        ((('.gitattributes', 'x.py private\n'),), 'private', 'x.py', (b'x.py',)),
        (
            (('sub/.gitattributes', '* private\n'),),
            'private',
            'sub/deep/x.py',
            (b'sub/deep/x.py',),
        ),
        ((('.gitattributes', 'glob/** private\n'),), 'private', 'glob/x.py', (b'glob/x.py',)),
        (
            (('.gitattributes', '[attr]secret private\nx.py secret\n'),),
            'private',
            'x.py',
            (b'x.py',),
        ),
        ((('.gitattributes', 'x.py private=set\n'),), 'private', 'x.py', (b'x.py',)),
        ((('.gitattributes', 'x.py private=yes\n'),), 'private', 'x.py', ()),
        ((('.gitattributes', 'x.py -private\n'),), 'private', 'x.py', ()),
        ((('.gitattributes', 'x.py !private\n'),), 'private', 'x.py', ()),
        ((), 'private', 'x.py', ()),
        ((('.gitattributes', 'x.py unpublished\n'),), 'unpublished', 'x.py', (b'x.py',)),
        ((('.gitattributes', 'x.py unpublished\n'),), 'private', 'x.py', ()),
        (
            (('.gitattributes', 'vendor/** private\n'),),
            'private',
            'vendor/nested',
            (b'vendor/nested',),
        ),
        ((('.gitattributes', 'folder private\n'),), 'private', 'folder/x.py', ()),
        (
            (('.gitattributes', 'notes/** private\n'),),
            'private',
            'notes/my notes \u00e9.py',
            (os.fsencode('notes/my notes \u00e9.py'),),
        ),
    ],
)
def test_git_reports_labels_that_match_the_contract(
    repo: Path,
    declarations: tuple[tuple[str, str], ...],
    attribute: str,
    path: str,
    expected: tuple[bytes, ...],
) -> None:
    for location, content in declarations:
        target = repo / location
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    encoded = os.fsencode(path)
    assert _labelled(repo, [encoded, b'other.py'], attribute) == set(expected)
