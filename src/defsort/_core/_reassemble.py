"""Exact source reassembly from positional layout and movable blocks."""

from collections.abc import Collection, Mapping

from defsort._core._layout import SourceLayout, permute_separators, render_layout
from defsort._types import Definition


def reassemble_source(
    layout: SourceLayout,
    blocks: Mapping[int, str],
    definitions: Collection[Definition],
) -> str:
    """Render definitions in the requested positional layout.

    Args:
        layout: Exact fixed source around the definition slots.
        blocks: Movable source keyed by definition index.
        definitions: Definition metadata in the desired order.

    Returns:
        Exact source with the requested block permutation.

    Raises:
        LayoutPreservationError: If the requested order cannot preserve layout
            and comment ownership.
        ValueError: If the layout, blocks, and definition identities violate an
            internal invariant.
    """
    order = [definition.index for definition in definitions]
    by_index = {definition.index: definition for definition in definitions}
    pinned: dict[int, int] = {}
    for position in range(len(order) - 1):
        left = by_index[order[position]]
        right = by_index[order[position + 1]]
        if (
            right.index == left.index + 1
            and right.name == left.name
            and not left.had_interstitial_code
        ):
            pinned[position] = left.index
    return render_layout(permute_separators(layout, order, pinned), blocks, order)
