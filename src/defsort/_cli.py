"""CLI for defsort."""

import argparse
import ast
import enum
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import override

from defsort._arguments import add_ordering_arguments, overrides_from_args
from defsort._color import should_color
from defsort._config import (
    ResolvedConfig,
    effective_config,
    render_config_warnings,
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
from defsort._display._policy import format_policy_block
from defsort._display._skipped import format_class_blocker
from defsort._display._types import CliMode, JsonFileRecord, SummaryRow
from defsort._downstream import stdout_closed
from defsort._files import (
    collect_files,
    read_file_result,
    write_source,
)
from defsort._git import (
    UnsupportedGitVersionError,
    batch_fetch_worktree_baselines,
    require_batch_z_support,
    try_get_repo_root,
)
from defsort._main import main as entry_main
from defsort._terminal import escape_terminal_text, path_text
from defsort._types import (
    COLOR_MODE_VALUES,
    EXIT_CHANGED,
    EXIT_ERROR,
    EXIT_OK,
    BlobReadResult,
    BlobReadStatus,
    ColorMode,
    DetectResult,
    EngineOptions,
    OrderKind,
    SortOverrides,
    SortResult,
    sort_policy_from_config,
)

_BASELINE_UNAVAILABLE = 'incremental: unable to read baseline from git'
_COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ('commands', (('hook', 'run as a coding-agent hook (see defsort hook --help)'),)),
)


class _FileStatus(enum.Enum):
    """Classify one file's CLI processing outcome."""

    CHANGED = 'changed'
    ERROR = 'error'
    INVALID = 'invalid'
    SKIPPED = 'skipped'
    UNCHANGED = 'unchanged'


class _RootParser(argparse.ArgumentParser):
    """Render the option groups, then the commands that have their own parsers."""

    @override
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        if formatter is None:
            formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        for action_group in self._action_groups:
            formatter.start_section(action_group.title)
            formatter.add_text(action_group.description)
            # argparse exposes no public accessor for a group's actions.
            formatter.add_arguments(action_group._group_actions)  # noqa: SLF001
            formatter.end_section()
        for title, commands in _COMMAND_GROUPS:
            formatter.start_section(title)
            formatter.add_arguments(
                [argparse.Action([], name, help=text) for name, text in commands]
            )
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


@dataclass(frozen=True, slots=True)
class _FileSortResult:
    """Carry one file's CLI sorting outcome."""

    status: _FileStatus
    sort_result: SortResult | None = None
    error_message: str | None = None
    encoding: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedConfig:
    """Pair one file's resolved configuration with its engine options."""

    resolved: ResolvedConfig
    options: EngineOptions


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
        with render_config_warnings():
            cfg = effective_config(resolve_runtime_config(path), self.overrides)
        if cfg not in self.warned_configs:
            warn_ineffective_policy(cfg.transform)
            self.warned_configs.add(cfg)
        resolved = _ResolvedConfig(
            resolved=cfg,
            options=EngineOptions(
                policy=sort_policy_from_config(cfg.transform),
                force=self.force,
                hoist=self.hoist,
                add_future=self.add_future,
            ),
        )
        self._cache[path] = resolved
        return resolved


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = _RootParser(
        prog='defsort',
        usage='%(prog)s [options] [PATH ...]',
        description='Sort Python functions, classes, and methods by a configurable policy.',
        allow_abbrev=False,
    )
    parser.add_argument(
        'paths',
        nargs='*',
        default=['.'],
        metavar='PATH',
        help='files or directories to check (default: current directory)',
    )
    parser.add_argument('--version', action='store_true', help='show the version and exit')
    output = parser.add_argument_group('output (choose one)')
    modes = output.add_mutually_exclusive_group()
    modes.add_argument(
        '-p', '--preview', action='store_true', help='show where definitions would move'
    )
    modes.add_argument('-d', '--diff', action='store_true', help='show a unified diff')
    modes.add_argument('-w', '--write', action='store_true', help='rewrite files in place')
    modes.add_argument('-j', '--json', action='store_true', help='print results as JSON')
    modes.add_argument(
        '--show-policy',
        action='store_true',
        help='show the ordering policy that applies to each path',
    )
    output.add_argument(
        '--detect',
        action='store_true',
        help='report current order instead of sorting (JSON always detects order)',
    )
    add_ordering_arguments(parser)
    other_changes = parser.add_argument_group('other changes')
    other_changes.add_argument(
        '--hoist',
        action='store_true',
        help='move simple constants from between definitions to the top of the module',
    )
    other_changes.add_argument(
        '--no-future',
        action='store_true',
        help='do not add from __future__ import annotations; files that need it are skipped',
    )
    safety = parser.add_argument_group('safety')
    safety.add_argument(
        '-f',
        '--force',
        action='store_true',
        help='sort files that have code between definitions or exceed a reorder limit',
    )
    selection = parser.add_argument_group('selection')
    incremental = selection.add_mutually_exclusive_group()
    incremental.add_argument(
        '-i',
        '--incremental',
        action='store_true',
        help='sort only definitions added since the last commit (requires Git)',
    )
    incremental.add_argument(
        '--no-incremental',
        action='store_true',
        help='sort every definition, even when configuration enables incremental mode',
    )
    display = parser.add_argument_group('display')
    display.add_argument('-v', '--verbose', action='store_true', help='also list unchanged files')
    display.add_argument(
        '--color',
        choices=COLOR_MODE_VALUES,
        help='when to use color (default: auto)',
    )
    skips = display.add_mutually_exclusive_group()
    skips.add_argument(
        '--show-skips',
        action='store_true',
        default=None,
        dest='show_skips',
        help='list skipped files in summary, diff, and write output (default: on)',
    )
    skips.add_argument(
        '--hide-skips',
        action='store_false',
        dest='show_skips',
        help='do not list skipped files in summary, diff, and write output',
    )
    return parser


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
            safe_path = escape_terminal_text(path_text(path))
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
    detect_result: DetectResult | None = None
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


def _guarded_skip(result: SortResult, warning: str) -> SortResult:
    """Clone a SortResult as a guarded skip (new_source reset to original)."""
    return replace(
        result,
        new_source=result.original_source,
        warning=warning,
        blocker=None,
        blocking_code=[],
        guarded=True,
        skipped=True,
        banner_constraints=list(result.banner_constraints),
    )


def _apply_reorder_guard(  # noqa: PLR0911
    result: SortResult,
    baseline: BlobReadResult | None,
    *,
    config: _ResolvedConfig,
) -> SortResult:
    """Apply the large-reorder guard, returning a guarded SortResult if triggered."""
    if result.skipped:
        return result
    if not config.resolved.guard_enabled:
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
        max_reorder_count=config.resolved.max_reorder_count,
        max_reorder_percent=config.resolved.max_reorder_percent,
    )
    if params is None:
        return result  # unparseable baseline -> skip guard
    guard_result = evaluate_reorder_guard(params, result.new_order)
    if not guard_result.triggered:
        return result

    return _guarded_skip(result, guard_result.message)


def _print_class_blockers(result: SortResult) -> None:
    """Print class blockers for a processed file."""
    for class_blocker in result.class_blockers:
        print(format_class_blocker(class_blocker), file=sys.stderr)


def _print_path_error(path: Path, detail: str) -> None:
    """Print one terminal-safe path error."""
    safe_path = escape_terminal_text(path_text(path))
    safe_detail = escape_terminal_text(detail)
    print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)


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
    entries: list[tuple[str, DetectResult | None]] = []
    error_seen = False

    for path in sorted(paths):
        options = resolver._for_path(path).options  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        try:
            source = read_file_result(path).source
        except (OSError, ValueError, SyntaxError, UnicodeDecodeError) as e:
            _print_path_error(path, str(e))
            error_seen = True
            continue
        try:
            tree = ast.parse(source)
            compile(tree, str(path), 'exec')
        except SyntaxError as e:
            _print_path_error(path, str(e))
            error_seen = True
            continue
        result = detect_order(
            source,
            banners=options.policy.banners,
            banner_pattern=options.policy.banner_pattern,
            _tree=tree,
        )

        if result is None:
            entries.append((str(path), None))
            continue

        total += 1
        counts[result.order] = counts.get(result.order, 0) + 1
        entries.append((str(path), result))

    print_detect(entries, (counts, total), verbose=verbose)

    if error_seen:
        return EXIT_ERROR
    return EXIT_OK


def _run_show_policy(raw_paths: list[str], overrides: SortOverrides) -> int:
    """Print resolved policies grouped by configuration source."""
    paths, error_seen = _collect_policy_paths(raw_paths)
    policies: dict[Path | None, ResolvedConfig] = {}
    for path in paths:
        with render_config_warnings():
            cfg, source = resolve_config_sourced(path)
        policies.setdefault(source, effective_config(cfg, overrides))
    blocks = [
        format_policy_block(
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
    if not config.resolved.incremental or baseline_raw is None:
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
        config.resolved.incremental
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
        config = resolver._for_path(path)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
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
        config = resolver._for_path(path)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        ordering = config.options.policy.definition_order
        baseline = baselines.get(path) if baselines else None
        file_result = _sort_one_file(path, config, baseline)

        if file_result.status in (_FileStatus.ERROR, _FileStatus.INVALID):
            _print_path_error(path, file_result.error_message or '')
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
            skipped_results.append((str(path), result, config.resolved.show_skips))
            changes_seen = True
            continue

        if result.warning:
            safe_path = escape_terminal_text(path_text(path))
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
                _print_path_error(path, str(e))
                error_seen = True
                _print_class_blockers(result)
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
            _print_class_blockers(result)

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
            _print_class_blockers(result)

        elif mode is CliMode.SUMMARY:
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
            summary_rows.append(build_summary_row(str(path), result))
            _print_class_blockers(result)

        elif mode is CliMode.WRITE:
            # WRITE does not set changes_seen -- exit 0 after successful writes.
            summary_rows.append(build_summary_row(str(path), result))
            _print_class_blockers(result)

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
                _print_class_blockers(skip_result)

    return changes_seen, error_seen


def run_cli(args: list[str]) -> int:  # noqa: C901, PLR0912
    """Parse CLI arguments and dispatch the selected operation."""
    parser = _build_parser()
    options = parser.parse_args(args)

    if options.version:
        return _print_version()

    if options.show_policy and options.detect:
        parser.error('--show-policy is not allowed with --detect')
    overrides = overrides_from_args(options)
    if options.show_policy:
        return _run_show_policy(options.paths, overrides)

    raw_paths: list[str] = options.paths
    paths = collect_files(raw_paths)

    resolver = _ConfigResolver(
        overrides=overrides,
        force=options.force,
        hoist=options.hoist,
        add_future=not options.no_future,
    )
    resolved_configs = [resolver._for_path(path) for path in paths]  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    config_colors: set[ColorMode | None] = {cfg.resolved.color for cfg in resolved_configs}
    config_color: ColorMode | None = (
        next(iter(config_colors)) if len(config_colors) == 1 else 'auto'
    )

    stdout_color = should_color(
        sys.stdout,
        cli_flag=options.color,
        config_value=config_color,
    )
    stderr_color = should_color(
        sys.stderr,
        cli_flag=options.color,
        config_value=config_color,
    )

    if options.detect:
        if options.preview or options.diff or options.write:
            parser.error('--detect is not allowed with -p/--preview, -d/--diff, or -w/--write')
        if not options.json:
            return _run_detect(
                paths,
                resolver=resolver,
                verbose=options.verbose,
            )
        # --detect with --json: silently ignored (detect fields always in JSON)

    needs_baselines = any(
        cfg.resolved.incremental or cfg.resolved.guard_enabled for cfg in resolved_configs
    )

    cli_baselines: dict[Path, BlobReadResult] | None = None
    if needs_baselines:
        try:
            cli_baselines = _batch_fetch_cli_baselines(paths)
        except UnsupportedGitVersionError as error:
            print(str(error), file=sys.stderr)
            return EXIT_ERROR

    if options.json:
        exit_code = _run_json_mode(paths, resolver, cli_baselines)
    else:
        multi = len(raw_paths) > 1 or any(Path(path).is_dir() for path in raw_paths)
        if options.write:
            mode = CliMode.WRITE
        elif options.preview:
            mode = CliMode.PREVIEW
        elif options.diff:
            mode = CliMode.DIFF
        else:
            mode = CliMode.SUMMARY

        changes_seen, error_seen = _run_text_mode(
            paths,
            mode,
            resolver,
            cli_baselines,
            verbose=options.verbose,
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
    """Run the command-line interface through the shared entry point.

    Args:
        argv: Arguments to parse, or `None` to use `sys.argv`.

    Returns:
        The process exit status.
    """
    return entry_main(argv)


if __name__ == '__main__':
    sys.exit(main())
