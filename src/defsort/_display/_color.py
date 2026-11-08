"""ANSI colors and rendering helpers for display output."""

from collections.abc import Collection

from defsort._vendor.hookfront._color import GREEN, RED

_BLUE = '\033[34m'
_CYAN = '\033[36m'
_MAGENTA = '\033[35m'
YELLOW = '\033[33m'

_SPECTRUM = [RED, YELLOW, GREEN, _CYAN, _BLUE, _MAGENTA]


def _spectral_color(position: int, total: int) -> str:
    """Return the ANSI color code for a position in a spectral gradient."""
    return _SPECTRUM[position * len(_SPECTRUM) // total]


def spectral_map(indices: Collection[int]) -> dict[int, str]:
    """Map definition indices to spectral colors by sorted position.

    Args:
        indices: `Definition.index` values in proposed (sorted) order.

    Returns:
        Dict mapping each `.index` to its ANSI color code.
    """
    total = len(indices)
    if total == 0:
        return {}
    return {idx: _spectral_color(pos, total) for pos, idx in enumerate(indices)}
