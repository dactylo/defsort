import inspect
import sys
import tomllib
import warnings
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from defsort import DefsortConfigWarning
from defsort._config import (
    ResolvedConfig,
    _load_toml,
    _parse_section,
    _resolve_config_sourced,
    _warn_config,
    clear_config_cache,
    effective_config,
    render_config_warnings,
    resolve_config,
    resolve_config_at_root,
    resolve_config_sourced,
    resolve_runtime_config,
    warn_ineffective_policy,
)
from defsort._types import (
    EVALUATION_ORDER_VALUES,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    TIE_BREAK_VALUES,
    BannerMode,
    DefsortConfig,
    SortOverrides,
)
from tests._helpers import _ENV, decode_text, run_setup

_ALL_AXIS_VALUES = [
    (key, value, field)
    for key, field, values in (
        ('evaluation-order', 'evaluation_order', EVALUATION_ORDER_VALUES),
        ('method-bodies', 'method_bodies', METHOD_BODIES_VALUES),
        ('method-visibility', 'method_visibility', METHOD_VISIBILITY_VALUES),
        ('method-dunders', 'method_dunders', METHOD_DUNDERS_VALUES),
        ('main', 'main', MAIN_PLACEMENT_VALUES),
        ('tie-break', 'tie_break', TIE_BREAK_VALUES),
    )
    for value in values
]

_AXIS_KEY_FIELDS = sorted({(key, field) for key, _value, field in _ALL_AXIS_VALUES})

_INEFFECTIVE_CASES = [
    ('method-bodies', 'method-order = "alphabetical"\nmethod-bodies = "across-categories"'),
    (
        'tie-break',
        'definition-order = "alphabetical"\nmethod-order = "alphabetical"\ntie-break = "source"',
    ),
    ('method-visibility', 'method-order = "none"\nmethod-visibility = "public-first"'),
    ('method-dunders', 'method-order = "none"\nmethod-dunders = "after-regular"'),
    ('main', 'definition-order = "none"\nmain = "sorted"'),
]


class TestAllowOnFailure:
    def test_default_false(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "dependency"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.allow_on_failure is False

    def test_invalid_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nallow-on-failure = "yes"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='allow-on-failure'):
            cfg = resolve_runtime_config(tmp_path)
        assert cfg.allow_on_failure is False
        assert capsys.readouterr().err == ''

    def test_reads_false(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nallow-on-failure = false\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.allow_on_failure is False

    def test_reads_true(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nallow-on-failure = true\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.allow_on_failure is True


class TestBannerConfig:
    def test_default_is_barriers(self) -> None:
        cfg = DefsortConfig()
        assert cfg.banners == 'barriers'
        assert cfg.banner_pattern is None

    def test_effective_mode_override_preserves_config_pattern(self) -> None:
        cfg = ResolvedConfig(
            transform=DefsortConfig(
                banners='strip',
                banner_pattern=r'# Section',
            ),
        )
        result = effective_config(cfg, SortOverrides(banners='ignore'))
        assert result.transform.banners == 'ignore'
        assert result.transform.banner_pattern == r'# Section'

    def test_effective_pattern_override_wins(self) -> None:
        cfg = ResolvedConfig(transform=DefsortConfig(banner_pattern=r'# Config'))
        result = effective_config(cfg, SortOverrides(banner_pattern=r'# Library'))
        assert result.transform.banner_pattern == r'# Library'

    def test_invalid_mode_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbanners = "move"\n',
            encoding='utf-8',
        )
        with pytest.warns(DefsortConfigWarning, match=r'Invalid \[tool.defsort\] banners'):
            cfg = resolve_config(tmp_path)
        assert cfg.banners == 'barriers'
        assert capsys.readouterr().err == ''

    def test_invalid_pattern_is_validated_in_ignore_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            "[tool.defsort]\nbanners = 'ignore'\nbanner-pattern = '('\n",
            encoding='utf-8',
        )
        with pytest.warns(DefsortConfigWarning, match=r'Invalid \[tool.defsort\] banner-pattern'):
            cfg = resolve_config(tmp_path)
        assert cfg.banners == 'ignore'
        assert cfg.banner_pattern is None
        assert capsys.readouterr().err == ''

    def test_invalid_pattern_warns_and_uses_builtin_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            "[tool.defsort]\nbanner-pattern = '[unterminated'\n",
            encoding='utf-8',
        )
        with pytest.warns(DefsortConfigWarning, match=r'Invalid \[tool.defsort\] banner-pattern'):
            cfg = resolve_config(tmp_path)
        assert cfg.banner_pattern is None
        assert capsys.readouterr().err == ''

    def test_non_string_pattern_warns_and_uses_builtin_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbanner-pattern = 42\n',
            encoding='utf-8',
        )
        with pytest.warns(DefsortConfigWarning, match='expected string'):
            cfg = resolve_config(tmp_path)
        assert cfg.banner_pattern is None
        assert capsys.readouterr().err == ''

    def test_reads_valid_custom_pattern(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            "[tool.defsort]\nbanner-pattern = '^# Section: .+$'\n",
            encoding='utf-8',
        )
        assert resolve_config(tmp_path).banner_pattern == '^# Section: .+$'

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [('ignore', 'ignore'), ('strip', 'strip')],
    )
    def test_reads_valid_mode(self, tmp_path: Path, raw: str, expected: BannerMode) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            f'[tool.defsort]\nbanners = "{raw}"\n',
            encoding='utf-8',
        )
        assert resolve_config(tmp_path).banners == expected

    def test_unsafe_banner_pattern_warns_and_uses_builtin_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            "[tool.defsort]\nbanner-pattern = '^# (a+)+$'\n",
            encoding='utf-8',
        )
        with pytest.warns(DefsortConfigWarning, match=r'Invalid \[tool.defsort\] banner-pattern'):
            cfg = resolve_config(tmp_path)
        assert cfg.banner_pattern is None
        assert capsys.readouterr().err == ''


class TestConfig:
    def test_explicit_incremental_false(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nincremental = false\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.incremental is False

    def test_kw_only_rejects_positional(self) -> None:
        with pytest.raises(TypeError, match='positional'):
            ResolvedConfig(None, None, 'dependency')  # pyright: ignore[reportCallIssue]

    def test_missing_incremental_returns_none(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "dependency"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.incremental is None


class TestConfigCache:
    def test_clear_config_cache_picks_up_edits(self, tmp_path: Path) -> None:
        clear_config_cache()
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8')
        assert resolve_config(tmp_path / 'mod.py').method_order == 'none'

        pyproject.write_text('[tool.defsort]\nmethod-order = "alphabetical"\n', encoding='utf-8')
        clear_config_cache()
        assert resolve_config(tmp_path / 'mod.py').method_order == 'alphabetical'

    def test_clear_config_cache_replays_warnings(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nbogus-key = 1\n', encoding='utf-8'
        )
        script = (
            'import sys\n'
            'import defsort\n'
            'defsort.resolve_config(sys.argv[1])\n'
            'defsort.clear_config_cache()\n'
            'defsort.resolve_config(sys.argv[1])\n'
        )
        proc = run_setup(
            [sys.executable, '-c', script, str(tmp_path)],
            env={**_ENV, 'PYTHONWARNINGS': 'default'},
        )
        assert decode_text(proc.stderr).count('Unknown [tool.defsort] key: bogus-key') == 2

    def test_distinct_nested_sources_load_separately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'nested'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "alphabetical"\n', encoding='utf-8'
        )

        original_load = _load_toml
        calls = 0

        def counting_load(path: Path) -> dict[str, object] | None:
            nonlocal calls
            calls += 1
            return original_load(path)

        monkeypatch.setattr('defsort._config._load_toml', counting_load)
        outer = resolve_config(tmp_path / 'outer.py')
        inner = resolve_config(nested / 'inner.py')
        assert calls == 2
        assert outer.method_order == 'none'
        assert inner.method_order == 'alphabetical'

    def test_invalid_shared_source_warns_once_per_cache_lifetime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text('not valid toml [', encoding='utf-8')
        first = tmp_path / 'first'
        second = tmp_path / 'second'
        first.mkdir()
        second.mkdir()

        original_load = _load_toml
        calls = 0

        def counting_load(path: Path) -> dict[str, object] | None:
            nonlocal calls
            calls += 1
            return original_load(path)

        monkeypatch.setattr('defsort._config._load_toml', counting_load)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', DefsortConfigWarning)
            assert resolve_config(first) == DefsortConfig()
            assert resolve_config(second) == DefsortConfig()
            assert calls == 1
            assert len(captured) == 1

            clear_config_cache()
            assert resolve_config(first) == DefsortConfig()
            assert resolve_config(second) == DefsortConfig()

        assert calls == 2
        assert len(captured) == 2
        assert all('Cannot read configuration' in str(item.message) for item in captured)

    def test_missing_source_skips_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / '.git').mkdir()
        first = tmp_path / 'first'
        second = tmp_path / 'second'
        first.mkdir()
        second.mkdir()

        original_load = _load_toml
        calls = 0

        def counting_load(path: Path) -> dict[str, object] | None:
            nonlocal calls
            calls += 1
            return original_load(path)

        monkeypatch.setattr('defsort._config._load_toml', counting_load)
        assert resolve_config(first) == DefsortConfig()
        assert resolve_config(second) == DefsortConfig()
        assert calls == 0

    def test_repeated_resolution_is_cached(self, tmp_path: Path) -> None:
        clear_config_cache()
        pyproject = tmp_path / 'pyproject.toml'
        pyproject.write_text('[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8')
        first = resolve_config(tmp_path / 'mod.py')
        assert first.method_order == 'none'

        pyproject.write_text('[tool.defsort]\nmethod-order = "alphabetical"\n', encoding='utf-8')
        assert resolve_config(tmp_path / 'mod.py').method_order == 'none'

    def test_starting_directories_share_governing_source_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        first = tmp_path / 'first'
        second = tmp_path / 'second' / 'nested'
        first.mkdir()
        second.mkdir(parents=True)

        original_load = _load_toml
        calls = 0

        def counting_load(path: Path) -> dict[str, object] | None:
            nonlocal calls
            calls += 1
            return original_load(path)

        monkeypatch.setattr('defsort._config._load_toml', counting_load)
        assert resolve_config(first).method_order == 'none'
        assert resolve_config_sourced(second)[0].transform.method_order == 'none'
        assert calls == 1


class TestConfigWarningRendering:
    def test_body_exception_remains_primary(self) -> None:

        def fail_body() -> None:
            warnings.warn('unrelated', UserWarning, stacklevel=1)
            raise RuntimeError('body failed')

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', UserWarning)
            with pytest.raises(RuntimeError, match='body failed'), render_config_warnings():
                fail_body()
        assert [str(item.message) for item in captured] == ['unrelated']

    def test_config_warning_ignores_outer_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DefsortConfigWarning)
            with render_config_warnings():
                _warn_config('invalid setting')
        assert capsys.readouterr().err == 'Warning: invalid setting\n'

    def test_config_warning_is_immediate(self, capsys: pytest.CaptureFixture[str]) -> None:
        with render_config_warnings():
            _warn_config('invalid setting')
            assert capsys.readouterr().err == 'Warning: invalid setting\n'

    def test_nested_exception_restores_outer_renderer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DefsortConfigWarning)
            with render_config_warnings():
                with pytest.raises(RuntimeError, match='inner failed'), render_config_warnings():
                    raise RuntimeError('inner failed')
                _warn_config('outer warning')
        assert capsys.readouterr().err == 'Warning: outer warning\n'

    def test_toml_warning_escapes_controls(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\n"bad\\u001b\\u0085\\n\\\\x1b" = true\n', encoding='utf-8'
        )
        with render_config_warnings():
            resolve_config_at_root(str(tmp_path))
        assert capsys.readouterr().err == (
            'Warning: Unknown [tool.defsort] key: bad\\x1b\\x85\\x0a\\x1b, ignoring\n'
        )

    def test_unrelated_warning_filter_still_raises(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('error', UserWarning)
            with render_config_warnings(), pytest.raises(UserWarning, match='unrelated'):
                warnings.warn('unrelated', UserWarning, stacklevel=1)

    def test_unrelated_warning_reaches_caller_immediately(self) -> None:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', UserWarning)
            with render_config_warnings():
                warnings.warn('unrelated', UserWarning, stacklevel=1)
                assert [str(item.message) for item in captured] == ['unrelated']


class TestEffectiveConfig:
    def test_all_none_returns_config_unchanged(self) -> None:
        cfg = ResolvedConfig()
        result = effective_config(cfg, SortOverrides())
        assert result == cfg

    def test_allow_on_failure_passes_through(self) -> None:
        cfg = ResolvedConfig(allow_on_failure=True)
        result = effective_config(cfg, SortOverrides())
        assert result.allow_on_failure is True

    def test_evaluation_order_override_wins(self) -> None:
        cfg = ResolvedConfig(transform=DefsortConfig(evaluation_order='preserve'))
        result = effective_config(
            cfg,
            SortOverrides(evaluation_order='allow-reordering'),
        )
        assert result.transform.evaluation_order == 'allow-reordering'

    def test_none_overrides_fall_through(self) -> None:
        cfg = ResolvedConfig(
            transform=DefsortConfig(
                definition_order='alphabetical',
                evaluation_order='allow-reordering',
                method_order='alphabetical',
            ),
            color='always',
            incremental=True,
        )
        result = effective_config(cfg, SortOverrides())
        assert result.color == 'always'
        assert result.incremental is True
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.evaluation_order == 'allow-reordering'
        assert result.transform.method_order == 'alphabetical'

    def test_order_override_does_not_replace_config_default(self) -> None:
        cfg = ResolvedConfig()
        result = effective_config(cfg, SortOverrides(order='alphabetical'))
        assert cfg.transform.definition_order == 'dependency'
        assert cfg.transform.method_order == 'dependency'
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.method_order == 'alphabetical'

    def test_override_incremental_wins(self) -> None:
        cfg = ResolvedConfig(incremental=False)
        result = effective_config(cfg, SortOverrides(incremental=True))
        assert result.incremental is True

    def test_override_show_skips_wins(self) -> None:
        cfg = ResolvedConfig(show_skips=True)
        result = effective_config(cfg, SortOverrides(show_skips=False))
        assert result.show_skips is False


class TestEffectiveConfigScopedOverrides:
    def test_cli_alpha_with_scoped_override(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        result = effective_config(
            cfg,
            SortOverrides(
                order='alphabetical',
                method_order='dependency',
            ),
        )
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.method_order == 'dependency'

    def test_cli_scoped_overrides_config_order(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        result = effective_config(
            cfg,
            SortOverrides(definition_order='dependency'),
        )
        assert result.transform.definition_order == 'dependency'
        assert result.transform.method_order == 'alphabetical'

    def test_config_scoped_not_clobbered_by_absent_cli_order(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\nmethod-order = "dependency"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        result = effective_config(cfg, SortOverrides())
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.method_order == 'dependency'


class TestIncrementalFallbackConfig:
    def test_default_is_warn(self) -> None:
        cfg = ResolvedConfig()
        assert cfg.incremental_fallback == 'warn'

    def test_effective_config_preserves_fallback(self) -> None:
        cfg = ResolvedConfig(incremental_fallback='full-sort')
        result = effective_config(cfg, SortOverrides())
        assert result.incremental_fallback == 'full-sort'

    def test_invalid_value_uses_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nincremental-fallback = "bogus"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='incremental-fallback'):
            cfg = resolve_runtime_config(tmp_path)
        assert cfg.incremental_fallback == 'warn'
        assert capsys.readouterr().err == ''

    def test_parse_full_sort(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nincremental-fallback = "full-sort"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.incremental_fallback == 'full-sort'

    def test_parse_warn(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nincremental-fallback = "warn"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.incremental_fallback == 'warn'


class TestInvalidUtf8Toml:
    def test_invalid_utf8_warns_and_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_bytes(b'[tool.defsort]\nba\xff\xfe = 1\n')
        clear_config_cache()
        with pytest.warns(DefsortConfigWarning, match='Cannot read configuration'):
            cfg = resolve_runtime_config(tmp_path / 'm.py')
        assert cfg == ResolvedConfig()


class TestLoadConfig:
    def test_boolean_color_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\ncolor = true\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        with pytest.warns(DefsortConfigWarning, match='color'):
            cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.color is None
        assert capsys.readouterr().err == ''

    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        cfg = resolve_config(tmp_path / 'nonexistent.py')
        assert cfg.definition_order == 'dependency'
        assert cfg.method_order == 'dependency'
        assert resolve_runtime_config(tmp_path / 'nonexistent.py').incremental is None

    def test_defaults_when_no_section(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text("[project]\nname = 'x'\n")
        cfg = resolve_config(tmp_path / 'foo.py')
        assert cfg.definition_order == 'dependency'
        assert cfg.method_order == 'dependency'
        assert resolve_runtime_config(tmp_path / 'foo.py').incremental is None

    def test_invalid_color_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\ncolor = "rainbow"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        with pytest.warns(DefsortConfigWarning, match='rainbow'):
            cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.color is None
        assert capsys.readouterr().err == ''

    def test_invalid_incremental_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nincremental = "yes"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        with pytest.warns(DefsortConfigWarning, match='yes'):
            cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.incremental is None
        assert capsys.readouterr().err == ''

    def test_invalid_order_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\norder = "random"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        with pytest.warns(DefsortConfigWarning, match='random'):
            cfg = resolve_config(tmp_path / 'foo.py')
        assert cfg.definition_order == 'dependency'
        assert cfg.method_order == 'dependency'
        assert capsys.readouterr().err == ''

    def test_invalid_show_skips_warns_and_defaults(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nshow-skips = "no"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        with pytest.warns(DefsortConfigWarning, match='show-skips'):
            cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.show_skips is True
        assert capsys.readouterr().err == ''

    def test_order_both_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        toml = '[tool.defsort]\norder = "both"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match="'both'"):
            cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'dependency'
        assert cfg.method_order == 'dependency'
        assert capsys.readouterr().err == ''

    def test_reads_color(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\ncolor = "never"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.color == 'never'

    def test_reads_incremental(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nincremental = true\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.incremental is True

    def test_reads_order(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_config(tmp_path / 'foo.py')
        assert cfg.definition_order == 'alphabetical'
        assert cfg.method_order == 'alphabetical'

    def test_reads_show_skips_false(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nshow-skips = false\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.show_skips is False

    def test_reads_show_skips_true(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nshow-skips = true\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_runtime_config(tmp_path / 'foo.py')
        assert cfg.show_skips is True

    def test_upward_traversal(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        subdir = tmp_path / 'src' / 'pkg'
        subdir.mkdir(parents=True)
        (subdir / 'mod.py').write_text('')
        cfg = resolve_config(subdir / 'mod.py')
        assert cfg.definition_order == 'alphabetical'
        assert cfg.method_order == 'alphabetical'


class TestMaxReorderConfig:
    def test_boolean_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        toml = '[tool.defsort]\nmax-reorder-count = true\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='max-reorder-count'):
            cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_count is None
        assert capsys.readouterr().err == ''

    def test_default_is_none(self) -> None:
        cfg = ResolvedConfig()
        assert cfg.max_reorder_count is None
        assert cfg.max_reorder_percent is None

    def test_effective_config_preserves(self) -> None:
        cfg = ResolvedConfig(max_reorder_count=5, max_reorder_percent=50)
        result = effective_config(cfg, SortOverrides())
        assert result.max_reorder_count == 5
        assert result.max_reorder_percent == 50

    def test_negative_count_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nmax-reorder-count = -1\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='max-reorder-count'):
            cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_count is None
        assert capsys.readouterr().err == ''

    def test_parse_count(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nmax-reorder-count = 5\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_count == 5

    def test_parse_percent(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nmax-reorder-percent = 50\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_percent == 50

    def test_percent_out_of_range(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\nmax-reorder-percent = 101\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='max-reorder-percent'):
            cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_percent is None
        assert capsys.readouterr().err == ''

    def test_zero_is_valid(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nmax-reorder-count = 0\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_runtime_config(tmp_path)
        assert cfg.max_reorder_count == 0


class TestNearestFileWins:
    def test_nested_malformed_yields_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text('this is not = valid = toml\n', encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='Cannot read configuration'):
            cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'dependency'

    def test_nested_with_empty_section_yields_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text('[tool.defsort]\n', encoding='utf-8')
        cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'dependency'

    def test_nested_with_non_table_tool_yields_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text('tool = "x"\n', encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match=r'Invalid \[tool\] table'):
            cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'dependency'

    def test_nested_with_section_wins(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "alphabetical"\n', encoding='utf-8'
        )
        cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'alphabetical'

    def test_nested_without_section_yields_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        (nested / 'pyproject.toml').write_text('[project]\nname = "pkg"\n', encoding='utf-8')
        cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'dependency'

    def test_parent_applies_when_no_nearer_file(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text(
            '[tool.defsort]\nmethod-order = "none"\n', encoding='utf-8'
        )
        nested = tmp_path / 'pkg'
        nested.mkdir()
        cfg = resolve_config(nested / 'mod.py')
        assert cfg.method_order == 'none'


class TestResolutionDiagnostics:
    def test_cached_resolution_warns_once_until_cache_clear(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nbanners = "bogus"\n')
        with pytest.warns(DefsortConfigWarning):
            resolve_config(tmp_path)
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            resolve_config(tmp_path)
        clear_config_cache()
        with pytest.warns(DefsortConfigWarning):
            resolve_config(tmp_path)

    def test_malformed_toml_warns_and_defaults(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[invalid toml <<<')
        with pytest.warns(DefsortConfigWarning, match='Cannot read configuration'):
            cfg = resolve_config(tmp_path)
        assert cfg == DefsortConfig()

    def test_missing_file_is_silent(self, tmp_path: Path) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            assert resolve_config(tmp_path) == DefsortConfig()

    def test_missing_section_is_silent(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.other]\nx = 1\n')
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            assert resolve_config(tmp_path) == DefsortConfig()

    def test_non_table_defsort_section_warns(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool]\ndefsort = 3\n')
        with pytest.warns(DefsortConfigWarning, match=r'\[tool.defsort\] section'):
            assert resolve_config(tmp_path) == DefsortConfig()

    def test_non_table_tool_key_at_root_warns_without_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / 'pyproject.toml').write_text('tool = 3\n')
        with pytest.warns(DefsortConfigWarning, match=r'\[tool\] table'):
            cfg = resolve_config_at_root(str(tmp_path))
        assert cfg == ResolvedConfig()
        assert capsys.readouterr().err == ''

    def test_non_table_tool_key_warns(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('tool = 3\n')
        with pytest.warns(DefsortConfigWarning, match=r'\[tool\] table'):
            assert resolve_config(tmp_path) == DefsortConfig()

    def test_unknown_key_warns_and_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\nbogus-key = 1\n')
        with pytest.warns(DefsortConfigWarning, match='Unknown .* bogus-key'):
            assert resolve_config(tmp_path) == DefsortConfig()

    def test_unreadable_file_warns_and_defaults(self, tmp_path: Path) -> None:
        target = tmp_path / 'pyproject.toml'
        target.write_text('[tool.defsort]\n')
        with (
            patch.object(Path, 'open', side_effect=PermissionError('read denied')),
            pytest.warns(DefsortConfigWarning, match='Cannot read configuration.*read denied'),
        ):
            cfg = resolve_config(tmp_path)
        assert cfg == DefsortConfig()


class TestResolveConfigContract:
    def test_accepts_pathlike_directory_and_str_file(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
        mod = tmp_path / 'mod.py'
        mod.write_text('x = 1\n')
        assert resolve_config(tmp_path).definition_order == 'alphabetical'
        assert resolve_config(str(mod)).definition_order == 'alphabetical'

    def test_accepts_str_input(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
        cfg = resolve_config(str(tmp_path))
        assert cfg.definition_order == 'alphabetical'

    def test_parameter_is_named_path(self) -> None:
        assert list(inspect.signature(resolve_config).parameters) == ['path']

    def test_returns_transformation_config_only(self, tmp_path: Path) -> None:
        cfg = resolve_config(tmp_path)
        assert isinstance(cfg, DefsortConfig)
        assert not hasattr(cfg, 'color')

    def test_starts_at_existing_directory(self, tmp_path: Path) -> None:
        sub = tmp_path / 'sub'
        sub.mkdir()
        (sub / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
        assert resolve_config(sub).definition_order == 'alphabetical'

    def test_starts_at_parent_for_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
        mod = tmp_path / 'mod.py'
        mod.write_text('x = 1\n')
        assert resolve_config(mod).definition_order == 'alphabetical'

    def test_starts_at_parent_for_nonexistent_path(self, tmp_path: Path) -> None:
        (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\norder = "alphabetical"\n')
        missing = tmp_path / 'not-yet' / 'new_file.py'
        assert resolve_config(missing).definition_order == 'alphabetical'
        assert resolve_config(str(missing)).definition_order == 'alphabetical'


class TestScopedEffectiveConfig:
    def test_cli_alpha_sets_both_scopes(self) -> None:
        cfg = ResolvedConfig()
        result = effective_config(cfg, SortOverrides(order='alphabetical'))
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.method_order == 'alphabetical'

    def test_cli_scoped_overrides_alpha(self) -> None:
        cfg = ResolvedConfig()
        result = effective_config(
            cfg,
            SortOverrides(
                order='alphabetical',
                method_order='dependency',
            ),
        )
        assert result.transform.definition_order == 'alphabetical'
        assert result.transform.method_order == 'dependency'

    def test_cli_scoped_overrides_config(self) -> None:
        cfg = ResolvedConfig(
            transform=DefsortConfig(
                definition_order='alphabetical',
                method_order='alphabetical',
            ),
        )
        result = effective_config(
            cfg,
            SortOverrides(definition_order='dependency'),
        )
        assert result.transform.definition_order == 'dependency'
        assert result.transform.method_order == 'alphabetical'

    def test_no_overrides_preserves_config(self) -> None:
        cfg = ResolvedConfig(
            transform=DefsortConfig(
                definition_order='none',
                method_order='alphabetical',
            ),
        )
        result = effective_config(cfg, SortOverrides())
        assert result.transform.definition_order == 'none'
        assert result.transform.method_order == 'alphabetical'


class TestScopedOrderConfig:
    def test_definition_order_parsed(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\ndefinition-order = "alphabetical"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'alphabetical'
        assert cfg.method_order == 'dependency'

    def test_invalid_scoped_order_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml = '[tool.defsort]\ndefinition-order = "random"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        with pytest.warns(DefsortConfigWarning, match='random'):
            cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'dependency'
        assert capsys.readouterr().err == ''

    def test_method_order_parsed(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\nmethod-order = "none"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'dependency'
        assert cfg.method_order == 'none'

    def test_none_is_valid_for_order_key(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "none"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'none'
        assert cfg.method_order == 'none'

    def test_order_alias_sets_both_scopes_with_scoped_precedence(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\nmethod-order = "none"\n'
        (tmp_path / 'pyproject.toml').write_text(toml)
        cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'alphabetical'
        assert cfg.method_order == 'none'

    def test_scoped_keys_override_order(self, tmp_path: Path) -> None:
        toml = '[tool.defsort]\norder = "alphabetical"\nmethod-order = "dependency"\n'
        (tmp_path / 'pyproject.toml').write_text(toml, encoding='utf-8')
        cfg = resolve_config(tmp_path)
        assert cfg.definition_order == 'alphabetical'
        assert cfg.method_order == 'dependency'


def _parse_for(body: str) -> ResolvedConfig:
    data = tomllib.loads(f'[tool.defsort]\n{body}')
    return _parse_section(data['tool']['defsort'])


@pytest.fixture(autouse=True)  # noqa: V103
def isolate_config_cache() -> Iterator[None]:
    clear_config_cache()
    yield
    clear_config_cache()


def test_axis_keys_parse() -> None:
    cfg = _parse_for('method-bodies = "across-categories"\nmain = "sorted"\ntie-break = "source"')
    assert cfg.transform.method_bodies == 'across-categories'
    assert cfg.transform.main == 'sorted'
    assert cfg.transform.tie_break == 'source'


def test_axis_override_beats_config() -> None:
    cfg = _parse_for('tie-break = "source"')
    effective = effective_config(cfg, SortOverrides(tie_break='alphabetical'))
    assert effective.transform.tie_break == 'alphabetical'


def test_clear_banner_pattern_override() -> None:
    cfg = _parse_for('banner-pattern = "^# ==="')
    effective = effective_config(cfg, SortOverrides(clear_banner_pattern=True))
    assert effective.transform.banner_pattern is None


def test_clear_then_explicit_banner_pattern_override() -> None:
    cfg = _parse_for('banner-pattern = "^# ==="')
    effective = effective_config(
        cfg,
        SortOverrides(clear_banner_pattern=True, banner_pattern='^# Replacement'),
    )
    assert effective.transform.banner_pattern == '^# Replacement'


def test_default_values_never_warn(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        resolve_config(tmp_path)


@pytest.mark.parametrize(('key', 'value', 'field'), _ALL_AXIS_VALUES)
def test_every_axis_value_parses(key: str, value: str, field: str) -> None:
    cfg = _parse_for(f'{key} = "{value}"')
    assert getattr(cfg.transform, field) == value


@pytest.mark.parametrize(
    ('key', 'value', 'field'),
    [
        ('color', 'auto', 'color'),
        ('color', 'always', 'color'),
        ('color', 'never', 'color'),
        ('incremental-fallback', 'warn', 'incremental_fallback'),
        ('incremental-fallback', 'full-sort', 'incremental_fallback'),
    ],
)
def test_every_runtime_vocabulary_value_parses(key: str, value: str, field: str) -> None:
    cfg = _parse_for(f'{key} = "{value}"')
    assert getattr(cfg, field) == value


def test_invalid_method_bodies_lists_every_valid_value() -> None:
    with pytest.warns(DefsortConfigWarning) as captured:
        cfg = _parse_for('method-bodies = "bogus"')
    assert cfg.transform.method_bodies == 'within-categories'
    assert str(captured[0].message) == (
        "Invalid [tool.defsort] method-bodies: 'bogus' "
        "(expected 'within-categories', 'ignore', or 'across-categories'), "
        'using default'
    )


def test_order_accepts_none() -> None:
    cfg = _parse_for('order = "none"')
    assert cfg.transform.definition_order == 'none'
    assert cfg.transform.method_order == 'none'


def test_order_override_leaves_axis_config_intact() -> None:
    cfg = _parse_for('method-dunders = "after-regular"')
    effective = effective_config(cfg, SortOverrides(order='alphabetical'))
    assert effective.transform.method_dunders == 'after-regular'
    assert effective.transform.definition_order == 'alphabetical'
    assert effective.transform.method_order == 'alphabetical'


@pytest.mark.parametrize(
    ('key', 'value', 'field', 'default'),
    [
        ('color', 'full-sort', 'color', None),
        ('incremental-fallback', 'never', 'incremental_fallback', 'warn'),
    ],
)
def test_runtime_vocabularies_remain_independent(
    key: str, value: str, field: str, default: str | None
) -> None:
    with pytest.warns(DefsortConfigWarning, match=key):
        cfg = _parse_for(f'{key} = "{value}"')
    assert getattr(cfg, field) == default


@pytest.mark.parametrize(('key', 'field'), _AXIS_KEY_FIELDS)
def test_invalid_axis_value_warns_and_defaults(key: str, field: str) -> None:
    with pytest.warns(DefsortConfigWarning, match=key):
        cfg = _parse_for(f'{key} = "bogus"')
    assert getattr(cfg.transform, field) == getattr(DefsortConfig(), field)


@pytest.mark.parametrize(('key', 'body'), _INEFFECTIVE_CASES)
def test_nondefault_ineffective_value_warns(
    key: str, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_ineffective_policy(_parse_for(body).transform)
    assert key in capsys.readouterr().err


def test_sourced_resolver_cleared_with_cache(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n', encoding='utf-8')
    assert _resolve_config_sourced(tmp_path)[1] is None
    (tmp_path / 'pyproject.toml').write_text('[tool.defsort]\norder = "none"\n', encoding='utf-8')
    clear_config_cache()
    assert _resolve_config_sourced(tmp_path)[1] == tmp_path / 'pyproject.toml'


def test_sourced_resolver_returns_path_for_config(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.defsort]\norder = "alphabetical"\n', encoding='utf-8'
    )
    cfg, source = _resolve_config_sourced(tmp_path)
    assert cfg.transform.definition_order == 'alphabetical'
    assert source == tmp_path / 'pyproject.toml'


def test_sourced_resolver_sectionless_and_malformed_report_no_source(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n', encoding='utf-8')
    assert _resolve_config_sourced(tmp_path)[1] is None
    clear_config_cache()
    (tmp_path / 'pyproject.toml').write_text('not toml [', encoding='utf-8')
    with pytest.warns(DefsortConfigWarning, match='Cannot read configuration'):
        assert _resolve_config_sourced(tmp_path)[1] is None
    clear_config_cache()
    (tmp_path / 'pyproject.toml').write_text('[tool]\ndefsort = "x"\n', encoding='utf-8')
    with pytest.warns(DefsortConfigWarning, match=r'\[tool.defsort\] section'):
        assert _resolve_config_sourced(tmp_path)[1] is None
