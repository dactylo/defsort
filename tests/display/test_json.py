import json

from defsort._display._json import build_json_record, format_json
from defsort._types import (
    BannerConstraint,
    ClassBlocker,
    HeldItem,
    MethodDefinition,
    MethodSortInfo,
    SectionBoundary,
    SortResult,
)
from tests.display._helpers import _make_def


class TestBuildJsonRecord:
    def test_banner_constraints_exact_contract(self) -> None:
        result = SortResult(
            original_source='src',
            new_source='src',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
            banner_constraints=[
                BannerConstraint(
                    scope='definitions',
                    class_name=None,
                    boundary=SectionBoundary(
                        section=1,
                        start_line=42,
                        end_line=44,
                        raw='# ----\n# Helpers\n# ----\n',
                        text='# ----\n# Helpers\n# ----',
                    ),
                    held=(
                        HeldItem(
                            name='helper',
                            kind='function',
                            occurrence=1,
                            line=51,
                            direction='before',
                        ),
                    ),
                )
            ],
        )

        record = build_json_record('test.py', 'unchanged', result=result)

        assert record['banner_constraints'] == [
            {
                'scope': 'definitions',
                'class_name': None,
                'banner': {'line': 42, 'text': '# ----\n# Helpers\n# ----'},
                'held': [
                    {
                        'name': 'helper',
                        'kind': 'function',
                        'occurrence': 1,
                        'line': 51,
                        'direction': 'before',
                    }
                ],
            }
        ]

    def test_changed_with_method_movements(self) -> None:
        orig = [_make_def(0, 'alpha'), _make_def(1, 'bravo')]
        m = MethodDefinition
        m_orig = [
            m(index=4, name='same', start_line=2, end_line=3, source=''),
            m(index=9, name='middle', start_line=5, end_line=6, source=''),
            m(index=7, name='same', start_line=8, end_line=9, source=''),
        ]
        m_new = [m_orig[2], m_orig[1], m_orig[0]]
        result = SortResult(
            original_source='before',
            new_source='after',
            original_order=orig,
            new_order=list(orig),
            warning=None,
            blocking_code=[],
            method_sorts=[
                MethodSortInfo(class_name='Foo', original_order=m_orig, new_order=m_new)
            ],
            total_methods=3,
        )
        record = build_json_record(
            'test.py',
            'changed',
            result=result,
            detect_result=('dependency', 0.9, 0.1),
        )
        assert record['methods'] == {'moved': 2, 'total': 3}
        assert record['method_movements'] == [
            {
                'class': 'Foo',
                'movements': [
                    {'name': 'same', 'from': 2, 'to': 0},
                    {'name': 'same', 'from': 0, 'to': 2},
                ],
            }
        ]

    def test_changed_with_movements(self) -> None:
        orig = [_make_def(4, 'same'), _make_def(9, 'middle'), _make_def(7, 'same')]
        new = [orig[2], orig[1], orig[0]]
        result = SortResult(
            original_source='before',
            new_source='after',
            original_order=orig,
            new_order=new,
            warning=None,
            blocking_code=[],
            total_methods=0,
        )
        record = build_json_record(
            'test.py',
            'changed',
            result=result,
            detect_result=('unordered', 0.5, 0.5),
        )
        assert record['definitions'] == {'moved': 2, 'total': 3}
        assert record['movements'] == [
            {'name': 'same', 'from': 2, 'to': 0},
            {'name': 'same', 'from': 0, 'to': 2},
        ]

    def test_class_blockers_serialized(self) -> None:
        result = SortResult(
            original_source='src',
            new_source='src',
            original_order=[],
            new_order=[],
            warning=None,
            blocking_code=[],
            skipped=True,
            class_blockers=(
                ClassBlocker(
                    class_name='C',
                    occurrence=2,
                    blocker='duplicates',
                    message='non-consecutive duplicate definitions',
                ),
            ),
        )
        record = build_json_record('mod.py', 'skipped', result=result)
        assert record['class_blockers'] == [
            {'class': 'C', 'occurrence': 2, 'blocker': 'duplicates'}
        ]

    def test_error_and_invalid_records_have_exact_empty_constraint_contract(self) -> None:
        for status in ('error', 'invalid'):
            record = build_json_record('bad.py', status, warning='unable to read')

            assert record == {
                'path': 'bad.py',
                'status': status,
                'warning': 'unable to read',
                'order': None,
                'dep_score': None,
                'alpha_score': None,
                'definitions': {'moved': 0, 'total': 0},
                'methods': {'moved': 0, 'total': 0},
                'movements': [],
                'method_movements': [],
                'banner_constraints': [],
                'class_blockers': [],
            }

    def test_null_detect_when_few_defs(self) -> None:
        defs = [_make_def(0, 'only')]
        result = SortResult(
            original_source='src',
            new_source='src',
            original_order=defs,
            new_order=list(defs),
            warning=None,
            blocking_code=[],
        )
        record = build_json_record('one.py', 'unchanged', result=result, detect_result=None)
        assert record['order'] is None
        assert record['dep_score'] is None
        assert record['alpha_score'] is None

    def test_skipped_empty_movements(self) -> None:
        orig = [_make_def(0, 'bravo'), _make_def(1, 'alpha')]
        new = [_make_def(1, 'alpha'), _make_def(0, 'bravo')]
        result = SortResult(
            original_source='src',
            new_source='src',
            original_order=orig,
            new_order=new,
            warning='code between definitions',
            blocking_code=[],
            skipped=True,
            total_methods=0,
        )
        record = build_json_record(
            'test.py',
            'skipped',
            warning='code between definitions',
            result=result,
            detect_result=('unordered', 0.3, 0.4),
        )
        assert record['definitions'] == {'moved': 2, 'total': 2}
        assert record['movements'] == []
        assert record['method_movements'] == []

    def test_sorted_all_fields_present(self) -> None:
        defs = [_make_def(0, 'alpha'), _make_def(1, 'bravo')]
        result = SortResult(
            original_source='src',
            new_source='src',
            original_order=defs,
            new_order=list(defs),
            warning=None,
            blocking_code=[],
            total_methods=5,
        )
        record = build_json_record(
            'test.py',
            'unchanged',
            result=result,
            detect_result=('dependency', 0.95, 0.12),
        )
        # All 12 per-file fields present
        expected_keys = {
            'path',
            'status',
            'warning',
            'order',
            'dep_score',
            'alpha_score',
            'definitions',
            'methods',
            'movements',
            'method_movements',
            'banner_constraints',
            'class_blockers',
        }
        assert set(record.keys()) == expected_keys
        assert record['path'] == 'test.py'
        assert record['status'] == 'unchanged'
        assert record['warning'] is None
        assert record['order'] == 'dependency'
        assert record['dep_score'] == 0.95
        assert record['alpha_score'] == 0.12
        assert record['definitions'] == {'moved': 0, 'total': 2}
        assert record['methods'] == {'moved': 0, 'total': 5}
        assert record['movements'] == []
        assert record['method_movements'] == []
        assert record['banner_constraints'] == []
        assert record['class_blockers'] == []


class TestFormatJson:
    def test_fully_populated_record_exact_contract(self) -> None:
        original = [_make_def(0, 'bravo'), _make_def(1, 'alpha')]
        methods = [
            MethodDefinition(index=0, name='zeta', start_line=2, end_line=3, source=''),
            MethodDefinition(index=1, name='alpha', start_line=5, end_line=6, source=''),
        ]
        result = SortResult(
            original_source='before',
            new_source='after',
            original_order=original,
            new_order=[original[1], original[0]],
            warning=None,
            blocking_code=[],
            method_sorts=[
                MethodSortInfo(
                    class_name='Example',
                    original_order=methods,
                    new_order=[methods[1], methods[0]],
                )
            ],
            total_methods=2,
            banner_constraints=[
                BannerConstraint(
                    scope='methods',
                    class_name='Example',
                    boundary=SectionBoundary(
                        section=1,
                        start_line=10,
                        end_line=10,
                        raw='# Helpers\n',
                        text='# Helpers',
                    ),
                    held=(
                        HeldItem(
                            name='alpha',
                            kind='method',
                            occurrence=1,
                            line=12,
                            direction='after',
                        ),
                    ),
                )
            ],
            class_blockers=(
                ClassBlocker(
                    class_name='Example',
                    occurrence=2,
                    blocker='duplicates',
                    message='non-consecutive duplicate definitions',
                ),
            ),
        )
        raw = format_json(
            [
                build_json_record(
                    'example.py',
                    'changed',
                    result=result,
                    detect_result=('unordered', 0.25, 0.75),
                )
            ]
        )

        parsed = json.loads(raw)
        assert parsed == {
            'version': 1,
            'files': [
                {
                    'path': 'example.py',
                    'status': 'changed',
                    'warning': None,
                    'order': 'unordered',
                    'dep_score': 0.25,
                    'alpha_score': 0.75,
                    'definitions': {'moved': 2, 'total': 2},
                    'methods': {'moved': 2, 'total': 2},
                    'movements': [
                        {'name': 'alpha', 'from': 1, 'to': 0},
                        {'name': 'bravo', 'from': 0, 'to': 1},
                    ],
                    'method_movements': [
                        {
                            'class': 'Example',
                            'movements': [
                                {'name': 'alpha', 'from': 1, 'to': 0},
                                {'name': 'zeta', 'from': 0, 'to': 1},
                            ],
                        }
                    ],
                    'banner_constraints': [
                        {
                            'scope': 'methods',
                            'class_name': 'Example',
                            'banner': {'line': 10, 'text': '# Helpers'},
                            'held': [
                                {
                                    'name': 'alpha',
                                    'kind': 'method',
                                    'occurrence': 1,
                                    'line': 12,
                                    'direction': 'after',
                                }
                            ],
                        }
                    ],
                    'class_blockers': [
                        {'class': 'Example', 'occurrence': 2, 'blocker': 'duplicates'}
                    ],
                }
            ],
        }
        record = parsed['files'][0]
        assert list(record) == [
            'path',
            'status',
            'warning',
            'order',
            'dep_score',
            'alpha_score',
            'definitions',
            'methods',
            'movements',
            'method_movements',
            'banner_constraints',
            'class_blockers',
        ]

    def test_json_preserves_control_values_with_machine_escaping(self) -> None:
        path = 'bad\x1b]2;path\x07\n.py'
        warning = 'unsafe\ttext\x85'

        raw = format_json([build_json_record(path, 'error', warning=warning)])

        record = json.loads(raw)['files'][0]
        assert record['path'] == path
        assert record['warning'] == warning
        assert '\x1b' not in raw
        assert '\x07' not in raw
        assert '\x85' not in raw
        assert r'\u001b]2;path\u0007\n.py' in raw
        assert r'unsafe\ttext\u0085' in raw

    def test_wraps_records(self) -> None:
        records = [
            build_json_record('a.py', 'unchanged', result=None, detect_result=None),
        ]
        raw = format_json(records)
        data = json.loads(raw)
        assert data['version'] == 1
        assert len(data['files']) == 1
        assert data['files'][0]['path'] == 'a.py'
        assert '\n' in raw
