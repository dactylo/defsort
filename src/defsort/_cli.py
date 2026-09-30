"""CLI for defsort."""

# pyright: reportPrivateUsage=false

import argparse
import ast
import contextlib
import enum
import os
import sys
import warnings
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from defsort._color import should_color
from defsort._config import (
    DefsortConfigWarning,
    ResolvedConfig,
    effective_config,
    resolve_config_sourced,
    resolve_runtime_config,
    warn_ineffective_policy,
)
from defsort._core import (
    detect_order,
    sort_file,
    sort_file_incremental,
)
from defsort._core._api import detect_definition_order
from defsort._core._full import count_top_level_methods
from defsort._core._guard import build_guard_params, evaluate_reorder_guard
from defsort._core._parse import parse_definitions
from defsort._display import (
    build_json_record,
    build_summary_row,
    format_banner_constraint_details,
    format_banner_constraint_summary,
    format_json,
    format_summary_rows,
    print_detect,
    print_diff,
    print_preview,
    print_skipped,
)
from defsort._display._skipped import format_class_blocker
from defsort._display._terminal import escape_terminal_text
from defsort._display._types import CliMode, JsonFileRecord, SummaryRow
from defsort._downstream import closable_stdout, stdout_closed
from defsort._files import (
    collect_files,
    ensure_utf8_stdio,
    read_file,
    read_file_result,
    write_source,
)
from defsort._git import (
    UnsupportedGitVersionError,
    batch_fetch_worktree_baselines,
    require_batch_z_support,
    try_get_repo_root,
)
from defsort._types import (
    BANNER_MODE_VALUES,
    EVALUATION_ORDER_VALUES,
    EXIT_CHANGED,
    EXIT_ERROR,
    EXIT_OK,
    MAIN_PLACEMENT_VALUES,
    METHOD_BODIES_VALUES,
    METHOD_DUNDERS_VALUES,
    METHOD_VISIBILITY_VALUES,
    SORT_SCOPE_ORDER_VALUES,
    TIE_BREAK_VALUES,
    BlobReadResult,
    BlobReadStatus,
    ColorMode,
    EngineOptions,
    OrderKind,
    SortOverrides,
    SortResult,
    sort_policy_from_config,
    validate_banner_pattern,
)

_BASELINE_UNAVAILABLE = 'incremental: unable to read baseline from git'


class _FileStatus(enum.Enum):
    """Classify one file's CLI processing outcome."""

    CHANGED = 'changed'
    ERROR = 'error'
    INVALID = 'invalid'
    SKIPPED = 'skipped'
    UNCHANGED = 'unchanged'


def _add_order_argument(parser: argparse.ArgumentParser) -> None:
    """Register the shared ordering-policy shorthand on a parser."""
    parser.add_argument(
        '-o',
        '--order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='Sort order for definitions and methods: dependency, alphabetical, none.',
    )


def _collect_policy_paths(raw_paths: list[str]) -> tuple[list[Path], bool]:
    """Expand policy paths while retaining empty directories."""
    paths: list[Path] = []
    seen: set[Path] = set()
    error_seen = False
    for raw in raw_paths:
        path = Path(raw)
        try:
            path.stat()
            if path.is_dir():
                with os.scandir(path):
                    pass
                candidates = collect_files([raw])
            else:
                with path.open('rb'):
                    pass
                candidates = [path]
        except OSError as exc:
            safe_path = escape_terminal_text(str(path))
            safe_detail = escape_terminal_text(str(exc))
            print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)
            error_seen = True
            continue
        if not candidates:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                paths.append(candidate)
    return paths, error_seen


def _compiled_regex_pattern(value: str) -> str:
    """Validate a regular expression while preserving its source string."""
    try:
        validate_banner_pattern(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _add_policy_arguments(
    parser: argparse.ArgumentParser,
    *,
    group_title: str | None = None,
) -> None:
    """Register detailed ordering-policy arguments on a parser."""
    arguments = parser if group_title is None else parser.add_argument_group(group_title)
    arguments.add_argument(
        '--definition-order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='Sort order for top-level definitions: dependency, alphabetical, none.',
    )
    arguments.add_argument(
        '--method-order',
        choices=SORT_SCOPE_ORDER_VALUES,
        help='Sort order for class methods: dependency, alphabetical, none.',
    )
    arguments.add_argument(
        '--evaluation-order',
        choices=EVALUATION_ORDER_VALUES,
        help='Preserve definition-time evaluation order or allow reordering.',
    )
    arguments.add_argument(
        '--method-bodies',
        choices=METHOD_BODIES_VALUES,
        help=(
            'Let method-body dependencies stay within categories, be ignored, or cross categories.'
        ),
    )
    arguments.add_argument(
        '--method-visibility',
        choices=METHOD_VISIBILITY_VALUES,
        help='Keep regular methods mixed or place public methods before private methods.',
    )
    arguments.add_argument(
        '--method-dunders',
        choices=METHOD_DUNDERS_VALUES,
        help='Place non-lifecycle dunders before or after regular methods.',
    )
    arguments.add_argument(
        '--main',
        choices=MAIN_PLACEMENT_VALUES,
        help='Anchor main() last or sort it normally.',
    )
    arguments.add_argument(
        '--tie-break',
        choices=TIE_BREAK_VALUES,
        help='Break dependency ties alphabetically or by source order.',
    )
    arguments.add_argument(
        '--banners',
        choices=BANNER_MODE_VALUES,
        help='Treat section banners as barriers (default), ignore them, or strip them.',
    )
    banner_pattern_group = arguments.add_mutually_exclusive_group()
    banner_pattern_group.add_argument(
        '--banner-pattern',
        type=_compiled_regex_pattern,
        help=('Add a safe full-match pattern to built-in banner recognition; overrides config.'),
    )
    banner_pattern_group.add_argument(
        '--no-banner-pattern',
        action='store_true',
        dest='clear_banner_pattern',
        help='Clear the configured custom banner pattern.',
    )


def _build_hook_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the hook subcommand."""
    parser = argparse.ArgumentParser(
        prog='defsort hook',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Agent hook entry point; reads the coding agent's payload as JSON from stdin.",
        allow_abbrev=False,
    )
    parser.add_argument('-a', '--agent', required=True, metavar='NAME')
    parser.add_argument('-e', '--event', metavar='NAME')
    _add_order_argument(parser)
    _add_policy_arguments(parser)
    parser.add_argument(
        '--allow-on-failure',
        action='store_true',
        help=(
            'Warn and allow infrastructure failures after a command is accepted; '
            'safety blockers still deny.'
        ),
    )
    parser.add_argument(
        '--incremental-fallback',
        choices=['warn', 'full-sort'],
        help='Fallback behavior when incremental sort is blocked.',
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-i', '--incremental', action='store_true')
    group.add_argument(
        '--no-incremental',
        action='store_true',
        help='Disable incremental mode, overriding configured incremental mode.',
    )
    return parser


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog='defsort',
        description='Sort Python definitions.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=('subcommands:\n  hook    Agent hook entry point (see defsort hook --help)'),
        allow_abbrev=False,
    )
    parser.add_argument('paths', nargs='*', default=['.'], help='Python files or directories.')
    parser.add_argument('--version', action='store_true', help='Show the installed version.')
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('-p', '--preview', action='store_true', help='Show movement tables.')
    mode_group.add_argument('-d', '--diff', action='store_true', help='Show unified diff.')
    mode_group.add_argument('-j', '--json', action='store_true', help='Output to JSON.')
    mode_group.add_argument('-w', '--write', action='store_true', help='Modify files in place.')
    _add_order_argument(parser)
    incremental_group = parser.add_mutually_exclusive_group()
    incremental_group.add_argument(
        '-i',
        '--incremental',
        action='store_true',
        help='Only sort new definitions (requires git).',
    )
    parser.add_argument(
        '-f',
        '--force',
        action='store_true',
        help='Bypass interstitial and reorder-guard blockers.',
    )
    parser.add_argument('-v', '--verbose', action='store_true', help='Include unchanged files.')
    parser.add_argument(
        '--color',
        choices=['auto', 'always', 'never'],
        help='Color output: auto (default), always, never.',
    )
    incremental_group.add_argument(
        '--no-incremental',
        action='store_true',
        default=False,
        help='Disable incremental mode (overrides config).',
    )
    skip_group = parser.add_mutually_exclusive_group()
    skip_group.add_argument(
        '--show-skips',
        action='store_true',
        default=None,
        dest='show_skips',
        help='Show skipped-file messages in compact modes.',
    )
    skip_group.add_argument(
        '--hide-skips',
        action='store_false',
        dest='show_skips',
        help='Hide skipped-file messages in compact modes.',
    )
    mode_group.add_argument(
        '--show-policy',
        action='store_true',
        help='Show the resolved ordering policy.',
    )
    parser.add_argument('--detect', action='store_true', help='Detect definition order.')
    parser.add_argument('--hoist', action='store_true', help='Hoist constants to preamble.')
    parser.add_argument(
        '--no-future', action='store_true', help="Don't add future annotations import."
    )
    _add_policy_arguments(parser, group_title='ordering policy options')
    return parser


def _format_policy_block(
    cfg: ResolvedConfig,
    source: Path | None,
    *,
    has_overrides: bool,
) -> str:
    """Format one resolved policy and its provenance."""
    policy = sort_policy_from_config(cfg.transform)
    header = (
        'built-in defaults'
        if source is None
        else f'policy from {escape_terminal_text(str(source))}'
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


def _guarded_skip(result: SortResult, warning: str) -> SortResult:
    """Clone a SortResult as a guarded skip (new_source reset to original)."""
    return SortResult(
        original_source=result.original_source,
        new_source=result.original_source,
        original_order=result.original_order,
        new_order=result.new_order,
        warning=warning,
        blocking_code=[],
        guarded=True,
        skipped=True,
        method_sorts=result.method_sorts,
        total_methods=result.total_methods,
        banner_constraints=list(result.banner_constraints),
        class_blockers=result.class_blockers,
    )


def _overrides_from_args(args: argparse.Namespace) -> SortOverrides:
    """Build configuration overrides from parsed CLI arguments."""
    incremental: bool | None = None
    if args.no_incremental:
        incremental = False
    elif args.incremental:
        incremental = True
    return SortOverrides(
        banners=args.banners,
        banner_pattern=args.banner_pattern,
        clear_banner_pattern=args.clear_banner_pattern,
        definition_order=args.definition_order,
        evaluation_order=args.evaluation_order,
        incremental=incremental,
        incremental_fallback=getattr(args, 'incremental_fallback', None),
        main=args.main,
        method_bodies=args.method_bodies,
        method_dunders=args.method_dunders,
        method_order=args.method_order,
        method_visibility=args.method_visibility,
        order=args.order,
        show_skips=getattr(args, 'show_skips', None),
        tie_break=args.tie_break,
    )


def _print_version() -> int:
    """Print the installed distribution version without other work."""
    import importlib.metadata  # noqa: PLC0415

    try:
        version = importlib.metadata.version('defsort')
    except importlib.metadata.PackageNotFoundError:
        print('error: installed distribution metadata for defsort is unavailable', file=sys.stderr)
        return EXIT_ERROR
    print(f'defsort {version}')
    return EXIT_OK


@contextlib.contextmanager
def _render_safe_config_warnings() -> Generator[None]:
    """Render configuration warnings with terminal-safe message text."""
    with warnings.catch_warnings():
        previous_showwarning = warnings.showwarning

        def dispatch_warning(
            message: Warning | str,
            category: type[Warning],
            location: tuple[str, int, TextIO | None, str | None],
        ) -> None:
            filename, lineno, file, line = location
            if issubclass(category, DefsortConfigWarning):
                detail = escape_terminal_text(str(message))
                print(f'Warning: {detail}', file=sys.stderr)
                return
            previous_showwarning(message, category, filename, lineno, file=file, line=line)

        warnings.showwarning = lambda message, category, filename, lineno, file=None, line=None: (
            dispatch_warning(message, category, (filename, lineno, file, line))
        )
        warnings.simplefilter('always', DefsortConfigWarning)
        yield


@dataclass(frozen=True, slots=True)
class _FileSortResult:
    """Carry one file's CLI sorting outcome."""

    status: _FileStatus
    sort_result: SortResult | None = None
    error_message: str | None = None
    encoding: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedConfig:
    """Carry effective CLI settings for one file."""

    options: EngineOptions
    incremental: bool
    max_reorder_count: int | None
    max_reorder_percent: int | None
    show_skips: bool
    color: ColorMode | None = None

    @property
    def _guard_enabled(self) -> bool:
        """Return whether at least one reorder guard threshold is configured."""
        return self.max_reorder_count is not None or self.max_reorder_percent is not None


@dataclass(frozen=True, slots=True)
class _ConfigResolver:
    """Resolves per-file configuration with CLI overrides applied."""

    overrides: SortOverrides
    force: bool
    hoist: bool
    add_future: bool
    warned_configs: set[ResolvedConfig] = field(
        default_factory=set[ResolvedConfig],
        compare=False,
        repr=False,
    )
    _cache: dict[Path, _ResolvedConfig] = field(
        default_factory=dict[Path, _ResolvedConfig],
        compare=False,
        repr=False,
    )

    def _for_path(self, path: Path) -> _ResolvedConfig:
        """Resolve effective configuration for one file."""
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        with _render_safe_config_warnings():
            cfg = effective_config(resolve_runtime_config(path), self.overrides)
        if cfg not in self.warned_configs:
            warn_ineffective_policy(cfg.transform)
            self.warned_configs.add(cfg)
        resolved = _ResolvedConfig(
            options=EngineOptions(
                policy=sort_policy_from_config(cfg.transform),
                force=self.force,
                hoist=self.hoist,
                add_future=self.add_future,
            ),
            incremental=cfg.incremental if cfg.incremental is not None else False,
            max_reorder_count=cfg.max_reorder_count,
            max_reorder_percent=cfg.max_reorder_percent,
            show_skips=cfg.show_skips,
            color=cfg.color,
        )
        self._cache[path] = resolved
        return resolved


def _apply_reorder_guard(  # noqa: PLR0911
    result: SortResult,
    baseline: BlobReadResult | None,
    *,
    config: _ResolvedConfig,
) -> SortResult:
    """Apply the large-reorder guard, returning a guarded SortResult if triggered."""
    if result.skipped:
        return result
    if not config._guard_enabled:
        return result
    if result.original_source == result.new_source:
        return result

    if baseline is None or baseline.status is BlobReadStatus.MISSING:
        return result

    if baseline.status in (BlobReadStatus.INFRA_ERROR, BlobReadStatus.DECODE_FAILED):
        return _guarded_skip(result, 'reorder guard: unable to read baseline from git')

    # CONTENT: parse baseline into definitions for the guard
    assert baseline.content is not None  # noqa: S101
    params = build_guard_params(
        baseline.content,
        max_reorder_count=config.max_reorder_count,
        max_reorder_percent=config.max_reorder_percent,
    )
    if params is None:
        return result  # unparseable baseline -> skip guard
    guard_result = evaluate_reorder_guard(params, result.new_order)
    if not guard_result.triggered:
        return result

    return _guarded_skip(result, guard_result.message)


def _file_result_to_json(
    path: Path,
    file_result: _FileSortResult,
    options: EngineOptions,
) -> JsonFileRecord:
    """Map a _FileSortResult to the dict format expected by format_json()."""
    status_value: str = file_result.status.value

    if file_result.status in (_FileStatus.ERROR, _FileStatus.INVALID):
        return build_json_record(
            str(path), file_result.status.value, warning=file_result.error_message
        )

    result = file_result.sort_result
    detect_result: tuple[OrderKind, float, float] | None = None
    if result is not None:
        detect_result = detect_definition_order(
            result.original_order,
            banners=options.policy.banners,
        )

    warning: str | None = None
    if file_result.status is _FileStatus.SKIPPED:
        warning = result.warning or 'safety check' if result is not None else 'safety check'
    elif file_result.status is _FileStatus.CHANGED:
        warning = result.warning if result is not None else None

    return build_json_record(
        str(path),
        status_value,
        warning=warning,
        result=result,
        detect_result=detect_result,
    )


def _repo_root_covers_parent(parent: Path, root: Path) -> bool:
    """Return whether no nested or uncertain Git marker blocks root reuse."""
    directory = parent
    while directory != root:
        try:
            (directory / '.git').lstat()
        except FileNotFoundError:
            directory = directory.parent
        except OSError:
            return False
        else:
            return False
    return True


def _discover_repo_roots(paths: list[Path]) -> dict[Path, Path | None]:
    """Map each resolved path to its git repository root, if any."""
    repo_roots: dict[Path, Path | None] = {}
    root_cache: dict[Path, Path | None] = {}
    successful_roots: set[Path] = set()

    for path in paths:
        resolved = path.resolve()
        parent = resolved.parent
        if parent not in root_cache:
            reusable_root = max(
                (root for root in successful_roots if parent.is_relative_to(root)),
                key=lambda root: len(root.parts),
                default=None,
            )
            if reusable_root is None or not _repo_root_covers_parent(parent, reusable_root):
                raw_root = try_get_repo_root(str(parent))
                discovered_root = Path(raw_root) if raw_root is not None else None
                root_cache[parent] = discovered_root
                if discovered_root is not None:
                    successful_roots.add(discovered_root)
            else:
                root_cache[parent] = reusable_root
        repo_roots[resolved] = root_cache[parent]

    return repo_roots


def _batch_fetch_cli_baselines(
    paths: list[Path],
) -> dict[Path, BlobReadResult]:
    """Batch-read baseline content for CLI incremental mode."""
    repo_roots = _discover_repo_roots(paths)
    results: dict[Path, BlobReadResult] = {}
    by_root: dict[Path, list[tuple[Path, str]]] = {}

    for path in paths:
        resolved = path.resolve()
        root = repo_roots.get(resolved)
        if root is None:
            results[path] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            results[path] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        pairs = by_root.setdefault(root, [])
        try:
            rel.encode('utf-8')
        except UnicodeEncodeError:
            results[path] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        pairs.append((path, rel))

    for root, pairs in by_root.items():
        if not pairs:
            require_batch_z_support(str(root))
            continue
        rels = [rel for _, rel in pairs]
        bl_result = batch_fetch_worktree_baselines(str(root), rels)
        if bl_result is None:
            for orig, _ in pairs:
                results[orig] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        for orig, rel in pairs:
            results[orig] = bl_result[rel]
    return results


def _run_detect(
    paths: list[Path],
    *,
    resolver: _ConfigResolver,
    verbose: bool = False,
) -> int:
    """Run --detect mode: classify file ordering."""
    counts: dict[OrderKind, int] = {}
    total = 0
    entries: list[tuple[str, OrderKind | None, float, float]] = []
    error_seen = False

    for path in sorted(paths):
        options = resolver._for_path(path).options
        try:
            source, _enc = read_file(path)
        except (OSError, ValueError, SyntaxError, UnicodeDecodeError) as e:
            safe_path = escape_terminal_text(str(path))
            safe_detail = escape_terminal_text(str(e))
            print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)
            error_seen = True
            continue
        try:
            tree = ast.parse(source)
            compile(tree, str(path), 'exec')
        except SyntaxError as e:
            safe_path = escape_terminal_text(str(path))
            safe_detail = escape_terminal_text(str(e))
            print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)
            error_seen = True
            continue
        result = detect_order(
            source,
            banners=options.policy.banners,
            banner_pattern=options.policy.banner_pattern,
            _tree=tree,
        )

        if result is None:
            entries.append((str(path), None, 0.0, 0.0))
            continue

        total += 1
        order, dep_score, alpha_score = result
        counts[order] = counts.get(order, 0) + 1
        entries.append((str(path), order, dep_score, alpha_score))

    print_detect(entries, (counts, total), verbose=verbose)

    if error_seen:
        return EXIT_ERROR
    return EXIT_OK


def _run_hook(argv: list[str]) -> int:
    """Handle the hook subcommand."""
    hook_parser = _build_hook_parser()
    args = hook_parser.parse_args(argv)

    if sys.stdin.isatty():
        print(
            "defsort hook: reads the coding agent's hook payload as JSON from stdin",
            file=sys.stderr,
        )
        print('See docs/hook.md for configuration.', file=sys.stderr)
        return EXIT_OK

    try:
        stdin_data = sys.stdin.read()
    except (ValueError, OSError):
        return EXIT_OK

    from defsort._hook import run_hook  # noqa: PLC0415

    return run_hook(
        stdin_data,
        host=args.agent,
        event=args.event,
        overrides=_overrides_from_args(args),
        allow_on_failure=args.allow_on_failure,
    )


def _run_show_policy(raw_paths: list[str], overrides: SortOverrides) -> int:
    """Print resolved policies grouped by configuration source."""
    paths, error_seen = _collect_policy_paths(raw_paths)
    policies: dict[Path | None, ResolvedConfig] = {}
    for path in paths:
        with _render_safe_config_warnings():
            cfg, source = resolve_config_sourced(path)
        policies.setdefault(source, effective_config(cfg, overrides))
    blocks = [
        _format_policy_block(
            cfg,
            source,
            has_overrides=overrides != SortOverrides(),
        )
        for source, cfg in policies.items()
    ]
    if blocks:
        print('\n\n'.join(blocks))
    return EXIT_ERROR if error_seen else EXIT_OK


def _run_sort(
    source: str,
    config: _ResolvedConfig,
    baseline_raw: BlobReadResult | None,
    *,
    tree: ast.Module,
) -> SortResult:
    """Run a sort against a pre-fetched baseline."""
    if not config.incremental or baseline_raw is None:
        return sort_file(source, options=config.options, _tree=tree)
    match baseline_raw.status:
        case BlobReadStatus.MISSING:
            return sort_file(source, options=config.options, _tree=tree)
        case BlobReadStatus.CONTENT:
            assert baseline_raw.content is not None  # noqa: S101
            return sort_file_incremental(
                source,
                baseline_raw.content,
                options=config.options,
                _tree=tree,
            )
        case BlobReadStatus.DECODE_FAILED | BlobReadStatus.INFRA_ERROR:
            raise AssertionError(baseline_raw.status)


def _sort_one_file(  # noqa: PLR0911
    path: Path,
    config: _ResolvedConfig,
    baseline: BlobReadResult | None,
) -> _FileSortResult:
    """Read, sort, and guard-check a single file."""
    try:
        file_result = read_file_result(path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        return _FileSortResult(status=_FileStatus.ERROR, error_message=str(exc))

    source, encoding = file_result.source, file_result.encoding

    try:
        tree = ast.parse(source)
        compile(tree, str(path), 'exec')
    except (SyntaxError, ValueError) as exc:
        return _FileSortResult(
            status=_FileStatus.INVALID,
            error_message=str(exc),
            encoding=encoding,
        )

    # Check baseline availability for incremental mode (after parse so
    # syntax errors take precedence, matching pre-refactor behavior).
    if (
        config.incremental
        and baseline is not None
        and baseline.status in (BlobReadStatus.INFRA_ERROR, BlobReadStatus.DECODE_FAILED)
    ):
        parsed = parse_definitions(
            source,
            banners=config.options.policy.banners,
            banner_pattern=config.options.policy.banner_pattern,
            tree=tree,
        )
        return _FileSortResult(
            status=_FileStatus.SKIPPED,
            sort_result=SortResult(
                original_source=source,
                new_source=source,
                original_order=parsed.definitions,
                new_order=parsed.definitions,
                warning=_BASELINE_UNAVAILABLE,
                blocking_code=[],
                skipped=True,
                total_methods=count_top_level_methods(tree),
            ),
            encoding=encoding,
        )

    try:
        result = _run_sort(source, config, baseline, tree=tree)
    except Exception as exc:  # noqa: BLE001
        return _FileSortResult(
            status=_FileStatus.ERROR,
            error_message=str(exc),
            encoding=encoding,
        )

    if result.skipped:
        return _FileSortResult(
            status=_FileStatus.SKIPPED,
            sort_result=result,
            encoding=encoding,
        )

    if not config.options.force:
        result = _apply_reorder_guard(result, baseline, config=config)
    if result.skipped:
        return _FileSortResult(
            status=_FileStatus.SKIPPED,
            sort_result=result,
            encoding=encoding,
        )

    if result.original_source == result.new_source:
        return _FileSortResult(
            status=_FileStatus.UNCHANGED,
            sort_result=result,
            encoding=encoding,
        )

    return _FileSortResult(
        status=_FileStatus.CHANGED,
        sort_result=result,
        encoding=encoding,
    )


def _run_json_mode(
    paths: list[Path],
    resolver: _ConfigResolver,
    baselines: dict[Path, BlobReadResult] | None,
) -> int:
    """Process files in JSON output mode."""
    records: list[JsonFileRecord] = []
    for path in paths:
        config = resolver._for_path(path)
        baseline = baselines.get(path) if baselines else None
        file_result = _sort_one_file(path, config, baseline)
        record = _file_result_to_json(path, file_result, config.options)
        records.append(record)
    print(format_json(records))
    has_error = any(r['status'] in ('error', 'invalid') for r in records)
    has_changes = any(r['status'] in ('changed', 'skipped') for r in records)
    if has_error:
        return EXIT_ERROR
    if has_changes:
        return EXIT_CHANGED
    return EXIT_OK


def _run_text_mode(  # noqa: C901, PLR0912, PLR0913, PLR0915
    paths: list[Path],
    mode: CliMode,
    resolver: _ConfigResolver,
    baselines: dict[Path, BlobReadResult] | None,
    *,
    verbose: bool,
    stdout_color: bool,
    stderr_color: bool,
    multi: bool,
) -> tuple[bool, bool]:
    """Run summary, write, preview, or diff mode."""
    summary_rows: list[SummaryRow] = []
    first_output = True
    changes_seen = False
    error_seen = False
    skipped_results: list[tuple[str, SortResult, bool]] = []
    had_output = False

    for path in paths:
        if stdout_closed():
            break
        config = resolver._for_path(path)
        ordering = config.options.policy.definition_order
        baseline = baselines.get(path) if baselines else None
        file_result = _sort_one_file(path, config, baseline)

        if file_result.status in (_FileStatus.ERROR, _FileStatus.INVALID):
            safe_path = escape_terminal_text(str(path))
            safe_detail = escape_terminal_text(file_result.error_message or '')
            print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)
            error_seen = True
            continue

        result = file_result.sort_result
        assert result is not None  # noqa: S101

        constraint_summary = format_banner_constraint_summary(
            str(path),
            result.banner_constraints,
        )
        if constraint_summary and (verbose or file_result.status is not _FileStatus.UNCHANGED):
            print(constraint_summary, file=sys.stderr)
        show_constraint_details = verbose and mode is not CliMode.PREVIEW
        if file_result.status is _FileStatus.SKIPPED and mode is CliMode.PREVIEW:
            show_constraint_details = True
        if show_constraint_details:
            constraint_details = format_banner_constraint_details(result.banner_constraints)
            if constraint_details:
                print(constraint_details, file=sys.stderr)

        if file_result.status is _FileStatus.SKIPPED:
            skipped_results.append((str(path), result, config.show_skips))
            changes_seen = True
            continue

        if result.warning:
            safe_path = escape_terminal_text(str(path))
            safe_warning = escape_terminal_text(result.warning)
            print(f'{safe_path}: warning ({safe_warning})', file=sys.stderr)

        # Write mode: apply changes immediately
        if mode is CliMode.WRITE and file_result.status is _FileStatus.CHANGED:
            assert file_result.encoding is not None  # noqa: S101
            try:
                write_source(
                    path,
                    result.new_source,
                    encoding=file_result.encoding,
                )
            except OSError as e:
                safe_path = escape_terminal_text(str(path))
                safe_detail = escape_terminal_text(str(e))
                print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)
                error_seen = True
                for class_blocker in result.class_blockers:
                    print(format_class_blocker(class_blocker), file=sys.stderr)
                continue

        if mode is CliMode.PREVIEW:
            emitted = print_preview(
                str(path),
                result,
                verbose=verbose,
                ordering=ordering,
                color=stdout_color,
                multi=multi,
                first=first_output,
            )
            if emitted:
                first_output = False
                had_output = True
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
            for class_blocker in result.class_blockers:
                print(format_class_blocker(class_blocker), file=sys.stderr)

        elif mode is CliMode.DIFF:
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
                had_output = True
            print_diff(
                result,
                str(path),
                verbose=verbose,
                ordering=ordering,
                color=stdout_color,
            )
            for class_blocker in result.class_blockers:
                print(format_class_blocker(class_blocker), file=sys.stderr)

        elif mode is CliMode.SUMMARY:
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
            summary_rows.append(build_summary_row(str(path), result))
            for class_blocker in result.class_blockers:
                print(format_class_blocker(class_blocker), file=sys.stderr)

        elif mode is CliMode.WRITE:
            # WRITE does not set changes_seen -- exit 0 after successful writes.
            summary_rows.append(build_summary_row(str(path), result))
            for class_blocker in result.class_blockers:
                print(format_class_blocker(class_blocker), file=sys.stderr)

    if summary_rows and mode in (CliMode.SUMMARY, CliMode.WRITE):
        output = format_summary_rows(summary_rows, verbose=verbose, color=stdout_color)
        if output:
            print(output)
            had_output = True

    visible_skips = [
        (skip_path, skip_result)
        for skip_path, skip_result, skip_visible in skipped_results
        if skip_visible or mode is CliMode.PREVIEW
    ]
    if visible_skips:
        sys.stdout.flush()
        if had_output:
            print(file=sys.stderr)
        for index, (skip_path, skip_result) in enumerate(visible_skips):
            if index and mode is CliMode.PREVIEW:
                print(file=sys.stderr)
            print_skipped(
                skip_path,
                skip_result,
                mode=mode,
                color=stderr_color,
                verbose=verbose,
            )
            if mode in (CliMode.SUMMARY, CliMode.PREVIEW):
                for class_blocker in skip_result.class_blockers:
                    print(format_class_blocker(class_blocker), file=sys.stderr)

    return changes_seen, error_seen


def _run_cli(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0912
    """Parse CLI arguments and dispatch the selected operation."""
    ensure_utf8_stdio()
    args_list = sys.argv[1:] if argv is None else list(argv)
    if args_list and args_list[0] == 'hook':
        return _run_hook(args_list[1:])

    parser = _build_parser()
    args = parser.parse_args(args_list)

    if args.version:
        return _print_version()

    if args.show_policy and args.detect:
        parser.error('--show-policy is not allowed with --detect')
    overrides = _overrides_from_args(args)
    if args.show_policy:
        return _run_show_policy(args.paths, overrides)

    raw_paths: list[str] = args.paths
    paths = collect_files(raw_paths)

    resolver = _ConfigResolver(
        overrides=overrides,
        force=args.force,
        hoist=args.hoist,
        add_future=not args.no_future,
    )
    resolved_configs = [resolver._for_path(path) for path in paths]
    config_colors: set[ColorMode | None] = {cfg.color for cfg in resolved_configs}
    config_color: ColorMode | None = (
        next(iter(config_colors)) if len(config_colors) == 1 else 'auto'
    )

    stdout_color = should_color(
        sys.stdout,
        cli_flag=args.color,
        config_value=config_color,
    )
    stderr_color = should_color(
        sys.stderr,
        cli_flag=args.color,
        config_value=config_color,
    )

    if args.detect:
        if args.preview or args.diff or args.write:
            parser.error('--detect is not allowed with -p/--preview, -d/--diff, or -w/--write')
        if not args.json:
            return _run_detect(
                paths,
                resolver=resolver,
                verbose=args.verbose,
            )
        # --detect with --json: silently ignored (detect fields always in JSON)

    needs_baselines = any(cfg.incremental or cfg._guard_enabled for cfg in resolved_configs)

    cli_baselines: dict[Path, BlobReadResult] | None = None
    if needs_baselines:
        try:
            cli_baselines = _batch_fetch_cli_baselines(paths)
        except UnsupportedGitVersionError as error:
            print(str(error), file=sys.stderr)
            return EXIT_ERROR

    if args.json:
        exit_code = _run_json_mode(paths, resolver, cli_baselines)
    else:
        multi = len(raw_paths) > 1 or any(Path(path).is_dir() for path in raw_paths)
        if args.write:
            mode = CliMode.WRITE
        elif args.preview:
            mode = CliMode.PREVIEW
        elif args.diff:
            mode = CliMode.DIFF
        else:
            mode = CliMode.SUMMARY

        changes_seen, error_seen = _run_text_mode(
            paths,
            mode,
            resolver,
            cli_baselines,
            verbose=args.verbose,
            stdout_color=stdout_color,
            stderr_color=stderr_color,
            multi=multi,
        )

        exit_code = EXIT_OK
        if changes_seen:
            exit_code = EXIT_CHANGED
        if error_seen:
            exit_code = EXIT_ERROR
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Run the defsort command-line interface.

    If a reader closes standard output early, the remaining output is discarded
    and the exit status reflects the work completed before the closure.

    Args:
        argv: Arguments to parse, or `None` to use `sys.argv`.

    Returns:
        The process exit status.
    """
    with closable_stdout():
        return _run_cli(argv)


if __name__ == '__main__':
    sys.exit(main())
