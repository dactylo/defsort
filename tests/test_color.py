import io
import sys

import pytest

from defsort._color import (
    _BLUE,
    _CYAN,
    _MAGENTA,
    _RESET,
    BOLD,
    GREEN,
    RED,
    YELLOW,
    _spectral_color,
    colorize,
    should_color,
    spectral_map,
)


class TestColorize:
    def test_empty_string(self) -> None:
        result = colorize('', GREEN)
        assert result == f'{GREEN}{_RESET}'

    def test_multiple_codes(self) -> None:
        result = colorize('hello', BOLD, GREEN)
        assert result == f'{BOLD}{GREEN}hello{_RESET}'

    def test_no_codes_returns_text(self) -> None:
        assert colorize('hello') == 'hello'

    def test_single_code(self) -> None:
        result = colorize('hello', RED)
        assert result == f'{RED}hello{_RESET}'


class TestExplicitAutoPrecedence:
    def test_explicit_auto_overrides_config_always(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()
        assert not should_color(stream, cli_flag='auto', config_value='always')

    def test_no_color_beats_explicit_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()
        stream.isatty = lambda: True
        assert should_color(stream, cli_flag='auto', config_value='always')

        monkeypatch.setenv('NO_COLOR', '1')
        assert not should_color(stream, cli_flag='auto', config_value='always')


class TestShouldColor:
    def test_always_overrides_no_color_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('NO_COLOR', '1')
        assert should_color(io.StringIO(), cli_flag='always') is True

    def test_always_returns_true(self) -> None:
        assert should_color(io.StringIO(), cli_flag='always') is True

    def test_auto_non_tty_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        assert should_color(io.StringIO()) is False

    def test_auto_tty_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        tty = io.StringIO()
        tty.isatty = lambda: True
        assert should_color(tty) is True

    def test_auto_with_no_color_env_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        tty = io.StringIO()
        tty.isatty = lambda: True
        assert should_color(tty) is True

        monkeypatch.setenv('NO_COLOR', '1')
        assert should_color(tty) is False

    def test_never_returns_false(self) -> None:
        assert should_color(sys.stdout, cli_flag='never') is False

    def test_no_color_empty_string_does_not_disable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv('NO_COLOR', '')
        tty = io.StringIO()
        tty.isatty = lambda: True
        assert should_color(tty) is True


class TestShouldColorConfig:
    def test_cli_flag_overrides_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()
        assert should_color(stream, cli_flag='never', config_value='always') is False
        assert should_color(stream, cli_flag='always', config_value='never') is True

    def test_config_always_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()  # not a tty
        assert should_color(stream, config_value='always') is True

    def test_config_never_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()
        assert should_color(stream, config_value='never') is False

    def test_empty_no_color_does_not_override_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv('NO_COLOR', '')
        stream = io.StringIO()
        assert should_color(stream, config_value='always') is True

    def test_no_color_beats_config_always(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()
        stream.isatty = lambda: True
        assert should_color(stream, config_value='always')

        monkeypatch.setenv('NO_COLOR', '1')
        assert not should_color(stream, config_value='always')

    def test_omitted_cli_flag_falls_through_to_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv('NO_COLOR', raising=False)
        stream = io.StringIO()  # not a tty
        assert should_color(stream, cli_flag=None, config_value='always') is True
        assert should_color(stream, cli_flag=None, config_value='never') is False


class TestSpectralColor:
    def test_first_position_is_red(self) -> None:
        assert _spectral_color(0, 6) == RED

    def test_last_position_large_total(self) -> None:
        assert _spectral_color(11, 12) == _MAGENTA

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

    def test_six_elements_full_spectrum(self) -> None:
        result = spectral_map([10, 20, 30, 40, 50, 60])
        assert result[10] == RED
        assert result[20] == YELLOW
        assert result[30] == GREEN
        assert result[40] == _CYAN
        assert result[50] == _BLUE
        assert result[60] == _MAGENTA
