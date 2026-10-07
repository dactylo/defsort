import errno
import io
import os
import sys
from collections.abc import Iterator

import pytest

from defsort import _downstream
from defsort._downstream import (
    _ClosableTextOutput,
    _is_downstream_closure,
    _silence_descriptor,
    closable_stdout,
    ensure_utf8_stdio,
    stdout_closed,
)
from tests._helpers import _ENV, process_transcript, run_setup


class TestEnsureUtf8Stdio:
    def test_posix_stdout_uses_backslashreplace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogateescape')
        stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict')
        stderr = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogatepass')
        monkeypatch.setattr('sys.platform', 'linux')
        monkeypatch.setattr('sys.stdin', stdin)
        monkeypatch.setattr('sys.stdout', stdout)
        monkeypatch.setattr('sys.stderr', stderr)

        ensure_utf8_stdio()

        assert (stdin.encoding, stdin.errors) == ('cp1252', 'surrogateescape')
        assert (stdout.encoding, stdout.errors) == ('cp1252', 'backslashreplace')
        assert (stderr.encoding, stderr.errors) == ('cp1252', 'surrogatepass')

    def test_string_streams_are_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr('sys.platform', 'win32')
        streams = [io.StringIO() for _ in range(3)]
        monkeypatch.setattr('sys.stdin', streams[0])
        monkeypatch.setattr('sys.stdout', streams[1])
        monkeypatch.setattr('sys.stderr', streams[2])
        ensure_utf8_stdio()
        assert (sys.stdin, sys.stdout, sys.stderr) == tuple(streams)

    def test_windows_stdout_uses_backslashreplace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogateescape')
        stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict')
        stderr = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogatepass')
        monkeypatch.setattr('sys.platform', 'win32')
        monkeypatch.setattr('sys.stdin', stdin)
        monkeypatch.setattr('sys.stdout', stdout)
        monkeypatch.setattr('sys.stderr', stderr)

        ensure_utf8_stdio()

        assert (stdin.encoding, stdin.errors) == ('utf-8', 'surrogateescape')
        assert (stdout.encoding, stdout.errors) == ('utf-8', 'backslashreplace')
        assert (stderr.encoding, stderr.errors) == ('utf-8', 'surrogatepass')


class TestStdoutEscapeBoundary:
    def test_surrogate_path_text_renders_escaped(self) -> None:
        code = (
            'import sys\n'
            'from defsort._downstream import ensure_utf8_stdio\n'
            'ensure_utf8_stdio()\n'
            'print(sys.stdout.errors)\n'
            "print('bad\\udcff.py')\n"
        )
        proc = run_setup(
            [sys.executable, '-c', code],
            env={**_ENV, 'PYTHONIOENCODING': 'utf-8:strict'},
            check=False,
        )
        assert proc.returncode == 0, process_transcript(proc)
        assert b'backslashreplace' in proc.stdout
        assert b'bad\\udcff.py' in proc.stdout


class _FailingStream(io.StringIO):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self.error = error
        self.attempts = 0

    def flush(self) -> None:
        raise self.error

    def write(self, _text: str) -> int:
        self.attempts += 1
        raise self.error


@pytest.mark.parametrize('windows', [False, True])
def test_broken_pipe_is_closure_everywhere(windows: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    assert _is_downstream_closure(BrokenPipeError())


def test_closable_stdout_flushes_when_leaving_by_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silenced: list[object] = []

    class _BufferedClosedStream(io.StringIO):
        def flush(self) -> None:
            raise BrokenPipeError

    stream = _BufferedClosedStream()

    def record(closed: object) -> None:
        silenced.append(closed)

    monkeypatch.setattr(_downstream, '_silence_descriptor', record)
    monkeypatch.setattr(sys, 'stdout', stream)

    def exit_after_help() -> None:
        print('usage: buffered help text')
        raise SystemExit(0)

    with pytest.raises(SystemExit), closable_stdout():
        exit_after_help()
    assert silenced == [stream]


def test_closable_stdout_restores_the_original_stream() -> None:
    original = sys.stdout
    with closable_stdout() as output:
        assert sys.stdout is output
        assert not stdout_closed()
    assert sys.stdout is original


def test_closable_stdout_silences_a_closed_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silenced: list[object] = []

    def record(stream: object) -> None:
        silenced.append(stream)

    monkeypatch.setattr(_downstream, '_silence_descriptor', record)
    monkeypatch.setattr(sys, 'stdout', _FailingStream(BrokenPipeError()))
    original = sys.stdout

    with closable_stdout():
        print('discarded')
        assert stdout_closed()
    assert silenced == [original]


def test_einval_is_closure_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(errno.EINVAL, 'Invalid argument')
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    assert _is_downstream_closure(error)
    monkeypatch.setattr(_downstream, '_WINDOWS', False)
    assert not _is_downstream_closure(error)


def test_flush_latches_windows_einval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    output = _ClosableTextOutput(_FailingStream(OSError(errno.EINVAL, 'closed')))

    output.flush()
    output.flush()
    assert output.closed_downstream


def test_flush_propagates_unrelated_errno() -> None:
    output = _ClosableTextOutput(_FailingStream(OSError(errno.EBADF, 'bad')))

    with pytest.raises(OSError, match='bad'):
        output.flush()


def test_other_attributes_come_from_the_wrapped_stream() -> None:
    stream = io.StringIO()
    output = _ClosableTextOutput(stream)

    assert output.getvalue is not None
    output.write('kept')
    assert stream.getvalue() == 'kept'


def test_unrelated_errno_is_not_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    assert not _is_downstream_closure(OSError(errno.EBADF, 'bad'))


@pytest.mark.parametrize(
    ('error', 'windows'),
    [(BrokenPipeError(), False), (OSError(errno.EINVAL, 'closed'), True)],
)
def test_write_latches_closure_and_discards_later_text(
    error: OSError, windows: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    stream = _FailingStream(error)
    output = _ClosableTextOutput(stream)

    assert output.write('first') == len('first')
    assert output.write('second') == len('second')
    assert output.closed_downstream
    assert stream.attempts == 1


@pytest.mark.parametrize(
    ('error', 'windows'),
    [(OSError(errno.EINVAL, 'closed'), False), (OSError(errno.EBADF, 'bad'), True)],
)
def test_write_propagates_errors_that_are_not_closure(
    error: OSError, windows: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    output = _ClosableTextOutput(_FailingStream(error))

    with pytest.raises(OSError, match=r'closed|bad') as raised:
        output.write('text')
    assert raised.value is error
    assert not output.closed_downstream


class _FakeOs:
    O_WRONLY = os.O_WRONLY
    devnull = os.devnull

    def __init__(self, *, redirect_error: OSError | None) -> None:
        self.events: list[tuple[str, int]] = []
        self.redirect_error = redirect_error

    def close(self, fd: int) -> None:
        self.events.append(('close', fd))

    def dup2(self, source: int, _target: int) -> None:
        self.events.append(('dup2', source))
        if self.redirect_error is not None:
            raise self.redirect_error

    def open(self, _path: str, _flags: int) -> int:
        return 37


class _DescriptorStream(io.StringIO):
    def fileno(self) -> int:
        return 1


def test_silence_descriptor_closes_devnull_and_propagates_redirect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=OSError('injected redirect failure'))
    monkeypatch.setattr(_downstream, 'os', fake)

    with pytest.raises(OSError, match='injected redirect failure'):
        _silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_silence_descriptor_leaves_streams_without_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)
    _silence_descriptor(io.StringIO())
    assert fake.events == []


def test_silence_descriptor_redirects_a_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)

    _silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_writelines_stops_consuming_after_closure() -> None:
    consumed: list[int] = []

    def lines() -> Iterator[str]:
        for index in range(5):
            consumed.append(index)
            yield f'{index}\n'

    output = _ClosableTextOutput(_FailingStream(BrokenPipeError()))
    output.writelines(lines())
    assert consumed == [0]

    consumed.clear()
    output.writelines(lines())
    assert consumed == []
