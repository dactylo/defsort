"""Display-cell width measurement for terminal alignment."""

import unicodedata

_WIDE_EAST_ASIAN = frozenset({'F', 'W'})


def display_width(text: str) -> int:
    """Return the terminal cell count for text.

    East Asian wide (`W`) and fullwidth (`F`) characters count as two cells;
    every other character counts as one. Grapheme clusters, combining marks,
    emoji sequences, and terminal-specific behavior are not modeled.

    Args:
        text: Text to measure.

    Returns:
        The display cell count.
    """
    return sum(2 if unicodedata.east_asian_width(char) in _WIDE_EAST_ASIAN else 1 for char in text)


def pad_cells(text: str, width: int) -> str:
    """Left-align text within a display-cell width.

    Args:
        text: Text to pad.
        width: Target width in display cells.

    Returns:
        The text followed by the spaces needed to reach `width` cells.
    """
    return text + ' ' * max(0, width - display_width(text))
