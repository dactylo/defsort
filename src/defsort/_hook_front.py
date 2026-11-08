"""Bind defsort options and engine calls to the shared hook front."""

from functools import partial

from defsort._arguments import add_ordering_arguments, overrides_from_args
from defsort._types import EXIT_OK, FALLBACK_MODE_VALUES, SortOverrides
from defsort._vendor.hookfront._front import Engine, HookOptions, read_payload, run_hook
from defsort._vendor.hookfront._parser import HookParser, add_allow_on_failure
from defsort._vendor.hookfront._tool import ToolIdentity

_TOOL = ToolIdentity('defsort', 'sort')


def _build_hook_parser() -> HookParser:
    """Build the argument parser for the hook subcommand."""
    parser = HookParser(_TOOL)
    incremental = parser.add_mutually_exclusive_group()
    incremental.add_argument(
        '-i',
        '--incremental',
        action='store_true',
        help='sort only definitions added since the last commit',
    )
    incremental.add_argument(
        '--no-incremental',
        action='store_true',
        help='sort every definition, even when configuration enables incremental mode',
    )
    parser.add_argument(
        '--incremental-fallback',
        choices=FALLBACK_MODE_VALUES,
        help='what to do when incremental sorting is blocked (default: warn)',
    )
    add_allow_on_failure(
        parser, 'allow the command when defsort itself fails; safety blockers still deny'
    )
    add_ordering_arguments(parser)
    return parser


def _load_engine(overrides: SortOverrides | None) -> Engine:
    """Load the sorting engine and bind the requested configuration overrides."""
    from defsort._hook import run_commit, run_post_tool  # noqa: PLC0415

    return Engine(
        partial(run_commit, overrides=overrides),
        partial(run_post_tool, overrides=overrides),
    )


def _run_hook(
    stdin_data: str,
    *,
    host: str,
    event: str | None = None,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> int:
    """Run one hook invocation with defsort's identity and sorting engine."""
    return run_hook(
        stdin_data,
        tool=_TOOL,
        options=HookOptions(host, event, allow_on_failure),
        load_engine=partial(_load_engine, overrides),
    )


def run_hook_command(argv: list[str]) -> int:
    """Handle the hook subcommand.

    Args:
        argv: Hook arguments after the `hook` command name.

    Returns:
        The hook exit status, or 0 when standard input is unavailable or a
        terminal.

    Raises:
        SystemExit: After displaying help or rejecting command-line arguments.
    """
    args = _build_hook_parser().parse_args(argv)
    stdin_data = read_payload(_TOOL)
    if stdin_data is None:
        return EXIT_OK
    return _run_hook(
        stdin_data,
        host=args.agent,
        event=args.event,
        overrides=overrides_from_args(args),
        allow_on_failure=args.allow_on_failure,
    )
