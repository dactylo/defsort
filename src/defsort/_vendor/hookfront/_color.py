"""Select ANSI color and render colored CLI output."""

import os
from collections.abc import Iterable
from typing import Literal, TextIO

BOLD = '\033[1m'
GREEN = '\033[32m'
RED = '\033[31m'
RESET = '\033[0m'

_ColorMode = Literal['auto', 'always', 'never']


def colorize(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes and a reset when codes are supplied.

    Args:
        text: Text to display.
        *codes: ANSI escape code strings to prepend.

    Returns:
        Colored text, or the original text when no codes are given.
    """
    if not codes:
        return text
    return ''.join(codes) + text + RESET


def project_color(values: Iterable[_ColorMode | None]) -> _ColorMode | None:
    """Return a shared color setting, or `None` when values differ or are absent.

    Args:
        values: Color settings resolved for collected files.
    """
    distinct = set(values)
    if len(distinct) == 1:
        return distinct.pop()
    return None


def should_color(
    stream: TextIO,
    *,
    cli_flag: _ColorMode | None = None,
    config_value: _ColorMode | None = None,
) -> bool:
    """Determine whether to colorize output for a stream.

    CLI `always` or `never` takes precedence, followed by non-empty `NO_COLOR`,
    explicit CLI `auto`, project configuration, and the stream's terminal
    status.

    Args:
        stream: Output stream, checked with `isatty` in automatic mode.
        cli_flag: CLI `--color` value, or `None` when unspecified.
        config_value: Shared color setting resolved for collected files.

    Returns:
        `True` when output should be colored.
    """
    if cli_flag == 'always':
        return True
    if cli_flag == 'never':
        return False
    if os.environ.get('NO_COLOR'):
        return False
    if cli_flag == 'auto':
        return hasattr(stream, 'isatty') and stream.isatty()
    if config_value in ('always', 'never'):
        return config_value == 'always'
    return hasattr(stream, 'isatty') and stream.isatty()
