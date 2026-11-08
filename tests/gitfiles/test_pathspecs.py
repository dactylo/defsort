import errno
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from defsort._vendor.gitfiles import (
    _pathspecs,
)
from defsort._vendor.gitfiles._listings import (
    match_tracked_py_files,
)
from defsort._vendor.gitfiles._pathspecs import (
    NonRegularPathspecError,
    PathspecFileError,
    parse_pathspec_records,
    read_pathspec_file,
)
from defsort._vendor.gitfiles._run import (
    GitCommandError,
    _git_executable,
)
from defsort._vendor.srcfiles import (
    decode_git_path,
)

from ._support import (
    git,
    latin1_decode,
    write_bytes_file,
)


class TestReadPathspecFile:
    def test_directory(self, tmp_path: Path) -> None:
        with pytest.raises(NonRegularPathspecError):
            read_pathspec_file(str(tmp_path), '.')

    def test_inaccessible_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / 'paths').write_bytes(b'test.py\n')

        def inaccessible(*_args: object, **_kwargs: object) -> None:
            raise PermissionError

        monkeypatch.setattr(os, 'open', inaccessible)
        with pytest.raises(PermissionError):
            read_pathspec_file(str(tmp_path), 'paths')

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_pathspec_file(str(tmp_path), 'missing')

    def test_opened_descriptor_checked_before_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / 'paths').write_bytes(b'test.py\n')
        opened: list[int] = []
        real_open = os.open
        real_fstat = os.fstat

        def open_resource(path: Path, flags: int) -> int:
            assert flags & getattr(os, 'O_NONBLOCK', 0) == getattr(os, 'O_NONBLOCK', 0)
            fd = real_open(path, flags)
            opened.append(fd)
            return fd

        def changed_type(fd: int) -> os.stat_result:
            values = list(real_fstat(fd))
            values[0] = stat.S_IFIFO
            return os.stat_result(values)

        def unexpected_read(*_args: object, **_kwargs: object) -> None:
            pytest.fail('resource read before checking opened descriptor')

        monkeypatch.setattr(os, 'open', open_resource)
        monkeypatch.setattr(os, 'fstat', changed_type)
        monkeypatch.setattr(os, 'fdopen', unexpected_read)
        with pytest.raises(NonRegularPathspecError):
            read_pathspec_file(str(tmp_path), 'paths')
        assert len(opened) == 1
        with pytest.raises(OSError, match=r'.+') as error:
            real_fstat(opened[0])
        assert error.value.errno == errno.EBADF

    @pytest.mark.parametrize('content', [b'', b'test.py\n', b'test.py\r\n', b'test.py\0'])
    @pytest.mark.parametrize('relative', [False, True])
    def test_ordinary_bytes(self, tmp_path: Path, content: bytes, *, relative: bool) -> None:
        path = tmp_path / 'paths'
        path.write_bytes(content)
        assert read_pathspec_file(str(tmp_path), 'paths' if relative else str(path)) == content

    @pytest.mark.parametrize('kind', ['fifo', 'device'])
    def test_resource_rejected_in_bounded_child(self, tmp_path: Path, kind: str) -> None:
        if kind == 'fifo':
            mkfifo = getattr(os, 'mkfifo', None)
            if not callable(mkfifo):
                pytest.skip('FIFOs unavailable')
            path = tmp_path / 'fifo'
            mkfifo(path)
        else:
            path = Path(os.devnull)
            if not stat.S_ISCHR(path.stat().st_mode):
                pytest.skip('Character device unavailable')
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                (
                    'import importlib, sys\n'
                    'module = importlib.import_module(sys.argv[3])\n'
                    'try:\n'
                    '    module.read_pathspec_file(sys.argv[1], sys.argv[2])\n'
                    'except module.NonRegularPathspecError:\n'
                    '    pass\n'
                    'else:\n'
                    '    raise AssertionError("nonregular resource accepted")\n'
                ),
                str(tmp_path),
                str(path),
                _pathspecs.__name__,
            ],
            capture_output=True,
            check=False,
            env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)},
            timeout=5,
        )
        assert result.returncode == 0, result.stderr

    def test_socket(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        family = getattr(socket, 'AF_UNIX', None)
        if not isinstance(family, int):
            pytest.skip('Unix sockets unavailable')
        path = tmp_path / 'socket'
        monkeypatch.chdir(tmp_path)
        with socket.socket(family) as resource:
            resource.bind('socket')
            with pytest.raises(NonRegularPathspecError):
                read_pathspec_file(str(tmp_path), str(path))

    def test_symlink_to_ordinary_file(self, tmp_path: Path) -> None:
        path = tmp_path / 'paths'
        path.write_bytes(b'test.py\n')
        link = tmp_path / 'link'
        link.symlink_to(path)
        assert read_pathspec_file(str(tmp_path), 'link') == b'test.py\n'


@pytest.mark.parametrize(
    ('case', 'record', 'expected'),
    [
        ('relative-file', b'test.py\n', ['test.py']),
        ('deleted', b'test.py\n', []),
        ('mixed-directory', b'.\n', ['test.py']),
        ('symlink', b'link.py\n', []),
        ('non-python', b'readme.md\n', []),
    ],
)
def test_file_records_preserve_selection_filters(
    repo: Path, case: str, record: bytes, expected: list[str]
) -> None:
    (repo / 'test.py').write_bytes(b'pass\n')
    (repo / 'readme.md').write_bytes(b'# notes\n')
    (repo / 'link.py').symlink_to(repo / 'test.py')
    git('add', '.', cwd=repo)
    if case == 'deleted':
        (repo / 'test.py').unlink()
    (repo / 'paths.txt').write_bytes(record)

    data = read_pathspec_file(str(repo), 'paths.txt')
    pathspecs = parse_pathspec_records(data, nul=False)
    assert match_tracked_py_files(str(repo), pathspecs, repo_root=str(repo)) == expected


@pytest.mark.parametrize(
    ('data', 'nul', 'expected', 'detail'),
    [
        (b'a.py\n', False, ['a.py'], None),
        (b'"sp ace.py"\n', False, ['sp ace.py'], None),
        (b'"a.py"x\n', False, ['a.py'], None),
        (b'"a.py\\000b"\n', False, ['a.py'], None),
        (b'a.py\r\n', False, ['a.py'], None),
        (b'a.py\0junk\n', False, ['a.py'], None),
        (b'a.py', False, ['a.py'], None),
        (b'', False, [], None),
        (b'\n', False, None, 'empty pathspec record'),
        (b'\r\n', False, None, 'empty pathspec record'),
        (b'""\n', False, None, 'empty pathspec record'),
        (b'\0a.py\n', False, None, 'empty pathspec record'),
        (b'"a.py\n', False, None, 'malformed quoted pathspec record'),
        (b'"\\q.py"\n', False, None, 'malformed quoted pathspec record'),
        (b'a.py\0b.py\0', True, ['a.py', 'b.py'], None),
        (b'"quoted".py\0', True, ['"quoted".py'], None),
        (b'back\\\\slash.py\0', True, ['back\\\\slash.py'], None),
        (b'""\0', True, ['""'], None),
        (b'a.py', True, ['a.py'], None),
        (b'a.py\0\0b.py\0', True, None, 'empty pathspec record'),
        (b'\0', True, None, 'empty pathspec record'),
    ],
)
def test_records_match_git(
    repo: Path, data: bytes, *, nul: bool, expected: list[str] | None, detail: str | None
) -> None:
    if expected is None:
        with pytest.raises(PathspecFileError) as caught:
            parse_pathspec_records(data, nul=nul)
        assert caught.value.detail == detail
    else:
        assert parse_pathspec_records(data, nul=nul) == expected

    if (
        os.name == 'nt'
        and expected is not None
        and any(character in name for name in expected for character in '"\r\n\\')
    ):
        return
    pathspec_file = repo / 'paths.txt'
    pathspec_file.write_bytes(data)
    for name in expected or []:
        (repo / name).write_bytes(b'pass\n')
    executable = _git_executable()
    assert executable is not None
    command = [executable, '-c', 'core.quotePath=false', 'add', '-n']
    if nul:
        command.append('--pathspec-file-nul')
    command.append(f'--pathspec-from-file={pathspec_file}')
    result = subprocess.run(command, cwd=repo, capture_output=True, check=False)
    if expected is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert [
            line.removeprefix(b"add '").removesuffix(b"'").decode()
            for line in result.stdout.splitlines()
        ] == expected


@pytest.mark.skipif(os.name == 'nt', reason='POSIX byte filenames')
def test_nul_byte_record_reaches_git(repo: Path) -> None:
    raw = b'bad-\xff.py'
    write_bytes_file(repo, raw, b'pass\n')
    git('add', os.fsdecode(raw), cwd=repo)
    records = parse_pathspec_records(raw + b'\0', nul=True)
    assert records == [decode_git_path(raw)]
    assert match_tracked_py_files(str(repo), records, repo_root=str(repo)) == records


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
@pytest.mark.parametrize(
    ('filename', 'record'),
    [
        ('plain.py', b'plain.py'),
        ('space name.py', b'"space name.py"'),
        ('quote"name.py', b'"quote\\"name.py"'),
        ('back\\slash.py', b'"back\\\\slash.py"'),
        ('tab\tname.py', b'"tab\\tname.py"'),
        ('line\nname.py', b'"line\\nname.py"'),
        ('\u20ac.py', b'"\\342\\202\\254.py"'),
        ('magic.py', b'":(literal)magic.py"'),
        ('nested/inner.py', b'"nested/"'),
    ],
)
def test_lf_records_select_same_files_as_git(
    repo: Path, filename: str, record: bytes, ending: bytes
) -> None:
    if os.name == 'nt' and any(character in filename for character in '"\\\t\n'):
        pytest.skip('Filename contains characters unsupported on Windows')
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'pass\n')
    git('config', 'core.precomposeunicode', 'false', cwd=repo)
    git('add', '--', filename, cwd=repo)
    pathspecs = parse_pathspec_records(record + ending, nul=False)
    assert match_tracked_py_files(str(repo), pathspecs, repo_root=str(repo)) == [filename]


def test_records_use_the_git_path_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pathspecs, 'decode_git_path', latin1_decode)
    assert parse_pathspec_records(b'caf\xc3\xa9.py\n', nul=False) == [
        b'caf\xc3\xa9.py'.decode('latin-1')
    ]


def test_selection_preserves_quoted_lf_and_literal_nul(repo: Path) -> None:
    names = ['space name.py', '[quoted].py', 'nested/inner.py']
    for name in names:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'pass\n')
    git('add', '.', cwd=repo)
    lf = parse_pathspec_records(b'"space name.py"\n"nested/"\n', nul=False)
    literal = parse_pathspec_records(b'[quoted].py\0', nul=True)
    assert match_tracked_py_files(str(repo), lf, repo_root=str(repo)) == [
        'nested/inner.py',
        'space name.py',
    ]
    assert match_tracked_py_files(str(repo), literal, repo_root=str(repo)) == ['[quoted].py']


def test_selection_propagates_git_failure(tmp_path: Path) -> None:
    with pytest.raises(GitCommandError):
        match_tracked_py_files(str(tmp_path), ['missing.py'], repo_root=str(tmp_path))


def test_windows_refuses_an_undecodable_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pathspecs, '_WINDOWS', True)
    raw = b'bad-\xff.py'
    with pytest.raises(PathspecFileError) as caught:
        parse_pathspec_records(raw + b'\0', nul=True)
    assert caught.value.detail == (
        'pathspec-file entry bad-\\udcff.py is not valid UTF-8; Git on Windows cannot receive it'
    )
