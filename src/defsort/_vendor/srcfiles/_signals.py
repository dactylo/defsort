"""Hold Ctrl+C and termination signals until a write finishes."""

import contextlib
import signal
import sys
import threading
from collections.abc import Callable, Generator

if sys.platform == 'win32':
    _TERMINATION_SIGNALS = (signal.SIGBREAK,)
else:
    _TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _deliver(recorded: list[int], notice: str | None) -> None:
    """Deliver held signals once each, termination signals before Ctrl+C."""
    termination = [sig for sig in _TERMINATION_SIGNALS if sig in recorded]
    try:
        if termination and notice is not None:
            print(notice, file=sys.stderr, flush=True)
    finally:
        # A broken or closed stderr must not turn a pending termination into an I/O error.
        for sig in termination:
            signal.raise_signal(sig)
            # The default action ends the process; a handler installed meanwhile may return.
            raise SystemExit(128 + sig)
    if signal.SIGINT in recorded:
        signal.raise_signal(signal.SIGINT)


def _replaceable() -> list[signal.Signals]:
    """List the supported signals whose handler is still Python's default."""
    replaceable = [sig for sig in _TERMINATION_SIGNALS if signal.getsignal(sig) == signal.SIG_DFL]
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        replaceable.append(signal.SIGINT)
    return replaceable


@contextlib.contextmanager
def deferred_signals(notice: Callable[[BaseException], str | None]) -> Generator[None]:
    """Hold Ctrl+C and termination signals across a block, then deliver them.

    The held signals are SIGINT, SIGTERM, and SIGHUP on POSIX, and SIGINT and
    SIGBREAK on Windows. A handler is replaced only while it is the default
    one, and only in the main thread; elsewhere the block runs without
    deferral. On exit the previous handlers return and each held signal is
    delivered once, so a termination signal ends the process as it would have.

    Args:
        notice: Given the error leaving the block, returns a line to print on
            stderr before a held termination signal ends the process, or `None`
            to print nothing.

    Yields:
        Control to the block while the signals are held.

    Raises:
        KeyboardInterrupt: If a held SIGINT is delivered on exit, with any
            error leaving the block as its context.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    recorded: list[int] = []
    previous = {sig: signal.getsignal(sig) for sig in _replaceable()}
    message = None
    try:
        with contextlib.ExitStack() as restore:
            # Register every original before a setter can change it and then be interrupted.
            # ExitStack unwinds all callbacks even when one raises, restoring SIGINT last.
            for sig, handler in reversed(previous.items()):
                restore.callback(signal.signal, sig, handler)
            try:
                for sig in previous:
                    signal.signal(sig, lambda signum, _frame: recorded.append(signum))
                yield
            except BaseException as error:
                # Preserve the write failure notice before cleanup can raise another error.
                message = notice(error)
                raise
    finally:
        _deliver(recorded, message)
