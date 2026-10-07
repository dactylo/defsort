"""Shared ordering arguments and configuration overrides for CLI commands."""

import argparse

from defsort._types import (
    BANNER_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
    SortOverrides,
    validate_banner_pattern,
)


def _compiled_regex_pattern(value: str) -> str:
    """Validate a regular expression while preserving its source string."""
    try:
        validate_banner_pattern(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def add_ordering_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ordering-policy options in the parser's `ordering` group.

    Args:
        parser: Parser to extend with shared definition and method ordering
            flags.
    """
    ordering = parser.add_argument_group('ordering')
    ordering.add_argument(
        '-o',
        '--order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='order for definitions and methods (default: dependency)',
    )
    ordering.add_argument(
        '--definition-order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='order for top-level definitions (default: dependency)',
    )
    ordering.add_argument(
        '--method-order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='order for class methods (default: dependency)',
    )
    ordering.add_argument(
        '--evaluation-order',
        choices=EVALUATION_ORDER_VALUES,
        help='keep code that runs at definition time in its order (default: preserve)',
    )
    ordering.add_argument(
        '--method-bodies',
        choices=METHOD_BODIES_VALUES,
        help='how calls inside methods affect method order (default: within-categories)',
    )
    ordering.add_argument(
        '--method-visibility',
        choices=METHOD_VISIBILITY_VALUES,
        help='where private methods go (default: mixed)',
    )
    ordering.add_argument(
        '--method-dunders',
        choices=METHOD_DUNDERS_VALUES,
        help='where dunder methods other than __new__ and __init__ go (default: before-regular)',
    )
    ordering.add_argument(
        '--main',
        choices=MAIN_PLACEMENT_VALUES,
        help='keep main() last or sort it normally (default: last)',
    )
    ordering.add_argument(
        '--tie-break',
        choices=TIE_BREAK_VALUES,
        help='order of definitions with no dependency between them (default: alphabetical)',
    )
    ordering.add_argument(
        '--banners',
        choices=BANNER_MODE_VALUES,
        help='how comment banners divide sections (default: barriers)',
    )
    banner_pattern = ordering.add_mutually_exclusive_group()
    banner_pattern.add_argument(
        '--banner-pattern',
        type=_compiled_regex_pattern,
        metavar='REGEX',
        help=(
            'also treat comment lines matching REGEX as banners; replaces the configured pattern'
        ),
    )
    banner_pattern.add_argument(
        '--no-banner-pattern',
        action='store_true',
        dest='clear_banner_pattern',
        help='ignore the configured banner pattern',
    )


def overrides_from_args(args: argparse.Namespace) -> SortOverrides:
    """Build configuration overrides from parsed CLI arguments.

    Args:
        args: Parsed CLI or hook arguments including shared ordering flags.

    Returns:
        Explicit flag overrides, leaving unspecified settings for
        configuration.
    """
    incremental: bool | None = None
    if args.no_incremental:
        incremental = False
    elif args.incremental:
        incremental = True
    return SortOverrides(
        banners=args.banners,
        banner_pattern=args.banner_pattern,
        clear_banner_pattern=args.clear_banner_pattern,
        definition_order=args.definition_order,
        evaluation_order=args.evaluation_order,
        incremental=incremental,
        incremental_fallback=getattr(args, 'incremental_fallback', None),
        main=args.main,
        method_bodies=args.method_bodies,
        method_dunders=args.method_dunders,
        method_order=args.method_order,
        method_visibility=args.method_visibility,
        order=args.order,
        show_skips=getattr(args, 'show_skips', None),
        tie_break=args.tie_break,
    )
