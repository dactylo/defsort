from pathlib import Path

import pytest

import defsort._files as files_mod
from defsort._files import (
    collect_files,
    encode_source,
    read_file_result,
    write_source,
)
from defsort._vendor.srcfiles import RestoreError
from tests._helpers import fail_writes_to, redirect_recovery, run_setup


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


class TestReadFileResult:
    def test_bad_bytes_raises_unicode_error(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: ascii -*-\nx = "\xff"\n')
        with pytest.raises(UnicodeDecodeError):
            read_file_result(p)

    def test_bad_cookie_raises_syntax_error(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: nosuchcodec -*-\nx = 1\n')
        with pytest.raises(SyntaxError):
            read_file_result(p)

    def test_latin1_cookie(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'# -*- coding: latin-1 -*-\nx = "\xe9"\n')
        result = read_file_result(p)
        assert '\xe9' in result.source
        assert result.encoding == 'iso-8859-1'

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

    def test_utf8_bom(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'\xef\xbb\xbfx = 1\n')
        result = read_file_result(p)
        assert result.source == 'x = 1\n'
        assert result.encoding == 'utf-8-sig'
        assert result.raw == b'\xef\xbb\xbfx = 1\n'

    def test_utf8_default(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'x = 1\n')
        result = read_file_result(p)
        assert result.source == 'x = 1\n'
        assert result.encoding == 'utf-8'


class TestWriteSource:
    def test_failed_restore_saves_the_original(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recovery = redirect_recovery(monkeypatch, tmp_path / 'home')
        target = tmp_path / 'test.py'
        original = b'x = 1\n'
        target.write_bytes(original)
        fail_writes_to(monkeypatch, target, restore_fails=True)
        with pytest.raises(RestoreError) as caught:
            write_source(target, 'y = 2\n' * 50, encoding='utf-8', raw=original)
        assert caught.value.recovery_path is not None
        assert caught.value.recovery_path.parent.parent == recovery
        assert caught.value.recovery_path.read_bytes() == original
        assert list(tmp_path.glob('.defsort-*')) == []

    def test_mixed_endings_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / 'test.py'
        p.write_bytes(b'old\n')
        written = write_source(p, 'x = 1\r\n \t\ny = 2\rz = 3', encoding='utf-8', raw=b'old\n')
        assert p.read_bytes() == written == b'x = 1\r\n \t\ny = 2\rz = 3'

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
        output_path.write_bytes(b'placeholder\n')
        result = read_file_result(source_path)

        write_source(output_path, result.source, encoding=result.encoding, raw=b'placeholder\n')

        assert output_path.read_bytes() == raw

    def test_write_source_forwards_raw_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / 'test.py'
        calls: list[tuple[Path, bytes, bytes, str]] = []

        def record_write(candidate: Path, data: bytes, *, expected: bytes, tool: str) -> None:
            calls.append((candidate, data, expected, tool))

        monkeypatch.setattr(files_mod, 'write_in_place', record_write)
        assert write_source(path, 'y = 2\n', encoding='utf-8', raw=b'x = 1\n') == b'y = 2\n'
        assert calls == [(path, b'y = 2\n', b'x = 1\n', 'defsort')]


def test_collect_files_names_the_defsort_attribute_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_setup(['git', 'init', '-q'], cwd=tmp_path)
    run_setup(['git', 'config', 'defsort.privateAttribute', 'a b'], cwd=tmp_path)
    path = tmp_path / 'mod.py'
    path.write_text('x = 1\n', encoding='utf-8')
    assert collect_files([str(tmp_path)]) == [path]
    assert collect_files([str(tmp_path)]) == [path]
    warnings = capsys.readouterr().err.splitlines()
    assert len(warnings) == 1
    assert warnings[0].startswith("warning: invalid git config defsort.privateAttribute 'a b': ")
