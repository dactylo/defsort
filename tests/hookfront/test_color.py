import io
import sys

import pytest

from defsort._vendor.hookfront._color import (
    BOLD,
    GREEN,
    RED,
    RESET,
    colorize,
    project_color,
    should_color,
)


class TestColorize:
    def test_empty_string(self) -> None:
        result = colorize('', GREEN)
        assert result == f'{GREEN}{RESET}'

    def test_multiple_codes(self) -> None:
        result = colorize('hello', BOLD, GREEN)
        assert result == f'{BOLD}{GREEN}hello{RESET}'

    def test_no_codes_returns_text(self) -> None:
        assert colorize('hello') == 'hello'

    def test_single_code(self) -> None:
        result = colorize('hello', RED)
        assert result == f'{RED}hello{RESET}'


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


def test_project_color_is_none_when_values_disagree() -> None:
    assert project_color(['always', None]) is None


def test_project_color_is_none_without_values() -> None:
    assert project_color([]) is None


def test_project_color_is_the_single_value() -> None:
    assert project_color(['always', 'always']) == 'always'
