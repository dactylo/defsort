import pytest

from defsort._display._skipped import format_class_blocker, print_skipped
from defsort._display._types import CliMode
from defsort._types import (
    Blocker,
    BlockingCode,
    ClassBlocker,
    Definition,
    SortResult,
    blocker_message,
)


class TestFormatClassBlocker:
    def test_code_between_methods(self) -> None:
        blocker = ClassBlocker(
            class_name='C',
            occurrence=1,
            blocker='interstitial',
            message='code between definitions',
        )

        assert format_class_blocker(blocker) == ('  class C: skipped (code between methods)')

    def test_duplicate_method_names(self) -> None:
        blocker = ClassBlocker(
            class_name='C',
            occurrence=2,
            blocker='duplicates',
            message='non-consecutive duplicate definitions',
        )

        assert format_class_blocker(blocker) == ('  class C: skipped (duplicate method names)')

    def test_non_conforming_methods(self) -> None:
        blocker = ClassBlocker(
            class_name='C',
            occurrence=1,
            blocker='non_conforming',
            message='baseline not in target order',
        )

        assert format_class_blocker(blocker) == (
            '  class C: skipped (baseline not in target order)'
        )

    def test_source_derived_blocker_message_escapes_controls(self) -> None:
        blocker = ClassBlocker(
            class_name='C',
            occurrence=1,
            blocker='non_conforming',
            message='baseline\r\n\t\x7f\x85unsafe',
        )

        assert format_class_blocker(blocker) == (
            r'  class C: skipped (baseline\x0d\x0a\x09\x7f\x85unsafe)'
        )


class TestPrintSkipped:
    def _make_guarded_result(self) -> SortResult:
        orig = [
            Definition(
                index=0,
                name='bravo',
                start_line=1,
                end_line=2,
                kind='function',
                source='def bravo():\n    pass\n',
            ),
            Definition(
                index=1,
                name='alpha',
                start_line=5,
                end_line=6,
                kind='function',
                source='def alpha():\n    pass\n',
            ),
        ]
        return SortResult(
            original_source='def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            new_source='def bravo():\n    pass\n\n\ndef alpha():\n    pass\n',
            original_order=orig,
            new_order=[orig[1], orig[0]],
            warning='would move 2 of 2 definitions; threshold: max-reorder-count = 0',
            blocking_code=[],
            skipped=True,
            guarded=True,
        )

    def _make_skipped_result(
        self,
        blocker: Blocker = 'interstitial',
    ) -> SortResult:
        orig = [
            Definition(
                index=0,
                name='bravo',
                start_line=1,
                end_line=2,
                kind='function',
                source='def bravo():\n    pass\n',
            ),
            Definition(
                index=1,
                name='alpha',
                start_line=5,
                end_line=6,
                kind='function',
                source='def alpha():\n    pass\n',
            ),
        ]
        return SortResult(
            original_source='def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n',
            new_source='def bravo():\n    pass\n\nregistry = {}\n\ndef alpha():\n    pass\n',
            original_order=orig,
            new_order=list(orig),
            warning=blocker_message(blocker),
            blocking_code=(
                [BlockingCode(line=4, text='registry = {}')] if blocker == 'interstitial' else []
            ),
            blocker=blocker,
            skipped=True,
        )

    def _make_skipped_result_without_details(self) -> SortResult:
        return SortResult(
            original_source='',
            new_source='',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
            blocker='interstitial',
            skipped=True,
        )

    def test_compact_color_bolds_path_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._make_skipped_result()
        print_skipped('test.py', result, mode=CliMode.SUMMARY, color=True)
        output = capsys.readouterr().err
        assert output == '\033[1mtest.py\033[0m: skipped (code between definitions)\n'

    @pytest.mark.parametrize('mode', [CliMode.SUMMARY, CliMode.DIFF, CliMode.WRITE])
    def test_non_preview_modes_one_liner(
        self,
        capsys: pytest.CaptureFixture[str],
        mode: CliMode,
    ) -> None:
        result = self._make_skipped_result()
        print_skipped('test.py', result, mode=mode, color=False)
        output = capsys.readouterr().err
        assert output == 'test.py: skipped (code between definitions)\n'

    def test_path_warning_and_blocking_code_escape_before_color(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = self._make_skipped_result()
        result.warning = 'unsafe\r\n\t\x85warning'
        result.blocking_code = [BlockingCode(line=4, text='call\x1b]2;title\x07()')]

        print_skipped(
            'bad\x1b]2;path\x07.py',
            result,
            mode=CliMode.PREVIEW,
            color=True,
        )

        output = capsys.readouterr().err
        assert output.startswith('\033[1mbad\\x1b]2;path\\x07.py\033[0m: skipped\n')
        assert r'  reason: unsafe\x0d\x0a\x09\x85warning' in output
        assert r'    line 4: call\x1b]2;title\x07()' in output
        assert '\x1b]2;title\x07' not in output

    def test_preview_color_bolds_path_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._make_skipped_result()

        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=True)

        output = capsys.readouterr().err
        assert output.startswith(
            '\033[1mtest.py\033[0m: skipped\n  reason: code between definitions\n'
        )
        assert '\033[1m: skipped' not in output
        assert '\033[1m  reason:' not in output

    def test_preview_default_omits_unchanged_rows(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = self._make_skipped_result()

        print_skipped(
            'test.py',
            result,
            mode=CliMode.PREVIEW,
            color=False,
            verbose=False,
        )

        assert capsys.readouterr().err == (
            'test.py: skipped\n'
            '  reason: code between definitions\n'
            '  fix: use --force to bypass safety checks\n'
            '\n'
            '  Code between definitions:\n'
            '    line 4: registry = {}\n'
        )

    def test_preview_duplicate_recommends_removing_duplicate(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = self._make_skipped_result('duplicates')

        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=False)

        output = capsys.readouterr().err
        assert '  fix: remove the duplicate definition\n' in output
        assert '--force' not in output

    def test_preview_eager_order_shows_compatible_order_guidance(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = self._make_skipped_result('eager_order')

        print_skipped(
            'test.py',
            result,
            mode=CliMode.PREVIEW,
            color=False,
            verbose=True,
        )

        output = capsys.readouterr().err
        guidance = (
            '  reason: sort would move a decorator, default value, base class, or class-body\n'
            '          reference before the definition it uses\n'
            '  fix: keep each referenced definition before the code that uses it'
        )
        assert guidance in output
        assert output.index('  reason:') < output.index('  fix:') < output.index('  Current')
        assert '--force' not in output

    def test_preview_future_refs_omit_force_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._make_skipped_result('forward_refs')

        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=False)

        output = capsys.readouterr().err
        assert '--force' not in output
        assert '  reason: forward annotation references require future annotations\n' in output

    def test_preview_lists_multiple_blocking_lines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = self._make_skipped_result()
        result.blocking_code.append(BlockingCode(line=9, text='cache = {}'))
        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=False)
        output = capsys.readouterr().err
        assert (
            '  Code between definitions:\n    line 4: registry = {}\n    line 9: cache = {}'
        ) in output
        assert output.index('  reason:') < output.index('  Code between definitions:')

    def test_preview_reorder_guard_full_diagnostic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = self._make_guarded_result()
        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=False)
        output = capsys.readouterr().err
        assert output == (
            'test.py: skipped\n'
            '  reason: would move 2 of 2 definitions; threshold: max-reorder-count = 0\n'
            '  fix: use --force to bypass the reorder guard\n'
            '\n'
            '  Current  Proposed  Move\n'
            '  bravo    alpha     ^1\n'
            '  alpha    bravo     v1\n'
        )

    def test_preview_verbose_includes_unchanged_rows(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = self._make_skipped_result()

        print_skipped(
            'test.py',
            result,
            mode=CliMode.PREVIEW,
            color=False,
            verbose=True,
        )

        assert capsys.readouterr().err == (
            'test.py: skipped\n'
            '  reason: code between definitions\n'
            '  fix: use --force to bypass safety checks\n'
            '\n'
            '  Code between definitions:\n'
            '    line 4: registry = {}\n'
            '\n'
            '  Current  Proposed  Move\n'
            '  bravo    bravo     -\n'
            '  alpha    alpha     -\n'
        )

    def test_single_file_without_details_has_single_separator(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = self._make_skipped_result_without_details()
        print_skipped('test.py', result, mode=CliMode.PREVIEW, color=False)
        output = capsys.readouterr().err
        assert output == (
            'test.py: skipped\n'
            '  reason: safety check\n'
            '  fix: use --force to bypass safety checks\n'
        )
