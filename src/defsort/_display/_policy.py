"""Resolved policy display formatting."""

from pathlib import Path

from defsort._config import ResolvedConfig
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import sort_policy_from_config


def format_policy_block(
    cfg: ResolvedConfig,
    source: Path | None,
    *,
    has_overrides: bool,
) -> str:
    """Format one resolved policy and its provenance.

    Args:
        cfg: Effective configuration to display.
        source: Config path, or `None` for built-in defaults.
        has_overrides: Whether CLI options altered the policy.

    Returns:
        Terminal-safe policy description.
    """
    policy = sort_policy_from_config(cfg.transform)
    header = (
        'built-in defaults'
        if source is None
        else f'policy from {escape_terminal_text(path_text(source))}'
    )
    if has_overrides:
        header += ' (with CLI overrides)'
    if policy.method_dunders == 'before-regular':
        categories = '__new__, __init__, dunders, regular'
    else:
        categories = '__new__, __init__, regular, dunders'
    body_dependencies = {
        'across-categories': 'body dependencies across categories',
        'ignore': 'body dependencies ignored',
        'within-categories': 'body dependencies within categories',
    }[policy.method_bodies]
    return (
        f'{header}:\n'
        f'  definitions: {policy.definition_order}; main {policy.main}; '
        f'ties {policy.tie_break}\n'
        f'  methods: {policy.method_order}; categories {categories} '
        f'({policy.method_visibility});\n'
        f'           {body_dependencies}; ties {policy.tie_break}\n'
        f'  evaluation order: {policy.evaluation_order}\n'
        f'  banners: {policy.banners}'
    )
