from defsort._display._color import (
    _BLUE,
    _CYAN,
    _MAGENTA,
    YELLOW,
    _spectral_color,
    spectral_map,
)
from defsort._vendor.hookfront._color import GREEN, RED


class TestSpectralColor:
    def test_single_item(self) -> None:
        assert _spectral_color(0, 1) == RED

    def test_six_items_one_per_color(self) -> None:
        colors = [_spectral_color(i, 6) for i in range(6)]
        assert colors == [RED, YELLOW, GREEN, _CYAN, _BLUE, _MAGENTA]

    def test_small_total_does_not_reach_magenta(self) -> None:
        colors = [_spectral_color(i, 3) for i in range(3)]
        # 0*6//3=0 (RED), 1*6//3=2 (GREEN), 2*6//3=4 (_BLUE)
        assert colors == [RED, GREEN, _BLUE]

    def test_twelve_items_two_per_color(self) -> None:
        colors = [_spectral_color(i, 12) for i in range(12)]
        assert colors[0] == colors[1] == RED
        assert colors[2] == colors[3] == YELLOW
        assert colors[10] == colors[11] == _MAGENTA


class TestSpectralMap:
    def test_empty_sequence(self) -> None:
        assert spectral_map([]) == {}

    def test_maps_index_to_positional_color(self) -> None:
        result = spectral_map([3, 0, 2, 1])
        # 4 items: 0*6//4=0 (RED), 1*6//4=1 (YELLOW), 2*6//4=3 (_CYAN), 3*6//4=4 (_BLUE)
        assert result[3] == RED  # index 3 is at position 0
        assert result[0] == YELLOW  # index 0 is at position 1
        assert result[2] == _CYAN  # index 2 is at position 2
        assert result[1] == _BLUE  # index 1 is at position 3

    def test_single_element(self) -> None:
        result = spectral_map([5])
        assert result == {5: RED}
