"""Dispatch the console script and `python -m defsort`."""

import sys
from collections.abc import Sequence

from defsort._downstream import closable_stdout, ensure_utf8_stdio
from defsort._hook_front import run_hook_command
from defsort._types import EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Run the defsort command-line interface.

    Started without standard output, nothing can be delivered: one error line
    goes to standard error and the status is 2 before any other work. If a
    reader closes standard output early, the remaining output is discarded and
    the exit status reflects the work completed before the closure.

    Args:
        argv: Arguments to parse, or `None` to use `sys.argv`.

    Returns:
        The process exit status.
    """
    if sys.stdout is None:
        # Started with standard output closed: nothing can be delivered, and the
        # closed-stdout wrapper cannot wrap a missing stream.
        print('error: standard output is unavailable', file=sys.stderr)
        return EXIT_ERROR
    ensure_utf8_stdio()
    with closable_stdout():
        args = sys.argv[1:] if argv is None else list(argv)
        if args and args[0] == 'hook':
            return run_hook_command(args[1:])
        from defsort._cli import run_cli  # noqa: PLC0415

        return run_cli(args)
