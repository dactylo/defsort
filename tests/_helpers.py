import ast
import atexit
import contextlib
import errno
import functools
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Buffer, Mapping, Sequence
from pathlib import Path
from types import FunctionType
from typing import Literal, Protocol
from unittest import mock

import pytest

from _build import _plugin_entry_points
from defsort._cli import _main as main
from defsort._config import clear_config_cache
from defsort._types import BannerMode, Blocker, SortScopeOrder

_RENAME_NONCONFORMING_SOURCE = 'def bravo():\n    return alpha()\n\n\ndef alpha():\n    pass\n'
_RENAME_ORDERED_SOURCE = 'def alpha():\n    pass\n\n\ndef bravo():\n    return alpha()\n'
_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_ENTRY_POINTS = _plugin_entry_points(_ROOT)['defsort.hook_adapters']
# Hang guard, not a performance assertion. Allow for scheduling on shared CI hosts.
_CHILD_TIMEOUT_SECONDS = 300
CLI = [sys.executable, '-m', 'defsort._cli']
_COLLECT_AFTER_KILL_SECONDS = 10
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')
# Finite diagnostic guard includes interpreter startup and scheduling under parallel CI.
DETECTOR_REGRESSION_TIMEOUT_SECONDS = _CHILD_TIMEOUT_SECONDS
DYNAMIC_ALL_BASELINE = 'def zeta(): pass\n\n__all__ = ["zeta"]\n__all__ += ["alpha"]\n'
DYNAMIC_ALL_SOURCE = DYNAMIC_ALL_BASELINE.replace('__all__ =', 'def alpha(): pass\n\n__all__ =')
DYNAMIC_ALL_WARNING = (
    'sort would reorder definitions that a dynamically built __all__ may depend on'
)
PROCESS_GROUP_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
_TRANSCRIPT_FIELD_CHARACTERS = 2048
_TRANSCRIPT_HEAD_BYTES = 2048
_TRANSCRIPT_TAIL_BYTES = 6144
_ATTRIBUTES_DIRECTORY = tempfile.TemporaryDirectory(prefix='defsort-attributes-')
_GIT_ATTRIBUTE_ISOLATION = {
    'GIT_ATTR_NOSYSTEM': '1',
    'XDG_CONFIG_HOME': _ATTRIBUTES_DIRECTORY.name,
}
_HOSTS_DIRECTORY = tempfile.TemporaryDirectory(prefix='defsort-hosts-')
HOSTS = Path(_HOSTS_DIRECTORY.name)
ENV = {
    **os.environ,
    'PYTHONPATH': os.pathsep.join([str(_ROOT), str(_ROOT / 'src'), str(HOSTS)]),
}


def _describe_status(returncode: int | None) -> str:
    if returncode is None:
        return 'not collected'
    if returncode < 0:
        try:
            return f'{returncode} ({signal.Signals(-returncode).name})'
        except ValueError:
            return str(returncode)
    return str(returncode)


def _escape_transcript_line(line: str) -> str:
    line = _CONTROL_CHARACTERS.sub(lambda match: f'\\x{ord(match.group()):02x}', line)
    line = line.replace('##[', '#\\#[')
    body = line.lstrip()
    if body.startswith('::'):
        line = line[: len(line) - len(body)] + '\\' + body
    return line


def _exit_status(error: SystemExit) -> int:
    code = error.code
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def _terminate_tree[T: (str, bytes)](process: subprocess.Popen[T]) -> None:
    if sys.platform == 'win32':
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(process.pid)],
                capture_output=True,
                timeout=_COLLECT_AFTER_KILL_SECONDS,
                check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _transcript_field(label: str, value: object) -> str:
    field = repr(value)
    if len(field) > _TRANSCRIPT_FIELD_CHARACTERS:
        omitted = len(field) - _TRANSCRIPT_FIELD_CHARACTERS
        field = f'{field[:_TRANSCRIPT_FIELD_CHARACTERS]} [... {omitted} characters omitted ...]'
    return f'{label}: {_escape_transcript_line(field)}'


def _transcript_stream(label: str, data: str | bytes | None) -> list[str]:
    if data is None:
        return [f'{label}: <not collected>']
    raw = data if isinstance(data, bytes) else data.encode('utf-8', 'backslashreplace')
    if not raw:
        return [f'{label}: <empty>']
    if len(raw) > _TRANSCRIPT_HEAD_BYTES + _TRANSCRIPT_TAIL_BYTES:
        omitted = len(raw) - _TRANSCRIPT_HEAD_BYTES - _TRANSCRIPT_TAIL_BYTES
        field = (
            raw[:_TRANSCRIPT_HEAD_BYTES].decode('utf-8', 'backslashreplace')
            + f'\n[... {omitted} bytes omitted ...]\n'
            + raw[-_TRANSCRIPT_TAIL_BYTES:].decode('utf-8', 'backslashreplace')
        )
    else:
        field = raw.decode('utf-8', 'backslashreplace')
    body = field.replace('\r\n', '\n').removesuffix('\n')
    return [f'{label}:', *(f'  | {_escape_transcript_line(line)}' for line in body.split('\n'))]


def assert_renamed_result(result: str, source: str, case: str, diagnostics: str) -> None:
    if case == 'conforming':
        assert result.index('def helper') < result.index('def alpha') < result.index('def bravo')
    else:
        assert result == source
        expected = (
            'baseline not in target order'
            if case == 'nonconforming'
            else 'threshold: max-reorder-count = 0'
        )
        assert 'skipped' in diagnostics
        assert expected in diagnostics


def decode_text(data: bytes) -> str:
    return data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


@functools.cache
def deep_expression_terms() -> int:
    # The deepest `a + a + ...` chain this interpreter parses here, less a margin for the
    # deeper stacks of the code under test, capped at 3,000. Python 3.13 fixes the limit when
    # it is built (about 1,000 levels on Windows ARM64); 3.14 and later use the real stack.
    def parses(terms: int) -> bool:
        try:
            ast.parse(' + '.join(['a'] * terms))
        except (RecursionError, MemoryError):
            return False
        return True

    if parses(3000 + 100):
        return 3000
    low, high = 1, 3000 + 100 - 1
    while low < high:
        middle = (low + high + 1) // 2
        if parses(middle):
            low = middle
        else:
            high = middle - 1
    return low - 100


class _Outcome(Protocol):
    @property
    def args(self) -> object: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> str | bytes | None: ...

    @property
    def stderr(self) -> str | bytes | None: ...


def execute_source(source: str) -> dict[str, object]:
    namespace: dict[str, object] = {}
    FunctionType(compile(source, '<test>', 'exec'), namespace)()
    return namespace


def fail_writes_to(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, restore_fails: bool = False
) -> list[bytes]:
    # The first write to the target's file stops halfway with ENOSPC. Later writes, which
    # restore the original, fail with EIO when restore_fails is set. Returns every attempt.
    inode = target.stat().st_ino
    real_write = os.write
    attempts: list[bytes] = []

    def write(fd: int, data: Buffer, /) -> int:
        if os.fstat(fd).st_ino != inode:
            return real_write(fd, data)
        chunk = bytes(data)
        attempts.append(chunk)
        if len(attempts) == 1:
            real_write(fd, chunk[: len(chunk) // 2])
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        if restore_fails:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_write(fd, chunk)

    monkeypatch.setattr(os, 'write', write)
    return attempts


def incremental_gate_case(
    case: str,
) -> tuple[str, str, BannerMode, SortScopeOrder, Blocker | None]:
    baseline = 'class C:\n    def z(self): pass\n\nregistry = {}\n\ndef zeta(): pass\n'
    banners = 'strip' if case == 'method_banner' else 'barriers'
    definition_order = 'none' if case == 'none' else 'dependency'
    blocker: Blocker | None = 'interstitial'
    if case.startswith('duplicates'):
        baseline = 'class C:\n    def z(self): pass\n\ndef zeta(): pass\n'
        blocker = 'duplicates'
    elif case.startswith('sectioned'):
        baseline = (
            'class C:\n    def z(self): pass\n\ndef zeta(): pass\n'
            '\n# --------\n# Section\n# --------\n\ndef zeta(): pass\n'
        )
        blocker = 'duplicates'
    baseline = {
        'method_banner': baseline.replace(
            '    def z',
            '    # --------\n    # Methods\n    # --------\n    def z',
        ),
        'future': 'from __future__ import annotations\n' + baseline,
        'non_conforming': 'def zeta(): pass\n\nregistry = {}\n\ndef alpha(): pass\n',
    }.get(case, baseline)
    if case == 'non_conforming':
        blocker = 'non_conforming'
    if case == 'duplicates_method':
        baseline += '\ndef alpha(): pass\n\ndef zeta(): pass\n'
    source = baseline
    if case in {'method', 'duplicates_method', 'sectioned_method', 'none'}:
        source = source.replace(
            '    def z(self): pass\n', '    def z(self): pass\n    def a(self): pass\n'
        )
    elif case == 'duplicates_top_level':
        source += '\ndef alpha(): pass\n\ndef zeta(): pass\n\ndef bravo(): pass\n'
    elif case in {'top_level', 'sectioned_top_level', 'non_conforming'}:
        source += '\ndef bravo(): pass\n'
    elif case == 'already_placed':
        source += '\ndef zzz(): pass\n'
    blocker = None if case in {'unchanged', 'already_placed', 'none'} else blocker
    return baseline, source, banners, definition_order, blocker


def invoke_cli(
    argv: Sequence[str],
    *,
    stdin: str = '',
    cwd: Path | str | None = None,
) -> tuple[int, str, str]:
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_buffer, encoding='utf-8', newline='')
    stderr = io.TextIOWrapper(
        stderr_buffer, encoding='utf-8', errors='backslashreplace', newline=''
    )
    stdin_stream = io.TextIOWrapper(io.BytesIO(stdin.encode('utf-8')), encoding='utf-8')
    clear_config_cache()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {'COLUMNS': '80'}))
        stack.enter_context(contextlib.redirect_stdout(stdout))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        stack.enter_context(mock.patch.object(sys, 'stdin', stdin_stream))
        if cwd is not None:
            stack.enter_context(contextlib.chdir(cwd))
        try:
            status = main(argv)
        except SystemExit as error:
            status = _exit_status(error)
        stdout.flush()
        stderr.flush()
    return (
        status,
        stdout_buffer.getvalue().decode('utf-8'),
        stderr_buffer.getvalue().decode('utf-8'),
    )


def isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Subprocess helpers capture ENV during collection, before fixtures run.
    for environment in (os.environ, ENV):
        monkeypatch.setitem(environment, 'GIT_CONFIG_GLOBAL', os.devnull)
        monkeypatch.setitem(environment, 'GIT_CONFIG_NOSYSTEM', '1')
        for key, value in _GIT_ATTRIBUTE_ISOLATION.items():
            monkeypatch.setitem(environment, key, value)
        for key in ('GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
            monkeypatch.delitem(environment, key, raising=False)


def process_transcript(
    result: _Outcome,
    *,
    cwd: Path | str | None = None,
    note: str | None = None,
) -> str:
    lines = [_transcript_field('command', result.args)]
    if cwd is not None:
        lines.append(_transcript_field('cwd', str(cwd)))
    if note is not None:
        lines.append(f'note: {_escape_transcript_line(note)}')
    lines.append(f'status: {_describe_status(result.returncode)}')
    lines.extend(_transcript_stream('stdout', result.stdout))
    lines.extend(_transcript_stream('stderr', result.stderr))
    return '\n'.join(lines)


def _kill_and_describe[T: (str, bytes)](
    process: subprocess.Popen[T],
    *,
    cwd: Path | str | None,
    reason: str,
) -> str:
    _terminate_tree(process)
    try:
        stdout, stderr = process.communicate(timeout=_COLLECT_AFTER_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        stdout = stderr = None
        reason = f'{reason}; a descendant outside the process group still holds the output pipes'
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_COLLECT_AFTER_KILL_SECONDS)
    killed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    return process_transcript(killed, cwd=cwd, note=reason)


def finish_process[T: (str, bytes)](
    process: subprocess.Popen[T],
    *,
    stdin: T | None = None,
    timeout: float = _CHILD_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[T]:
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            _kill_and_describe(process, cwd=cwd, reason=f'timed out after {timeout:g} s')
        ) from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def json_stdout(result: subprocess.CompletedProcess[str]) -> object:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f'stdout is not JSON\n{process_transcript(result)}') from None


def redirect_recovery(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    # Moves the per-user location under home and returns defsort's recovery directory there.
    home.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        monkeypatch.setenv('LOCALAPPDATA', str(home))
        return home / 'defsort' / 'recovery'
    if sys.platform == 'darwin':
        monkeypatch.setenv('HOME', str(home))
        return home / 'Library' / 'Application Support' / 'defsort' / 'recovery'
    monkeypatch.setenv('XDG_STATE_HOME', str(home))
    return home / 'defsort' / 'recovery'


def register_distribution(root: Path, name: str, entry_points: Mapping[str, str]) -> None:
    dist = root / f'{name.replace("-", "_")}-0.0.0.dist-info'
    dist.mkdir()
    (dist / 'METADATA').write_text(
        f'Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.0\n'
        'Project-URL: Documentation, https://example.invalid/docs\n',
        encoding='utf-8',
    )
    lines = [
        '[defsort.hook_adapters]',
        *(f'{host} = {target}' for host, target in entry_points.items()),
    ]
    (dist / 'entry_points.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_cli(  # noqa: PLR0913
    argv: Sequence[str],
    *,
    expect: int | None = None,
    stdin: str = '',
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = _CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [sys.executable, '-m', 'defsort', *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        env=dict(ENV if env is None else env),
        cwd=cwd,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    result = finish_process(process, stdin=stdin, timeout=timeout, cwd=cwd)
    if expect is not None and result.returncode != expect:
        raise AssertionError(f'expected status {expect}\n{process_transcript(result, cwd=cwd)}')
    return result


def run_closing_after_first_line(
    argv: Sequence[str],
    *,
    timeout: float = _CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        [sys.executable, '-m', 'defsort', *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ENV,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    stdout = process.stdout
    assert stdout is not None
    descriptor = stdout.fileno()

    def read_first_line() -> None:
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk or b'\n' in chunk:
                return

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            _kill_and_describe(process, cwd=None, reason=f'no stdout line within {timeout:g} s')
        )
    stdout.close()
    process.stdout = None  # communicate() must not read the closed pipe on Windows
    return finish_process(process, timeout=timeout)


def run_setup(  # noqa: PLR0913
    command: Sequence[str],
    *,
    check: bool = True,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: float = _CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        cwd=cwd,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    result = finish_process(process, stdin=stdin, timeout=timeout, cwd=cwd)
    if check and result.returncode != 0:
        raise AssertionError(f'setup command failed\n{process_transcript(result, cwd=cwd)}')
    return result


def init_empty_git_repo(path: Path) -> None:
    run_setup(['git', 'init', str(path)])
    run_setup(['git', '-C', str(path), 'config', 'user.email', 'test@test.com'])
    run_setup(['git', '-C', str(path), 'config', 'user.name', 'Test'])


def init_git_repo(  # pyright: ignore[reportUnusedFunction]
    path: Path, source: str
) -> None:
    init_empty_git_repo(path)
    f = path / 'test.py'
    f.write_bytes(b'# placeholder\n')
    run_setup(['git', '-C', str(path), 'add', '.'])
    run_setup(['git', '-C', str(path), 'commit', '-m', 'init'])
    f.write_bytes(source.encode('utf-8'))


def install_stand_in_git(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    version: str | None = None,
    fail: str | None = None,
) -> None:
    original_path = os.environ['PATH']
    real_git = shutil.which('git', path=original_path)
    assert real_git is not None
    real_git = str(Path(real_git).absolute())
    bin_dir = root / 'bin'
    bin_dir.mkdir()
    script = bin_dir / 'git'
    script.write_text(
        '#!/bin/sh\n'
        + (
            f'if [ "$1" = "--version" ]; then printf "%s\\n" '
            f'{shlex.quote("git version " + version)}; exit 0; fi\n'
            if version is not None
            else ''
        )
        + (
            'if [ "$1" = "cat-file" ]; then\n'
            '  for arg do\n'
            '    if [ "$arg" = "-Z" ]; then exit 129; fi\n'
            '  done\n'
            'fi\n'
            if version is not None
            else ''
        )
        + (
            'for arg do\n'
            f'  if [ "$arg" = {shlex.quote(fail)} ]; then\n'
            '    printf "%s\\n" "fatal: simulated failure" >&2\n'
            '    exit 128\n'
            '  fi\n'
            'done\n'
            if fail is not None
            else ''
        )
        + f'PATH={shlex.quote(original_path)} exec {shlex.quote(real_git)} "$@"\n',
        encoding='utf-8',
    )
    script.chmod(0o755)
    monkeypatch.setenv('PATH', os.pathsep.join([str(bin_dir), original_path]))


def prepare_renamed_file(repo: Path, case: str) -> tuple[Path, str]:
    init_empty_git_repo(repo)
    baseline = _RENAME_ORDERED_SOURCE if case == 'conforming' else _RENAME_NONCONFORMING_SOURCE
    (repo / 'old.py').write_text(baseline, encoding='utf-8')
    run_setup(['git', 'add', 'old.py'], cwd=repo)
    run_setup(['git', 'commit', '-m', 'baseline'], cwd=repo)
    run_setup(['git', 'mv', 'old.py', 'new.py'], cwd=repo)
    source = (
        baseline.replace('    pass', '    return helper()') if case == 'conforming' else baseline
    )
    source += '\n\ndef helper():\n    pass\n'
    target = repo / 'new.py'
    target.write_text(source, encoding='utf-8')
    run_setup(['git', 'add', 'new.py'], cwd=repo)
    paired = run_setup(['git', 'diff', '--name-status', '-M', 'HEAD'], cwd=repo).stdout
    assert paired.startswith(b'R')
    assert paired.endswith(b'old.py\tnew.py\n')
    if case == 'guard':
        (repo / 'pyproject.toml').write_text(
            '[tool.defsort]\nmax-reorder-count = 0\n', encoding='utf-8'
        )
    return target, source


def run_with_closed_output(
    argv: Sequence[str],
    *,
    closed: Literal['stdout', 'stderr'] = 'stdout',
    stdin: bytes | None = None,
    timeout: float = _CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # every write to the closed stream happens after its reader closed
    try:
        process = subprocess.Popen(
            [sys.executable, '-m', 'defsort', *argv],
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            stdout=write_fd if closed == 'stdout' else subprocess.PIPE,
            stderr=write_fd if closed == 'stderr' else subprocess.PIPE,
            env=ENV,
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
    finally:
        os.close(write_fd)
    return finish_process(process, stdin=stdin, timeout=timeout)


def shell_path(path: Path) -> str:
    return shlex.quote(path.as_posix())


register_distribution(HOSTS, 'defsort', BUILTIN_ENTRY_POINTS)
atexit.register(_HOSTS_DIRECTORY.cleanup)
