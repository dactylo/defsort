"""Engine-backed hook work: commit sorting and PostToolUse editing."""

import enum
import sys
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

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
from defsort._files import encode_source, private_paths, read_file_result, write_source
from defsort._git import (
    batch_fetch_worktree_baselines,
    is_hook_enabled,
    is_null_hash,
    prepare_index_entry,
    read_baseline_blobs,
    read_head_baselines,
    read_staged_blobs,
    repo_relative,
)
from defsort._types import (
    CLASS_BLOCKER_LABELS,
    DYNAMIC_ALL_MESSAGE,
    BannerConstraint,
    BlobReadResult,
    BlobReadStatus,
    Blocker,
    ClassBlocker,
    Definition,
    FallbackMode,
    HookContext,
    SortOverrides,
    SortPolicy,
    blocker_message,
    is_soft_blocker,
    sort_policy_from_config,
)
from defsort._vendor.gitfiles._adds import run_pre_commit_adds
from defsort._vendor.gitfiles._blobs import UnsupportedGitVersionError
from defsort._vendor.gitfiles._index import IndexEntry, apply_index_entries
from defsort._vendor.gitfiles._listings import (
    get_modified_tracked_py_files,
    get_staged_py_info,
    head_rename_sources,
    match_tracked_py_files,
)
from defsort._vendor.gitfiles._pathspecs import (
    NonRegularPathspecError,
    PathspecFileError,
    parse_pathspec_records,
    read_pathspec_file,
)
from defsort._vendor.gitfiles._repo import (
    RepoDiscoveryResult,
    RepoDiscoveryStatus,
    discover_repo_root,
)
from defsort._vendor.gitfiles._run import GitCommandError
from defsort._vendor.hookfront._front import (
    Outcome,
    allow,
    describe_error,
    error_summary,
    fail,
    resolve_edited_paths,
)
from defsort._vendor.hookfront._notes import (
    OUTSIDE_REPOSITORY,
    git_unavailable_reason,
    post_tool_note,
    sync_note,
    syntax_reason,
    unstaged_reason,
)
from defsort._vendor.hookfront._terminal import escape_terminal_text, path_text
from defsort._vendor.hookfront._writes import CommitWrites
from defsort._vendor.hookshell._grammar import CommitMode, CommitPlan
from defsort._vendor.srcfiles import ALIAS_CONFLICT, FileChangedError, group_paths, names_agree

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


@dataclass(frozen=True, slots=True)
class _BaselineResolution:
    """Result of baseline resolution for a single file."""

    skip: bool = False
    skip_message: str | None = None
    baseline_content: str | None = None
    guard_params: ReorderGuardParams | None = None
    failure: str | None = None


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
    failure: str | None = None


@dataclass(slots=True)
class _PostToolMessages:
    """Buffer per-file notes and syntax reasons in payload order."""

    names: dict[Path, str]
    absolute_names: dict[Path, str]
    notes: dict[Path, list[str]] = field(default_factory=dict[Path, list[str]])
    reasons: dict[Path, str] = field(default_factory=dict[Path, str])


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


@dataclass(frozen=True, slots=True, kw_only=True)
class _SortAttempt:
    """Carry the normalized outcome of one sorting attempt."""

    new_source: str | None = None
    blocker: Blocker | None = None
    syntax_detail: str | None = None
    sorted_defs: tuple[Definition, ...] = ()
    banner_constraints: tuple[BannerConstraint, ...] = ()
    class_blockers: tuple[ClassBlocker, ...] = ()
    message: str | None = None


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


@dataclass(frozen=True, slots=True)
class _PostToolSort:
    """One PostToolUse file's bytes and source, and its sorted source when sorting changed it."""

    path: Path
    raw: bytes
    source: str
    encoding: str
    new_source: str | None = None


@dataclass(frozen=True, slots=True)
class _WorktreeFileEntry:
    """A worktree file read for sorting, with its bytes and its sorted source."""

    path: str
    original: str
    new_source: str
    encoding: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class _WorktreeSync:
    """A planned working-tree write and the bytes it was planned against."""

    path: Path
    new_source: str
    encoding: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class _StagedSync:
    """One file's ordered working-tree write or reason for leaving it alone."""

    names: list[str]
    write: _WorktreeSync | None = None
    reason: str | None = None


def _agreed_worktree_entries(
    ctx: HookContext, entries: list[_WorktreeFileEntry], unreadable: list[str]
) -> list[_WorktreeFileEntry] | Outcome:
    """Choose one sorted name per file, refusing files whose names need different results."""
    # A name that could not be read needs a result nobody computed, so it blocks its file.
    root = Path(ctx.repo_root)
    by_path = {root / entry.path: entry for entry in entries}
    names_by_path = {path: entry.path for path, entry in by_path.items()}
    names_by_path.update({root / rel_path: rel_path for rel_path in unreadable})
    agreed: list[_WorktreeFileEntry] = []
    for group in group_paths(names_by_path):
        members = [by_path[path] for path in group if path in by_path]
        changed = [entry for entry in members if entry.new_source != entry.original]
        if not changed:
            continue
        needed = [
            (
                by_path[path].raw
                if by_path[path].new_source == by_path[path].original
                else encode_source(by_path[path].new_source, by_path[path].encoding)
            )
            if path in by_path
            else None
            for path in group
        ]
        if not names_agree(needed):
            names = ', '.join(names_by_path[path] for path in group)
            decision = fail(
                f'defsort: {names}: {ALIAS_CONFLICT}', allow_on_failure=ctx.allow_on_failure
            )
            if decision.decision == 'deny':
                return decision
            continue
        agreed.append(changed[0])
    return agreed


def _attempt_blocker_message(attempt: _SortAttempt, display_path: str) -> str | None:
    """Describe an attempt's blocking condition for the hook surface."""
    if attempt.blocker is None:
        return None
    if attempt.blocker == 'eager_order' and attempt.message == DYNAMIC_ALL_MESSAGE:
        path = escape_terminal_text(path_text(display_path))
        cause = DYNAMIC_ALL_MESSAGE.removeprefix('sort ')
        return f'ERROR: {path} {cause}. Write __all__ as one literal list to enable sorting.'
    return _BLOCKER_MSGS[attempt.blocker].format(
        path=escape_terminal_text(path_text(display_path))
    )


def _baseline_read_failure(
    baseline_entry: BlobReadResult, display_path: str, *, is_new: bool
) -> _BaselineResolution | None:
    """Classify a failed PreToolUse baseline before any guard or new-file fallback."""
    status = baseline_entry.status
    if status is BlobReadStatus.INFRA_ERROR or (status is BlobReadStatus.MISSING and not is_new):
        detail = baseline_entry.detail or 'blob missing from git cat-file output'
        return _BaselineResolution(
            failure=f'defsort: failed to read baseline for {display_path}: {detail}'
        )
    if status is BlobReadStatus.DECODE_FAILED:
        detail = baseline_entry.detail or 'unknown decoding error'
        message = (
            f'defsort: {path_text(display_path)}: skipped (baseline cannot be decoded: {detail})'
        )
        return _BaselineResolution(skip=True, skip_message=escape_terminal_text(message))
    return None


def _drop_private(
    repo_root: str | Path, paths: list[str], *, allow_on_failure: bool
) -> list[str] | Outcome:
    """Remove paths whose private git attribute is set."""
    private, detail = private_paths(str(repo_root), paths)
    if detail is not None:
        return fail(
            f'defsort: failed to read private labels: {detail}',
            allow_on_failure=allow_on_failure,
        )
    return [path for path in paths if path not in private]


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


def _post_tool_messages(edited_paths: Sequence[str], cwd: str) -> _PostToolMessages:
    """Retain payload spelling and order for existing Python files."""
    names: dict[Path, str] = {}
    absolute_names: dict[Path, str] = {}
    for payload_path in edited_paths:
        for resolved in resolve_edited_paths([payload_path], cwd):
            raw = Path(resolved)
            if not raw.is_symlink() and raw.is_file():
                path = raw.resolve()
                names.setdefault(path, payload_path)
                absolute_names.setdefault(path, path_text(raw))
    return _PostToolMessages(names, absolute_names)


def _post_tool_outcome(messages: _PostToolMessages) -> Outcome:
    """Emit ordered notes with adjacent tracebacks and return ordered syntax reasons."""
    for path in messages.names:
        for note in messages.notes.get(path, []):
            print(note, file=sys.stderr)
    reasons = [messages.reasons[path] for path in messages.names if path in messages.reasons]
    return Outcome('allow', '\n'.join(reasons) or None)


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
        f'defsort: {path_text(display_path)}: skipped ({guard_result.message})',
        emit_diagnostics=emit_diagnostics,
    )
    return replace(attempt, new_source=None, sorted_defs=())


def _read_pathspec_records(
    cwd: str, mode: 'CommitMode', *, allow_on_failure: bool
) -> list[str] | Outcome:
    """Validate pathspec-file records before replaying any preceding add."""
    name = mode.pathspec_from_file
    if name is None:
        return []
    try:
        data = read_pathspec_file(cwd, name)
    except NonRegularPathspecError:
        return Outcome('deny', 'defsort: pathspec resource must be a regular file')
    except OSError as error:
        return fail(
            f'defsort: cannot read pathspec file {name}: {describe_error(error)}',
            allow_on_failure=allow_on_failure,
        )
    try:
        return parse_pathspec_records(data, nul=mode.pathspec_file_nul)
    except PathspecFileError as error:
        return fail(
            f'defsort: cannot use pathspec file {name}: {error.detail}',
            allow_on_failure=allow_on_failure,
        )


def _receipt(count: int) -> str:
    """Return the allow reason naming how many staged files were rewritten."""
    noun = 'file' if count == 1 else 'files'
    return f'defsort: sorted {count} staged Python {noun} for this commit'


def _report_class_blockers(attempt: _SortAttempt, display_path: str) -> None:
    """Report each hard class blocker without treating it as a file blocker."""
    for cb in attempt.class_blockers:
        if cb.blocker in CLASS_BLOCKER_LABELS:
            print(
                escape_terminal_text(
                    f'defsort: {path_text(display_path)}: class {cb.class_name}: '
                    f'skipped ({CLASS_BLOCKER_LABELS[cb.blocker]})'
                ),
                file=sys.stderr,
            )


def _resolve_baseline_and_guard(  # noqa: PLR0913
    ctx: HookContext,
    baseline_entry: BlobReadResult | None,
    *,
    file_cfg: ResolvedConfig,
    display_path: str,
    is_new: bool,
    fail_on_unreadable: bool = False,
) -> _BaselineResolution:
    """Resolve baseline content and build guard params."""
    if fail_on_unreadable and baseline_entry is not None:
        failure = _baseline_read_failure(baseline_entry, display_path, is_new=is_new)
        if failure is not None:
            return failure

    if is_new or baseline_entry is None:
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

    # DECODE_FAILED or INFRA_ERROR
    if file_cfg.guard_enabled:
        msg = (
            f'defsort: {escape_terminal_text(path_text(display_path))}: skipped '
            '(reorder guard: unable to read baseline)'
        )
        return _BaselineResolution(skip=True, skip_message=msg)
    return _BaselineResolution(skip=ctx.incremental)


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


def _resolve_commit_context(
    plan: 'CommitPlan',
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
    hook_read_failure: str | None,
) -> HookContext | Outcome:
    """Resolve the existing failure policy before reporting a hook-setting failure."""
    discovery = discover_repo_root(plan.cwd)
    if discovery.status is not RepoDiscoveryStatus.WORKTREE:
        if discovery.status is RepoDiscoveryStatus.NOT_WORKTREE and hook_read_failure is None:
            return allow()
        with render_config_warnings():
            config = resolve_config_at_root(plan.cwd)
        return fail(
            (
                f'defsort: cannot read git config defsort.hook: {hook_read_failure}'
                if hook_read_failure is not None
                else 'defsort: failed to discover git work tree'
                + (f': {discovery.detail}' if discovery.detail else '')
            ),
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
    if hook_read_failure is not None:
        return fail(
            f'defsort: cannot read git config defsort.hook: {hook_read_failure}',
            allow_on_failure=ctx.allow_on_failure,
        )
    return ctx


def _resolve_pathspec_modified_files(
    ctx: HookContext,
    mode: CommitMode,
    pathspec_records: Sequence[str] = (),
) -> list[str] | Outcome:
    """Resolve pathspecs and filter to modified tracked .py files."""
    try:
        wt_paths = match_tracked_py_files(
            ctx.cwd,
            [*mode.pathspecs, *pathspec_records],
            repo_root=ctx.repo_root,
        )
    except GitCommandError as error:
        return fail(
            f'defsort: cannot use pathspecs: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    try:
        modified = get_modified_tracked_py_files(ctx.cwd)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to discover modified files: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    modified_set = frozenset(modified)
    return _drop_private(
        ctx.repo_root,
        [p for p in wt_paths if p in modified_set],
        allow_on_failure=ctx.allow_on_failure,
    )


def _resolve_post_tool_contexts(
    paths_by_repo: dict[str, list[Path]],
    overrides: SortOverrides,
) -> tuple[dict[str, HookContext], dict[str, str]]:
    """Resolve enabled roots and retain failures for every affected payload path."""
    contexts: dict[str, HookContext] = {}
    hook_failures: dict[str, str] = {}
    for repo_root in paths_by_repo:
        try:
            enabled = is_hook_enabled(repo_root)
        except GitCommandError as error:
            hook_failures[repo_root] = error.detail
            continue
        if not enabled:
            continue
        contexts[repo_root] = _resolve_hook_context(
            repo_root,
            repo_root,
            overrides=overrides,
        )

    return contexts, hook_failures


def _resolve_worktree_baselines(
    ctx: HookContext,
    paths: list[str],
    file_configs: dict[str, ResolvedConfig],
) -> dict[str, BlobReadResult] | Outcome:
    """Fetch baselines required by incremental mode or per-file guards."""
    needs_baselines = ctx.incremental or any(cfg.guard_enabled for cfg in file_configs.values())
    if not needs_baselines:
        return {}

    try:
        sources = head_rename_sources(ctx.repo_root)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to pair renamed files: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    baseline_targets = [
        path for path in paths if ctx.incremental or file_configs[path].guard_enabled
    ]
    try:
        baselines = batch_fetch_worktree_baselines(
            ctx.repo_root, baseline_targets, sources=sources
        )
    except GitCommandError as error:
        return fail(
            f'defsort: failed to read baselines: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    return baselines


def _soften_skipped_attempt(
    attempt: _SortAttempt,
    blocker: Blocker,
    *,
    display_path: str,
    emit_diagnostics: bool,
) -> _SortAttempt:
    """Report and soften a skipped incremental sort attempt."""
    _print_sort_diagnostic(
        f'defsort: {path_text(display_path)}: skipped ({blocker_message(blocker)})',
        emit_diagnostics=emit_diagnostics,
    )
    return replace(attempt, new_source=None, blocker=None, sorted_defs=())


def _sync_staged_worktree(wt_writes: list[_StagedSync]) -> None:
    """Attempt every authorized sync, then report reasons in staging and group order."""
    notes: list[str] = []
    for sync in wt_writes:
        reason = sync.reason
        if (write := sync.write) is not None:
            try:
                if write.path.is_symlink():
                    reason = unstaged_reason(sync.names, of=len(sync.names))
                else:
                    write_source(
                        write.path, write.new_source, encoding=write.encoding, raw=write.raw
                    )
            except Exception as error:  # noqa: BLE001
                reason = f'cannot write it: {describe_error(error)}'
        if reason is not None:
            notes.append(sync_note('defsort', sync.names, reason, result='sorted'))
    for note in notes:
        print(note, file=sys.stderr)


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
        return _SortAttempt(syntax_detail=syntax_reason(e))
    if result.blocker is not None:
        return _SortAttempt(
            new_source=result.new_source,
            blocker=result.blocker,
            message=result.message,
            banner_constraints=result.banner_constraints,
            class_blockers=result.class_blockers,
        )
    return _SortAttempt(
        new_source=result.new_source,
        sorted_defs=result.sorted_defs,
        banner_constraints=result.banner_constraints,
        class_blockers=result.class_blockers,
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
        attempt = _try_sort(source, policy=policy)
        attempt = _apply_reorder_guard(
            attempt,
            guard,
            display_path=display_path,
            emit_diagnostics=emit_diagnostics,
        )
    else:
        attempt = _try_sort(source, policy=policy, baseline=baseline)
        blocker = attempt.blocker
        if blocker is not None:
            if _is_fallback_blocker(blocker) and incremental_fallback == 'full-sort':
                _print_sort_diagnostic(
                    f'defsort: {path_text(display_path)}: falling back to full sort '
                    f'({blocker_message(blocker)})',
                    emit_diagnostics=emit_diagnostics,
                )
                attempt = _try_sort(source, policy=policy)
                attempt = _apply_reorder_guard(
                    attempt,
                    guard,
                    display_path=display_path,
                    emit_diagnostics=emit_diagnostics,
                )
            elif is_soft_blocker(blocker):
                attempt = _soften_skipped_attempt(
                    attempt,
                    blocker,
                    display_path=display_path,
                    emit_diagnostics=emit_diagnostics,
                )

    if emit_diagnostics:
        summary = format_banner_constraint_summary(display_path, attempt.banner_constraints)
        if summary:
            print(summary, file=sys.stderr)
    return attempt


def _resolve_and_sort(  # noqa: PLR0913
    ctx: HookContext,
    source: str,
    *,
    file_cfg: ResolvedConfig,
    display_path: str,
    baseline_entry: BlobReadResult | None,
    is_new: bool,
    emit_diagnostics: bool = True,
    fail_on_unreadable: bool = False,
) -> _FileSortOutcome:
    """Resolve a baseline, sort source, and classify the outcome."""
    # 1. Baseline resolution and guard param construction.
    resolution = _resolve_baseline_and_guard(
        ctx,
        baseline_entry,
        file_cfg=file_cfg,
        display_path=display_path,
        is_new=is_new,
        fail_on_unreadable=fail_on_unreadable,
    )
    if resolution.skip or resolution.failure is not None:
        if resolution.skip_message and emit_diagnostics:
            print(resolution.skip_message, file=sys.stderr)
        return _FileSortOutcome(
            disposition=_Disposition.SKIP,
            skip_message=resolution.skip_message,
            failure=resolution.failure,
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

    if emit_diagnostics:
        _report_class_blockers(result, display_path)

    # 3. Result handling.
    if result.syntax_detail is not None:
        return _FileSortOutcome(disposition=_Disposition.SKIP)
    blocker_message = _attempt_blocker_message(result, display_path)
    if blocker_message is not None:
        return _FileSortOutcome(
            disposition=_Disposition.BLOCK,
            blocker_message=blocker_message,
        )
    if result.new_source is None:
        return _FileSortOutcome(disposition=_Disposition.SKIP)
    return _FileSortOutcome(disposition=_Disposition.CHANGE, new_source=result.new_source)


def _post_tool_skip(
    messages: _PostToolMessages, path: Path, reason: str, *, names: list[Path] | None = None
) -> None:
    """Queue a skip line at the file's first payload occurrence."""
    displayed = ', '.join(messages.names[name] for name in (names or [path]))
    messages.notes.setdefault(path, []).append(post_tool_note('defsort', displayed, reason))


def _fetch_post_tool_baselines(
    files_by_repo: dict[str, list[_PostToolFile]],
    contexts: dict[str, HookContext],
    messages: _PostToolMessages,
) -> tuple[
    dict[_PostToolFile, BlobReadResult],
    set[_PostToolFile],
    set[_PostToolFile],
]:
    """Fetch each repository's required PostToolUse baselines once."""
    baselines: dict[_PostToolFile, BlobReadResult] = {}
    unavailable: set[_PostToolFile] = set()
    unsupported: set[_PostToolFile] = set()
    reported_unsupported_git = False
    for repo_root, repo_files in files_by_repo.items():
        ctx = contexts[repo_root]
        targets = [file for file in repo_files if ctx.incremental or file.config.guard_enabled]
        if not targets:
            continue
        try:
            result = read_head_baselines(
                repo_root,
                [file.path for file in targets],
            )
        except UnsupportedGitVersionError as error:
            if not reported_unsupported_git:
                print(escape_terminal_text(str(error)), file=sys.stderr)
                reported_unsupported_git = True
            unsupported.update(targets)
            continue
        except GitCommandError as error:
            for file in targets:
                reason = git_unavailable_reason(
                    error.detail, incremental=ctx.incremental, guard=file.config.guard_enabled
                )
                _post_tool_skip(messages, file.path.resolve(), reason)
            unavailable.update(targets)
            continue
        baselines.update((file, result[file.path]) for file in targets)
    return baselines, unavailable, unsupported


def _post_tool_error(
    messages: _PostToolMessages,
    path: Path,
    error: Exception,
    *,
    reading: bool = False,
    writing: bool = False,
) -> None:
    """Queue expected failures tersely and unexpected failures with their traceback."""
    if reading and isinstance(error, (OSError, UnicodeDecodeError, SyntaxError)):
        reason = f'cannot read it: {describe_error(error)}'
    elif writing and isinstance(error, FileChangedError):
        reason = str(error)
    elif writing and isinstance(error, OSError):
        reason = f'cannot write it: {describe_error(error)}'
    elif isinstance(error, SyntaxError) and not writing:
        messages.reasons[path] = post_tool_note(
            'defsort', messages.names[path], syntax_reason(error)
        )
        return
    else:
        reason = f'unexpected error: {error_summary(error)}'
        _post_tool_skip(messages, path, reason)
        messages.notes[path].append(''.join(traceback.format_exception(error)).rstrip())
        return
    _post_tool_skip(messages, path, reason)


def _sort_post_tool_file(
    file: _PostToolFile,
    *,
    ctx: HookContext,
    baseline_entry: BlobReadResult | None,
    messages: _PostToolMessages,
) -> _PostToolSort | None:
    """Sort one PostToolUse file without writing it and record syntax feedback."""
    local_path = file.path.resolve()
    file_path = messages.absolute_names[local_path]

    try:
        file_result = read_file_result(local_path)
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        _post_tool_error(messages, local_path, error, reading=True)
        return None
    unsorted = _PostToolSort(
        local_path,
        file_result.raw,
        file_result.source,
        file_result.encoding,
    )

    resolution = _resolve_baseline_and_guard(
        ctx,
        baseline_entry,
        file_cfg=file.config,
        display_path=file_path,
        is_new=False,
    )
    if resolution.skip:
        if resolution.skip_message:
            print(resolution.skip_message, file=sys.stderr)
        return unsorted

    baseline = resolution.baseline_content
    guard = resolution.guard_params

    result = _plan_file_sort(
        unsorted.source,
        policy=sort_policy_from_config(file.config.transform),
        incremental=ctx.incremental,
        baseline=baseline,
        display_path=file_path,
        incremental_fallback=file.config.incremental_fallback,
        guard=guard,
    )
    if result.syntax_detail is not None:
        messages.reasons[local_path] = post_tool_note(
            'defsort', messages.names[local_path], result.syntax_detail
        )
        return None

    _report_class_blockers(result, file.rel_path)
    blocker_message = _attempt_blocker_message(result, file_path)
    if blocker_message is not None:
        print(blocker_message, file=sys.stderr)
        return unsorted

    return replace(unsorted, new_source=result.new_source)


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
    *,
    overrides: SortOverrides,
    warned_configs: set[ResolvedConfig],
    messages: _PostToolMessages,
) -> tuple[
    list[tuple[str, _PostToolFile]],
    dict[str, list[_PostToolFile]],
    dict[str, HookContext],
    list[Path],
]:
    """Prepare PostToolUse files in payload order, and list names whose repository is unknown."""
    parent_roots: dict[Path, str | None] = {}
    parent_discoveries: dict[Path, RepoDiscoveryResult] = {}
    accepted: list[tuple[Path, Path, str]] = []
    undiscovered: list[Path] = []
    paths_by_repo: dict[str, list[Path]] = {}
    for raw in messages.names:
        path = raw.resolve()
        parent = path.parent
        if parent not in parent_roots:
            discovery = discover_repo_root(str(parent))
            parent_discoveries[parent] = discovery
            parent_roots[parent] = (
                None
                if discovery.status is RepoDiscoveryStatus.ERROR
                else discovery.root or str(parent)
            )
        repo_root = parent_roots[parent]
        if repo_root is None:
            detail = parent_discoveries[parent].detail
            _post_tool_skip(
                messages,
                raw,
                'failed to discover git work tree' + (f': {detail}' if detail else ''),
            )
            undiscovered.append(raw)
            continue
        if parent_discoveries[parent].status is RepoDiscoveryStatus.WORKTREE:
            messages.names[raw] = repo_relative(str(path), repo_root)
        accepted.append((raw, path, repo_root))
        paths_by_repo.setdefault(repo_root, []).append(path)

    contexts, hook_failures = _resolve_post_tool_contexts(paths_by_repo, overrides)

    prepared: list[tuple[str, _PostToolFile]] = []
    files_by_repo: dict[str, list[_PostToolFile]] = {}
    for payload_path, path, repo_root in accepted:
        if repo_root in hook_failures:
            _post_tool_skip(
                messages,
                payload_path,
                f'cannot read git config defsort.hook: {hook_failures[repo_root]}',
            )
            undiscovered.append(payload_path)
            continue
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
        if parent_discoveries[path.parent].status is RepoDiscoveryStatus.NOT_WORKTREE and (
            ctx.incremental or file.config.guard_enabled
        ):
            _post_tool_skip(messages, payload_path, OUTSIDE_REPOSITORY)
            undiscovered.append(payload_path)
            continue
        prepared.append((repo_root, file))
        files_by_repo.setdefault(repo_root, []).append(file)
    return prepared, files_by_repo, contexts, undiscovered


def _prepare_staged_files(
    ctx: HookContext,
    *,
    exclude_paths: frozenset[str] = frozenset(),
    warned_configs: set[ResolvedConfig] | None = None,
) -> list[_PreparedStagedFile] | Outcome:
    """Complete required Git batch reads for staged Python files."""
    try:
        info_list = get_staged_py_info(ctx.cwd)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to list staged files: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    selected = _drop_private(
        ctx.repo_root,
        [info.path for info in info_list],
        allow_on_failure=ctx.allow_on_failure,
    )
    if isinstance(selected, Outcome):
        return selected
    selected_set = frozenset(selected)
    info_list = [info for info in info_list if info.path in selected_set]
    if exclude_paths:
        info_list = [info for info in info_list if info.path not in exclude_paths]
    if not info_list:
        return []

    try:
        contents = read_staged_blobs(ctx.cwd, info_list)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to read staged contents: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )

    baselines: dict[str, BlobReadResult] = {}
    file_configs = {info.path: _file_config(ctx, info.path) for info in info_list}
    if warned_configs is None:
        warned_configs = set()
    _warn_file_configs(file_configs.values(), warned_configs)
    needs_baselines = ctx.incremental or any(cfg.guard_enabled for cfg in file_configs.values())
    if needs_baselines:
        baseline_targets = [
            info for info in info_list if ctx.incremental or file_configs[info.path].guard_enabled
        ]
        try:
            baseline_results = read_baseline_blobs(ctx.cwd, baseline_targets)
        except GitCommandError as error:
            return fail(
                f'defsort: failed to read baselines: {error.detail}',
                allow_on_failure=ctx.allow_on_failure,
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
        )
        for info in info_list
    ]


def _without_line_endings(text: str) -> str:
    """Normalize CRLF and CR to LF, as the worktree sync comparison does."""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _plan_worktree_sync(
    ctx: HookContext,
    entry: _StagedFileEntry,
    wt_path: Path,
    file_cfg: ResolvedConfig,
) -> tuple[_WorktreeSync | None, str | None]:
    """Plan a worktree write when its text matches staging, keeping any failure reason."""
    if wt_path.is_symlink():
        return None, unstaged_reason([], of=1)
    operation = 'read'
    try:
        wt_result = read_file_result(wt_path)
        wt_source = wt_result.raw.decode(entry.encoding)
        if wt_source == entry.original:
            new_source = entry.new_source
        elif _without_line_endings(wt_source) != _without_line_endings(entry.original):
            return None, unstaged_reason([], of=1)
        else:
            operation = 'sort'
            outcome = _resolve_and_sort(
                ctx,
                wt_source,
                file_cfg=file_cfg,
                display_path=entry.path,
                baseline_entry=entry.baseline_entry,
                is_new=entry.is_new,
                emit_diagnostics=False,
            )
            if outcome.disposition is not _Disposition.CHANGE:
                return None, None
            assert outcome.new_source is not None  # noqa: S101
            new_source = outcome.new_source
    except (FileNotFoundError, UnicodeDecodeError, SyntaxError) as error:
        reason = (
            unstaged_reason([], of=1)
            if operation == 'read'
            else f'cannot sort it: {describe_error(error)}'
        )
        return None, reason
    except Exception as error:  # noqa: BLE001
        return None, f'cannot {operation} it: {describe_error(error)}'
    return _WorktreeSync(wt_path, new_source, entry.encoding, wt_result.raw), None


def _unchanged_worktree_bytes(
    entry: _StagedFileEntry, wt_path: Path
) -> tuple[bytes | None, str | None]:
    """Read an unchanged staged name's authorized bytes or its failure reason."""
    if wt_path.is_symlink():
        return None, unstaged_reason([], of=1)
    try:
        wt_result = read_file_result(wt_path)
        source = wt_result.raw.decode(entry.encoding)
    except (FileNotFoundError, UnicodeDecodeError, SyntaxError):
        return None, unstaged_reason([], of=1)
    except OSError as error:
        return None, f'cannot read it: {describe_error(error)}'
    if _without_line_endings(source) != _without_line_endings(entry.original):
        return None, unstaged_reason([], of=1)
    return wt_result.raw, None


def _agreed_staged_sync(
    group: list[Path],
    plans: dict[str, tuple[_WorktreeSync | None, str | None]],
    unsorted_names: dict[str, _StagedFileEntry],
    root: Path,
) -> _StagedSync:
    """Authorize a shared file only when every staged name needs the same result."""
    names = [path_text(path.relative_to(root)) for path in group]
    if any(str(path) not in plans and str(path) not in unsorted_names for path in group):
        return _StagedSync(names, reason=ALIAS_CONFLICT)
    needed = {
        path: None if write is None else encode_source(write.new_source, write.encoding)
        for path, (write, _) in plans.items()
    }
    unchanged = {
        str(path): _unchanged_worktree_bytes(unsorted_names[str(path)], path)
        for path in group
        if str(path) not in plans
    }
    needed.update({path: raw for path, (raw, _) in unchanged.items()})
    reasons = {path: reason for path, (_, reason) in plans.items()}
    reasons.update({path: reason for path, (_, reason) in unchanged.items()})
    differing = [
        path_text(path.relative_to(root))
        for path in group
        if reasons[str(path)] == unstaged_reason([], of=1)
    ]
    errors = [
        reason
        for reason in reasons.values()
        if reason is not None and reason != unstaged_reason([], of=1)
    ]
    if errors:
        reason = errors[0]
    elif differing:
        reason = unstaged_reason(differing, of=len(group))
    elif any(write is None and reason is None for write, reason in plans.values()):
        reason = None
    elif not names_agree(list(needed.values())):
        reason = ALIAS_CONFLICT
    else:
        return _StagedSync(names, next(write for write, _ in plans.values() if write is not None))
    return _StagedSync(names, reason=reason)


def _plan_staged_syncs(
    ctx: HookContext,
    indexed: list[_StagedFileEntry],
    others: list[_StagedFileEntry | Path],
    file_configs: dict[str, ResolvedConfig],
) -> list[_StagedSync]:
    """Plan one ordered worktree result per file, requiring all staged names to agree."""
    root = Path(ctx.repo_root)
    sorted_names = {str(root / entry.path): entry for entry in indexed}
    unsorted_names = {str(root / item.path): item for item in others if not isinstance(item, Path)}
    unreadable = [item for item in others if isinstance(item, Path)]
    syncs: list[_StagedSync] = []
    for group in group_paths(
        sorted(
            [
                *(root / entry.path for entry in sorted_names.values()),
                *(root / entry.path for entry in unsorted_names.values()),
                *unreadable,
            ],
            key=lambda path: path_text(path.relative_to(root)),
        )
    ):
        plans = {
            str(path): _plan_worktree_sync(
                ctx, sorted_names[str(path)], path, file_configs[sorted_names[str(path)].path]
            )
            for path in group
            if str(path) in sorted_names
        }
        if not plans:
            continue
        names = [path_text(path.relative_to(root)) for path in group]
        if len(group) == 1:
            write, reason = plans[str(group[0])]
            syncs.append(_StagedSync(names, write, reason))
            continue
        syncs.append(_agreed_staged_sync(group, plans, unsorted_names, root))
    return syncs


def _write_post_tool_sort(
    members: list[_PostToolSort], group: list[Path], messages: _PostToolMessages
) -> None:
    """Write one file's PostToolUse result once, unless its names need different results."""
    changed = [(member, member.new_source) for member in members if member.new_source is not None]
    if not changed:
        return
    needed: list[bytes | None] = [
        member.raw
        if member.new_source is None
        else encode_source(member.new_source, member.encoding)
        for member in members
    ]
    needed.extend([None] * (len(group) - len(members)))
    if not names_agree(needed):
        _post_tool_skip(messages, group[0], ALIAS_CONFLICT, names=group)
        return
    member, new_source = changed[0]
    write_source(member.path, new_source, encoding=member.encoding, raw=member.raw)


def _write_staged_sorts(
    ctx: HookContext,
    to_sort: list[_StagedFileEntry],
    unsorted: list[_StagedFileEntry],
    unreadable: list[Path],
    file_configs: dict[str, ResolvedConfig],
) -> Outcome:
    """Prepare sorted blobs, update the index atomically, and sync eligible worktree copies."""
    # Pass 2a: create blobs and determine working tree writes (no mutations yet)
    entries: list[IndexEntry] = []
    indexed: list[_StagedFileEntry] = []
    for entry in to_sort:
        try:
            idx_entry = prepare_index_entry(
                ctx.cwd,
                entry.path,
                entry.new_source,
                entry.mode,
                encoding=entry.encoding,
            )
        except GitCommandError as error:
            decision = fail(
                f'defsort: failed to prepare index entry for {entry.path}: {error.detail}',
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            # Its index entry stays as staged, so its sync is planned as an unsorted name's.
            unsorted.append(entry)
            continue
        entries.append(idx_entry)
        indexed.append(entry)

    # Pass 2b: apply all index entries atomically via --index-info
    if not entries:
        return allow()
    wt_writes = _plan_staged_syncs(ctx, indexed, [*unsorted, *unreadable], file_configs)
    try:
        apply_index_entries(ctx.cwd, entries)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to update git index: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )

    # Pass 2c: sync working tree (best-effort, never blocks the commit)
    _sync_staged_worktree(wt_writes)

    return Outcome('allow', _receipt(len(entries)))


def _write_worktree_entries(ctx: HookContext, entries: list[_WorktreeFileEntry]) -> Outcome:
    """Write sorted worktree files, rolling back earlier writes when a later one fails."""
    # Pass 2: write sorted content (primary operation -- fail-closed).
    writes = CommitWrites('defsort', ctx.repo_root)
    for entry in entries:
        full_path = Path(ctx.repo_root) / entry.path
        if full_path.is_symlink():
            continue
        try:
            data = write_source(
                full_path, entry.new_source, encoding=entry.encoding, raw=entry.raw
            )
            writes.record(full_path, entry.raw, data)
        except OSError as error:
            outcome = writes.failed(full_path, error, allow_on_failure=ctx.allow_on_failure)
            if outcome is not None:
                return outcome

    return allow()


def run_post_tool(
    edited_paths: Sequence[str],
    cwd: str,
    *,
    overrides: SortOverrides | None = None,
) -> Outcome:
    """Sort the Python files a post-tool payload reports as written.

    Args:
        edited_paths: File paths reported by the host adapter as edited.
        cwd: Working directory used to resolve relative edited paths.
        overrides: Hook flag overrides applied to each file's configuration.

    Returns:
        An allow outcome whose reason lists syntax errors, one per file that
        could not be parsed, in input order, or `None`. Other notes go to
        standard error;
        a successful sort adds no feedback.
    """
    if overrides is None:
        overrides = SortOverrides()
    messages = _post_tool_messages(edited_paths, cwd)
    prepared, files_by_repo, contexts, undiscovered = _prepare_post_tool_files(
        overrides=overrides,
        warned_configs=set(),
        messages=messages,
    )
    baselines, unavailable, unsupported = _fetch_post_tool_baselines(
        files_by_repo, contexts, messages
    )
    by_path = {file.path.resolve(): (root, file) for root, file in prepared}
    selected = [path for path in messages.names if path in by_path or path in undiscovered]
    for group in group_paths(selected):
        members: list[_PostToolSort] = []
        path = group[0]
        writing = False
        try:
            for path in group:
                if path not in by_path:
                    continue
                root, file = by_path[path]
                if file in unsupported or file in unavailable:
                    continue
                result = _sort_post_tool_file(
                    file, ctx=contexts[root], baseline_entry=baselines.get(file), messages=messages
                )
                if result is not None:
                    members.append(result)
            path = next(
                (member.path for member in members if member.new_source is not None), group[0]
            )
            writing = True
            _write_post_tool_sort(members, group, messages)
        except Exception as error:  # noqa: BLE001
            _post_tool_error(messages, path, error, writing=writing)
    return _post_tool_outcome(messages)


def _sort_staged_files(  # noqa: C901, PLR0911
    ctx: HookContext,
    *,
    prepared_files: list[_PreparedStagedFile] | None = None,
    warned_configs: set[ResolvedConfig] | None = None,
) -> Outcome:
    """Sort staged Python files and classify hook continuation."""
    if prepared_files is None:
        preparation = _prepare_staged_files(ctx, warned_configs=warned_configs)
        if isinstance(preparation, Outcome):
            return preparation
        prepared_files = preparation
    if not prepared_files:
        return allow()

    file_configs = {prepared.path: prepared.file_config for prepared in prepared_files}

    # Pass 1: sort each file, check for blockers (no index writes)
    to_sort: list[_StagedFileEntry] = []
    unsorted: list[_StagedFileEntry] = []
    unreadable: list[Path] = []
    blockers: list[str] = []

    for prepared in prepared_files:
        bl_entry = prepared.content
        if bl_entry is None or bl_entry.status is not BlobReadStatus.CONTENT:
            decision = fail(
                f'defsort: failed to read staged content for {prepared.path}: '
                + (
                    bl_entry.detail
                    if bl_entry is not None and bl_entry.detail
                    else 'blob missing from git cat-file output'
                ),
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            unreadable.append(Path(ctx.repo_root) / prepared.path)
            continue
        assert bl_entry.content is not None  # noqa: S101

        outcome = _resolve_and_sort(
            ctx,
            bl_entry.content,
            file_cfg=prepared.file_config,
            display_path=prepared.path,
            baseline_entry=prepared.baseline_entry,
            is_new=prepared.is_new,
            fail_on_unreadable=True,
        )
        if outcome.failure is not None:
            decision = fail(outcome.failure, allow_on_failure=ctx.allow_on_failure)
            if decision.decision == 'deny':
                return decision
            unreadable.append(Path(ctx.repo_root) / prepared.path)
            continue
        if outcome.disposition is _Disposition.BLOCK:
            assert outcome.blocker_message is not None  # noqa: S101
            blockers.append(outcome.blocker_message)
            continue
        entry = _StagedFileEntry(
            path=prepared.path,
            original=bl_entry.content,
            new_source=bl_entry.content if outcome.new_source is None else outcome.new_source,
            mode=prepared.mode,
            encoding=bl_entry.encoding,
            baseline_entry=prepared.baseline_entry,
            is_new=prepared.is_new,
        )
        (unsorted if outcome.disposition is _Disposition.SKIP else to_sort).append(entry)

    # If any file has a blocker, deny the commit without modifying the index
    if blockers:
        return Outcome('deny', '\n'.join(blockers))

    if not to_sort:
        return allow()

    return _write_staged_sorts(ctx, to_sort, unsorted, unreadable, file_configs)


def _sort_worktree_files(  # noqa: C901
    ctx: HookContext,
    paths: list[str],
    *,
    warned_configs: set[ResolvedConfig] | None = None,
) -> Outcome:
    """Sort Python files on the working tree in two passes."""
    if not paths:
        return allow()

    # Pre-fetch baselines in batch when incremental mode or guard is active.
    file_configs = {path: _file_config(ctx, path) for path in paths}
    if warned_configs is None:
        warned_configs = set()
    _warn_file_configs(file_configs.values(), warned_configs)
    baseline_resolution = _resolve_worktree_baselines(ctx, paths, file_configs)
    if isinstance(baseline_resolution, Outcome):
        return baseline_resolution
    wt_baselines = baseline_resolution

    # Pass 1: read and sort all files, check for blockers.
    # Store original content alongside new content for rollback on write failure.
    to_write: list[_WorktreeFileEntry] = []
    unreadable: list[str] = []
    blockers: list[str] = []

    for rel_path in paths:
        full_path = Path(ctx.repo_root) / rel_path
        if not full_path.is_file() or full_path.is_symlink():
            continue

        try:
            file_result = read_file_result(full_path)
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            decision = fail(
                f'defsort: cannot read {rel_path}: {describe_error(error)}',
                allow_on_failure=ctx.allow_on_failure,
            )
            if decision.decision == 'deny':
                return decision
            unreadable.append(rel_path)
            continue
        source, encoding, raw = file_result.source, file_result.encoding, file_result.raw
        bl_entry = wt_baselines.get(rel_path)
        is_new = bl_entry is None or bl_entry.status is BlobReadStatus.MISSING

        outcome = _resolve_and_sort(
            ctx,
            source,
            file_cfg=file_configs[rel_path],
            display_path=rel_path,
            baseline_entry=bl_entry,
            is_new=is_new,
            fail_on_unreadable=True,
        )
        if outcome.failure is not None:
            decision = fail(outcome.failure, allow_on_failure=ctx.allow_on_failure)
            if decision.decision == 'deny':
                return decision
            unreadable.append(rel_path)
            continue
        if outcome.disposition is _Disposition.BLOCK:
            assert outcome.blocker_message is not None  # noqa: S101
            blockers.append(outcome.blocker_message)
            continue
        to_write.append(
            _WorktreeFileEntry(
                path=rel_path,
                original=source,
                new_source=source if outcome.new_source is None else outcome.new_source,
                encoding=encoding,
                raw=raw,
            )
        )

    if blockers:
        return Outcome('deny', '\n'.join(blockers))
    agreed = _agreed_worktree_entries(ctx, to_write, unreadable)
    return agreed if isinstance(agreed, Outcome) else _write_worktree_entries(ctx, agreed)


def _handle_auto_stage(
    ctx: HookContext,
    warned_configs: set[ResolvedConfig],
) -> Outcome:
    """Handle `git commit -a` and `git commit --all`."""
    try:
        wt_paths = get_modified_tracked_py_files(ctx.cwd)
    except GitCommandError as error:
        return fail(
            f'defsort: failed to discover modified files: {error.detail}',
            allow_on_failure=ctx.allow_on_failure,
        )
    selected = _drop_private(ctx.repo_root, wt_paths, allow_on_failure=ctx.allow_on_failure)
    if isinstance(selected, Outcome):
        return selected
    wt_paths = selected
    if wt_paths:
        return _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
    return allow()


def _handle_include(
    ctx: HookContext,
    mode: CommitMode,
    warned_configs: set[ResolvedConfig],
    pathspec_records: Sequence[str] = (),
) -> Outcome:
    """Handle --include: working-tree sort for pathspecs + index sort for remaining staged."""
    result = _resolve_pathspec_modified_files(ctx, mode, pathspec_records)
    if isinstance(result, Outcome):
        return result
    wt_paths = result
    wt_path_set = frozenset(wt_paths)
    preparation = _prepare_staged_files(
        ctx,
        exclude_paths=wt_path_set,
        warned_configs=warned_configs,
    )
    if isinstance(preparation, Outcome) and preparation.decision == 'deny':
        return preparation
    if wt_paths:
        worktree_decision = _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
        if worktree_decision.decision == 'deny':
            return worktree_decision

    if isinstance(preparation, Outcome):
        return preparation

    # Sort remaining staged files, excluding pathspec files
    # (they'll be re-staged from working tree by --include)
    return _sort_staged_files(
        ctx,
        prepared_files=preparation,
        warned_configs=warned_configs,
    )


def _handle_pathspec_only(
    ctx: HookContext,
    mode: CommitMode,
    warned_configs: set[ResolvedConfig],
    pathspec_records: Sequence[str] = (),
) -> Outcome:
    """Handle --only / bare pathspec / --pathspec-from-file (without --include)."""
    result = _resolve_pathspec_modified_files(ctx, mode, pathspec_records)
    if isinstance(result, Outcome):
        return result
    wt_paths = result
    if wt_paths:
        return _sort_worktree_files(ctx, wt_paths, warned_configs=warned_configs)
    return allow()


def _sort_commit(  # noqa: C901, PLR0912
    plan: 'CommitPlan',
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
    hook_read_failure: str | None,
) -> Outcome:
    """Replay the add, then sort the content an accepted commit will consume."""
    mode = plan.mode
    replayed_add = False
    try:
        context = _resolve_commit_context(
            plan,
            overrides=overrides,
            allow_on_failure=allow_on_failure,
            hook_read_failure=hook_read_failure,
        )
        if isinstance(context, Outcome):
            return context
        ctx = context
        allow_on_failure = ctx.allow_on_failure
        warned_configs: set[ResolvedConfig] = set()

        records = _read_pathspec_records(plan.cwd, mode, allow_on_failure=allow_on_failure)
        if isinstance(records, Outcome):
            return records

        if plan.pre_commit_adds:
            replayed_add = True
            replay_failure = run_pre_commit_adds(plan.pre_commit_adds)
        else:
            replay_failure = None

        if replay_failure is not None:
            result = fail(
                f'defsort: failed to replay git add: {replay_failure}',
                allow_on_failure=allow_on_failure,
            )
        else:
            # Determine which paths need working-tree sort.
            # For --pathspec-from-file, only set has_pathspec when the file is
            # non-empty.  Git treats an empty pathspec file as "no restriction"
            # (same as a normal commit), so we must fall through to index sort.
            has_pathspec = bool(mode.pathspecs) or bool(records)

            if mode.only and not has_pathspec:
                result = allow()
            elif mode.auto_stage:
                result = _handle_auto_stage(ctx, warned_configs)
            elif has_pathspec and not mode.include:
                result = _handle_pathspec_only(ctx, mode, warned_configs, records)
            elif mode.include:
                result = _handle_include(ctx, mode, warned_configs, records)
            else:
                result = _sort_staged_files(
                    ctx,
                    warned_configs=warned_configs,
                )
    except UnsupportedGitVersionError as error:
        result = fail(str(error), allow_on_failure=allow_on_failure)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        result = fail(
            f'defsort: unexpected error during sort: {error_summary(error)}',
            allow_on_failure=allow_on_failure,
        )
    if replayed_add and result.decision == 'deny':
        reason = result.reason or 'defsort: commit blocked'
        return Outcome(
            'deny',
            f'{reason} Any files staged by the preceding git add remain staged.',
            result.notes,
        )
    return result


def run_commit(
    plan: CommitPlan,
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
) -> Outcome:
    """Sort the content consumed by a supported commit when its hook is enabled.

    Processing may replay preceding Git adds, update staged blobs, and write
    eligible working-tree files according to the commit mode. It preserves
    partial staging and does not execute the commit itself. A denial after
    replaying an add leaves those files staged.

    Args:
        plan: Commit plan accepted by classification.
        overrides: Hook flag overrides applied to each file's configuration.
        allow_on_failure: Warn instead of deny on infrastructure failures.
            Safety blockers still deny the commit.

    Returns:
        Allow when the hook is disabled for the plan's directory; otherwise the
        outcome of processing the commit.
    """
    try:
        enabled = is_hook_enabled(plan.cwd)
    except GitCommandError as error:
        hook_read_failure = error.detail
    else:
        if not enabled:
            return allow()
        hook_read_failure = None
    return _sort_commit(
        plan,
        overrides=overrides,
        allow_on_failure=allow_on_failure,
        hook_read_failure=hook_read_failure,
    )
