import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles._blobs import (
    BlobRead,
    UnsupportedGitVersionError,
    read_blobs_by_hash,
    read_blobs_by_name,
    require_batch_z_support,
)
from defsort._vendor.gitfiles._run import (
    GitCommandError,
)

from ._support import (
    git,
)


def _output(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, data, b'')

    monkeypatch.setattr(subprocess, 'run', complete)


def _read(delimiter: bytes, count: int) -> list[BlobRead]:
    if delimiter == b'\n':
        return read_blobs_by_hash('/repo', ['abc123'] * count)
    return read_blobs_by_name(
        '/repo', [b'HEAD:actual.py'] * count, tool='tool', purpose='reading blobs by name'
    )


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
def test_empty_requests_launch_nothing(monkeypatch: pytest.MonkeyPatch, delimiter: bytes) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('empty request launched Git')

    monkeypatch.setattr(subprocess, 'run', unexpected)
    assert _read(delimiter, 0) == []


def test_launch_failure_does_not_probe_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fail(command: list[str], **_kwargs: object) -> None:
        calls.append(command[1:])
        raise OSError('git unavailable')

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(GitCommandError, match='git unavailable'):
        _read(b'\0', 1)
    assert calls == [['cat-file', '--batch', '-Z']]


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
@pytest.mark.parametrize('failure_index', [0, 1, 2])
@pytest.mark.parametrize(
    'failure',
    [
        b'badheader|',
        b'abc123 blob|hello|',
        b'abc123 tree|hello|',
        b'abc123 blob 5|hello!',
        b'abc123 blob 100|short|',
        b'abc123 blob |hello|',
        b'abc123 blob -1|hello|',
        b'abc123 blob 1.5|hello|',
        b'abc123 blob five|hello|',
        b'abc123 blob ' + b'9' * 4301 + b'|',
    ],
    ids=[
        'desync',
        'short-header',
        'unknown-length-tree',
        'wrong-terminator',
        'truncated-payload',
        'empty-size',
        'negative-size',
        'decimal-size',
        'word-size',
        'oversized-size',
    ],
)
def test_lost_framing_preserves_resolved_and_fails_rest(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes, failure_index: int, failure: bytes
) -> None:
    record = b'abc123 blob 5' + delimiter + b'hello' + delimiter
    _output(monkeypatch, record * failure_index + failure.replace(b'|', delimiter) + record)
    assert _read(delimiter, 3) == [BlobRead('content', b'hello')] * failure_index + [
        BlobRead('unavailable')
    ] * (3 - failure_index)


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
@pytest.mark.parametrize('failure_index', [0, 1, 2])
@pytest.mark.parametrize(
    'failure',
    [b'abc123 blob 5', b'abc123 blob 5|hello'],
    ids=['truncated-header', 'missing-terminator'],
)
def test_eof_framing_preserves_resolved_and_fails_rest(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes, failure_index: int, failure: bytes
) -> None:
    record = b'abc123 blob 5' + delimiter + b'hello' + delimiter
    _output(monkeypatch, record * failure_index + failure.replace(b'|', delimiter))
    assert _read(delimiter, 3) == [BlobRead('content', b'hello')] * failure_index + [
        BlobRead('unavailable')
    ] * (3 - failure_index)


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
def test_missing_continues_and_echo_uses_exact_request(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes
) -> None:
    request = b'abc123' if delimiter == b'\n' else b'HEAD:actual.py'
    record = b'abc123 blob 5' + delimiter + b'hello' + delimiter
    _output(monkeypatch, record + request + b' missing' + delimiter + record)
    assert _read(delimiter, 3) == [
        BlobRead('content', b'hello'),
        BlobRead('missing'),
        BlobRead('content', b'hello'),
    ]


def test_name_reader_requires_exact_missing_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    _output(monkeypatch, b'HEAD:wrong.py missing\0HEAD:actual.py missing\0')
    assert _read(b'\0', 2) == [BlobRead('unavailable')] * 2


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
def test_non_blob_fails_only_its_request(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes
) -> None:
    record = b'abc123 blob 5' + delimiter + b'hello' + delimiter
    tree = b'abc123 tree 3' + delimiter + b'a' + delimiter + b'b' + delimiter
    _output(monkeypatch, record + tree + record)
    assert _read(delimiter, 3) == [
        BlobRead('content', b'hello'),
        BlobRead('not-blob'),
        BlobRead('content', b'hello'),
    ]


def test_nonzero_exit_probes_and_reports_old_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        if command[1:] == ['--version']:
            return subprocess.CompletedProcess(command, 0, b'git version 2.41.7', b'')
        return subprocess.CompletedProcess(command, 129, b'', b'unknown option')

    monkeypatch.setattr(subprocess, 'run', complete)
    with pytest.raises(UnsupportedGitVersionError) as caught:
        _read(b'\0', 1)
    assert str(caught.value) == (
        'tool: Git 2.42 or newer is required for reading blobs by name (found Git 2.41.7)'
    )
    assert calls == [['cat-file', '--batch', '-Z'], ['--version']]


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
def test_payload_holding_the_framing_byte_keeps_framing(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes
) -> None:
    payload = b'a' + delimiter + b'b'
    _output(
        monkeypatch,
        b'abc123 blob 3'
        + delimiter
        + payload
        + delimiter
        + b'abc123 blob 1'
        + delimiter
        + b'x'
        + delimiter,
    )
    assert _read(delimiter, 2) == [BlobRead('content', payload), BlobRead('content', b'x')]


@pytest.mark.parametrize('failure_index', [0, 1, 2])
@pytest.mark.parametrize('identity', [b'garbage', b'abc456'])
@pytest.mark.parametrize('kind', [b' missing\n', b' blob 1\nx\n'])
def test_hash_reader_requires_exact_identity(
    monkeypatch: pytest.MonkeyPatch, failure_index: int, identity: bytes, kind: bytes
) -> None:
    record = b'abc123 blob 1\nx\n'
    _output(monkeypatch, record * failure_index + identity + kind + record)
    assert read_blobs_by_hash('/repo', ['abc123'] * 3) == [
        BlobRead('content', b'x')
    ] * failure_index + [BlobRead('unavailable')] * (3 - failure_index)


def test_readers_return_exact_bytes(repo: Path) -> None:
    data = b'\xef\xbb\xbfx = 1\r\n'
    (repo / 'm.py').write_bytes(data)
    git('add', 'm.py', cwd=repo)
    git('commit', '-qm', 'Add source', cwd=repo)
    blob_hash = git('rev-parse', 'HEAD:m.py', cwd=repo).stdout.decode().strip()
    assert read_blobs_by_hash(str(repo), [blob_hash]) == [BlobRead('content', data)]
    assert read_blobs_by_name(
        str(repo), [b'HEAD:m.py', b'HEAD:absent.py'], tool='tool', purpose='reading blobs by name'
    ) == [BlobRead('content', data), BlobRead('missing')]


@pytest.mark.parametrize(
    ('delimiter', 'object_name'),
    [(b'\0', b'HEAD:actual.py'), (b'\0', b'abc123'), (b'\n', b'abc123')],
)
def test_success_sends_framed_requests_without_version_probe(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes, object_name: bytes
) -> None:
    calls: list[tuple[list[str], object]] = []

    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command[1:], kwargs['input']))
        data = b'abc123 blob 1' + delimiter + b'x' + delimiter
        return subprocess.CompletedProcess(command, 0, data, b'')

    monkeypatch.setattr(subprocess, 'run', complete)
    result = (
        read_blobs_by_hash('/repo', [object_name.decode()])
        if delimiter == b'\n'
        else read_blobs_by_name(
            '/repo', [object_name], tool='tool', purpose='reading blobs by name'
        )
    )
    assert result == [BlobRead('content', b'x')]
    command = ['cat-file', '--batch'] + (['-Z'] if delimiter == b'\0' else [])
    assert calls == [(command, object_name + delimiter)]


@pytest.mark.parametrize(
    'version',
    [
        b'unknown',
        b'git version 2.42.0',
        b'git version 2.55.0.windows.3',
        'git version \N{SUPERSCRIPT TWO}.41'.encode(),
        b'git version ' + b'9' * 5000 + b'.41',
        b'git version 2.' + b'9' * 5000,
        b'\xff',
    ],
    ids=[
        'unknown',
        'minimum',
        'vendor',
        'non-ascii',
        'oversized-major',
        'oversized-minor',
        'invalid-utf8',
    ],
)
def test_nonzero_exit_keeps_failure_for_unrecognized_or_supported_version(
    monkeypatch: pytest.MonkeyPatch, version: bytes
) -> None:
    calls: list[list[str]] = []

    def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        if command[1:] == ['--version']:
            return subprocess.CompletedProcess(command, 0, version, b'')
        return subprocess.CompletedProcess(command, 129, b'', b'original failure')

    monkeypatch.setattr(subprocess, 'run', complete)
    with pytest.raises(GitCommandError, match=r'^original failure$'):
        _read(b'\0', 1)
    assert calls == [['cat-file', '--batch', '-Z'], ['--version']]


@pytest.mark.parametrize(
    'failure', [None, OSError('unreadable'), UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')]
)
def test_unreadable_version_preserves_original_failure(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    calls: list[list[str]] = []

    def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        if command[1:] == ['--version']:
            if failure is not None:
                raise failure
            return subprocess.CompletedProcess(command, 1, b'', b'version failure')
        return subprocess.CompletedProcess(command, 129, b'', b'original failure')

    monkeypatch.setattr(subprocess, 'run', complete)
    with pytest.raises(GitCommandError, match=r'^original failure$'):
        _read(b'\0', 1)
    assert calls == [['cat-file', '--batch', '-Z'], ['--version']]


@pytest.mark.parametrize('version', [b'2.41.7', b'2.42.0', b'2.55.0.windows.3', b'unknown'])
def test_require_batch_z_support(monkeypatch: pytest.MonkeyPatch, version: bytes) -> None:
    _output(monkeypatch, b'git version ' + version)
    if version == b'2.41.7':
        with pytest.raises(UnsupportedGitVersionError):
            require_batch_z_support('/repo', tool='tool', purpose='reading blobs by name')
    else:
        assert (
            require_batch_z_support('/repo', tool='tool', purpose='reading blobs by name') is None
        )


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
@pytest.mark.parametrize('payload', [b'', b'\xff\xfe'])
def test_content_includes_empty_and_undecodable_bytes(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes, payload: bytes
) -> None:
    _output(
        monkeypatch,
        b'abc123 blob ' + str(len(payload)).encode() + delimiter + payload + delimiter,
    )
    result = _read(delimiter, 1)[0]
    assert result.status == 'content'
    assert result.data == payload


@pytest.mark.parametrize('delimiter', [b'\0', b'\n'])
def test_size_syntax_preserves_each_framing_format(
    monkeypatch: pytest.MonkeyPatch, delimiter: bytes
) -> None:
    _output(monkeypatch, b'abc123 blob +1' + delimiter + b'x' + delimiter)
    expected = BlobRead('content', b'x') if delimiter == b'\n' else BlobRead('unavailable')
    assert _read(delimiter, 1) == [expected]
