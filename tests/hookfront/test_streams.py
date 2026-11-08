import errno
import io
import os
import sys
from collections.abc import Iterator
from typing import TextIO, cast

import pytest

from defsort._vendor.hookfront import (
    _streams,
)
from defsort._vendor.hookfront._streams import (
    _ClosableTextOutput,
    _is_downstream_closure,
    _silence_descriptor,
    closable_stdout,
    ensure_utf8_stdio,
    stdout_closed,
    write_stdout_bytes,
)


class TestEnsureUtf8Stdio:
    @pytest.mark.parametrize('platform', ['linux', 'win32'])
    @pytest.mark.parametrize('stdout_errors', ['strict', 'surrogateescape'])
    def test_ensure_utf8_stdio_sets_utf8_and_escapes_stdout(
        self, platform: str, stdout_errors: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogateescape')
        stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors=stdout_errors)
        stderr = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='surrogatepass')
        monkeypatch.setattr('sys.platform', platform)
        monkeypatch.setattr('sys.stdin', stdin)
        monkeypatch.setattr('sys.stdout', stdout)
        monkeypatch.setattr('sys.stderr', stderr)

        ensure_utf8_stdio()

        assert (stdin.encoding, stdin.errors) == ('utf-8', 'surrogateescape')
        assert (stdout.encoding, stdout.errors) == ('utf-8', 'backslashreplace')
        assert (stderr.encoding, stderr.errors) == ('utf-8', 'surrogatepass')

    def test_string_streams_are_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr('sys.platform', 'win32')
        streams = [io.StringIO() for _ in range(3)]
        monkeypatch.setattr('sys.stdin', streams[0])
        monkeypatch.setattr('sys.stdout', streams[1])
        monkeypatch.setattr('sys.stderr', streams[2])
        ensure_utf8_stdio()
        assert (sys.stdin, sys.stdout, sys.stderr) == tuple(streams)

    def test_undecodable_name_prints_escaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict', newline='\n')
        monkeypatch.setattr(sys, 'stdout', stdout)
        ensure_utf8_stdio()

        print('a\udcffb.py')
        stdout.flush()
        assert stdout.buffer.getvalue() == b'a\\udcffb.py\n'


class _ByteBuffer:
    def __init__(self, events: list[str], failure: str | None) -> None:
        self._events = events
        self._failure = failure

    def flush(self) -> None:
        self._events.append('byte_flush')
        if self._failure == 'byte_flush':
            raise BrokenPipeError

    def write(self, _data: bytes) -> int | None:
        self._events.append('byte_write')
        if self._failure == 'byte_write':
            raise BrokenPipeError
        return len(_data)


class _ByteStream:
    def __init__(self, failure: str | None) -> None:
        self.events: list[str] = []
        self._failure = failure
        self.buffer = _ByteBuffer(self.events, failure)

    def flush(self) -> None:
        self.events.append('text_flush')
        if self._failure == 'text_flush':
            raise BrokenPipeError

    def write(self, text: str) -> int:
        self.events.append('text_write')
        return len(text)


class _PartialBuffer(_ByteBuffer):
    def __init__(self, events: list[str], outcomes: list[int | OSError | None]) -> None:
        super().__init__(events, None)
        self._outcomes = iter(outcomes)
        self.data = bytearray()

    def write(self, data: bytes) -> int | None:
        self._events.append('byte_write')
        outcome = next(self._outcomes)
        if isinstance(outcome, OSError):
            raise outcome
        if outcome is not None:
            self.data.extend(data[:outcome])
        return outcome


class _FailingStream(io.StringIO):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self._error = error
        self.attempts = 0

    def flush(self) -> None:
        raise self._error

    def write(self, _text: str) -> int:
        self.attempts += 1
        raise self._error


@pytest.mark.parametrize('platform', ['posix', 'windows'])
def test_broken_pipe_is_closure_everywhere(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_streams, '_WINDOWS', platform == 'windows')
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

    monkeypatch.setattr(_streams, '_silence_descriptor', record)
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

    monkeypatch.setattr(_streams, '_silence_descriptor', record)
    monkeypatch.setattr(sys, 'stdout', _FailingStream(BrokenPipeError()))
    original = sys.stdout

    with closable_stdout():
        print('discarded')
        assert stdout_closed()
    assert silenced == [original]


def test_einval_is_closure_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(errno.EINVAL, 'Invalid argument')
    monkeypatch.setattr(_streams, '_WINDOWS', True)
    assert _is_downstream_closure(error)
    monkeypatch.setattr(_streams, '_WINDOWS', False)
    assert not _is_downstream_closure(error)


def test_flush_latches_windows_einval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_streams, '_WINDOWS', True)
    stream = _FailingStream(OSError(errno.EINVAL, 'closed'))
    output = _ClosableTextOutput(stream)

    output.flush()
    output.flush()
    assert output.write('discarded') == len('discarded')
    assert stream.attempts == 0


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
    monkeypatch.setattr(_streams, '_WINDOWS', True)
    assert not _is_downstream_closure(OSError(errno.EBADF, 'bad'))


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_write_bytes_flushes_text_first(newline: str) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='utf-8', newline=newline)
    output = _ClosableTextOutput(stream)

    output.write('a\n')
    assert output.write_bytes(b'b\r\n')
    output.write('c\n')
    output.flush()

    assert buffer.getvalue() == b'a' + newline.encode() + b'b\r\nc' + newline.encode()


def test_write_bytes_propagates_unrelated_errors() -> None:
    class _EioBuffer(_ByteBuffer):
        def write(self, _data: bytes) -> int:
            raise OSError(errno.EIO, 'injected I/O failure')

    stream = _ByteStream(None)
    stream.buffer = _EioBuffer(stream.events, None)
    output = _ClosableTextOutput(cast('TextIO', stream))

    with pytest.raises(OSError, match='injected I/O failure'):
        output.write_bytes(b'data')
    with pytest.raises(OSError, match='injected I/O failure'):
        output.write_bytes(b'again')


def test_write_bytes_without_binary_interface_returns_false() -> None:
    stream = io.StringIO()
    output = _ClosableTextOutput(stream)

    assert not output.write_bytes(b'bytes')
    assert output.write('text') == 4
    assert stream.getvalue() == 'text'


@pytest.mark.parametrize(
    ('error', 'platform'),
    [(BrokenPipeError(), 'posix'), (OSError(errno.EINVAL, 'closed'), 'windows')],
)
def test_write_latches_closure_and_discards_later_text(
    error: OSError, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_streams, '_WINDOWS', platform == 'windows')
    stream = _FailingStream(error)
    output = _ClosableTextOutput(stream)

    assert output.write('first') == len('first')
    assert output.write('second') == len('second')
    assert stream.attempts == 1


@pytest.mark.parametrize(
    ('error', 'platform'),
    [(OSError(errno.EINVAL, 'closed'), 'posix'), (OSError(errno.EBADF, 'bad'), 'windows')],
)
def test_write_propagates_errors_that_are_not_closure(
    error: OSError, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_streams, '_WINDOWS', platform == 'windows')
    output = _ClosableTextOutput(_FailingStream(error))

    with pytest.raises(OSError, match=r'closed|bad') as raised:
        output.write('text')
    assert raised.value is error
    with pytest.raises(OSError, match=r'closed|bad'):
        output.write('again')


class _FakeOs:
    O_WRONLY = os.O_WRONLY
    devnull = os.devnull

    def __init__(self, *, redirect_error: OSError | None) -> None:
        self.events: list[tuple[str, int]] = []
        self._redirect_error = redirect_error

    def close(self, fd: int) -> None:
        self.events.append(('close', fd))

    def dup2(self, source: int, _target: int) -> None:
        self.events.append(('dup2', source))
        if self._redirect_error is not None:
            raise self._redirect_error

    def open(self, _path: str, _flags: int) -> int:
        return 37


class _DescriptorStream(io.StringIO):
    def fileno(self) -> int:
        return 1


@pytest.mark.parametrize('point', ['text_flush', 'byte_write', 'byte_flush'])
def test_closure_latches_during_point(point: str) -> None:
    stream = _ByteStream(point)
    output = _ClosableTextOutput(cast('TextIO', stream))

    assert output.write_bytes(b'first')
    events = stream.events.copy()
    assert output.write('discarded') == len('discarded')
    assert output.write_bytes(b'discarded')
    assert stream.events == events


def test_silence_descriptor_closes_devnull_and_propagates_redirect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=OSError('injected redirect failure'))
    monkeypatch.setattr(_streams, 'os', fake)

    with pytest.raises(OSError, match='injected redirect failure'):
        _silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_silence_descriptor_leaves_streams_without_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_streams, 'os', fake)
    _silence_descriptor(io.StringIO())
    assert fake.events == []


def test_silence_descriptor_redirects_a_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_streams, 'os', fake)

    _silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_write_stdout_bytes_on_a_plain_stream(
    newline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='utf-8', newline=newline)
    monkeypatch.setattr(sys, 'stdout', stream)

    print('text')
    assert write_stdout_bytes(b'bytes\r\n')
    assert buffer.getvalue() == b'text' + newline.encode() + b'bytes\r\n'

    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    assert not write_stdout_bytes(b'bytes')


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_write_stdout_bytes_uses_installed_wrapper(
    newline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='utf-8', newline=newline)
    monkeypatch.setattr(sys, 'stdout', stream)

    with closable_stdout():
        print('text')
        assert write_stdout_bytes(b'bytes\r\n')

    assert buffer.getvalue() == b'text' + newline.encode() + b'bytes\r\n'


@pytest.mark.parametrize('wrapped', [False, True])
def test_byte_output_completes_short_writes(
    *, wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _ByteStream(None)
    buffer = _PartialBuffer(stream.events, [3, 3, 3, 1])
    stream.buffer = buffer
    output = cast('TextIO', stream)
    if wrapped:
        output = cast('TextIO', _ClosableTextOutput(output))
    monkeypatch.setattr(sys, 'stdout', output)

    assert write_stdout_bytes(b'abcdefghij')
    assert buffer.data == b'abcdefghij'
    assert stream.events == ['text_flush', *['byte_write'] * 4, 'byte_flush']


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('progress', [0, None])
def test_byte_output_rejects_no_progress(
    progress: int | None, *, wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _ByteStream(None)
    buffer = _PartialBuffer(stream.events, [3, progress, 2])
    stream.buffer = buffer
    output = cast('TextIO', stream)
    if wrapped:
        output = cast('TextIO', _ClosableTextOutput(output))
    monkeypatch.setattr(sys, 'stdout', output)

    with pytest.raises(BlockingIOError, match='no progress'):
        write_stdout_bytes(b'abcdefghij')
    assert buffer.data == b'abc'
    assert stream.events == ['text_flush', 'byte_write', 'byte_write']
    assert not stdout_closed()
    assert write_stdout_bytes(b'xy')
    assert buffer.data == b'abcxy'


@pytest.mark.parametrize('wrapped', [False, True])
def test_byte_output_propagates_error_after_short_write(
    *, wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OSError(errno.EIO, 'injected I/O failure')
    stream = _ByteStream(None)
    buffer = _PartialBuffer(stream.events, [3, error, 2])
    stream.buffer = buffer
    output = cast('TextIO', stream)
    if wrapped:
        output = cast('TextIO', _ClosableTextOutput(output))
    monkeypatch.setattr(sys, 'stdout', output)

    with pytest.raises(OSError, match='injected I/O failure') as raised:
        write_stdout_bytes(b'abcdefghij')
    assert raised.value is error
    assert buffer.data == b'abc'
    assert stream.events == ['text_flush', 'byte_write', 'byte_write']
    assert not stdout_closed()
    assert write_stdout_bytes(b'xy')
    assert buffer.data == b'abcxy'


@pytest.mark.parametrize(
    ('error', 'platform'),
    [(BrokenPipeError(), 'posix'), (OSError(errno.EINVAL, 'closed'), 'windows')],
)
def test_byte_output_latches_closure_after_short_write(
    error: OSError, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_streams, '_WINDOWS', platform == 'windows')
    stream = _ByteStream(None)
    buffer = _PartialBuffer(stream.events, [3, error])
    stream.buffer = buffer
    output = _ClosableTextOutput(cast('TextIO', stream))
    monkeypatch.setattr(sys, 'stdout', output)

    assert write_stdout_bytes(b'abcdefghij')
    assert stdout_closed()
    assert output.write('discarded') == len('discarded')
    assert write_stdout_bytes(b'discarded')
    output.flush()
    assert buffer.data == b'abc'
    assert stream.events == ['text_flush', 'byte_write', 'byte_write']


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
