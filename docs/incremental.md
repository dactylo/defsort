# Incremental mode

By default, defsort fully sorts every file -- no config needed. Incremental mode is an opt-in way
to limit later churn after a file's committed baseline already follows the configured order. For an
unsorted legacy file, use a full sort to establish that baseline or let the hook's `full-sort`
fallback adopt the file when it is first touched.

## When to use incremental mode

You have a large codebase where sorting every file in one commit would produce disruptive diffs.
You want to establish sorted baselines file by file, then keep existing definitions stable while
placing new definitions.

On its normal path, incremental mode sorts only *new* definitions -- those added since git HEAD --
and preserves the relative order of existing definitions. The committed baseline must already
conform to the current policy. Incremental insertion does not repair or gradually converge an
unsorted baseline; such a baseline is skipped or sent through the hook's configured fallback.

If you don't have this problem, you don't need incremental mode. The default (full sort) is what
you want.

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

3. **Hook integration**: register the PreToolUse hook. A non-conforming file is fully sorted on its
   first eligible supported commit under `full-sort`. Later commits use incremental insertion while
   the baseline remains conforming.

4. **Optional bulk adoption**: when ready, run `defsort --no-incremental -w .` to override the
   configured incremental mode and sort every remaining file in one reordering commit. You can add
   that commit to `.git-blame-ignore-revs`.

## Range of conservativeness

defsort offers a spectrum from aggressive (sort everything) to conservative (sort as little as
possible). Each level adds one more safety net.

### Full sort (default)

Every file is fully sorted on every supported commit. Zero config. Use this unless you have a
reason not to.

### Incremental with full-sort fallback

New definitions are sorted into place when the baseline conforms. Files that cannot be handled
incrementally because the baseline is non-conforming or target-incompatible fall back to a full
sort. This supports file-at-a-time adoption, but each fallback fully reorders that file.

The full-sort fallback is a hook feature. The CLI skips blocked files and reports the reason.

```toml
[tool.defsort]
incremental = true
incremental-fallback = "full-sort"
```

### Incremental with warn (skip)

New definitions are sorted when the baseline conforms. Non-conforming or target-incompatible files
are skipped with a warning rather than adopted. Existing definitions stay in place on the normal
path. Without a current-path `HEAD` baseline, defsort treats a file as new and full-sorts it unless
PreToolUse detects and skips a staged rename. This is the most conservative incremental setting
without the reorder guard.

```toml
[tool.defsort]
incremental = true
# incremental-fallback defaults to "warn" (skip)
```

### Incremental with reorder guard

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

"Existing definitions" means top-level definitions present in both git HEAD and the sorted output.
New definitions added since HEAD and deleted definitions are excluded from the count. Method-level
reorders within classes are not counted.

When a usable `HEAD` baseline exists, the guard applies to both full sort mode and incremental
full-sort fallback. New files, unborn repositories, and renamed current paths without a baseline
are full-sorted without guard protection, except that PreToolUse skips a detected staged rename.
The guard does not apply to incremental mode's normal path (existing definitions are not reordered
there).

The CLI's `--force` flag bypasses the guard. The hook has no `--force` equivalent -- guarded files
are skipped with a stderr warning.

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

### Policy changes and baselines

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

## Skip conditions

Incremental mode skips or falls back for certain files:

Baseline validation requires executable module syntax, not only an AST. Constructs that
`ast.parse()` accepts but module compilation rejects, such as a top-level `return`, `break`, or
`nonlocal`, are treated as non-conforming baselines. Incremental sorting leaves the current source
unchanged so users can commit a repair.

| Condition                 | CLI behavior                            | Hook behavior                                                                                                           |
| ------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| New file (not in HEAD)    | Falls back to full sort                 | Falls back to full sort                                                                                                 |
| Unborn repo (no commits)  | Falls back to full sort                 | Falls back to full sort                                                                                                 |
| Not a git repo            | File is skipped                         | Not applicable                                                                                                          |
| Non-conforming baseline   | Warned + skipped                        | Warned + skipped; full-sort fallback if configured                                                                      |
| Target order incompatible | Warned + skipped                        | Warned + skipped; full-sort fallback if configured                                                                      |
| User reordered from HEAD  | Warned + skipped                        | Warned + skipped                                                                                                        |
| Renamed file              | Treated as new; falls back to full sort | PreToolUse skips staged rename; PostToolUse treats missing current-path baseline as new and full-sorts                  |
| Git older than 2.42       | Exit 2 before output or writes          | See [hook infrastructure policy](hook.md#infrastructure-failures)                                                       |
| Infrastructure error      | Skipped                                 | PreToolUse denies by default or warns and allows with `allow-on-failure`; PostToolUse skips transient baseline failures |

When a file is skipped, the CLI reports the reason in its output. The hook emits a warning to
stderr for blocker-based skips (non-conforming, target incompatible, user reordered). Transient
PostToolUse infrastructure skips remain silent.

**Note:** When a file has top-level drift (e.g., non-conforming baseline) but also has new methods
that can be sorted, the CLI applies the method sorts and reports the top-level skip as a warning.
The hook's default `warn` handling emits a stderr warning and clears the candidate, leaving the
entire file unchanged. In `"full-sort"` fallback mode, the hook replaces the incremental attempt
with a full sort.

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

## Blockers

Some conditions that block full sorting also block incremental sorting:

| Blocker           | Description                                                                |
| ----------------- | -------------------------------------------------------------------------- |
| Interstitial code | Code between definitions prevents safe sorting                             |
| Eager order       | Candidate changes a covered binding, or eager-group order under `preserve` |
| Forward refs      | Forward annotation references; add `from __future__ import annotations`    |
| Duplicates        | Non-consecutive definitions with the same name                             |

These blockers apply in both full and incremental mode. `--force` can bypass interstitial blockers,
but it does not select `allow-reordering` and never bypasses duplicate names, applicable eager
order, or unresolved forward annotations.

With `banners = "barriers"`, duplicate detection is section-aware in both modes. Consecutive
same-name definitions form groups only within one section; repeating the name in another section
still creates separate duplicate groups and blocks sorting.
