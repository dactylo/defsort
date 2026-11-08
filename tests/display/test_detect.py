from __future__ import annotations

from typing import TYPE_CHECKING

from defsort._display._detect import print_detect
from defsort._types import DetectResult

if TYPE_CHECKING:
    import pytest


class TestPrintDetect:
    def test_path_controls_escape_before_alignment(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        entries: list[tuple[str, DetectResult | None]] = [
            (
                'bad\tname\n\x85.py',
                DetectResult(order='dependency', dep_score=0.95, alpha_score=0.30),
            ),
            ('plain.py', DetectResult(order='alphabetical', dep_score=0.30, alpha_score=0.95)),
        ]
        print_detect(entries, verbose=False)

        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith(r'bad\x09name\x0a\x85.py')
        assert lines[0].index('dependency') == lines[1].index('alphabetical')

    def test_prints_per_file_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        entries: list[tuple[str, DetectResult | None]] = [
            ('mod.py', DetectResult(order='dependency', dep_score=0.95, alpha_score=0.30)),
        ]
        print_detect(entries, verbose=False)
        output = capsys.readouterr().out
        assert 'mod.py' in output
        assert 'dependency' in output

    def test_prints_summary_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        entries: list[tuple[str, DetectResult | None]] = [
            ('a.py', DetectResult(order='dependency', dep_score=0.95, alpha_score=0.30)),
            ('b.py', DetectResult(order='alphabetical', dep_score=0.30, alpha_score=0.95)),
        ]
        print_detect(entries, verbose=False)
        output = capsys.readouterr().out
        assert '2 files' in output

    def test_verbose_includes_scores(self, capsys: pytest.CaptureFixture[str]) -> None:
        entries: list[tuple[str, DetectResult | None]] = [
            ('mod.py', DetectResult(order='dependency', dep_score=0.95, alpha_score=0.30)),
        ]
        print_detect(entries, verbose=True)
        output = capsys.readouterr().out
        assert 'tau' in output

    def test_verbose_skip_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        entries: list[tuple[str, DetectResult | None]] = [
            ('single.py', None),
        ]
        print_detect(entries, verbose=True)
        output = capsys.readouterr().out
        assert 'single.py' in output
        assert 'too few definitions' in output
