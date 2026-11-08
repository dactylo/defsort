# Incremental mode

Use incremental mode to place new definitions while preserving existing order in a codebase with
sorted baselines. This guide covers adoption, fallback choices, and limits on reordering.

## When to use incremental mode

Full sorting is the default. Incremental mode is opt-in and requires a committed baseline that
already follows your policy; it does not gradually repair an unsorted baseline. Establish one with
a full sort, or use the hook's full-sort fallback to adopt files as they are touched.

## Adoption workflow

To adopt defsort file by file in an existing codebase:

1. **Choose the fallback**: use `full-sort` to adopt each non-conforming file when the hook first
   encounters it. Use `warn` only when skipped legacy files are preferable to automatic adoption.

2. **Configure**: enable incremental mode and the selected fallback in `pyproject.toml`:

   ```toml
   [tool.defsort]
   incremental = true
   incremental-fallback = "full-sort"
   ```

3. **Hook integration**: register the [PreToolUse hook](hook.md#configuration). A non-conforming
   file is fully sorted on its first eligible supported commit under `full-sort`. Later commits use
   incremental insertion while the baseline remains conforming.

4. **Optional bulk adoption**: when ready, run `defsort --no-incremental -w .` to override the
   configured incremental mode and sort every remaining file in one reordering commit. You can add
   that commit to `.git-blame-ignore-revs`.

## Choose the scope

| Mode                                   | What changes                                   | Unsuitable baseline                                             |
| -------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------- |
| Full sort (default)                    | All eligible definitions and methods           | No conforming baseline needed.                                  |
| Incremental, `full-sort` fallback      | New definitions on the normal path             | Hook fully sorts non-conforming or target-incompatible files.   |
| Incremental, `warn` fallback (default) | New definitions on the normal path             | Warn and skip.                                                  |
| Either mode with a reorder guard       | As above, within the configured movement limit | Skip when a usable baseline exists and a threshold is exceeded. |

The full-sort fallback is a hook feature. The CLI skips unsuitable baselines. New files and
unpaired renames are fully sorted even with `warn`; see
[baselines and renames](#baselines-and-renames).

## Configuration

### pyproject.toml

```toml
[tool.defsort]
incremental = true            # default: false
incremental-fallback = "warn" # "warn" (default) or "full-sort" (hook only)
max-reorder-count = 5         # disabled by default
max-reorder-percent = 50      # disabled by default
evaluation-order = "preserve" # "preserve" (default) or "allow-reordering"
banners = "barriers"          # "barriers" (default), "ignore", or "strip"
```

### CLI flags

| Flag               | Short | Description                         |
| ------------------ | ----- | ----------------------------------- |
| `--incremental`    | `-i`  | Enable incremental mode             |
| `--no-incremental` |       | Explicitly disable incremental mode |

These two flags are mutually exclusive.

### Precedence

CLI flags always override `pyproject.toml`. The resolution chain:

1. `--no-incremental` -- full mode, regardless of config.
2. `-i` / `--incremental` -- incremental mode.
3. `[tool.defsort].incremental` -- use config value.
4. Context default -- full mode for both CLI and hooks.

## Baselines and renames

Incremental mode tests the committed baseline against the current effective ordering policy. A
policy change can therefore invalidate a baseline that conformed under the previous policy. This
includes adopting new defaults or changing strategies, method-body scope, categories, `main()`
placement, tie-breaking, evaluation order, or banner recognition.

A baseline that no longer matches the target reports `non_conforming`; one whose existing items
cannot reach the new target without reordering reports `target_incompatible`. The CLI skips that
file. The hook either warns and skips or uses its configured full-sort fallback. Full sorting
remains the canonical way to establish a baseline for the new policy.

Changing `evaluation-order` does not make incremental mode reorder definitions or methods already
present in the baseline. Incremental output therefore need not match a whole-file sort under the
new value. Run `defsort --no-incremental -w .` when you want to apply it to every eligible item.

A rename paired by Git uses the source file's HEAD baseline. A plain unstaged move or a move below
Git's similarity threshold may not be paired; it is then treated as a new file and fully sorted.
New files and repositories with no commits do not need a conforming baseline.

## Reorder guard

The reorder guard limits how many existing top-level definitions may move during full sorting or an
incremental full-sort fallback when a usable `HEAD` baseline exists. If sorting would exceed the
configured count or percentage, the file is skipped. The guard does not measure changed lines or
method movement, so it does not guarantee a small diff or unchanged blame attribution.

```toml
[tool.defsort]
incremental = true
incremental-fallback = "full-sort"
max-reorder-count = 5              # skip if more than 5 existing definitions would move
max-reorder-percent = 50           # skip if more than 50% would move
```

Either threshold can be set independently. When both are set, either being exceeded triggers the
guard (OR logic). Default: neither set (guard disabled).

An effective guard requires a Git worktree. Files outside one are skipped with
`not in a Git repository`. A repository-level Git failure stops the CLI with exit 2 before output
or writes and names Git's reason. `--force` bypasses the guard and this requirement; incremental
mode still requires Git when enabled.

"Existing definitions" means top-level definitions present in both git HEAD and the sorted output.
New definitions added since HEAD and deleted definitions are excluded from the count. Method-level
reorders within classes are not counted.

When a usable `HEAD` baseline exists, the guard applies to both full sort mode and incremental
full-sort fallback. Renames paired by Git use the source file's baseline for the guard. New files,
unborn repositories, and files Git does not pair as renames are full-sorted without guard
protection. The guard does not apply to incremental mode's normal path (existing definitions are
not reordered there).

The CLI's `--force` flag bypasses the guard. The hook has no `--force` equivalent -- guarded files
are skipped with a stderr warning.

## Skip conditions

Incremental mode skips or falls back for certain files:

Baseline validation requires executable module syntax, not only an AST. Constructs that
`ast.parse()` accepts but module compilation rejects, such as a top-level `return`, `break`, or
`nonlocal`, are treated as non-conforming baselines. Incremental sorting leaves the current source
unchanged so users can commit a repair.

| Condition                  | CLI behavior                                                             | Hook behavior                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| New file (not in HEAD)     | Falls back to full sort                                                  | Falls back to full sort                                                                                                                  |
| Unborn repo (no commits)   | Falls back to full sort                                                  | Falls back to full sort                                                                                                                  |
| Not a git repo             | File is skipped with `not in a Git repository`                           | PostToolUse leaves the file unchanged and reports `not in a Git repository`                                                              |
| Non-conforming baseline    | Warned + skipped                                                         | Warned + skipped; full-sort fallback if configured                                                                                       |
| Target order incompatible  | Warned + skipped                                                         | Warned + skipped; full-sort fallback if configured                                                                                       |
| User reordered from HEAD   | Warned + skipped                                                         | Warned + skipped                                                                                                                         |
| Renamed file               | Uses the source file's baseline when Git pairs the rename; otherwise new | Uses the source file's baseline when Git pairs the rename; otherwise new                                                                 |
| Git older than 2.42        | Exit 2 before output or writes                                           | Staged-index sorting works; path-based modes fail (see below).                                                                           |
| Repository-level Git error | Exit 2 before output or writes, with Git's reason                        | PreToolUse denies by default or warns and allows with `allow-on-failure`; PostToolUse leaves the file unchanged and reports Git's reason |

defsort finds a file's baseline under Git's spelling of it, so a path written with different
capital letters uses the same baseline. A spelling that `HEAD` holds exactly is used as given. If
`HEAD` lacks the spelling given and Git tracks more than one spelling that is this file, defsort
skips it, as it does a baseline it cannot read.

When a file is skipped, the CLI reports the reason in its output. The hook emits a warning to
stderr for blocker-based skips (non-conforming, target incompatible, user reordered).
Repository-level PostToolUse baseline failures report
`defsort: PostToolUse skipped NAME: incremental mode cannot use Git: DETAIL`, naming the reorder
guard instead or in addition when effective. Individual unreadable or ambiguous baselines retain
their per-file skip policy. A file whose Git repository cannot be determined is skipped with a note
on stderr; see the [hook guide](hook.md).

**Note:** When a file has top-level drift (e.g., non-conforming baseline) but also has new methods
that can be sorted, the CLI applies the method sorts and reports the top-level skip as a warning.
The hook's default `warn` handling emits a stderr warning and clears the candidate, leaving the
entire file unchanged. In `"full-sort"` fallback mode, the hook replaces the incremental attempt
with a full sort.

### Repository failures

A repository-level failure reports Git's reason on stderr as
`error: incremental mode cannot use Git: DETAIL; pass --no-incremental to process whole files`. A
guard alone reports `error: the reorder guard cannot use Git: DETAIL`; when both modes apply, the
line names `incremental mode and the reorder guard` and offers no disabling flag. Missing Git,
untrusted repositories, and failed baseline or file-list queries all stop the run. Individual
unreadable or ambiguous baselines still skip only their files.

Git 2.42 or newer is required for path-based baselines in CLI, PostToolUse, and working-tree commit
modes; staged-index commits read by hash and work with older Git. On POSIX, a filename without a
strict UTF-8 representation has an unavailable baseline and is skipped, while other encodable files
continue. See [platform requirements](platform.md#requirements).

## Banner boundaries

In `barriers` mode, recognized top-level and method banners divide the current source into
sections. The baseline and current source are parsed independently. Baseline conformance uses the
baseline's sections; target compatibility and insertion use the current source's sections. The
current boundary layout is authoritative.

Incremental sorting applies this complete boundary-edit policy:

- Adding, removing, or moving a boundary is a legal comment edit when the global order of existing
  definitions or methods does not change. A boundary edit alone does not create a blocker.
- Existing definitions or methods may change section membership when a boundary moves. This is
  legal while their global relative order remains unchanged. A global existing-item reorder still
  produces `user_reordered`.
- Each current section is checked against its configured target independently. If the existing
  items in one current section conflict with that target, the result is `target_incompatible`.
- New definitions and methods merge only into the section assigned by the current source. They do
  not cross a current boundary to reach a global target position.
- A boundary-only edit with no new definitions is preserved byte-for-byte in `barriers` mode.
  Incremental sorting does not reassemble the file merely to normalize its spacing.
- `strip` is an independent source transformation. It removes recognized boundaries even when there
  are no new definitions or methods and even when both ordering scopes are `none`.

`ignore` and `strip` use unconstrained global ordering for merge decisions. In all modes, method
sorting applies the same policy within each class body as top-level sorting applies to the module.

Class-local blockers remain local in both full and incremental mode. A blocked class keeps its
method order and contributes a `class_blockers` record, but it never blocks safe top-level changes
or work in another class. Layout-only transformations still run the shared structural gates:
hoisting or stripping cannot bypass interstitial-code or duplicate checks. In `strip` mode, a class
with one method also removes its recognized method banner in both modes.

## Actual-candidate safety

Incremental safety is evaluated against what the merge actually proposes, not the ideal full-sort
order. Direct eager reads preserve exact binding identities in both scopes: top-level definitions
are compared with the merged top-level candidate, and each current class's methods are compared
with its incremental method candidate. A candidate blocks when it changes the binder visible to a
covered read, including when a newly preceding definition replaces a preamble assignment or
constant binding. Unchanged unresolved reads remain non-worsening.

With `evaluation-order = preserve`, the merge also preserves the relative source order of eager
groups in both scopes. Defaults and decorators make function and method groups eager; decorated
classes and classes with eager reads are eager at top level. `allow-reordering` removes only this
relative-order pin for new items that incremental mode may place. Existing items retain their
baseline order, and final `eager_order` validation still rejects binding-identity changes under
either value.

Sibling-method annotation references use the same exact-binding-identity comparison under the
method rules. Top-level annotations retain the existing top-level rule instead: once the top-level
order changes, defsort checks the changed candidate for forward annotation references and either
preserves or adds the future import, or blocks when addition is disabled. This top-level annotation
check applies to the proposed order once it changes.

Structural and policy checks remain authoritative when they prevent an unambiguous candidate. These
include applicable `interstitial`, `duplicates`, `non_conforming`, `target_incompatible`, and
`user_reordered` results.

Once actual top-level and method candidates exist, semantic hazards are aggregated across both
scopes. `eager_order` precedes `forward_refs`, even when the eager and annotation hazards occur in
different scopes. This prevents a future import from being presented as a repair for an eager
decorator, default, base, class-keyword, or class-suite read.

The CLI's final orchestration also examines accepted method-only candidates. A sibling-method
annotation inversion can therefore add `from __future__ import annotations` even when the top-level
order did not change. Incremental hook sorting never adds the import. It removes an existing
standalone annotations-only import only from an annotation-free module. Public `defsort.sort()` and
`defsort.check()` with a baseline neither add nor remove it.

Full-mode safety checks also apply. See the [safety model](design.md#safety-model) for blockers,
force limits, and annotation behavior.

With `banners = "barriers"`, duplicate detection is section-aware in both modes. Consecutive
same-name definitions form groups only within one section; repeating the name in another section
still creates separate duplicate groups and blocks sorting.

## Hook integration

Use one event mode and serialize modifying hooks under one registration; see
[hook composition](hook.md#composing-with-other-hooks). Normal staged commits preserve partial
staging through the [working-tree synchronization rules](hook-reference.md#working-tree-sync).
Commit pathspecs use the separate [working-tree path](hook-reference.md#working-tree-path).
