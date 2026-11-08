"""Suggest a registered host when a name is close to one."""

from collections.abc import Iterable


def closest(word: str, candidates: Iterable[str]) -> str | None:
    """Return the closest candidate, or `None` when none is close enough.

    Args:
        word: Host name to match.
        candidates: Registered host names to consider.
    """
    # An eager import costs about 1.2 ms per hook run.
    import difflib  # noqa: PLC0415

    matches = difflib.get_close_matches(word, list(candidates), n=1)
    return matches[0] if matches else None
