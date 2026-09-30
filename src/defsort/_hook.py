"""Hook core shared by every host adapter."""

import contextlib
import enum
import io
import json
import sys
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from defsort._classify import classify_command
from defsort._config import (
    ResolvedConfig,
    effective_config,
    render_config_warnings,
    resolve_config_at_root,
    resolve_runtime_config,
    warn_ineffective_policy,
)
from defsort._core._guard import ReorderGuardParams, build_guard_params, evaluate_reorder_guard
from defsort._core._incremental import (
    sort_for_hook_detailed,
    sort_incremental_detailed,
)
from defsort._display import format_banner_constraint_summary
from defsort._display._terminal import escape_terminal_text
from defsort._files import read_file_result, write_source
from defsort._git import (
    NonRegularPathspecError,
    RepoDiscoveryStatus,
    UnsupportedGitVersionError,
    apply_index_entries,
    batch_fetch_worktree_baselines,
    detect_renames,
    discover_repo_root,
    get_modified_tracked_py_files,
    get_staged_py_info,
    get_unstaged_paths,
    is_hook_enabled,
    is_null_hash,
    prepare_index_entry,
    read_baseline_blobs,
    read_pathspec_file,
    read_staged_blobs,
    repo_relative,
    resolve_pathspec_py_files,
    resolve_sync_paths,
    run_pre_commit_adds,
)
from defsort._grammar import CommitMode, CommitPlan
from defsort._hosts import AdapterLoadError, load_adapter
from defsort._shell import normalize_msys_path
from defsort._types import (
    EXIT_OK,
    BannerConstraint,
    BlobReadResult,
    BlobReadStatus,
    Blocker,
    Definition,
    FallbackMode,
    HookContext,
    SortOverrides,
    SortPolicy,
    blocker_message,
    sort_policy_from_config,
)
from defsort.hooks import HostInput

if TYPE_CHECKING:
    from defsort._classify import Classification
    from defsort.hooks import HookAdapter, HookInput

_DUPLICATES_MSG = (
    'ERROR: {path} has non-consecutive definitions with the same name.'
    ' Consolidate or rename them to enable auto-sort.'
)
_EAGER_ORDER_MSG = (
    'ERROR: {path} would move a definition-time reference before the definition it uses.'
    ' Restructure that reference or choose an order that keeps the referenced definition'
    ' before the reader.'
)
_FALLBACK_BLOCKER_VALUES: frozenset[str] = frozenset({'non_conforming', 'target_incompatible'})
_FORWARD_REF_MSG = (
    'ERROR: {path} needs sort with forward annotation references.'
    ' Add `from __future__ import annotations` to enable auto-sort.'
)
_INTERSTITIAL_MSG = (
    'ERROR: {path} has code between definitions.'
    ' Move constants/type aliases to preamble to enable sorting.'
)
_LAST_RESORT_EXIT = 2

_BLOCKER_MSGS: dict[Blocker, str] = {
    'duplicates': _DUPLICATES_MSG,
    'eager_order': _EAGER_ORDER_MSG,
    'forward_refs': _FORWARD_REF_MSG,
    'interstitial': _INTERSTITIAL_MSG,
    'layout': (
        'ERROR: {path}: requested reorder cannot preserve source layout and comment ownership.'
        ' Separate attached comments from section banners with blank lines before sorting.'
    ),
}

_WARN_ONLY_BLOCKER_VALUES: frozenset[str] = frozenset({'user_reordered'})


class _ClassificationError(Exception):
    """Report that classification failed before a trusted plan existed."""


class _LastResortError(Exception):
    """Signal that no functioning host adapter exists for this invocation."""


@dataclass(frozen=True, slots=True)
class _Outcome:
    """Decision of the hook core before a host adapter renders it."""

    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BaselineResolution:
    """Result of baseline resolution for a single file."""

    skip: bool = False
    skip_message: str | None = None
    baseline_content: str | None = None
    guard_params: ReorderGuardParams | None = None


class _Disposition(enum.Enum):
    """Classify the action required for one sorted file."""

    BLOCK = 'block'
    CHANGE = 'change'
    SKIP = 'skip'


@dataclass(frozen=True, slots=True, kw_only=True)
class _FileSortOutcome:
    """Outcome of `_resolve_and_sort` for a single file."""

    disposition: _Disposition
    new_source: str | None = None
    blocker_message: str | None = None
    skip_message: str | None = None


@dataclass(frozen=True, slots=True)
class _PostToolFile:
    """A PostToolUse file prepared with repository-relative configuration."""

    path: Path
    rel_path: str
    config: ResolvedConfig


@dataclass(frozen=True, slots=True)
class _PreparedStagedFile:
    """A staged file whose required Git batch reads are complete."""

    path: str
    mode: str
    content: BlobReadResult | None
    file_config: ResolvedConfig
    baseline_entry: BlobReadResult | None
    is_new: bool
    is_renamed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _SortAttempt:
    """Carry the normalized outcome of one sorting attempt."""

    new_source: str | None = None
    blocker: Blocker | None = None
    syntax_detail: str | None = None
    sorted_defs: tuple[Definition, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()

    @property
    def _syntax_error(self) -> bool:
        """Return whether the attempt contains a syntax error."""
        return self.syntax_detail is not None


@dataclass(frozen=True, slots=True)
class _StagedFileEntry:
    """A staged file planned for index update after sort."""

    path: str
    original: str
    new_source: str
    mode: str
    encoding: str
    baseline_entry: BlobReadResult | None
    is_new: bool
    is_renamed: bool


@dataclass(frozen=True, slots=True)
class _WorktreeFileEntry:
    """A worktree file planned for disk write after sort."""

    path: str
    original: str
    new_source: str
    encoding: str


def _allow() -> _Outcome:
    """Return the silent allow outcome."""
    return _Outcome('allow')


def _check_command_cwd(host: str, value: object) -> None:
    """Reject an adapter directory that is neither `None` nor an absolute path string."""
    if value is None:
        return
    if isinstance(value, str) and value and Path(normalize_msys_path(value)).is_absolute():
        return
    raise _LastResortError(
        f"host '{host}' supplied an invalid command_cwd {value!r};"
        ' expected None or an absolute path'
    )


def _classify_for_hook(command: str, command_cwd: str | None) -> 'Classification':
    """Translate classifier exceptions into the fail-closed hook boundary."""
    try:
        normalized = None if command_cwd is None else normalize_msys_path(command_cwd)
        return classify_command(command, normalized)
    except Exception as error:
        raise _ClassificationError('defsort: unexpected error during classification') from error


def _fail(reason: str, *, allow_on_failure: bool) -> _Outcome:
    """Handle an infrastructure failure as a deny or a warned allow."""
    reason = escape_terminal_text(reason)
    if allow_on_failure:
        print(f'WARNING: {reason}', file=sys.stderr)
        return _allow()
    return _Outcome('deny', reason)


def _fetch_post_tool_baselines(
    files_by_repo: dict[str, list[_PostToolFile]],
    contexts: dict[str, HookContext],
) -> tuple[
    dict[_PostToolFile, BlobReadResult],
    set[_PostToolFile],
    set[_PostToolFile],
]:
    """Fetch each repository's required PostToolUse baselines once."""
    baselines: dict[_PostToolFile, BlobReadResult] = {}
    fetch_failed: set[_PostToolFile] = set()
    unsupported: set[_PostToolFile] = set()
    for repo_root, repo_files in files_by_repo.items():
        ctx = contexts[repo_root]
        targets = [file for file in repo_files if ctx.incremental or file.config.guard_enabled]
        if not targets:
            continue
        try:
            result = batch_fetch_worktree_baselines(
                repo_root,
                [file.rel_path for file in targets],
            )
        except UnsupportedGitVersionError as error:
            for _file in targets:
                print(escape_terminal_text(str(error)), file=sys.stderr)
            unsupported.update(targets)
            continue
        if result is None:
            fetch_failed.update(targets)
            continue
        baselines.update((file, result[file.rel_path]) for file in targets)
    return baselines, fetch_failed, unsupported


def _file_config(ctx: HookContext, rel_path: str) -> ResolvedConfig:
    """Resolve configuration for one repository-relative file."""
    with render_config_warnings():
        return effective_config(
            resolve_runtime_config(Path(ctx.repo_root) / rel_path),
            ctx.overrides,
        )


def _is_fallback_blocker(blocker: Blocker) -> bool:
    """Whether this blocker triggers the hook's full-sort fallback disposition."""
    return blocker in _FALLBACK_BLOCKER_VALUES


def _is_warn_only_blocker(blocker: Blocker) -> bool:
    """Whether this blocker is warn-only (never a deny) for the hook."""
    return blocker in _WARN_ONLY_BLOCKER_VALUES


def _last_resort(diagnostic: str) -> int:
    """Write one diagnostic line and exit 2 when no functioning adapter exists."""
    print(escape_terminal_text(f'defsort: {diagnostic}'), file=sys.stderr)
    return _LAST_RESORT_EXIT


def _post_tool_paths(cwd: object, paths: Iterable[object]) -> tuple[str, tuple[str, ...]]:
    """Validate the PostToolUse path fields consumed by the hook core."""
    if not isinstance(cwd, str):
        raise TypeError('post-tool cwd must be a string')
    edited_paths: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            raise TypeError('post-tool edited paths must be strings')
        edited_paths.append(path)
    return cwd, tuple(edited_paths)


def _parse_input(
    adapter: 'HookAdapter',
    host: str,
    stdin_data: str,
    event: str | None,
) -> 'HookInput | None':
    """Decode the payload and translate it through the adapter."""
    try:
        payload = json.loads(stdin_data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        hook_input = adapter.parse(cast('dict[str, object]', payload), event)
        if hook_input is None:
            return None
        command_cwd = hook_input.command_cwd
        _check_command_cwd(host, command_cwd)
        hook_event = hook_input.event
        if hook_event == 'post_tool':
            cwd, edited_paths = _post_tool_paths(hook_input.cwd, hook_input.edited_paths)
            return HostInput(hook_event, None, cwd, command_cwd, edited_paths)
        return HostInput(hook_event, hook_input.command, '', command_cwd)
    except _LastResortError:
        raise
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to parse the payload: {error}") from error


def _print_sort_diagnostic(message: str, *, emit_diagnostics: bool) -> None:
    """Print a sort diagnostic when diagnostic emission is enabled."""
    if emit_diagnostics:
        print(escape_terminal_text(message), file=sys.stderr)


def _apply_reorder_guard(
    attempt: _SortAttempt,
    guard: ReorderGuardParams | None,
    *,
    display_path: str,
    emit_diagnostics: bool,
) -> _SortAttempt:
    """Apply the reorder guard to a sort attempt."""
    if guard is None:
        return attempt
    guard_result = evaluate_reorder_guard(guard, attempt.sorted_defs)
    if not guard_result.triggered:
        return attempt
    _print_sort_diagnostic(
        f'defsort: {display_path}: skipped ({guard_result.message})',
        emit_diagnostics=emit_diagnostics,
    )
    return replace(attempt, new_source=None, sorted_defs=())


def _receipt(count: int) -> str:
    """Return the allow reason naming how many staged files were rewritten."""
    noun = 'file' if count == 1 else 'files'
    return f'defsort: sorted {count} staged Python {noun} for this commit'


def _report_banner_constraints(display_path: str, attempt: _SortAttempt) -> _SortAttempt:
    """Emit one compact hook diagnostic and return the attempt unchanged."""
    summary = format_banner_constraint_summary(display_path, attempt.banner_constraints)
    if summary:
        print(summary, file=sys.stderr)
    return attempt


def _resolve_baseline_and_guard(  # noqa: PLR0911, PLR0913
    ctx: HookContext,
    baseline_entry: BlobReadResult | None,
    *,
    file_cfg: ResolvedConfig,
    display_path: str,
    is_new: bool,
    is_renamed: bool,
    fetch_failed: bool = False,
) -> _BaselineResolution:
    """Resolve baseline content and build guard params."""
    if is_renamed and ctx.incremental:
        return _BaselineResolution(skip=True)

    if not fetch_failed:
        if is_new:
            return _BaselineResolution()

        if baseline_entry is None:
            return _BaselineResolution()

        if baseline_entry.status is BlobReadStatus.CONTENT:
            assert baseline_entry.content is not None  # noqa: S101
            baseline_content = baseline_entry.content if ctx.incremental else None
            guard: ReorderGuardParams | None = None
            if file_cfg.guard_enabled:
                guard = build_guard_params(
                    baseline_entry.content,
                    max_reorder_count=file_cfg.max_reorder_count,
                    max_reorder_percent=file_cfg.max_reorder_percent,
                )
            return _BaselineResolution(
                baseline_content=baseline_content,
                guard_params=guard,
            )

        if baseline_entry.status is BlobReadStatus.MISSING:
            return _BaselineResolution()

    # fetch_failed, DECODE_FAILED, or INFRA_ERROR
    if file_cfg.guard_enabled:
        msg = (
            f'defsort: {escape_terminal_text(display_path)}: skipped '
            '(reorder guard: unable to read baseline)'
        )
        return _BaselineResolution(skip=True, skip_message=msg)
    if ctx.incremental:
        return _BaselineResolution(skip=True)
    return _BaselineResolution()


def _resolve_edited_paths(paths: Sequence[str], cwd: str) -> list[str]:
    """Resolve reported Python paths against the event directory, preserving order."""
    base = Path(normalize_msys_path(cwd))
    resolved: list[str] = []
    for path in paths:
        normalized = normalize_msys_path(path)
        if not normalized.endswith('.py'):
            continue
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved.append(str(candidate))
    return list(dict.fromkeys(resolved))


def _resolve_hook_context(
    cwd: str,
    repo_root: str,
    *,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> HookContext:
    """Resolve hook config with hook-specific defaults."""
    with render_config_warnings():
        config = resolve_config_at_root(repo_root)
    if overrides is None:
        overrides = SortOverrides()
    cfg = effective_config(config, overrides)
    return HookContext(
        cwd=cwd,
        repo_root=repo_root,
        allow_on_failure=allow_on_failure or cfg.allow_on_failure,
        incremental=cfg.incremental if cfg.incremental is not None else False,
        overrides=overrides,
    )


def _resolve_pathspec_modified_files(
    ctx: HookContext,
    mode: CommitMode,
    pathspec_file_content: bytes | None = None,
) -> list[str] | _Outcome:
    """Resolve pathspecs and filter to modified tracked .py files."""
    wt_paths = resolve_pathspec_py_files(
        ctx.cwd,
        mode.pathspecs,
        repo_root=ctx.repo_root,
        pathspec_file_nul=mode.pathspec_file_nul,
        pathspec_file_content=pathspec_file_content,
    )
    if wt_paths is None:
        return _fail(
            'defsort: failed to resolve pathspec files', allow_on_failure=ctx.allow_on_failure
        )
    modified = get_modified_tracked_py_files(ctx.cwd)
    if modified is None:
        return _fail(
            'defsort: failed to discover modified files', allow_on_failure=ctx.allow_on_failure
        )
    modified_set = frozenset(modified)
    return [p for p in wt_paths if p in modified_set]


def _resolve_worktree_baselines(
    ctx: HookContext,
    paths: list[str],
    file_configs: dict[str, ResolvedConfig],
) -> tuple[set[str], dict[str, BlobReadResult]] | _Outcome:
    """Fetch baselines required by incremental mode or per-file guards."""
    needs_baselines = ctx.incremental or any(cfg.guard_enabled for cfg in file_configs.values())
    if not needs_baselines:
        return set(), {}

    renamed = detect_renames(ctx.repo_root)
    if renamed is None:
        return _fail(
            'defsort: failed to detect staged renames',
            allow_on_failure=ctx.allow_on_failure,
        )
    baseline_targets = [
        path
        for path in paths
        if path not in renamed and (ctx.incremental or file_configs[path].guard_enabled)
    ]
    baselines = batch_fetch_worktree_baselines(ctx.repo_root, baseline_targets)
    if baselines is not None:
        return renamed, baselines

    return _fail('defsort: failed to read baselines', allow_on_failure=ctx.allow_on_failure)


def _response_fields(stdout: object, stderr: object, exit_code: object) -> tuple[str, str, int]:
    """Validate every response field before either output stream is written."""
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TypeError('adapter response streams must be strings')
    if not isinstance(exit_code, int):
        raise TypeError('adapter response exit_code must be an integer')
    return stdout, stderr, exit_code


def _render_response(adapter: 'HookAdapter', host: str, result: _Outcome) -> tuple[str, str, int]:
    """Translate a native render exception into an adapter failure."""
    try:
        response = adapter.render(result)
        return _response_fields(response.stdout, response.stderr, response.exit_code)
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to render the response") from error


def _render(adapter: 'HookAdapter', host: str, result: _Outcome) -> tuple[str, str, int]:
    """Render a result, falling back to the adapter's failure response."""
    try:
        return _render_response(adapter, host, result)
    except _LastResortError:
        try:
            response = adapter.render_failure(
                f"defsort: host '{escape_terminal_text(host)}' failed to render the response"
            )
            return _response_fields(response.stdout, response.stderr, response.exit_code)
        except Exception as error:
            raise _LastResortError(
                f"host '{host}' failed to render the failure response"
            ) from error


def _return_sort_attempt(
    display_path: str,
    attempt: _SortAttempt,
    *,
    emit_diagnostics: bool,
) -> _SortAttempt:
    """Return an attempt after optionally reporting its banner constraints."""
    if emit_diagnostics:
        return _report_banner_constraints(display_path, attempt)
    return attempt


def _select_adapter(host: str, event: str | None) -> 'HookAdapter':
    """Load the selected adapter and enforce its `--event` requirement."""
    try:
        adapter = load_adapter(host)
        requires_event = adapter.requires_event
    except AdapterLoadError as error:
        raise _LastResortError(str(error)) from error
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to load: {error}") from error
    if requires_event and event is None:
        raise _LastResortError(f"host '{host}' requires --event")
    if not requires_event and event is not None:
        raise _LastResortError(f"host '{host}' does not accept --event")
    return adapter


def _soften_skipped_attempt(
    attempt: _SortAttempt,
    blocker: Blocker,
    *,
    display_path: str,
    emit_diagnostics: bool,
) -> _SortAttempt:
    """Report and soften a skipped incremental sort attempt."""
    _print_sort_diagnostic(
        f'defsort: {display_path}: skipped ({blocker_message(blocker)})',
        emit_diagnostics=emit_diagnostics,
    )
    return replace(attempt, new_source=None, blocker=None, sorted_defs=())


def _try_sort(
    source: str,
    *,
    policy: SortPolicy,
    baseline: str | None = None,
) -> _SortAttempt:
    """Attempt to sort source and normalize every outcome."""
    try:
        if baseline is not None:
            result = sort_incremental_detailed(
                source,
                baseline,
                policy=policy,
            )
        else:
            result = sort_for_hook_detailed(
                source,
                policy=policy,
            )
    except SyntaxError as e:
        line = e.lineno or 0
        return _SortAttempt(syntax_detail=f'{escape_terminal_text(e.msg)} (line {line})')
    if result.blocker is not None:
        return _SortAttempt(
            new_source=result.new_source,
            blocker=result.blocker,
            banner_constraints=result.banner_constraints,
        )
    return _SortAttempt(
        new_source=result.new_source,
        sorted_defs=result.sorted_defs,
        banner_constraints=result.banner_constraints,
    )


def _plan_file_sort(  # noqa: PLR0913
    source: str,
    *,
    policy: SortPolicy,
    incremental: bool,
    baseline: str | None,
    display_path: str,
    incremental_fallback: FallbackMode = 'warn',
    guard: ReorderGuardParams | None = None,
    emit_diagnostics: bool = True,
) -> _SortAttempt:
    """Sort one file and return its normalized attempt result."""
    if not incremental or baseline is None:
        result = _try_sort(
            source,
            policy=policy,
        )
        result = _apply_reorder_guard(
            result,
            guard,
            display_path=display_path,
            emit_diagnostics=emit_diagnostics,
        )
        return _return_sort_attempt(
            display_path,
            result,
            emit_diagnostics=emit_diagnostics,
        )

    attempt = _try_sort(
        source,
        policy=policy,
        baseline=baseline,
    )
    if attempt.blocker is None:
        return _return_sort_attempt(
            display_path,
            attempt,
            emit_diagnostics=emit_diagnostics,
        )

    blocker = attempt.blocker

    if _is_fallback_blocker(blocker):
        if incremental_fallback == 'full-sort':
            _print_sort_diagnostic(
                f'defsort: {display_path}: falling back to full sort ({blocker_message(blocker)})',
                emit_diagnostics=emit_diagnostics,
            )
            fallback_result = _try_sort(
                source,
                policy=policy,
            )
            fallback_result = _apply_reorder_guard(
                fallback_result,
                guard,
                display_path=display_path,
                emit_diagnostics=emit_diagnostics,
            )
            return _return_sort_attempt(
                display_path,
                fallback_result,
                emit_diagnostics=emit_diagnostics,
            )
        return _return_sort_attempt(
            display_path,
            _soften_skipped_attempt(
                attempt,
                blocker,
                display_path=display_path,
                emit_diagnostics=emit_diagnostics,
            ),
            emit_diagnostics=emit_diagnostics,
        )

    if _is_warn_only_blocker(blocker):
        return _return_sort_attempt(
            display_path,
            _soften_skipped_attempt(
                attempt,
                blocker,
                display_path=display_path,
                emit_diagnostics=emit_diagnostics,
            ),
            emit_diagnostics=emit_diagnostics,
        )

    return _return_sort_attempt(
        display_path,
        attempt,
        emit_diagnostics=emit_diagnostics,
    )


def _resolve_and_sort(  # noqa: PLR0913
    ctx: HookContext,
    source: str,
    *,
    file_cfg: ResolvedConfig,
    display_path: str,
    baseline_entry: BlobReadResult | None,
    is_new: bool,
    is_renamed: bool,
    emit_diagnostics: bool = True,
) -> _FileSortOutcome:
    """Resolve a baseline, sort source, and classify the outcome."""
    # 1. Baseline resolution and guard param construction.
    resolution = _resolve_baseline_and_guard(
        ctx,
        baseline_entry,
        file_cfg=file_cfg,
        display_path=display_path,
        is_new=is_new,
        is_renamed=is_renamed,
    )
    if resolution.skip:
        if resolution.skip_message and emit_diagnostics:
            print(resolution.skip_message, file=sys.stderr)
        return _FileSortOutcome(
            disposition=_Disposition.SKIP, skip_message=resolution.skip_message
        )

    baseline_content = resolution.baseline_content
    guard = resolution.guard_params

    # Compute effective sort parameters. New files in incremental mode
    # get a full sort without a guard (no baseline to compare against).
    use_incremental = ctx.incremental and not is_new and baseline_content is not None
    if ctx.incremental and not is_new and baseline_content is None:
        return _FileSortOutcome(disposition=_Disposition.SKIP)
    effective_guard = None if (ctx.incremental and is_new) else guard

    result = _plan_file_sort(
        source,
        policy=sort_policy_from_config(file_cfg.transform),
        incremental=use_incremental,
        baseline=baseline_content if use_incremental else None,
        display_path=display_path,
        incremental_fallback=file_cfg.incremental_fallback,
        guard=effective_guard,
        emit_diagnostics=emit_diagnostics,
    )

    # 3. Result handling.
    if result._syntax_error:  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        return _FileSortOutcome(disposition=_Disposition.SKIP)
    if result.blocker is not None:
        return _FileSortOutcome(
            disposition=_Disposition.BLOCK,
            blocker_message=_BLOCKER_MSGS[result.blocker].format(
                path=escape_terminal_text(display_path)
            ),
        )
    if result.new_source is None:
        return _FileSortOutcome(disposition=_Disposition.SKIP)
    return _FileSortOutcome(disposition=_Disposition.CHANGE, new_source=result.new_source)


def _plan_worktree_sync(
    ctx: HookContext,
    entry: _StagedFileEntry,
    wt_path: Path,
    file_cfg: ResolvedConfig,
) -> tuple[Path, str, str] | None:
    """Plan a best-effort exact worktree synchronization."""
    if wt_path.is_symlink():
        return None
    try:
        wt_result = read_file_result(wt_path)
        if wt_result.source == entry.original:
            new_source = entry.new_source
        else:
            outcome = _resolve_and_sort(
                ctx,
                wt_result.source,
                file_cfg=file_cfg,
                display_path=entry.path,
                baseline_entry=entry.baseline_entry,
                is_new=entry.is_new,
                is_renamed=entry.is_renamed,
                emit_diagnostics=False,
            )
            if outcome.disposition is not _Disposition.CHANGE:
                return None
            assert outcome.new_source is not None  # noqa: S101
            new_source = outcome.new_source
    except Exception:  # noqa: BLE001
        return None
    return wt_path, new_source, wt_result.encoding


def _run_post_tool_use_path(
    file: _PostToolFile,
    *,
    ctx: HookContext,
    baseline_entry: BlobReadResult | None,
    fetch_failed: bool,
) -> str | None:
    """Sort one prepared PostToolUse Python file."""
    file_path = str(file.path)
    local_path = file.path.resolve()

    try:
        file_result = read_file_result(local_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    source, encoding = file_result.source, file_result.encoding

    resolution = _resolve_baseline_and_guard(
        ctx,
        baseline_entry,
        file_cfg=file.config,
        display_path=file_path,
        is_new=False,
        is_renamed=False,
        fetch_failed=fetch_failed,
    )
    if resolution.skip:
        if resolution.skip_message:
            print(resolution.skip_message, file=sys.stderr)
        return None

    baseline = resolution.baseline_content
    guard = resolution.guard_params

    try:
        result = _plan_file_sort(
            source,
            policy=sort_policy_from_config(file.config.transform),
            incremental=ctx.incremental,
            baseline=baseline,
            display_path=file_path,
            incremental_fallback=file.config.incremental_fallback,
            guard=guard,
        )
    except Exception:  # noqa: BLE001
        print(
            f'defsort: PostToolUse error sorting {escape_terminal_text(file_path)}',
            file=sys.stderr,
        )
        traceback.print_exc()
        return None

    if result._syntax_error:  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        return f'Cannot sort {escape_terminal_text(file_path)}: {result.syntax_detail}.'

    if result.blocker is not None:
        print(
            _BLOCKER_MSGS[result.blocker].format(path=escape_terminal_text(file_path)),
            file=sys.stderr,
        )
        return None

    if result.new_source is not None:
        try:
            write_source(local_path, result.new_source, encoding=encoding)
        except OSError:
            print(
                f'defsort: cannot write {escape_terminal_text(file_path)}; file left unsorted',
                file=sys.stderr,
            )

    return None


def _warn_file_configs(
    configs: Iterable[ResolvedConfig],
    warned: set[ResolvedConfig],
) -> None:
    """Warn once for each distinct effective file configuration."""
    for config in configs:
        if config in warned:
            continue
        warn_ineffective_policy(config.transform)
        warned.add(config)


def _prepare_post_tool_files(
    edited_paths: Sequence[str],
    cwd: str,
    *,
    overrides: SortOverrides,
    warned_configs: set[ResolvedConfig],
) -> tuple[
    list[tuple[str, _PostToolFile]], dict[str, list[_PostToolFile]], dict[str, HookContext]
]:
    """Resolve and prepare PostToolUse files in payload order."""
    parent_roots: dict[Path, str | None] = {}
    accepted: list[tuple[Path, Path, str]] = []
    paths_by_repo: dict[str, list[Path]] = {}
    for file_path in _resolve_edited_paths(edited_paths, cwd):
        raw = Path(file_path)
        if raw.is_symlink():
            continue
        path = raw.resolve()
        if not path.is_file():
            continue
        parent = path.parent
        if parent not in parent_roots:
            discovery = discover_repo_root(str(parent))
            parent_roots[parent] = (
                None
                if discovery.status is RepoDiscoveryStatus.ERROR
                else discovery.root or str(parent)
            )
        repo_root = parent_roots[parent]
        if repo_root is None:
            continue
        accepted.append((raw, path, repo_root))
        paths_by_repo.setdefault(repo_root, []).append(path)

    contexts: dict[str, HookContext] = {}
    for repo_root in paths_by_repo:
        if not is_hook_enabled(repo_root):
            continue
        contexts[repo_root] = _resolve_hook_context(
            repo_root,
            repo_root,
            overrides=overrides,
        )

    prepared: list[tuple[str, _PostToolFile]] = []
    files_by_repo: dict[str, list[_PostToolFile]] = {}
    for payload_path, path, repo_root in accepted:
        ctx = contexts.get(repo_root)
        if ctx is None:
            continue
        rel_path = repo_relative(str(path), repo_root)
        file = _PostToolFile(
            path=payload_path,
            rel_path=rel_path,
            config=_file_config(ctx, rel_path),
        )
        _warn_file_configs((file.config,), warned_configs)
        prepared.append((repo_root, file))
        files_by_repo.setdefault(repo_root, []).append(file)
    return prepared, files_by_repo, contexts


def _prepare_staged_files(
    ctx: HookContext,
    *,
    exclude_paths: frozenset[str] = frozenset(),
    warned_configs: set[ResolvedConfig] | None = None,
) -> list[_PreparedStagedFile] | _Outcome:
    """Complete required Git batch reads for staged Python files."""
    info_list = get_staged_py_info(ctx.cwd)
    if info_list is None:
        return _fail('defsort: failed to list staged files', allow_on_failure=ctx.allow_on_failure)
    if exclude_paths:
        info_list = [info for info in info_list if info.path not in exclude_paths]
    if not info_list:
        return []

    contents = read_staged_blobs(ctx.cwd, info_list)
    if contents is None:
        return _fail(
            'defsort: failed to read staged contents', allow_on_failure=ctx.allow_on_failure
        )

    baselines: dict[str, BlobReadResult] = {}
    renamed: set[str] = set()
    file_configs = {info.path: _file_config(ctx, info.path) for info in info_list}
    if warned_configs is None:
        warned_configs = set()
    _warn_file_configs(file_configs.values(), warned_configs)
    needs_baselines = ctx.incremental or any(cfg.guard_enabled for cfg in file_configs.values())
    if needs_baselines:
        renamed = {info.path for info in info_list if info.status[0] in ('R', 'C')}
        baseline_infos = [info for info in info_list if info.status[0] not in ('R', 'C')]
        baseline_targets = [
            info
            for info in baseline_infos
            if ctx.incremental or file_configs[info.path].guard_enabled
        ]
        baseline_results = read_baseline_blobs(ctx.cwd, baseline_targets)
        if baseline_results is None:
            return _fail(
                'defsort: failed to read baselines', allow_on_failure=ctx.allow_on_failure
            )
        baselines = baseline_results

    return [
        _PreparedStagedFile(
            path=info.path,
            mode=info.mode,
            content=contents.get(info.path),
            file_config=file_configs[info.path],
            baseline_entry=baselines.get(info.path),
            is_new=is_null_hash(info.old_hash),
            is_renamed=info.path in renamed,
        )
        for info in info_list
    ]


def _run_post_tool_use(
    edited_paths: Sequence[str],
    cwd: str,
    *,
    overrides: SortOverrides | None = None,
) -> _Outcome:
    """Sort the Python files a post-tool payload reports as written."""
    if overrides is None:
        overrides = SortOverrides()
    warned_configs: set[ResolvedConfig] = set()
    prepared, files_by_repo, contexts = _prepare_post_tool_files(
        edited_paths,
        cwd,
        overrides=overrides,
        warned_configs=warned_configs,
    )
    baselines, fetch_failed, unsupported = _fetch_post_tool_baselines(
        files_by_repo,
        contexts,
    )
    feedback: list[str] = []
    for repo_root, file in prepared:
        if file in unsupported:
            continue
        message = _run_post_tool_use_path(
            file,
            ctx=contexts[repo_root],
            baseline_entry=baselines.get(file),
            fetch_failed=file in fetch_failed,
        )

        if message is not None:
            feedback.append(message)
    return _Outcome('allow', '\n'.join(feedback) or None)


def _sort_staged_files(  # noqa: C901, PLR0911, PLR0912
    ctx: HookContext,
    *,
    prepared_files: list[_PreparedStagedFile] | None = None,
    sync_working_tree_paths: frozenset[str] = frozenset(),
    warned_configs: set[ResolvedConfig] | None = None,
) -> _Outcome:
    """Sort staged Python files and classify hook continuation."""
    if prepared_files is None:
        preparation = _prepare_staged_files(ctx, warned_configs=warned_configs)
        if isinstance(preparation, _Outcome):
            return preparation
        prepared_files = preparation
    if not prepared_files:
        return _allow()

    file_configs = {prepared.path: prepared.file_config for prepared in prepared_files}

    # Pass 1: sort each file, check for blockers (no index writes)
    to_sort: list[_StagedFileEntry] = []
    blockers: list[str] = []

    for prepared in prepared_files:
        bl_entry = prepared.content
        if bl_entry is None or bl_entry.status is not BlobReadStatus.CONTENT:
            decision = _fail(
                f'defsort: failed to read staged content for {prepared.path}',
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            continue
        assert bl_entry.content is not None  # noqa: S101

        outcome = _resolve_and_sort(
            ctx,
            bl_entry.content,
            file_cfg=prepared.file_config,
            display_path=prepared.path,
            baseline_entry=prepared.baseline_entry,
            is_new=prepared.is_new,
            is_renamed=prepared.is_renamed,
        )
        if outcome.disposition is _Disposition.SKIP:
            continue
        if outcome.disposition is _Disposition.BLOCK:
            assert outcome.blocker_message is not None  # noqa: S101
            blockers.append(outcome.blocker_message)
            continue
        assert outcome.new_source is not None  # noqa: S101
        to_sort.append(
            _StagedFileEntry(
                path=prepared.path,
                original=bl_entry.content,
                new_source=outcome.new_source,
                mode=prepared.mode,
                encoding=bl_entry.encoding,
                baseline_entry=prepared.baseline_entry,
                is_new=prepared.is_new,
                is_renamed=prepared.is_renamed,
            )
        )

    # If any file has a blocker, deny the commit without modifying the index
    if blockers:
        return _Outcome('deny', '\n'.join(blockers))

    if not to_sort:
        return _allow()

    # Pass 2a: create blobs and determine working tree writes (no mutations yet)
    entries: list[tuple[str, str, str]] = []
    wt_writes: list[tuple[Path, str, str]] = []
    unstaged_paths = get_unstaged_paths(ctx.repo_root, [entry.path for entry in to_sort])
    if unstaged_paths is None:
        unstaged_paths = frozenset(entry.path for entry in to_sort)
    for entry in to_sort:
        idx_entry = prepare_index_entry(
            ctx.cwd,
            entry.path,
            entry.new_source,
            entry.mode,
            encoding=entry.encoding,
        )
        if idx_entry is None:
            decision = _fail(
                f'defsort: failed to prepare index entry for {entry.path}',
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            continue
        entries.append(idx_entry)
        # Determine if working tree should be synced: always for compound
        # commands (subsequent git add needs sorted content), and for bare
        # commits when no unstaged changes exist. Uses git diff to detect
        # unstaged changes (respects text normalization, filters, eol rules).
        if entry.path in sync_working_tree_paths or entry.path not in unstaged_paths:
            wt_write = _plan_worktree_sync(
                ctx,
                entry,
                Path(ctx.repo_root) / entry.path,
                file_configs[entry.path],
            )
            if wt_write is not None:
                wt_writes.append(wt_write)

    # Pass 2b: apply all index entries atomically via --index-info
    if not entries:
        return _allow()
    if not apply_index_entries(ctx.cwd, entries):
        return _fail('defsort: failed to update git index', allow_on_failure=ctx.allow_on_failure)

    # Pass 2c: sync working tree (best-effort, never blocks the commit)
    for wt_path, new_source, encoding in wt_writes:
        if wt_path.is_symlink():
            continue
        try:
            write_source(wt_path, new_source, encoding=encoding)
        except OSError:
            print(
                f'defsort: cannot write {escape_terminal_text(wt_path.name)}; '
                'worktree copy left unsorted',
                file=sys.stderr,
            )

    return _Outcome('allow', _receipt(len(entries)))


def _handle_normal_commit(
    ctx: HookContext,
    sync_paths: frozenset[str],
    warned_configs: set[ResolvedConfig],
) -> _Outcome:
    """Handle normal commit: index sort (default path)."""
    return _sort_staged_files(
        ctx,
        sync_working_tree_paths=sync_paths,
        warned_configs=warned_configs,
    )


def _sort_worktree_files(  # noqa: C901, PLR0912
    ctx: HookContext,
    paths: list[str],
    *,
    warned_configs: set[ResolvedConfig] | None = None,
) -> _Outcome:
    """Sort Python files on the working tree in two passes."""
    if not paths:
        return _allow()

    # Pre-fetch baselines in batch when incremental mode or guard is active.
    file_configs = {path: _file_config(ctx, path) for path in paths}
    if warned_configs is None:
        warned_configs = set()
    _warn_file_configs(file_configs.values(), warned_configs)
    baseline_resolution = _resolve_worktree_baselines(ctx, paths, file_configs)
    if isinstance(baseline_resolution, _Outcome):
        return baseline_resolution
    renamed, wt_baselines = baseline_resolution

    # Pass 1: read and sort all files, check for blockers.
    # Store original content alongside new content for rollback on write failure.
    to_write: list[_WorktreeFileEntry] = []
    blockers: list[str] = []

    for rel_path in paths:
        full_path = Path(ctx.repo_root) / rel_path
        if not full_path.is_file() or full_path.is_symlink():
            continue

        try:
            file_result = read_file_result(full_path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            decision = _fail(
                f'defsort: cannot read {rel_path}',
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            continue
        source, encoding = file_result.source, file_result.encoding
        bl_entry = wt_baselines.get(rel_path)
        is_new = bl_entry is None or bl_entry.status is BlobReadStatus.MISSING

        outcome = _resolve_and_sort(
            ctx,
            source,
            file_cfg=file_configs[rel_path],
            display_path=rel_path,
            baseline_entry=bl_entry,
            is_new=is_new,
            is_renamed=rel_path in renamed,
        )
        if outcome.disposition is _Disposition.SKIP:
            continue
        if outcome.disposition is _Disposition.BLOCK:
            assert outcome.blocker_message is not None  # noqa: S101
            blockers.append(outcome.blocker_message)
            continue
        assert outcome.new_source is not None  # noqa: S101
        to_write.append(
            _WorktreeFileEntry(
                path=rel_path,
                original=source,
                new_source=outcome.new_source,
                encoding=encoding,
            )
        )

    if blockers:
        return _Outcome('deny', '\n'.join(blockers))

    # Pass 2: write sorted content (primary operation -- fail-closed).
    # Track successful writes for rollback if a subsequent write fails.
    written: list[tuple[Path, str, str]] = []
    for entry in to_write:
        full_path = Path(ctx.repo_root) / entry.path
        if full_path.is_symlink():
            continue
        try:
            write_source(full_path, entry.new_source, encoding=entry.encoding)
            written.append((full_path, entry.original, entry.encoding))
        except OSError:
            if not ctx.allow_on_failure:
                # The failed target is untouched (atomic write). Restore the
                # already-written files best-effort and deny.
                restore_failures: list[str] = []
                for wb_path, wb_original, wb_enc in written:
                    try:
                        write_source(wb_path, wb_original, encoding=wb_enc)
                    except OSError:
                        restore_failures.append(wb_path.name)
                return _fail(
                    f'defsort: cannot write {full_path.name}'
                    + (
                        f'; rollback failed for: {", ".join(restore_failures)} '
                        '(sorted content kept)'
                        if restore_failures
                        else ''
                    ),
                    allow_on_failure=False,
                )
            # Best-effort: warn about this file but keep going.
            _fail(
                f'defsort: cannot write {full_path.name}',
                allow_on_failure=True,
            )

    return _allow()


def _handle_auto_stage(
    ctx: HookContext,
    warned_configs: set[ResolvedConfig],
) -> _Outcome:
    """Handle `git commit -a` and `git commit --all`."""
    wt_paths = get_modified_tracked_py_files(ctx.cwd)
    if wt_paths is None:
        return _fail(
            'defsort: failed to discover modified files', allow_on_failure=ctx.allow_on_failure
        )
    if wt_paths:
        return _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
    return _allow()


def _handle_include(
    ctx: HookContext,
    mode: CommitMode,
    sync_paths: frozenset[str],
    warned_configs: set[ResolvedConfig],
    pathspec_file_content: bytes | None = None,
) -> _Outcome:
    """Handle --include: working-tree sort for pathspecs + index sort for remaining staged."""
    result = _resolve_pathspec_modified_files(ctx, mode, pathspec_file_content)
    if isinstance(result, _Outcome):
        return result
    wt_paths = result
    wt_path_set = frozenset(wt_paths)
    preparation = _prepare_staged_files(
        ctx,
        exclude_paths=wt_path_set,
        warned_configs=warned_configs,
    )
    if isinstance(preparation, _Outcome) and preparation.decision == 'deny':
        return preparation
    if wt_paths:
        worktree_decision = _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
        if worktree_decision.decision == 'deny':
            return worktree_decision

    if isinstance(preparation, _Outcome):
        return preparation

    # Sort remaining staged files, excluding pathspec files
    # (they'll be re-staged from working tree by --include)
    return _sort_staged_files(
        ctx,
        prepared_files=preparation,
        sync_working_tree_paths=sync_paths,
        warned_configs=warned_configs,
    )


def _handle_pathspec_only(
    ctx: HookContext,
    mode: CommitMode,
    warned_configs: set[ResolvedConfig],
    pathspec_file_content: bytes | None = None,
) -> _Outcome:
    """Handle --only / bare pathspec / --pathspec-from-file (without --include)."""
    result = _resolve_pathspec_modified_files(ctx, mode, pathspec_file_content)
    if isinstance(result, _Outcome):
        return result
    wt_paths = result
    if wt_paths:
        return _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
    return _allow()


def _sort_commit(  # noqa: C901, PLR0912
    plan: 'CommitPlan',
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
) -> _Outcome:
    """Replay the add, then sort the content an accepted commit will consume."""
    mode = plan.mode
    replayed_add = False
    try:
        discovery = discover_repo_root(plan.cwd)
        if discovery.status is RepoDiscoveryStatus.NOT_WORKTREE:
            return _allow()
        if discovery.status is RepoDiscoveryStatus.ERROR:
            with render_config_warnings():
                config = resolve_config_at_root(plan.cwd)
            return _fail(
                'defsort: failed to discover git work tree',
                allow_on_failure=allow_on_failure or config.allow_on_failure,
            )
        assert discovery.root is not None  # noqa: S101
        repo_root = discovery.root
        ctx = _resolve_hook_context(
            plan.cwd,
            repo_root,
            overrides=SortOverrides() if overrides is None else overrides,
            allow_on_failure=allow_on_failure,
        )
        allow_on_failure = ctx.allow_on_failure
        warned_configs: set[ResolvedConfig] = set()

        pathspec_file_content = None
        if mode.pathspec_from_file is not None:
            try:
                pathspec_file_content = read_pathspec_file(plan.cwd, mode.pathspec_from_file)
            except NonRegularPathspecError as error:
                return _Outcome('deny', escape_terminal_text(str(error)))
            if pathspec_file_content is None:
                return _fail(
                    'defsort: failed to read pathspec file', allow_on_failure=allow_on_failure
                )

        sync_paths: frozenset[str] = frozenset()
        if plan.pre_commit_adds:
            run_pre_commit_adds(plan.pre_commit_adds)
            replayed_add = True
            sync_paths = resolve_sync_paths(plan.pre_commit_adds, repo_root)

        # Determine which paths need working-tree sort.
        # For --pathspec-from-file, only set has_pathspec when the file is
        # non-empty.  Git treats an empty pathspec file as "no restriction"
        # (same as a normal commit), so we must fall through to index sort.
        has_pathspec = bool(mode.pathspecs) or bool(pathspec_file_content)

        if mode.only and not has_pathspec:
            result = _allow()
        elif mode.auto_stage:
            result = _handle_auto_stage(ctx, warned_configs)
        elif has_pathspec and not mode.include:
            result = _handle_pathspec_only(ctx, mode, warned_configs, pathspec_file_content)
        elif mode.include:
            result = _handle_include(ctx, mode, sync_paths, warned_configs, pathspec_file_content)
        else:
            result = _handle_normal_commit(ctx, sync_paths, warned_configs)
    except UnsupportedGitVersionError as error:
        result = _fail(str(error), allow_on_failure=allow_on_failure)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        result = _fail('defsort: unexpected error during sort', allow_on_failure=allow_on_failure)
    if replayed_add and result.decision == 'deny':
        reason = result.reason or 'defsort: commit blocked'
        return _Outcome(
            'deny',
            f'{reason} Any files staged by the preceding git add remain staged.',
            result.notes,
        )
    return result


def _run_pre_tool_use(
    command: str,
    command_cwd: str | None,
    *,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> _Outcome:
    """Classify a pre-tool command before repository access and sort a supported commit."""
    try:
        classification = _classify_for_hook(command, command_cwd)
    except _ClassificationError as error:
        traceback.print_exc()
        return _Outcome('deny', str(error))
    if classification.outcome == 'unsupported':
        return _Outcome('deny', classification.reason)
    plan = classification.plan
    if not isinstance(plan, CommitPlan) or not is_hook_enabled(plan.cwd):
        return _allow()
    return _sort_commit(plan, overrides=overrides, allow_on_failure=allow_on_failure)


def _run_event(
    hook_input: 'HookInput',
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
) -> _Outcome:
    """Dispatch neutral input to the pre-tool or post-tool core."""
    if hook_input.event == 'post_tool':
        return _run_post_tool_use(hook_input.edited_paths, hook_input.cwd, overrides=overrides)
    if hook_input.command is None:
        return _allow()
    return _run_pre_tool_use(
        hook_input.command,
        hook_input.command_cwd,
        overrides=overrides,
        allow_on_failure=allow_on_failure,
    )


def run_hook(
    stdin_data: str,
    *,
    host: str,
    event: str | None = None,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> int:
    """Run one hook invocation through the selected host adapter.

    Args:
        stdin_data: Raw JSON text the host wrote to standard input.
        host: Adapter name selected by `--agent`.
        event: `--event` value for a host whose payload carries no event.
        overrides: Hook flag overrides applied to each file's configuration.
        allow_on_failure: Warn instead of deny on infrastructure failures after
            a command is accepted.

    Returns:
        The exit status the adapter rendered. Invalid JSON and payloads the
        adapter reports as inapplicable exit 0 silently. When no functioning
        adapter exists, one diagnostic line goes to standard error and the
        status is 2.
    """
    try:
        adapter = _select_adapter(host, event)
        hook_input = _parse_input(adapter, host, stdin_data, event)
        if hook_input is None:
            return EXIT_OK
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            outcome = _run_event(
                hook_input,
                overrides=overrides,
                allow_on_failure=allow_on_failure,
            )
        notes = tuple(captured.getvalue().splitlines())
        stdout, stderr, exit_code = _render(
            adapter, host, _Outcome(outcome.decision, outcome.reason, notes)
        )
    except _LastResortError as error:
        return _last_resort(str(error))
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code
