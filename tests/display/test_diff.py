import pytest

from defsort._display._diff import print_diff
from defsort._vendor.hookfront._diff import DiffSides


class TestPrintDiff:
    def test_unchanged_no_verbose_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        sides = DiffSides(b'unchanged\n', b'unchanged\n', 'utf-8')
        print_diff(sides, 'test.py', verbose=False, ordering='dependency', color=False)
        assert capsys.readouterr().out == ''

    def test_unchanged_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        sides = DiffSides(b'unchanged\n', b'unchanged\n', 'utf-8')
        print_diff(sides, 'test.py', verbose=True, ordering='dependency', color=False)
        assert 'already in dependency order' in capsys.readouterr().out
