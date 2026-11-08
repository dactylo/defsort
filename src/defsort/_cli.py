"""CLI for defsort."""

import argparse
import ast
import enum
import io
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import redirect_stderr
from dataclasses import dataclass, field, replace
from pathlib import Path

from defsort._arguments import add_ordering_arguments, overrides_from_args
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
from defsort._core._syntax import parse_source
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
from defsort._files import (
    collect_file_groups,
    collect_files,
    encode_source,
    read_file_result,
    write_source,
)
from defsort._git import (
    read_head_baselines,
    require_batch_z_support,
)
from defsort._main import main as entry_main
from defsort._types import (
    COLOR_MODE_VALUES,
    EXIT_CHANGED,
    EXIT_ERROR,
    EXIT_OK,
    BlobReadResult,
    BlobReadStatus,
    DetectResult,
    EngineOptions,
    SortOverrides,
    SortResult,
    sort_policy_from_config,
)
from defsort._vendor.gitfiles._blobs import UnsupportedGitVersionError
from defsort._vendor.gitfiles._repo import (
    RepoDiscoveryResult,
    RepoDiscoveryStatus,
    discover_repo_root,
)
from defsort._vendor.gitfiles._run import GitCommandError
from defsort._vendor.hookfront._color import project_color, should_color
from defsort._vendor.hookfront._diff import DiffSides
from defsort._vendor.hookfront._notes import OUTSIDE_REPOSITORY, git_unavailable_message
from defsort._vendor.hookfront._parser import RootParser
from defsort._vendor.hookfront._streams import stdout_closed
from defsort._vendor.hookfront._terminal import escape_terminal_text, path_text
from defsort._vendor.hookfront._version import print_version
from defsort._vendor.srcfiles import ALIAS_CONFLICT, names_agree

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


class _RootParser(RootParser):
    """Render the option groups, then the commands that have their own parsers."""

    _command_groups = _COMMAND_GROUPS


@dataclass(frozen=True, slots=True)
class _FileSortResult:
    """Carry one file's CLI sorting outcome."""

    status: _FileStatus
    sort_result: SortResult | None = None
    error_message: str | None = None
    encoding: str | None = None
    raw: bytes = b''


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
    _warned_configs: set[ResolvedConfig] = field(
        default_factory=set[ResolvedConfig],
        compare=False,
        repr=False,
    )
    _cache: dict[Path, _ResolvedConfig] = field(
        default_factory=dict[Path, _ResolvedConfig],
        compare=False,
        repr=False,
    )

    def for_path(self, path: Path) -> _ResolvedConfig:
        """Resolve and cache effective configuration for one file.

        Configuration diagnostics go to standard error. Ineffective-policy
        warnings are emitted once per distinct resolved configuration on this
        resolver.

        Args:
            path: File whose configuration and CLI overrides should apply.

        Returns:
            Effective runtime configuration and engine options. Repeated calls
            with the same path reuse the cached result.
        """
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        with render_config_warnings():
            cfg = effective_config(resolve_runtime_config(path), self.overrides)
        if cfg not in self._warned_configs:
            warn_ineffective_policy(cfg.transform)
            self._warned_configs.add(cfg)
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
    """Map a `_FileSortResult` to the dict format expected by `format_json()`."""
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
    """Clone a `SortResult` as a guarded skip (`new_source` reset to original)."""
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
    if baseline is not None and baseline.status is BlobReadStatus.OUTSIDE_REPOSITORY:
        return _guarded_skip(result, OUTSIDE_REPOSITORY)
    if result.original_source == result.new_source:
        return result

    if baseline is None or baseline.status is BlobReadStatus.MISSING:
        return result

    if baseline.status in (BlobReadStatus.INFRA_ERROR, BlobReadStatus.DECODE_FAILED):
        return _guarded_skip(
            result,
            'reorder guard: unable to read baseline from git'
            + (f': {baseline.detail}' if baseline.detail else ''),
        )

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


def _print_class_blockers(result: SortResult, path: str, *, preview: bool = False) -> None:
    """Print class blockers for a processed file."""
    for class_blocker in result.class_blockers:
        print(format_class_blocker(class_blocker, path=path, preview=preview), file=sys.stderr)


def _print_path_error(path: Path, detail: str) -> None:
    """Print one terminal-safe path error."""
    safe_path = escape_terminal_text(path_text(path))
    safe_detail = escape_terminal_text(detail)
    print(f'error: {safe_path}: {safe_detail}', file=sys.stderr)


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


def _discover_repo_roots(paths: list[Path]) -> dict[Path, RepoDiscoveryResult]:
    """Map each resolved path to its git repository root, if any."""
    repo_roots: dict[Path, RepoDiscoveryResult] = {}
    root_cache: dict[Path, RepoDiscoveryResult] = {}
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
                discovery = discover_repo_root(str(parent))
                root_cache[parent] = discovery
                if discovery.root is not None:
                    successful_roots.add(Path(discovery.root))
            else:
                root_cache[parent] = RepoDiscoveryResult(
                    RepoDiscoveryStatus.WORKTREE, root=str(reusable_root)
                )
        repo_roots[resolved] = root_cache[parent]

    return repo_roots


def _batch_fetch_cli_baselines(
    paths: list[Path],
) -> dict[Path, BlobReadResult]:
    """Batch-read baseline content for CLI incremental mode."""
    repo_roots = _discover_repo_roots(paths)
    results: dict[Path, BlobReadResult] = {}
    by_root: dict[Path, list[Path]] = {}

    for path in paths:
        resolved = path.resolve()
        discovery = repo_roots[resolved]
        if discovery.status is RepoDiscoveryStatus.ERROR:
            raise GitCommandError(discovery.detail or 'failed to discover git work tree')
        if discovery.root is None:
            results[path] = BlobReadResult(status=BlobReadStatus.OUTSIDE_REPOSITORY)
            continue
        root = Path(discovery.root)
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            results[path] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        repo_paths = by_root.setdefault(root, [])
        try:
            rel.encode('utf-8')
        except UnicodeEncodeError:
            results[path] = BlobReadResult(status=BlobReadStatus.INFRA_ERROR)
            continue
        repo_paths.append(path)

    for root, repo_paths in by_root.items():
        if not repo_paths:
            require_batch_z_support(str(root))
            continue
        bl_result = read_head_baselines(str(root), repo_paths)
        results.update(bl_result)
    return results


def _preflight_cli_baselines(
    paths: list[Path], resolved_configs: list[_ResolvedConfig], diagnostics: io.StringIO
) -> dict[Path, BlobReadResult] | int | None:
    """Release deferred diagnostics unless required Git preflight fails."""
    baseline_configs = [
        (path, cfg)
        for path, cfg in zip(paths, resolved_configs, strict=True)
        if cfg.resolved.incremental or (cfg.resolved.guard_enabled and not cfg.options.force)
    ]
    cli_baselines: dict[Path, BlobReadResult] | None = None
    try:
        if baseline_configs:
            cli_baselines = _batch_fetch_cli_baselines([path for path, _ in baseline_configs])
    except GitCommandError as error:
        message = git_unavailable_message(
            error.detail,
            incremental=any(cfg.resolved.incremental for _, cfg in baseline_configs),
            guard=any(
                cfg.resolved.guard_enabled and not cfg.options.force for _, cfg in baseline_configs
            ),
        )
        print('error: ' + escape_terminal_text(message), file=sys.stderr)
        return EXIT_ERROR
    except UnsupportedGitVersionError as error:
        sys.stderr.write(diagnostics.getvalue())
        print(str(error), file=sys.stderr)
        return EXIT_ERROR
    sys.stderr.write(diagnostics.getvalue())
    return cli_baselines


def _run_detect(
    paths: list[Path],
    *,
    resolver: _ConfigResolver,
    verbose: bool = False,
) -> int:
    """Run --detect mode: classify file ordering."""
    entries: list[tuple[str, DetectResult | None]] = []
    error_seen = False

    for path in sorted(paths):
        options = resolver.for_path(path).options
        try:
            source = read_file_result(path).source
        except (OSError, ValueError, SyntaxError, UnicodeDecodeError) as e:
            _print_path_error(path, str(e))
            error_seen = True
            continue
        try:
            tree = parse_source(source, filename=str(path), check=True)
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

        entries.append((str(path), result))

    print_detect(entries, verbose=verbose)

    if error_seen:
        return EXIT_ERROR
    return EXIT_OK


def _main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface through the shared entry point."""
    return entry_main(argv)


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
        case BlobReadStatus.MISSING | BlobReadStatus.OUTSIDE_REPOSITORY:
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


def _sort_read_source(  # noqa: PLR0911
    path: Path,
    source: str,
    encoding: str,
    config: _ResolvedConfig,
    baseline: BlobReadResult | None,
) -> _FileSortResult:
    """Sort and guard-check one file's decoded source."""
    try:
        tree = parse_source(source, filename=str(path), check=True)
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
        and baseline.status
        in (
            BlobReadStatus.INFRA_ERROR,
            BlobReadStatus.DECODE_FAILED,
            BlobReadStatus.OUTSIDE_REPOSITORY,
        )
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
                warning=(
                    OUTSIDE_REPOSITORY
                    if baseline.status is BlobReadStatus.OUTSIDE_REPOSITORY
                    else _BASELINE_UNAVAILABLE
                    + (f': {baseline.detail}' if baseline.detail else '')
                ),
                guarded=(
                    baseline.status is BlobReadStatus.OUTSIDE_REPOSITORY
                    and config.resolved.guard_enabled
                    and not config.options.force
                ),
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


def _sort_one_file(
    path: Path,
    config: _ResolvedConfig,
    baseline: BlobReadResult | None,
) -> _FileSortResult:
    """Read, sort, and guard-check a single file."""
    try:
        file_result = read_file_result(path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        return _FileSortResult(status=_FileStatus.ERROR, error_message=str(exc))
    result = _sort_read_source(path, file_result.source, file_result.encoding, config, baseline)
    return replace(result, raw=file_result.raw)


def _alias_result(
    path: Path,
    resolver: _ConfigResolver,
    baselines: dict[Path, BlobReadResult] | None,
) -> tuple[_FileSortResult, bytes | None]:
    """Sort one name of a file, with the bytes it needs or None when that cannot be computed."""
    config = resolver.for_path(path)
    file_result = _sort_one_file(path, config, baselines.get(path) if baselines else None)
    result = file_result.sort_result
    encoding = file_result.encoding
    if (
        file_result.status in (_FileStatus.ERROR, _FileStatus.INVALID)
        or result is None
        or encoding is None
    ):
        return file_result, None
    if file_result.status is _FileStatus.CHANGED:
        return file_result, encode_source(result.new_source, encoding)
    return file_result, file_result.raw


def _group_results(
    groups: list[list[Path]],
    resolver: _ConfigResolver,
    baselines: dict[Path, BlobReadResult] | None,
) -> Iterator[tuple[list[Path], _FileSortResult | None]]:
    """Preserve alias write agreement and report outside skips under their own names."""
    for group in groups:
        if stdout_closed():
            return
        sorted_names = [_alias_result(path, resolver, baselines) for path in group]
        outside_results = [
            ([path], result)
            for path, (result, _) in zip(group, sorted_names, strict=True)
            if result.sort_result is not None and result.sort_result.warning == OUTSIDE_REPOSITORY
        ]
        if not names_agree([needed for _, needed in sorted_names]):
            yield from outside_results
            yield group, None
            continue
        # Agreeing names keep the original bytes. Each outside name still needs its own skip.
        yield from outside_results or [(group, sorted_names[0][0])]


def _run_json_mode(
    groups: list[list[Path]],
    resolver: _ConfigResolver,
    baselines: dict[Path, BlobReadResult] | None,
) -> int:
    """Process files in JSON output mode."""
    records: list[JsonFileRecord] = []
    for group, file_result in _group_results(groups, resolver, baselines):
        path = group[0]
        if file_result is None:
            names = ', '.join(str(name) for name in group)
            records.append(
                build_json_record(
                    str(path), 'error', warning=f'{ALIAS_CONFLICT}: {names}; not processed'
                )
            )
            continue
        config = resolver.for_path(path)
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
    groups: list[list[Path]],
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
    skipped_results: list[tuple[str, SortResult, bool, bool]] = []
    had_output = False

    for group, file_result in _group_results(groups, resolver, baselines):
        if stdout_closed():
            break
        path = group[0]
        if file_result is None:
            names = ', '.join(escape_terminal_text(path_text(name)) for name in group)
            print(f'error: {names}: {ALIAS_CONFLICT}; not processed', file=sys.stderr)
            error_seen = True
            continue
        config = resolver.for_path(path)
        ordering = config.options.policy.definition_order

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
            hoist_clears = False
            if (
                mode is CliMode.PREVIEW
                and result.blocker == 'interstitial'
                and not config.options.hoist
            ):
                assert file_result.encoding is not None  # noqa: S101
                hoist_config = replace(config, options=replace(config.options, hoist=True))
                baseline = baselines.get(path) if baselines else None
                hoist_result = _sort_read_source(
                    path, result.original_source, file_result.encoding, hoist_config, baseline
                )
                hoist_clears = hoist_result.status in (_FileStatus.CHANGED, _FileStatus.UNCHANGED)
            skipped_results.append((str(path), result, config.resolved.show_skips, hoist_clears))
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
                    raw=file_result.raw,
                )
            except OSError as e:
                _print_path_error(path, str(e))
                error_seen = True
                _print_class_blockers(result, str(path))
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
            _print_class_blockers(result, str(path), preview=True)

        elif mode is CliMode.DIFF:
            assert file_result.encoding is not None  # noqa: S101
            new_bytes = file_result.raw
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
                had_output = True
                new_bytes = encode_source(result.new_source, file_result.encoding)
            print_diff(
                DiffSides(file_result.raw, new_bytes, file_result.encoding),
                str(path),
                verbose=verbose,
                ordering=ordering,
                color=stdout_color,
            )
            _print_class_blockers(result, str(path))

        elif mode is CliMode.SUMMARY:
            if file_result.status is _FileStatus.CHANGED:
                changes_seen = True
            summary_rows.append(build_summary_row(str(path), result))
            _print_class_blockers(result, str(path))

        elif mode is CliMode.WRITE:
            # WRITE does not set changes_seen -- exit 0 after successful writes.
            summary_rows.append(build_summary_row(str(path), result))
            _print_class_blockers(result, str(path))

    if summary_rows and mode in (CliMode.SUMMARY, CliMode.WRITE):
        output = format_summary_rows(summary_rows, verbose=verbose, color=stdout_color)
        if output:
            print(output)
            had_output = True

    visible_skips = [
        (skip_path, skip_result, hoist_clears)
        for skip_path, skip_result, skip_visible, hoist_clears in skipped_results
        if skip_visible or mode is CliMode.PREVIEW
    ]
    if visible_skips:
        sys.stdout.flush()
        if had_output:
            print(file=sys.stderr)
        for index, (skip_path, skip_result, hoist_clears) in enumerate(visible_skips):
            if index and mode is CliMode.PREVIEW:
                print(file=sys.stderr)
            print_skipped(
                skip_path,
                skip_result,
                mode=mode,
                color=stderr_color,
                verbose=verbose,
                hoist_clears=hoist_clears,
            )
            if mode in (CliMode.SUMMARY, CliMode.PREVIEW):
                _print_class_blockers(skip_result, skip_path, preview=mode is CliMode.PREVIEW)

    return changes_seen, error_seen


def run_cli(args: list[str]) -> int:  # noqa: C901, PLR0912
    """Parse CLI arguments and dispatch the selected operation.

    The selected mode prints diagnostics or results and may rewrite files when
    `--write` is selected.

    Args:
        args: Command-line arguments excluding the executable name.

    Returns:
        0 on success, 1 when proposed changes or skips need attention, or 2
        when an error occurs.

    Raises:
        SystemExit: After displaying help or rejecting command-line arguments.
    """
    parser = _build_parser()
    options = parser.parse_args(args)

    if options.version:
        return print_version('defsort')

    if options.show_policy and options.detect:
        parser.error('--show-policy is not allowed with --detect')
    overrides = overrides_from_args(options)
    if options.show_policy:
        return _run_show_policy(options.paths, overrides)

    raw_paths: list[str] = options.paths
    diagnostics = io.StringIO()
    try:
        with redirect_stderr(diagnostics):
            groups = collect_file_groups(raw_paths)
            paths = [path for group in groups for path in group]
            if not paths:
                print('warning: no Python files found under the given paths', file=sys.stderr)

            resolver = _ConfigResolver(
                overrides=overrides,
                force=options.force,
                hoist=options.hoist,
                add_future=not options.no_future,
            )
            resolved_configs = [resolver.for_path(path) for path in paths]
    except BaseException:
        sys.stderr.write(diagnostics.getvalue())
        raise
    config_color = project_color(cfg.resolved.color for cfg in resolved_configs)

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
            sys.stderr.write(diagnostics.getvalue())
            parser.error('--detect is not allowed with -p/--preview, -d/--diff, or -w/--write')
        if not options.json:
            sys.stderr.write(diagnostics.getvalue())
            return _run_detect(
                [group[0] for group in groups],
                resolver=resolver,
                verbose=options.verbose,
            )
        # --detect with --json: silently ignored (detect fields always in JSON)

    cli_baselines = _preflight_cli_baselines(paths, resolved_configs, diagnostics)
    if isinstance(cli_baselines, int):
        return cli_baselines

    if options.json:
        exit_code = _run_json_mode(groups, resolver, cli_baselines)
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
            groups,
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


if __name__ == '__main__':
    sys.exit(_main())
