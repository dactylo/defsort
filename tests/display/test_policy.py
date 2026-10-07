from pathlib import Path

from defsort._config import ResolvedConfig
from defsort._display._policy import format_policy_block
from defsort._types import DefsortConfig


def test_builtin_policy_block_reports_resolved_defaults() -> None:
    rendered = format_policy_block(ResolvedConfig(), None, has_overrides=False)

    assert rendered == (
        'built-in defaults:\n'
        '  definitions: dependency; main last; ties alphabetical\n'
        '  methods: dependency; categories __new__, __init__, dunders, regular (mixed);\n'
        '           body dependencies within categories; ties alphabetical\n'
        '  evaluation order: preserve\n'
        '  banners: barriers'
    )


def test_policy_block_escapes_source_and_shows_override(tmp_path: Path) -> None:
    source = tmp_path / 'bad\x1b]2;path\x07.toml'
    config = ResolvedConfig(
        transform=DefsortConfig(method_dunders='after-regular', method_bodies='ignore')
    )

    rendered = format_policy_block(config, source, has_overrides=True)

    assert r'bad\x1b]2;path\x07.toml (with CLI overrides):' in rendered
    assert '\x1b' not in rendered
    assert 'categories __new__, __init__, regular, dunders' in rendered
    assert 'body dependencies ignored' in rendered
