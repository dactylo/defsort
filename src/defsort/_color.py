"""ANSI color support for CLI output."""

import os
from collections.abc import Collection
from typing import TextIO

from defsort._types import ColorMode

_BLUE = '\033[34m'
BOLD = '\033[1m'
_CYAN = '\033[36m'
GREEN = '\033[32m'
_MAGENTA = '\033[35m'
RED = '\033[31m'
_RESET = '\033[0m'
YELLOW = '\033[33m'

_SPECTRUM = [RED, YELLOW, GREEN, _CYAN, _BLUE, _MAGENTA]


def _spectral_color(position: int, total: int) -> str:
    """Return the ANSI color code for a position in a spectral gradient."""
    return _SPECTRUM[position * len(_SPECTRUM) // total]


def colorize(text: str, *codes: str) -> str:
    """Apply ANSI color codes to text.

    Args:
        text: The text to colorize.
        *codes: ANSI escape code strings to prepend.

    Returns:
        Text wrapped in ANSI codes with a trailing reset, or the original text
        if no codes are given.
    """
    if not codes:
        return text
    return ''.join(codes) + text + _RESET


def should_color(
    stream: TextIO,
    *,
    cli_flag: ColorMode | None = None,
    config_value: ColorMode | None = None,
) -> bool:
    """Determine whether to colorize output for the given stream.

    Precedence: CLI always/never > NO_COLOR > explicit CLI auto > config value
    > auto default.

    Args:
        stream: The output stream (checked for isatty in auto mode).
        cli_flag: CLI --color flag value, or None if not specified.
        config_value: [tool.defsort].color config value, or None.

    Returns:
        True if output should be colored.
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


def spectral_map(indices: Collection[int]) -> dict[int, str]:
    """Map definition indices to spectral colors by sorted position.

    Args:
        indices: Definition .index values in Proposed (sorted) order.

    Returns:
        Dict mapping each .index to its ANSI color code.
    """
    total = len(indices)
    if total == 0:
        return {}
    return {idx: _spectral_color(pos, total) for pos, idx in enumerate(indices)}
