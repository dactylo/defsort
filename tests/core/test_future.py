import ast

import pytest

import defsort._core._future as future
from defsort._core._future import _remove_future_annotations_import, ensure_future_annotations

_FUTURE_IMPORT = 'from __future__ import annotations'


class TestAstOwnedRemoval:
    def test_compile_invalid_source_unchanged(self) -> None:
        source = 'x = 1\nfrom __future__ import annotations\n'

        assert _remove_future_annotations_import(source) == source

    def test_crlf_span_removed_exactly(self) -> None:
        source = 'from __future__ import ( annotations )\r\n\r\ndef f():\r\n    return 1\r\n'
        assert _remove_future_annotations_import(source) == '\r\ndef f():\r\n    return 1\r\n'

    def test_mixed_ending_seam_preserves_two_separators(self) -> None:
        source = '"""doc"""\rfrom __future__ import annotations\r\n\ndef f():\n    return 1\n'
        result = _remove_future_annotations_import(source)

        assert result == '"""doc"""\n\rdef f():\n    return 1\n'
        assert len(result.splitlines(keepends=True)) == 4
        assert _remove_future_annotations_import(result) == result

    def test_recognized_spellings_removed(self) -> None:
        for spelling in (
            'from __future__ import annotations\n',
            'from __future__ import (annotations)\n',
            'from __future__ import ( annotations )\n',
            'from __future__ import (annotations,)\n',
            'from __future__ import  annotations\n',
            'from __future__ import \\\n    annotations\n',
            'from __future__ import (\n    annotations\n)\n',
        ):
            source = spelling + '\n\ndef f():\n    return 1\n'
            removed = _remove_future_annotations_import(source)
            assert removed == '\n\ndef f():\n    return 1\n', spelling
            assert _remove_future_annotations_import(removed) == removed, spelling

    def test_unowned_or_ineligible_spans_unchanged(self) -> None:
        for source in (
            'from __future__ import annotations as _a\n_A = _a\n',
            'from __future__ import annotations, generator_stop\n',
            'import os; from __future__ import annotations\n',
            'from __future__ import annotations  # keep\n',
            'from __future__ import (\n    # keep\n    annotations\n)\n',
        ):
            assert _remove_future_annotations_import(source) == source, source


class TestAutomaticFutureCleanup:
    def test_future_cleanup_preserves_mixed_physical_source(self) -> None:
        prefix = '"""Doc\fpage."""\r\n# BEFORE\n'
        retained = f'{_FUTURE_IMPORT}  # keep\r'
        body = '# AFTER\r\ndef feature():\n    return 1'
        source = (
            prefix
            + f'{_FUTURE_IMPORT}\r\n'
            + retained
            + 'from __future__ import \\\n    annotations\r\n'
            + body
        )
        tree = ast.parse(source)

        assert future.can_remove_future_annotations(tree)
        result = future.remove_unused_future_annotations(source, _tree=tree)

        assert result == prefix + retained + body
        assert future.remove_unused_future_annotations(result) == result

    @pytest.mark.parametrize(
        'body',
        [
            'def feature():\n    return annotations.compiler_flag\n',
            'annotations = 1\n',
            'del annotations\n',
            'def local():\n    annotations = 1\n',
            'value: int\n',
            'def feature(value: int):\n    pass\n',
            'async def feature() -> int:\n    pass\n',
        ],
        ids=['load', 'store', 'delete', 'local-store', 'assignment', 'parameter', 'return'],
    )
    def test_future_preserves_annotations_and_binding_occurrences(self, body: str) -> None:
        source = f'{_FUTURE_IMPORT}\n{body}'
        tree = ast.parse(source)

        assert not future.can_remove_future_annotations(tree)
        assert future.remove_unused_future_annotations(source, _tree=tree) == source

    @pytest.mark.parametrize('source', ['def invalid(\n', f'value = 1\n{_FUTURE_IMPORT}\n'])
    def test_future_preserves_invalid_source(self, source: str) -> None:
        assert future.remove_unused_future_annotations(source) == source


class TestEnsureFutureAnnotations:
    def test_after_docstring_and_comments(self) -> None:
        source = '"""Module docstring."""\r\n# Author: test\n# License: MIT\r\r\ndef alpha(): pass'
        expected = (
            '"""Module docstring."""\r\n'
            '# Author: test\n'
            '# License: MIT\r'
            f'{_FUTURE_IMPORT}\r'
            '\r\n'
            'def alpha(): pass'
        )

        assert ensure_future_annotations(source) == expected

    def test_after_module_docstring_multiline(self) -> None:
        source = '"""Module\r\ndocstring.\n"""\r\n\rdef alpha(): pass'
        expected = f'"""Module\r\ndocstring.\n"""\r\n{_FUTURE_IMPORT}\r\n\rdef alpha(): pass'

        assert ensure_future_annotations(source) == expected

    def test_after_shebang_and_encoding(self) -> None:
        source = '#!/usr/bin/env python\r\n# -*- coding: utf-8 -*-\n\rdef alpha(): pass'
        expected = (
            '#!/usr/bin/env python\r\n'
            '# -*- coding: utf-8 -*-\n'
            f'{_FUTURE_IMPORT}\n'
            '\rdef alpha(): pass'
        )

        assert ensure_future_annotations(source) == expected

    def test_already_present(self) -> None:
        source = f'{_FUTURE_IMPORT}\r\n\rdef alpha(): pass'

        assert ensure_future_annotations(source) == source

    def test_docstring_shared_line_import_precedes_code(self) -> None:
        source = '"""d\u00e9c"""; x = 1\n'

        result = ensure_future_annotations(source)

        assert result == ('"""d\u00e9c"""; from __future__ import annotations\nx = 1\n')
        compile(result, '<test>', 'exec')
        assert ast.get_docstring(ast.parse(result)) == 'd\u00e9c'

    def test_docstring_with_hash_line(self) -> None:
        source = '"""Module doc.\n# Inside the docstring.\rMore text.\n"""\rdef f(): pass'
        expected = (
            '"""Module doc.\n'
            '# Inside the docstring.\r'
            'More text.\n'
            f'"""\r{_FUTURE_IMPORT}\r'
            'def f(): pass'
        )

        assert ensure_future_annotations(source) == expected

    def test_escaped_triple_quote_docstring_is_not_split(self) -> None:
        source = '"""Start\ncontains \\""" escaped\nstill docstring\n"""\ndef f():\n    pass\n'
        assert ensure_future_annotations(source) == (
            '"""Start\ncontains \\""" escaped\nstill docstring\n"""\n'
            'from __future__ import annotations\n'
            'def f():\n    pass\n'
        )

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            ('', _FUTURE_IMPORT),
            ('def alpha(): pass', f'{_FUTURE_IMPORT}\ndef alpha(): pass'),
            (
                '# header\rdef alpha(): pass',
                f'# header\r{_FUTURE_IMPORT}\rdef alpha(): pass',
            ),
            (
                '#!/usr/bin/env python\r\n',
                f'#!/usr/bin/env python\r\n{_FUTURE_IMPORT}\r\n',
            ),
            (
                '# -*- coding: utf-8 -*-\n',
                f'# -*- coding: utf-8 -*-\n{_FUTURE_IMPORT}\n',
            ),
            (
                '"""Module docstring."""\r',
                f'"""Module docstring."""\r{_FUTURE_IMPORT}\r',
            ),
            (
                'def alpha(): pass\n',
                f'{_FUTURE_IMPORT}\ndef alpha(): pass\n',
            ),
            (
                'def alpha(): pass\r\n',
                f'{_FUTURE_IMPORT}\r\ndef alpha(): pass\r\n',
            ),
            (
                'def alpha(): pass\r',
                f'{_FUTURE_IMPORT}\rdef alpha(): pass\r',
            ),
            (
                'def alpha(): pass\r\n# trailing comment\n',
                f'{_FUTURE_IMPORT}\r\ndef alpha(): pass\r\n# trailing comment\n',
            ),
            (
                '# header\rdef alpha(): pass\r\n',
                f'# header\r{_FUTURE_IMPORT}\rdef alpha(): pass\r\n',
            ),
            ('# header', f'# header\n{_FUTURE_IMPORT}'),
            (
                '# first header\r\n# final header',
                f'# first header\r\n# final header\r\n{_FUTURE_IMPORT}',
            ),
        ],
        ids=[
            'empty',
            'one-line-unterminated-code',
            'before-final-unterminated-code',
            'after-terminated-shebang',
            'after-terminated-encoding-cookie',
            'after-terminated-docstring',
            'uniform-lf',
            'uniform-crlf',
            'uniform-cr',
            'mixed-following-ending',
            'mixed-preceding-ending',
            'after-unterminated-comment-at-eof',
            'unterminated-eof-reuses-nearest-ending',
        ],
    )
    def test_exact_minimum_span_insertion(self, source: str, expected: str) -> None:
        assert ensure_future_annotations(source) == expected

    def test_form_feed_comment_header_keeps_physical_coordinates(self) -> None:
        source = '# header\fnote\nx = 1\n'
        assert ensure_future_annotations(source) == (
            '# header\fnote\nfrom __future__ import annotations\nx = 1\n'
        )

    def test_prefixed_docstring(self) -> None:
        source = 'r"""Raw docstring."""\ndef alpha(): pass'
        expected = f'r"""Raw docstring."""\n{_FUTURE_IMPORT}\ndef alpha(): pass'

        assert ensure_future_annotations(source) == expected

    def test_unparseable_source_is_returned_unchanged(self) -> None:
        source = 'def broken(:\n'
        assert ensure_future_annotations(source) == source


class TestRemoveFutureAnnotationsImport:
    def test_docstring_decoy_with_form_feeds_is_not_removed(self) -> None:
        source = (
            '"""DOC-A\fDOC-B\fDOC-C\nfrom __future__ import annotations\n"""\n'
            'from __future__ import annotations\n'
            'def alpha():\n    pass\n'
        )
        assert _remove_future_annotations_import(source) == (
            '"""DOC-A\fDOC-B\fDOC-C\nfrom __future__ import annotations\n"""\n'
            'def alpha():\n    pass\n'
        )

    def test_docstring_multiline_decoy_preserves_owned_source(self) -> None:
        source = (
            '"""DOC-START\rfrom __future__ import (\n    annotations,\r\n)\r'
            'DOC-END\n"""\r\n# REAL-BEFORE\r'
            'from __future__ import (\r\n    annotations,\n)\r'
            '# REAL-AFTER\ndef alpha():\r    pass'
        )
        expected = (
            '"""DOC-START\rfrom __future__ import (\n    annotations,\r\n)\r'
            'DOC-END\n"""\r\n# REAL-BEFORE\r'
            '# REAL-AFTER\ndef alpha():\r    pass'
        )

        assert _remove_future_annotations_import(source) == expected

    def test_docstring_single_line_decoy_preserves_owned_source(self) -> None:
        source = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\nfrom __future__ import annotations\r# REAL-AFTER\r\n'
            'def alpha():\n    pass'
        )
        expected = (
            '"""DOC-START\r\nfrom __future__ import annotations\nDOC-END\r"""\r\n'
            '# REAL-BEFORE\n# REAL-AFTER\r\ndef alpha():\n    pass'
        )

        assert _remove_future_annotations_import(source) == expected

    @pytest.mark.parametrize(
        'source',
        [
            'from __future__ import annotations, generator_stop\r\n',
            ('from __future__ import (\n    annotations,\r\n    generator_stop,\r)\n'),
            'from __future__ import annotations  # keep\n',
            'from __future__ import annotations; value = 1\n',
            'from __future__ import annotations\nif:\n',
            'def alpha(): pass\r',
        ],
        ids=[
            'combined-single-line',
            'combined-parenthesized',
            'inline-comment',
            'shared-line',
            'syntax-error',
            'no-import',
        ],
    )
    def test_preserves_unowned_source(self, source: str) -> None:
        assert _remove_future_annotations_import(source) == source

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (
                'from __future__ import (\r\n    annotations,\n)\r \t\r\ndef alpha(): pass',
                ' \t\r\ndef alpha(): pass',
            ),
            (
                'from __future__ import (\n    annotations\r\n)\n\ndef alpha(): pass\r',
                '\ndef alpha(): pass\r',
            ),
            (
                'from __future__ import (annotations)\r\n\rdef alpha(): pass',
                '\rdef alpha(): pass',
            ),
        ],
        ids=['mixed-endings', 'no-trailing-comma', 'single-line-parenthesized'],
    )
    def test_removes_exact_parenthesized_span(self, source: str, expected: str) -> None:
        assert _remove_future_annotations_import(source) == expected

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [
            (f'{_FUTURE_IMPORT}\n\n\ndef alpha(): pass\n', '\n\ndef alpha(): pass\n'),
            (
                f'{_FUTURE_IMPORT}\r\n \t\n\rdef alpha(): pass',
                ' \t\n\rdef alpha(): pass',
            ),
            (f'{_FUTURE_IMPORT}\ndef alpha(): pass', 'def alpha(): pass'),
            (
                f'#!/usr/bin/env python\r\n{_FUTURE_IMPORT}\n\r\nimport os\r',
                '#!/usr/bin/env python\r\n\r\nimport os\r',
            ),
            (f'{_FUTURE_IMPORT}', ''),
            (
                f'{_FUTURE_IMPORT}\r\ndef alpha(): pass\n',
                'def alpha(): pass\n',
            ),
        ],
        ids=[
            'blank-lines',
            'whitespace-and-mixed-endings',
            'unterminated-following-code',
            'header-and-final-ending',
            'unterminated-import-only',
            'import-ending-independent-of-following-ending',
        ],
    )
    def test_removes_exact_single_line_span(self, source: str, expected: str) -> None:
        assert _remove_future_annotations_import(source) == expected

    def test_removes_first_ast_eligible_candidate(self) -> None:
        source = f'from __future__ import ( annotations )\r\n{_FUTURE_IMPORT}\n'

        assert _remove_future_annotations_import(source) == f'{_FUTURE_IMPORT}\n'

    def test_removes_only_first_recognized_duplicate(self) -> None:
        source = f'{_FUTURE_IMPORT}\r\n{_FUTURE_IMPORT}\n'

        assert _remove_future_annotations_import(source) == f'{_FUTURE_IMPORT}\n'
