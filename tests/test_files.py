import io
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Self

import pytest

import defsort._files as files_mod
from defsort._files import (
    _ReadResult,
    collect_files,
    encode_source,
    ensure_utf8_stdio,
    read_file,
    read_file_result,
    write_source,
)
from tests._helpers import _ENV, process_transcript, run_setup


class TestCollectFiles:
    def test_dedup_by_resolved_path(self, tmp_path: Path) -> None:
        f = tmp_path / 'a.py'
        f.write_text('x = 1\n', encoding='utf-8')
        result = collect_files([str(f), str(f)])
        assert len(result) == 1

    def test_directory_excludes_git_ignored_nested_worktree(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        nested = tmp_path / 'nested'
        nested.mkdir()
        run_setup(['git', 'init', '-q'], cwd=nested)
        inside = nested / 'inside.py'
        inside.write_text('x = 1\n', encoding='utf-8')
        (tmp_path / '.gitignore').write_text('nested/\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == []

    def test_directory_excludes_git_ignored_python(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        kept = tmp_path / 'kept.py'
        kept.write_text('x = 1\n', encoding='utf-8')
        ignored = tmp_path / 'ignored.py'
        ignored.write_text('x = 2\n', encoding='utf-8')
        (tmp_path / '.gitignore').write_text('ignored.py\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == [kept]

    def test_directory_expansion(self, tmp_path: Path) -> None:
        d = tmp_path / 'pkg'
        d.mkdir()
        (d / 'a.py').write_text('x = 1\n', encoding='utf-8')
        (d / 'b.py').write_text('x = 1\n', encoding='utf-8')
        result = collect_files([str(d)])
        assert len(result) == 2

    @pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows CreateProcess lookup')
    def test_directory_ignores_inert_git_exe_in_parent_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trusted_git = shutil.which('git')
        assert trusted_git is not None
        trusted_git = str(Path(trusted_git).absolute())
        run_setup([trusted_git, 'init', '-q'], cwd=tmp_path)
        kept = tmp_path / 'kept.py'
        kept.write_text('x = 1\n', encoding='utf-8')
        (tmp_path / 'ignored.py').write_text('x = 2\n', encoding='utf-8')
        (tmp_path / '.gitignore').write_text('ignored.py\n', encoding='utf-8')
        # Finish all Git setup before introducing an invalid, inert executable.
        decoy = tmp_path / 'git.exe'
        decoy.write_bytes(b'not an executable\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('PATH', str(Path(trusted_git).parent))
        monkeypatch.delenv('NoDefaultCurrentDirectoryInExePath', raising=False)

        assert collect_files([str(tmp_path)]) == [kept]
        assert decoy.read_bytes() == b'not an executable\n'

    def test_directory_keeps_tracked_ignored_python(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        source = tmp_path / 'tracked.py'
        source.write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'tracked.py'], cwd=tmp_path)
        (tmp_path / '.gitignore').write_text('tracked.py\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == [source]

    def test_directory_keeps_untracked_nonignored_python(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        source = tmp_path / 'untracked.py'
        source.write_text('x = 1\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == [source]

    def test_directory_recurses_into_nested_git_worktree(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        top = tmp_path / 'top.py'
        top.write_text('x = 1\n', encoding='utf-8')
        nested = tmp_path / 'nested'
        nested.mkdir()
        run_setup(['git', 'init', '-q'], cwd=nested)
        inside = nested / 'inside.py'
        inside.write_text('x = 2\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == [inside, top]

    def test_empty_result(self, tmp_path: Path) -> None:
        d = tmp_path / 'empty'
        d.mkdir()
        result = collect_files([str(d)])
        assert result == []

    def test_explicit_git_ignored_python_is_included(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        ignored = tmp_path / 'ignored.py'
        ignored.write_text('x = 1\n', encoding='utf-8')
        (tmp_path / '.gitignore').write_text('ignored.py\n', encoding='utf-8')

        assert collect_files([str(ignored)]) == [ignored]

    @pytest.mark.parametrize('failure', ['error-status', 'oserror'])
    def test_git_failure_falls_back_to_filesystem(
        self,
        failure: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = tmp_path / 'source.py'
        source.write_text('x = 1\n', encoding='utf-8')
        selected = files_mod.resolve_git_executable(cwd=tmp_path)

        def fail_git(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            assert kwargs['executable'] == selected
            if failure == 'oserror':
                raise OSError
            return subprocess.CompletedProcess([], 128, b'source.py\0', b'fatal error')

        monkeypatch.setattr(files_mod.subprocess, 'run', fail_git)

        assert collect_files([str(tmp_path)]) == [source]

    def test_git_resolution_failure_falls_back_to_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / 'source.py'
        source.write_text('x = 1\n', encoding='utf-8')

        def missing_git(*_args: object, **_kwargs: object) -> str:
            raise FileNotFoundError('git unavailable')

        monkeypatch.setattr(files_mod, 'resolve_git_executable', missing_git)

        assert collect_files([str(tmp_path)]) == [source]

    def test_missing_file_included(self, tmp_path: Path) -> None:
        missing = tmp_path / 'gone.py'
        result = collect_files([str(missing)])
        assert result == [missing]

    def test_nested_git_worktree_uses_own_tracked_state(self, tmp_path: Path) -> None:
        run_setup(['git', 'init', '-q'], cwd=tmp_path)
        nested = tmp_path / 'nested'
        nested.mkdir()
        run_setup(['git', 'init', '-q'], cwd=nested)
        tracked = nested / 'tracked.py'
        tracked.write_text('x = 1\n', encoding='utf-8')
        run_setup(['git', 'add', 'tracked.py'], cwd=nested)
        ignored = nested / 'ignored.py'
        ignored.write_text('x = 2\n', encoding='utf-8')
        (nested / '.gitignore').write_text('*.py\n', encoding='utf-8')

        assert collect_files([str(tmp_path)]) == [tracked]

    def test_non_py_existing_silently_dropped(self, tmp_path: Path) -> None:
        f = tmp_path / 'readme.txt'
        f.write_text('hi', encoding='utf-8')
        result = collect_files([str(f)])
        assert result == []

    def test_non_py_missing_included(self, tmp_path: Path) -> None:
        f = tmp_path / 'gone.txt'
        result = collect_files([str(f)])
        assert result == [f]

    def test_single_py_file(self, tmp_path: Path) -> None:
        f = tmp_path / 'a.py'
        f.write_text('x = 1\n', encoding='utf-8')
        result = collect_files([str(f)])
        assert result == [f]

    def test_skip_dirs(self, tmp_path: Path) -> None:
        d = tmp_path / 'proj'
        d.mkdir()
        (d / 'good.py').write_text('x = 1\n', encoding='utf-8')
        cache = d / '__pycache__'
        cache.mkdir()
        (cache / 'bad.py').write_text('x = 1\n', encoding='utf-8')
        venv = d / '.venv'
        venv.mkdir()
        (venv / 'bad.py').write_text('x = 1\n', encoding='utf-8')
        result = collect_files([str(d)])
        assert len(result) == 1
        assert result[0].name == 'good.py'

    def test_skip_dirs_are_pruned_before_file_inspection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / 'proj'
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

        assert collect_files([str(project)]) == [good]
        assert all('.venv' not in path.parts for path in inspected)

    def test_symlink_explicit_processed(self, tmp_path: Path) -> None:
        target = tmp_path / 'target.py'
        target.write_text('x = 1\n', encoding='utf-8')
        link = tmp_path / 'link.py'
        link.symlink_to(target)
        result = collect_files([str(link)])
        assert result == [link]

    def test_symlink_in_directory_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / 'pkg'
        d.mkdir()
        real = d / 'real.py'
        real.write_text('x = 1\n', encoding='utf-8')
        link = d / 'link.py'
        link.symlink_to(real)
        result = collect_files([str(d)])
        assert len(result) == 1
        assert result[0].name == 'real.py'


class TestEncodeSource:
    def test_latin1(self) -> None:
        result = encode_source('\xe9\r\n', 'latin-1')
        assert result == b'\xe9\r\n'

    def test_mixed_endings_utf8(self) -> None:
        result = encode_source('a\r\nb\nc\rd', 'utf-8')
        assert result == b'a\r\nb\nc\rd'

    def test_utf8_bom(self) -> None:
        result = encode_source('x = 1\r', 'utf-8-sig')
        assert result == b'\xef\xbb\xbfx = 1\r'


class TestEnsureUtf8Stdio:
    def test_noop_without_reconfigure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr('sys.platform', 'win32')
        monkeypatch.setattr('sys.stdin', io.StringIO())
        monkeypatch.setattr('sys.stdout', io.StringIO())
        monkeypatch.setattr('sys.stderr', io.StringIO())
        ensure_utf8_stdio()  # StringIO lacks reconfigure; should not raise

    def test_posix_stdout_uses_backslashreplace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, str]] = []

        class _FakeStream:
            errors = 'surrogateescape'

            def reconfigure(self, **kwargs: str) -> None:
                calls.append(kwargs)

        stream = _FakeStream()
        monkeypatch.setattr('sys.platform', 'linux')
        monkeypatch.setattr('sys.stdin', stream)
        monkeypatch.setattr('sys.stdout', stream)
        monkeypatch.setattr('sys.stderr', stream)

        ensure_utf8_stdio()

        assert calls == [{'errors': 'backslashreplace'}]

    def test_reconfigures_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, str]] = []

        class _FakeStderr:
            errors = 'surrogatepass'

            def reconfigure(self, **kwargs: str) -> None:
                calls.append(kwargs)

        monkeypatch.setattr('sys.platform', 'win32')
        monkeypatch.setattr('sys.stdin', io.StringIO())  # no reconfigure
        monkeypatch.setattr('sys.stdout', io.StringIO())  # no reconfigure
        monkeypatch.setattr('sys.stderr', _FakeStderr())
        ensure_utf8_stdio()
        assert calls == [{'encoding': 'utf-8', 'errors': 'surrogatepass'}]

    def test_reconfigures_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, str]] = []

        class _FakeStdin:
            errors = 'surrogateescape'

            def reconfigure(self, **kwargs: str) -> None:
                calls.append(kwargs)

        monkeypatch.setattr('sys.platform', 'win32')
        monkeypatch.setattr('sys.stdin', _FakeStdin())
        monkeypatch.setattr('sys.stdout', io.StringIO())  # no reconfigure
        monkeypatch.setattr('sys.stderr', io.StringIO())  # no reconfigure
        ensure_utf8_stdio()
        assert calls == [{'encoding': 'utf-8', 'errors': 'surrogateescape'}]

    def test_windows_stdout_uses_backslashreplace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, str]] = []

        class _FakeStream:
            errors = 'surrogateescape'

            def reconfigure(self, **kw: str) -> None:
                calls.append(kw)

        monkeypatch.setattr('sys.platform', 'win32')
        monkeypatch.setattr('sys.stdin', io.StringIO())  # no reconfigure
        monkeypatch.setattr('sys.stdout', _FakeStream())
        monkeypatch.setattr('sys.stderr', io.StringIO())  # no reconfigure
        ensure_utf8_stdio()
        assert calls == [{'encoding': 'utf-8', 'errors': 'backslashreplace'}]


class TestReadFile:
    def test_bad_bytes_raises_unicode_error(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: ascii -*-\nx = "\xff"\n')
        with pytest.raises(UnicodeDecodeError):
            read_file(p)

    def test_bad_cookie_raises_syntax_error(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: nosuchcodec -*-\nx = 1\n')
        with pytest.raises(SyntaxError):
            read_file(p)

    def test_latin1_cookie(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: latin-1 -*-\nx = "\xe9"\n')
        source, enc = read_file(p)
        assert '\xe9' in source
        assert enc == 'iso-8859-1'

    def test_utf8_bom(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'\xef\xbb\xbfx = 1\n')
        source, enc = read_file(p)
        assert source == 'x = 1\n'
        assert enc == 'utf-8-sig'

    def test_utf8_default(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'x = 1\n')
        source, enc = read_file(p)
        assert source == 'x = 1\n'
        assert enc == 'utf-8'


class TestReadFileResult:
    def test_encoding_cookie(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: latin-1 -*-\nx = "\xe9"\n')
        r = read_file_result(p)
        assert '\xe9' in r.source
        assert r.encoding == 'iso-8859-1'

    def test_matches_read_file(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'x = 1\n')
        r = read_file_result(p)
        source, enc = read_file(p)
        assert r.source == source
        assert r.encoding == enc

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            pytest.param(b'x = 1\r\ny = 2\r\n', 'x = 1\r\ny = 2\r\n', id='crlf'),
            pytest.param(b'x = 1\ry = 2\r', 'x = 1\ry = 2\r', id='lone-cr'),
            pytest.param(
                b'x = 1\r\n \t\ny = 2\rz = 3',
                'x = 1\r\n \t\ny = 2\rz = 3',
                id='mixed-whitespace-blank-no-final-ending',
            ),
            pytest.param(b'x = 1', 'x = 1', id='no-final-ending'),
        ],
    )
    def test_source_preserves_exact_decoded_text(
        self,
        tmp_path: Path,
        raw: bytes,
        expected: str,
    ) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(raw)
        r = read_file_result(p)
        assert r.source == expected

    def test_utf8_lf(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'x = 1\n')
        r = read_file_result(p)
        assert r.source == 'x = 1\n'
        assert r.encoding == 'utf-8'


class TestReadResult:
    def test_fields(self) -> None:
        r = _ReadResult(source='x = 1\n', encoding='utf-8')
        assert r.source == 'x = 1\n'
        assert r.encoding == 'utf-8'


class TestStdoutEscapeBoundary:
    def test_surrogate_path_text_renders_escaped(self) -> None:
        code = (
            'import sys\n'
            'from defsort._files import ensure_utf8_stdio\n'
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


class TestWriteSource:
    def test_mixed_endings_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        write_source(p, 'x = 1\r\n \t\ny = 2\rz = 3')
        assert p.read_bytes() == b'x = 1\r\n \t\ny = 2\rz = 3'

    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
    def test_new_file_gets_umask_default_mode(self, tmp_path: Path) -> None:
        target = tmp_path / 'new.py'
        write_source(target, 'x = 1\n')
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~umask

    def test_partial_write_leaves_target_intact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / 'test.py'
        original = b'def original():\n    return 42\n'
        target.write_bytes(original)
        real_fdopen = os.fdopen

        class _PartialFile:
            def __init__(self, inner: BinaryIO) -> None:
                self._inner = inner

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_exc: object) -> None:
                self._inner.close()

            def write(self, data: bytes) -> int:
                self._inner.write(data[:16])
                raise OSError('injected partial write')

        def partial_fdopen(fd: int, _mode: str) -> _PartialFile:
            return _PartialFile(real_fdopen(fd, 'wb'))

        monkeypatch.setattr('defsort._files.os.fdopen', partial_fdopen)
        with pytest.raises(OSError, match='injected partial write'):
            write_source(target, 'x = 1\n' * 200)
        assert target.read_bytes() == original
        assert list(tmp_path.glob('.defsort-*')) == []

    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
    def test_preserves_permission_bits(self, tmp_path: Path) -> None:
        target = tmp_path / 'test.py'
        target.write_bytes(b'x = 1\n')
        target.chmod(0o640)
        write_source(target, 'y = 2\n')
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert target.read_bytes() == b'y = 2\n'

    def test_read_only_target_refused(self, tmp_path: Path) -> None:
        target = tmp_path / 'test.py'
        target.write_bytes(b'x = 1\n')
        target.chmod(0o444)
        try:
            with pytest.raises(PermissionError):
                write_source(target, 'y = 2\n')
            assert target.read_bytes() == b'x = 1\n'
            assert list(tmp_path.glob('.defsort-*')) == []
        finally:
            target.chmod(0o644)

    @pytest.mark.parametrize(
        'raw',
        [
            pytest.param(b'x = 1\r\n', id='utf8'),
            pytest.param(
                b'# -*- coding: latin-1 -*-\r\nx = "\xe9"\r',
                id='latin1',
            ),
            pytest.param(b'\xef\xbb\xbfx = 1\r\n', id='utf8-bom'),
        ],
    )
    def test_read_write_round_trip(self, tmp_path: Path, raw: bytes) -> None:
        source_path = tmp_path / 'source.py'
        output_path = tmp_path / 'output.py'
        source_path.write_bytes(raw)
        result = read_file_result(source_path)

        write_source(output_path, result.source, encoding=result.encoding)

        assert output_path.read_bytes() == raw

    def test_replace_failure_leaves_target_and_no_temp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / 'test.py'
        original = b'x = 1\n'
        target.write_bytes(original)

        def fail_replace(_self: Path, _target: Path) -> Path:
            raise OSError('injected replace failure')

        monkeypatch.setattr('defsort._files.Path.replace', fail_replace)
        with pytest.raises(OSError, match='injected replace failure'):
            write_source(target, 'y = 2\n')
        assert target.read_bytes() == original
        assert list(tmp_path.glob('.defsort-*')) == []

    def test_respects_latin1_encoding(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        write_source(p, '# -*- coding: latin-1 -*-\r\nx = "\xe9"\r', encoding='latin-1')
        assert p.read_bytes() == b'# -*- coding: latin-1 -*-\r\nx = "\xe9"\r'

    def test_restores_utf8_bom(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        write_source(p, 'x = 1\r\n', encoding='utf-8-sig')
        assert p.read_bytes() == b'\xef\xbb\xbfx = 1\r\n'

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation')
    def test_symlink_target_write_through(self, tmp_path: Path) -> None:
        real = tmp_path / 'real.py'
        real.write_bytes(b'x = 1\n')
        link = tmp_path / 'link.py'
        link.symlink_to(real)
        write_source(link, 'y = 2\n')
        assert link.is_symlink()
        assert real.read_bytes() == b'y = 2\n'
