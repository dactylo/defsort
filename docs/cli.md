# CLI reference

Use the CLI to inspect definition order, preview changes, and apply a project policy. Files stay
unchanged unless you pass `--write`.

```bash
defsort .         # summarize proposed changes
defsort --diff .  # inspect the patch
defsort --write . # apply eligible changes
```

Use `--preview` when you want to see which definitions move, `--detect` to describe existing order,
or `--json` to consume results in another tool. Read [concepts and safety](design.md) when choosing
an ordering policy, and [incremental mode](incremental.md) when adopting it gradually. Agent
registration belongs in the [hook guide](hook.md).

## Synopsis

```text
defsort [options] [PATH ...]
```

`defsort` accepts `.py` files or directories (recursively expanded to `**/*.py`). Non-`.py` files
are silently ignored. Recursive collection always prunes common non-source directories (`.git`,
`__pycache__`, `.venv`, `build`, `dist`, etc.), including inside Git worktrees. In a Git worktree,
directory expansion otherwise includes tracked files and untracked, non-ignored files while
honoring standard Git exclusions. An explicitly named Python file remains eligible even when Git
ignores it. Nested worktrees apply their own tracked state and exclusions. Symlinks are processed
when passed explicitly but skipped during directory recursion. Paths default to `.` when omitted.
`hook` is the agent hook entry point; `-a, --agent NAME` selects the coding agent whose protocol
the adapter handles (see the [agent hook guide](hook.md)).

## Options

### Output modes

| Flag            | Short | Description                                         |
| --------------- | ----- | --------------------------------------------------- |
| `--preview`     | `-p`  | Show where definitions would move                   |
| `--diff`        | `-d`  | Show a unified diff                                 |
| `--write`       | `-w`  | Rewrite files in place                              |
| `--json`        | `-j`  | Print results as JSON                               |
| `--show-policy` |       | Show the ordering policy that applies to each path  |
| `--detect`      |       | Report each file's current order instead of sorting |
| `--version`     |       | Show the version and exit                           |

`--version` prints `defsort <version>` from installed distribution metadata and exits 0. If
metadata is unavailable, it reports an error and exits 2. Other arguments are ignored only after
successful parsing.

Without a mode flag, `defsort` shows a columnar summary table of files that would change. Preview,
diff, JSON, write, and policy display are mutually exclusive. Detect mode cannot be combined with
preview, diff, write, or policy display. `--json` accepts `--detect` but ignores it; JSON records
already contain detection fields.

### Ordering policy

Each policy flag overrides the matching `[tool.defsort]` key for every processed file. Defaults
below are the built-in defaults; `order` and scoped configuration can change the effective
strategy.

| Flag                  | Short | Values                                             | Default             |
| --------------------- | ----- | -------------------------------------------------- | ------------------- |
| `--order`             | `-o`  | `dependency`, `alphabetical`, `none`               | `dependency`        |
| `--definition-order`  |       | `dependency`, `alphabetical`, `none`               | `dependency`        |
| `--method-order`      |       | `dependency`, `alphabetical`, `none`               | `dependency`        |
| `--evaluation-order`  |       | `preserve`, `allow-reordering`                     | `preserve`          |
| `--method-bodies`     |       | `within-categories`, `ignore`, `across-categories` | `within-categories` |
| `--method-visibility` |       | `mixed`, `public-first`                            | `mixed`             |
| `--method-dunders`    |       | `before-regular`, `after-regular`                  | `before-regular`    |
| `--main`              |       | `last`, `sorted`                                   | `last`              |
| `--tie-break`         |       | `alphabetical`, `source`                           | `alphabetical`      |
| `--banners`           |       | `barriers`, `ignore`, `strip`                      | `barriers`          |
| `--banner-pattern`    |       | Regular expression                                 | unset               |
| `--no-banner-pattern` |       | Clear a configured custom pattern                  |                     |

`order` sets both scope strategies. A scoped flag then overrides its scope. Use `-o alphabetical`
to select alphabetical ordering for both scopes.

`method-bodies` affects dependency method order. `within-categories` uses body relationships only
inside the configured structural category. `ignore` uses definition-time relationships only.
`across-categories` lets body relationships cross categories, which then break ties.

For strict category-plus-name order, set `method-order = "alphabetical"` or pass
`--method-order alphabetical`. To retain definition-time dependency ordering but omit body-derived
ordering preferences, set `method-bodies = "ignore"` or pass `--method-bodies ignore`. For classes
without definition-time relationships, `ignore` therefore usually produces alphabetical method
order. Definition-time requirements remain safety constraints under every strategy; see
[Ordering Safety and Future Annotations](#ordering-safety-and-future-annotations) and
[Method Sorting](design.md#method-sorting).

`method-visibility = public-first` splits regular methods into public and private categories;
`mixed` keeps them together. `method-dunders` places non-lifecycle dunders before or after regular
methods. `__new__` and `__init__` remain the first two categories.

`main = last` anchors a top-level function named `main` last unless another definition reads it at
definition time. Under `evaluation-order = preserve`, an eager `main()` also keeps its source order
relative to other eager groups, so it can remain earlier. `main = sorted` removes the anchor.
`tie-break` affects dependency order: `alphabetical` breaks ties by name, while `source` preserves
the original order among ready items. An unconnected method appended at the end therefore stays
there under `source`.

`evaluation-order = preserve` keeps the relative source order of groups with decorators, defaults,
or other covered definition-time evaluation. `allow-reordering` removes only that conservative pin
for top-level definitions and methods whose scopes are enabled. It explicitly accepts that their
relative execution effects may change. defsort does not infer purity or maintain a decorator list;
binding dependencies and the safety checks below still apply.

### Other modifiers

| Flag           | Short | Description                                                             |
| -------------- | ----- | ----------------------------------------------------------------------- |
| `--show-skips` |       | List skipped files in summary, diff, and write output (default: on)     |
| `--hide-skips` |       | Do not list skipped files in summary, diff, and write output            |
| `--force`      | `-f`  | Sort files that have code between definitions or exceed a reorder limit |
| `--verbose`    | `-v`  | Also list unchanged files                                               |
| `--color`      |       | When to use color: `auto` (default), `always`, or `never`               |
| `--hoist`      |       | Hoist safe `UPPER_SNAKE_CASE` constants from between defs to preamble   |
| `--no-future`  |       | Don't auto-add `from __future__ import annotations` for forward refs    |

### Banner handling

A built-in banner is a comment-only block between definitions or methods that contains a rule line.
A rule line consists of `#`, optional horizontal whitespace, and four or more identical `-`, `=`,
`*`, `#`, or `~` characters. It may be a bare rule, part of a box, or a separate rule line beside a
title comment. A same-line form such as `# ---- Helpers` needs a custom `banner-pattern`.

`--banner-pattern REGEX` adds full-match recognition for scope-relative comment lines to the
built-in rule. The input includes the leading `#` and excludes the line ending. A match recognizes
the entire surrounding comment block. Quote the expression as required by your shell. The accepted
subset consists of literals, escaped atoms other than decimal backreferences, character classes,
`.`, an optional leading `^`, an optional trailing `$`, and at most one `*` or `+` attached to an
atom in the entire expression. For example, `^# Section: .+$` is supported. Grouping, alternation,
lookaround, counted repetition, `?`, backreferences, misplaced anchors, unattached repetition, and
multiple repetitions are rejected. A literal `{` or `}` must be written as `\{` or `\}`, or placed
inside a character class, even though `re` alone would read a lone brace literally. An invalid or
unsupported CLI expression is a usage error with exit code 2.

`--no-banner-pattern` clears a custom pattern inherited from configuration and restores built-in
recognition for the invocation. It conflicts with `--banner-pattern`.

`--banners` selects one of three modes:

- `barriers` preserves recognized banners and sorts definitions and methods only within their
  sections.
- `ignore` skips banner recognition and sorts globally.
- `strip` deletes recognized banner blocks and sorts globally. Removal is independent of
  `--definition-order` and `--method-order`; `none` does not protect a recognized banner.

`strip` is intentionally destructive. A broad custom pattern can match one ordinary comment line
and cause its entire surrounding comment block to be deleted. Shebangs, encoding cookies, directive
comments, and preamble or postamble blocks remain protected, but inspect a diff before using
`--write --banners strip`.

### Incremental mode

| Flag               | Short | Description                                                             |
| ------------------ | ----- | ----------------------------------------------------------------------- |
| `--incremental`    | `-i`  | Sort only definitions added since the last commit (requires Git)        |
| `--no-incremental` |       | Sort every definition, even when configuration enables incremental mode |

`-i` / `--incremental` and `--no-incremental` are mutually exclusive.

## Output

### Default mode (summary)

Columnar table showing sort statistics per file (Name, Defs, Methods, Total, Move, Move%). Without
`-v`, only files with changes are shown. When 2+ files have changes, a TOTAL row aggregates the
changed files. Filenames and percentages of files with nonzero moves use severity coloring
(yellow). File is not modified.

- **Already ordered**: silent. Use `-v` to include in the table with zero counts.
- **Skipped (safety)**: prints `<path>: skipped (<reason>)` on stderr.

### Preview mode (`--preview`)

Prints movement tables for definitions and methods. By default, only moved rows are shown. Use `-v`
to include already-ordered files and show all rows in changed or skipped movement tables. Unchanged
verbose rows show `-` in the `Move` column and remain uncolored; moved rows use spectral colors
when color is enabled. Method tables include a class header with moved/total counts. File is not
modified.

Skipped safety blockers expand on stderr. A detailed block starts with `path: skipped`, followed by
the reason, any applicable remedy, blocking line previews, and the hypothetical proposed reorder.
Eligible interstitial and reorder-guard blockers include a `--force` remedy. Duplicate-name and
other non-bypassable blockers do not. `--hide-skips` does not hide preview diagnostics.

### Diff mode (`--diff`)

Prints unified diff to stdout. File is not modified.

### Write mode (`--write`)

Writes changes in place and prints a one-line summary for each file written successfully. A file
whose write fails appears only in the error output, not in the written-file summary.
Already-ordered files are silent unless `-v` is used. Default, diff, and write modes keep skipped
diagnostics compact:

```text
path.py: skipped (code between definitions)
```

If stdout closes while defsort is producing or flushing output, defsort stops writing, examines no
further files, prints no traceback, and exits with the status of the work completed so far: for
example, 1 when an interrupted diff had changes.

### Warnings

Printed to stderr:

- Safety check warnings (interstitial code, eager-order hazards, forward annotation refs, and
  duplicate names).
- Blocking interstitial code previews (truncated to 60 chars).
- Invalid or unsupported configured `banner-pattern` expressions. The pattern is ignored after the
  warning.
- Non-default policy values that have no effect under the resolved strategies.

Ineffective-policy warnings depend on the resolved value, not on whether it came from config or a
CLI flag. Default values never warn. defsort warns for:

- non-default `method-bodies` when method order is not `dependency`;
- `tie-break = source` when neither scope uses dependency order;
- non-default `method-visibility` or `method-dunders` when method order is `none`; and
- `main = sorted` when definition order is `none`.

These warnings do not reject the invocation. defsort ignores the ineffective value.

In `barriers` mode, a constrained file also prints
`defsort: <path>: sort constrained by <n> banner(s)` to stderr. Preview and verbose output add one
line per boundary with the source line, banner label, held definitions or methods, and crossing
directions. JSON mode records the same information without writing this diagnostic.

## Entry point

The installed `defsort` command and `python -m defsort` accept the same arguments. Use the module
form when you need to select a particular Python environment:

```bash
python -m defsort --diff .
```

## Detect mode (`--detect`)

`--detect` classifies each file's current definition order without modifying anything. It uses the
same effective banner mode and custom pattern as sorting and JSON detection fields. Use `--json`
for machine-readable records containing `order`, `dep_score`, and `alpha_score`; adding `--detect`
does not change JSON output, exit status, or Git baseline requirements. Detection cannot be
combined with `--preview`, `--diff`, `--write`, or `--show-policy`.

Default output (one line per file):

```text
src/foo.py                               dependency
src/bar.py                               alphabetical
src/baz.py                               unordered
```

With `-v`/`--verbose`, Kendall tau scores are shown alongside the classification:

```text
src/foo.py                               dependency    (tau: 0.92)
src/bar.py                               alphabetical  (tau: 0.85)
src/baz.py                               unordered     (dep tau: 0.12, alpha tau: -0.30)
```

Classifications:

| Label          | Meaning                                              |
| -------------- | ---------------------------------------------------- |
| `dependency`   | Matches dependency order                             |
| `alphabetical` | Matches alphabetical order                           |
| `both`         | Matches both (the two orderings agree for this file) |
| `unordered`    | Matches neither                                      |

A summary line is printed after the file list: `N files: X dependency, Y alphabetical, ...`. Files
with fewer than 2 definitions are skipped (shown only with `-v`).

In `barriers` mode, each section is classified independently and sections with fewer than two
definitions are neutral. Cross-section pairs do not affect the classification or tau scores.
defsort sums discordant and total pair counts across comparable sections, so each section is
weighted by its number of pairs. If every section is neutral, the file is skipped. `ignore` and
`strip` detect global order; detection never changes the file.

Detection classifies top-level definitions against canonical dependency and alphabetical targets.
It does not test conformance with non-default policy axes. A file that conforms under
`tie-break = source`, `main = sorted`, or `evaluation-order = allow-reordering` can classify as
`unordered`; with `definition-order = none`, the result is informational. To test the effective
policy, run `defsort` normally and check that it proposes no changes.

## Incremental mode (`-i`)

`-i`/`--incremental` limits sorting to new definitions -- those not present in git HEAD. Existing
definitions are left in their current positions. New definitions are inserted at their target
positions in dependency or alphabetical order.

Git-backed CLI sorting requires Git 2.42 or newer. This includes incremental mode and any run whose
effective configuration enables a reorder guard. An older Git exits 2 before text or JSON output
and before any source write. Ordinary full sorting without a guard remains Git-independent.

On POSIX, a Python filename can contain bytes that have no strict UTF-8 representation. Such a path
cannot be placed in defsort's NUL-framed Git baseline request, so incremental and reorder-guard
modes classify that file as having an unavailable baseline and skip it. Other encodable files in
the same repository continue through the shared batch request.

Use this to sort code as you write it without disturbing definitions already committed.

- **Renamed file**: a renamed path has no `HEAD` baseline under its new name, so an explicit
  incremental CLI invocation treats it as new and full-sorts it.
- **New file** (not in HEAD): falls back to full sort.
- **Unborn repo** (no commits): falls back to full sort.
- **Infrastructure error** (not a git repo): file is skipped.
- **Non-conforming baseline** (HEAD not in target order): skipped for that file.

## Source preservation

Ordinary sorting is a lossless positional block permutation. defsort moves complete definition or
method blocks, including their decorators and owned comments, into the existing source slots. It
preserves every character outside those blocks, including blank lines, whitespace-filled lines,
line-ending sequences, preambles, postambles, class headers and footers, final-newline state, and
fixed banner layout. File and Git I/O preserve that exact source text through decoding and
encoding.

Complete separators between adjacent members of a consecutive same-name definition group or
property group move intact with that group. Other separators and their ownership metadata are
reassigned to the remaining source positions.

To keep an owned trailing comment distinct from the next sortable item, defsort may relocate at
most one authored blank line per affected boundary. It preserves that line exactly and does not
change the suffix or final-newline state. At the sole ambiguous mixed-ending seam, it may order an
adjacent lone CR and LF as LF then CR so they remain two physical endings instead of one CRLF
ending. Every authored byte remains present; this is not general line-ending normalization.

defsort does not implement top-level or method blank-line style. Run a formatter separately when
you want it to enforce a style convention.

Three requested transformations have narrow authority to change more than block position:

- `--hoist` moves each approved constant assignment's exact physical span plus consecutive authored
  blank lines immediately after it, or consecutive blank lines immediately before it when none
  follow, into the positional prefix.
- Future-import management inserts or removes only the authorized
  `from __future__ import annotations` span and any minimum line terminator needed for insertion.
- `--banners strip` removes only recognized banner spans.

These transformations preserve all source outside their owned spans; none authorizes whitespace or
line-ending normalization.

## Ordering safety and future annotations

defsort rejects an actual full or incremental candidate that changes the binding seen by a covered
definition-time read. These reads include decorators, defaults, class bases and keywords, and
executable class-suite expressions. With `evaluation-order = preserve`, `eager_order` also rejects
a candidate that changes the relative source order of eager groups. With `allow-reordering`, only
that relative-order check is removed; binding-identity checks remain. A future import cannot repair
an `eager_order` blocker.

`--force` bypasses interstitial-code and large-reorder-guard blockers. It does not select
`allow-reordering` and never bypasses duplicate-name, applicable `eager_order`, or unresolved
forward-annotation blockers. A skipped preview for a non-bypassable blocker therefore does not
suggest `--force`; duplicate-name blockers recommend removing the duplicate definition.

Both full and incremental CLI sorting manage the future import with the same rules:

| CLI mode    | Add `from __future__ import annotations`         | Remove it                                                   |
| ----------- | ------------------------------------------------ | ----------------------------------------------------------- |
| Full        | When sorting creates a covered annotation hazard | When annotation-free with no `annotations` name occurrences |
| Incremental | When sorting creates a covered annotation hazard | When annotation-free with no `annotations` name occurrences |

An annotation-free module has no annotated assignment and no function or async-function parameter
or return annotation at any depth. Removal also preserves the import whenever the name
`annotations` occurs as a load, store, or deletion, including in a local scope. This protects live
uses such as `annotations.compiler_flag`; reflective uses through string names are not detected.
`--no-future` disables automatic addition, so a covered annotation hazard without an existing
import is blocked as `forward_refs`; it does not weaken `eager_order`.

When policy authorizes removal, automatic cleanup removes every eligible unused duplicate in one
sort. Each deletion uses a direct top-level, unaliased, annotations-only import's exclusively owned
physical-line span. Python's parser accepts legal spacing, parentheses, trailing commas, and line
continuations, including backslashes. Matching docstring text, aliased or combined imports, and
comments or peer statements on the candidate lines remain unchanged. Cleanup removes all eligible
duplicates so another sort produces the same result.

On Python 3.14 and later, automatic addition intentionally selects the future import's stringized
annotation representation instead of Python 3.14's default deferred annotation values. defsort uses
this conservative cross-version policy without project-version inference.

## Configuration (`[tool.defsort]`)

defsort reads `[tool.defsort]` from `pyproject.toml`, discovered by walking up from each input file
and stopping at the git repository root. The nearest `pyproject.toml` wins outright: a missing
`[tool.defsort]` section, an empty section, or an unreadable file yields default settings rather
than inheriting a parent project's configuration.

### Multi-project configuration

Summary, preview, diff, write, and JSON modes resolve configuration for each file, so one
invocation may span projects and repositories.

Explicit CLI flags apply invocation-wide and override every file's project configuration. Without
an explicit flag, each file uses its own resolved transformation, incremental, guard, and display
settings.

Color is an invocation-level property of the output streams. If all collected files resolve to the
same `color` value, that value applies to the invocation. Conflicting project values select
automatic terminal detection, independent of argument order.

Start with only the settings you want to change. For example, preserve method order while keeping
the default dependency ordering at module scope:

```toml
[tool.defsort]
method-order = "none"
```

The [ordering policy table](#ordering-policy) lists transformation keys and defaults. Operational
settings control selection, output, and hook failures:

```toml
[tool.defsort]
incremental = false           # full sorting; opt in only when a baseline is useful
incremental-fallback = "warn" # hook only: skip a non-conforming baseline
color = "auto"
allow-on-failure = false      # hook infrastructure errors deny by default
show-skips = true
# Optional churn limits; neither is enabled by default:
# max-reorder-count = 5
# max-reorder-percent = 50
```

See [incremental mode](incremental.md) before enabling a fallback or reorder guard.

The transformation keys are `definition-order`, `method-order`, `evaluation-order`,
`method-bodies`, `method-visibility`, `method-dunders`, `main`, `tie-break`, `banners`, and
`banner-pattern`. They resolve to the corresponding `DefsortConfig` fields. `order` is a
configuration shorthand that sets both scoped orders; it is not a `DefsortConfig` field. The public
dataclass also has a programmatic `force` field, but `[tool.defsort]` has no `force` key.

The remaining keys are CLI and hook operational settings, not `DefsortConfig` fields:
`allow-on-failure`, `color`, `incremental`, `incremental-fallback`, `max-reorder-count`,
`max-reorder-percent`, and `show-skips`.

`order` accepts `"dependency"`, `"alphabetical"`, or `"none"` and sets both scopes.
`definition-order` and `method-order` override it individually. CLI `--order` overrides both
configured scope strategies, then scoped CLI flags override their individual scopes. Axis flags
override their matching keys; `--order` does not reset axis values.

`banner-pattern` adds regular expression full-match to built-in recognition for each scope-relative
comment line using the restricted syntax described under [banner handling](#banner-handling). An
invalid or unsupported configured pattern emits `DefsortConfigWarning`; the CLI renders it on
stderr and falls back to built-in recognition. Configuration still validates the pattern in
`ignore` mode even though that mode performs no recognition.

When guard thresholds are configured, files where sorting would move more definitions than the
threshold are skipped. Use `--force` to bypass.

Skipped-file messages are shown by default in summary, diff, and write modes. Set
`show-skips = false` or pass `--hide-skips` to hide those compact messages while preserving exit
status. Preview and JSON output still report skipped files.

When `--force` bypasses an interstitial-code safety check, preview mode shows the normal movement
table instead of the skipped-file diagnostic.

TOML keys use kebab-case (`allow-on-failure`, not `allow_on_failure`), following `pyproject.toml`
conventions (`requires-python`, `optional-dependencies`). Transformation fields use snake_case on
`DefsortConfig`; internal runtime configuration carries the operational settings separately.

CLI flags always override config values. Setting `order = "alphabetical"` is equivalent to always
passing `-o alphabetical` for files in that configuration scope. Setting `incremental = true`
enables incremental mode for files in that scope unless an explicit invocation-wide flag overrides
it.

Color precedence is explicit `--color always` or `--color never`, then `NO_COLOR`, then explicit
`--color auto`, then the unanimous project value, then automatic terminal detection. Explicit
`--color auto` therefore bypasses project color settings, while `--color always` overrides
`NO_COLOR`.

## Resolved policy (`--show-policy`)

`--show-policy [PATH ...]` resolves configuration, prints the policy, and exits without sorting.
With no path, it resolves the current directory. Output contains one block for each distinct
configuration source in first-encountered path order, even when two sources produce identical
values:

```text
policy from /path/to/pyproject.toml (with CLI overrides):
  definitions: dependency; main last; ties alphabetical
  methods: dependency; categories __new__, __init__, dunders, regular (mixed);
           body dependencies within categories; ties alphabetical
  evaluation order: preserve
  banners: barriers
```

The header says `built-in defaults` when no `[tool.defsort]` table applies. The
`(with CLI overrides)` suffix appears only when the invocation includes a supported CLI override.
The display has no JSON form. It is mutually exclusive with preview, diff, JSON, and write modes,
and explicitly rejects `--detect`. Unreadable paths report an error and produce exit code 2.

## Exit codes

Closing stdout early preserves the status of completed work, as described under
[output behavior](#write-mode---write). Starting defsort with stdout already closed is an error: it
prints `error: standard output is unavailable` to stderr and exits 2 before reading or writing any
file, in every mode including `--help`, `--version`, and `hook`.

| Code | Meaning                                                                   |
| ---- | ------------------------------------------------------------------------- |
| 0    | No changes needed, or eligible writes succeeded with no skipped files     |
| 1    | Changes pending in a read-only mode, or files skipped in any sorting mode |
| 2    | Error (I/O failure, syntax error, encoding error)                         |

## JSON output

`--json` reports analysis without writing source files; it is mutually exclusive with `--write`.
The top-level object contains the schema version and one record per selected file, in processing
order:

```json
{
  "version": 1,
  "files": []
}
```

Every file record has all of these fields:

| Field                | Type and default    | Meaning                                                                       |
| -------------------- | ------------------- | ----------------------------------------------------------------------------- |
| `path`               | string              | Path reported for the selected file                                           |
| `status`             | string              | One of the status values below                                                |
| `warning`            | string or null      | Error, skip, guard, or partial-outcome explanation                            |
| `order`              | string or null      | Detected original order: `both`, `dependency`, `alphabetical`, or `unordered` |
| `dep_score`          | number or null      | Dependency-order score for the detected original order                        |
| `alpha_score`        | number or null      | Alphabetical-order score for the detected original order                      |
| `definitions`        | count object        | Top-level `moved` and `total` counts                                          |
| `methods`            | count object        | Method `moved` and `total` counts                                             |
| `movements`          | array, default `[]` | Top-level movement records                                                    |
| `method_movements`   | array, default `[]` | Per-class method movement records                                             |
| `banner_constraints` | array, default `[]` | Barriers that hold items from their unconstrained target                      |
| `class_blockers`     | array, default `[]` | Classes whose method sorting was blocked                                      |

The count objects have the shape `{"moved": 0, "total": 3}`. For `error` and `invalid` records,
both counts are zero and the structured arrays are empty. The three detection fields are null when
no comparable section contains at least two definitions or analysis did not produce a sort result.
`warning` is null when there is no error, skip reason, guard warning, or partial outcome to report.

### Status values

| Status      | Meaning                                                                                   |
| ----------- | ----------------------------------------------------------------------------------------- |
| `unchanged` | The file already has the target order                                                     |
| `changed`   | Sorting would change the file                                                             |
| `skipped`   | Sorting was not applied because of a safety, guard, baseline, or infrastructure condition |
| `error`     | File I/O, decoding, or unexpected processing failed                                       |
| `invalid`   | Source parsing or executable-module validation failed                                     |

### Movement records

Each top-level movement is `{"name": "helper", "from": 2, "to": 0}`. `from` and `to` are zero-based
positions in the original and proposed orders. `method_movements` groups the same record shape by
class:

```json
{
  "class": "Service",
  "movements": [
    {
      "name": "helper",
      "from": 2,
      "to": 0
    }
  ]
}
```

Movement arrays are empty when no item moved. A guarded result may include the movements that the
guard prevented.

### Schema versioning

The top-level `version` field indicates a major-compatibility generation. It is bumped when the
schema changes in a breaking way: a field is removed, renamed, or its type/semantics change.
Additive optional fields do not bump the version -- consumers should ignore unknown fields and
treat absent optional fields as their documented default.

Each file record has an always-present `banner_constraints` list. It is `[]` unless `barriers`
prevents one or more definitions or methods from reaching the unconstrained target. Each affected
boundary appears once, and an item that would cross several boundaries appears in every affected
record. The exact shape is:

```json
{
  "banner_constraints": [
    {
      "scope": "definitions",
      "class_name": null,
      "banner": {
        "line": 42,
        "text": "# ----\n# Helpers\n# ----"
      },
      "held": [
        {
          "name": "helper",
          "kind": "function",
          "occurrence": 1,
          "line": 51,
          "direction": "before"
        }
      ]
    }
  ]
}
```

`scope` is `definitions` or `methods`; `class_name` is null for top-level definitions. `kind` is
`class`, `function`, or `method`. `occurrence` is one-based among same-name items in that scope,
and `line` uses one-based source line numbers. `direction` is `before` or `after`: the side toward
which the unconstrained target would move the item across the banner. The barrier retains the item
on the opposite side. Consumers should accept additional fields as described under schema
versioning.

Each file record also has an always-present `class_blockers` array. A class whose method sort is
blocked contributes `{"class": "C", "occurrence": 1, "blocker": "duplicates"}` while independent
safe changes may still give the file `changed` status. `occurrence` is one-based among same-name
classes. The array is empty when no class has a blocker.
