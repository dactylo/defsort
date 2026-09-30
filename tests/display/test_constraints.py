from defsort._display._constraints import (
    format_banner_constraint_details,
    format_banner_constraint_summary,
)
from defsort._types import BannerConstraint, HeldItem, SectionBoundary


class TestConstraintDetails:
    def test_banner_label_escapes_terminal_controls(self) -> None:
        constraint = _constraint(text='# ----\n# Alpha\x1b]2;spoof\x07\x85Beta\n# ----')

        assert format_banner_constraint_details([constraint]) == (
            r'line 42: Alpha\x1b]2;spoof\x07\x85Beta: helper (before)'
        )

    def test_bare_rule_skips_blank_leading_comment_line(self) -> None:
        constraint = _constraint(text='#\n# --------')

        assert format_banner_constraint_details([constraint]) == (
            'line 42: --------: helper (before)'
        )

    def test_bare_rule_uses_rule_as_label(self) -> None:
        constraint = _constraint(text='# --------')

        assert format_banner_constraint_details([constraint]) == (
            'line 42: --------: helper (before)'
        )

    def test_title_and_all_held_directions_are_rendered(self) -> None:
        constraint = _constraint(
            held=(
                HeldItem('helper', 'function', 1, 51, 'before'),
                HeldItem('service', 'class', 1, 12, 'after'),
            )
        )

        assert format_banner_constraint_details([constraint]) == (
            'line 42: Helpers: helper (before), service (after)'
        )


class TestConstraintSummary:
    def test_empty_constraints_are_silent(self) -> None:
        assert format_banner_constraint_summary('sample.py', []) == ''

    def test_path_escapes_terminal_controls(self) -> None:
        constraint = _constraint()

        assert format_banner_constraint_summary('bad\r\n\t\x7f.py', [constraint]) == (
            r'defsort: bad\x0d\x0a\x09\x7f.py: sort constrained by 1 banner'
        )

    def test_singular_and_plural_counts(self) -> None:
        constraint = _constraint()

        assert format_banner_constraint_summary('sample.py', [constraint]) == (
            'defsort: sample.py: sort constrained by 1 banner'
        )
        assert format_banner_constraint_summary('sample.py', [constraint, constraint]) == (
            'defsort: sample.py: sort constrained by 2 banners'
        )


def _constraint(
    *,
    line: int = 42,
    text: str = '# ----\n# Helpers\n# ----',
    held: tuple[HeldItem, ...] = (HeldItem('helper', 'function', 1, 51, 'before'),),
) -> BannerConstraint:
    return BannerConstraint(
        scope='definitions',
        class_name=None,
        boundary=SectionBoundary(1, line, line + 2, '', text),
        held=held,
    )
