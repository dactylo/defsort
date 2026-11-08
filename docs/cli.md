# CLI reference

Use this page to preview and apply defsort changes or look up commands, configuration, and output.
For policy choices and safety limits, read [Concepts and safety](design.md).

## Synopsis

```text
defsort [options] [PATH ...]
```

Pass Python files or directories; the default is the current directory. The installed command and
`python -m defsort` accept the same arguments. Agent registration uses the separate
[hook guide](hook.md).

## Preview and apply changes

Start with a summary, then inspect movement or wrapping and the patch:

```bash
defsort src/
defsort --preview src/
defsort --diff src/
```

These commands leave files unchanged. Read-only runs exit 1 when changes are pending; see
[exit codes](#exit-codes) for skips and errors.

A failed Git selection check can widen a directory scan before writing; review any
[selection warnings](#when-git-filtering-fails) before applying changes.

```bash
defsort --write src/
```

## Options

Spell options in full; abbreviations are not accepted. An unknown long option gets a suggestion
when a close match exists, such as `--wirte (did you mean --write?)`.

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

See [method sorting](design.md#method-sorting) for category and body-relationship behavior, and
[ordering policy](design.md#choose-an-ordering-policy) for `main` and tie-breaking. Definition-time
requirements take precedence over ordering preferences.

`--evaluation-order allow-reordering` accepts changes to the relative execution order of covered
definition-time effects; binding and other safety checks remain. Read the
[safety model](design.md#safety-model) before enabling it.

### Modifiers

| Flag           | Short | Description                                                                      |
| -------------- | ----- | -------------------------------------------------------------------------------- |
| `--show-skips` |       | List skipped files in summary, diff, and write output (default: on)              |
| `--hide-skips` |       | Do not list skipped files in summary, diff, and write output                     |
| `--force`      | `-f`  | Override interstitial-code or reorder-limit safety checks; review the diff first |
| `--verbose`    | `-v`  | Also list unchanged files                                                        |
| `--color`      |       | When to use color: `auto` (default), `always`, or `never`                        |
| `--hoist`      |       | Hoist safe `UPPER_SNAKE_CASE` constants from between defs to preamble            |
| `--no-future`  |       | Don't auto-add `from __future__ import annotations` for forward refs             |

### Banner handling

A built-in banner is a comment-only block before or between definitions or methods that contains a
rule line. A rule line consists of `#`, optional horizontal whitespace, and four or more identical
`-`, `=`, `*`, `#`, or `~` characters. It may be a bare rule, part of a box, or a separate rule
line beside a title comment. A same-line form such as `# ---- Helpers` needs a custom
`banner-pattern`.

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
comments, and the structural preamble and postamble remain protected. Comments directly before the
first definition or method are not part of the preamble: a recognized banner there, such as a
section header after the imports, is removed too. Inspect a diff before using
`--write --banners strip`.

### Incremental mode

| Flag               | Short | Description                                                             |
| ------------------ | ----- | ----------------------------------------------------------------------- |
| `--incremental`    | `-i`  | Sort only definitions added since the last commit (requires Git)        |
| `--no-incremental` |       | Sort every definition, even when configuration enables incremental mode |

`-i` / `--incremental` and `--no-incremental` are mutually exclusive.

`--force` does not select `allow-reordering` and never bypasses duplicate-name, layout, applicable
eager-order, or unresolved annotation blockers. Review a diff before a forced write. `--hoist` and
`--no-future` follow the [source transformation rules](design.md#source-preservation).

Incremental mode requires a suitable Git baseline. See [incremental mode](incremental.md) for Git
versions, skipped files, and the hook-only fallback.

## Configuration

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

Transformation keys use the names and values in [Ordering policy](#ordering-policy), without the
`--` prefix. The `order` shorthand sets both scopes; `DefsortConfig` instead exposes the resolved
fields and a programmatic `force`. There is no TOML `force` key.

| Operational key        | Default  | Purpose                                                               |
| ---------------------- | -------- | --------------------------------------------------------------------- |
| `incremental`          | `false`  | Use a HEAD baseline.                                                  |
| `incremental-fallback` | `"warn"` | Hook only: `warn` skips or `full-sort` adopts an unsuitable baseline. |
| `max-reorder-count`    | Unset    | Limit movement of existing top-level definitions.                     |
| `max-reorder-percent`  | Unset    | Limit the percentage of existing top-level definitions that move.     |
| `allow-on-failure`     | `false`  | Hook infrastructure-failure policy.                                   |
| `show-skips`           | `true`   | Show compact skipped-file messages.                                   |
| `color`                | `"auto"` | `auto`, `always`, or `never`.                                         |

These operational settings are not `DefsortConfig` fields. See the
[reorder guard](incremental.md#reorder-guard) and
[hook failure policy](hook-reference.md#infrastructure-failures) for their limits.

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
threshold are skipped. Raise the applicable threshold or use `--force` to override the guard.

Skipped-file messages are shown by default in summary, diff, and write modes. Set
`show-skips = false` or pass `--hide-skips` to hide those compact messages while preserving exit
status. Preview and JSON output still report skipped files.

When `--force` overrides an interstitial-code safety check, preview mode shows the normal movement
table instead of the skipped-file diagnostic.

TOML keys use kebab-case (`allow-on-failure`, not `allow_on_failure`), following `pyproject.toml`
conventions (`requires-python`, `optional-dependencies`). Transformation fields use snake_case on
`DefsortConfig`.

CLI flags always override config values. Setting `order = "alphabetical"` is equivalent to always
passing `-o alphabetical` for files in that configuration scope. Setting `incremental = true`
enables incremental mode for files in that scope unless an explicit invocation-wide flag overrides
it.

### Color

Color precedence is explicit `--color always` or `--color never`, then `NO_COLOR`, then explicit
`--color auto`, then the unanimous project value, then automatic terminal detection. Explicit
`--color auto` therefore bypasses project color settings, while `--color always` overrides
`NO_COLOR`.

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
the reason, a concrete `fix:` line, blocking line previews, and the hypothetical proposed reorder.
For code between definitions, preview suggests `--hoist` only when a trial sort with that option
clears the blocker; otherwise it suggests moving the code outside the definitions. A separate
`override:` line explains that `--force` sorts anyway and that you should review the diff first.
The reorder guard suggests raising its threshold or using `--force`. `--hide-skips` does not hide
preview diagnostics. Each blocked class in preview has its own `fix:` line.

### Diff mode (`--diff`)

Prints unified diff to stdout. File is not modified. On a terminal, source lines are decoded for
display, with control characters escaped except tabs. A replacement output stream without a binary
interface receives text.

With `--color never`, redirected output is an exact byte patch, including the source encoding, line
endings, and final newline. Apply it with `git apply -p0` or GNU `patch -p0` from the directory
where defsort ran. On Windows, use `patch -p0 --binary`: without `--binary`, GNU `patch` there
rejects changes to files with CRLF line endings. The paths must name files inside that directory
without `..` components or symlinked directories. Absolute operands are shortened only when their
spelling starts with the current directory as `os.getcwd()` spells it; otherwise their headers
remain absolute and patch tools reject them. Names that need quoting use Git's quoted header form.
macOS's own `patch` does not accept those quoted names. Forced color makes the output unsuitable as
a patch.

### Write mode (`--write`)

Writes changes in place and prints a one-line summary for each file written successfully. A file
whose write fails appears only in the error output, not in the written-file summary. A file that
changed after defsort read it is not written; defsort reports an error for it, continues with the
other files, and exits 2. See [writes and file metadata](platform.md#writes-and-file-metadata).
Already-ordered files are silent unless `-v` is used. Default, diff, and write modes keep skipped
diagnostics compact:

```text
path.py: skipped (code between definitions)
```

If stdout closes while defsort is producing or flushing output, defsort stops writing, examines no
further files, prints no traceback, and exits with the status of the work completed so far: for
example, 1 when an interrupted diff had changes.

### Detect mode (`--detect`)

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

### Resolved policy (`--show-policy`)

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

### JSON output

`--json` reports analysis without writing source files; it is mutually exclusive with `--write`.
Configuration diagnostics and the empty-selection warning may appear on stderr. The top-level
object contains the schema version and one record per selected file, in processing order:

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

#### Status values

| Status      | Meaning                                                                                   |
| ----------- | ----------------------------------------------------------------------------------------- |
| `unchanged` | The file already has the target order                                                     |
| `changed`   | Sorting would change the file                                                             |
| `skipped`   | Sorting was not applied because of a safety, guard, baseline, or infrastructure condition |
| `error`     | File I/O, decoding, or unexpected processing failed                                       |
| `invalid`   | Source parsing or executable-module validation failed                                     |

#### Movement records

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

#### Schema versioning

The top-level `version` field indicates a major-compatibility generation. It is bumped when the
schema changes in a breaking way: a field is removed, renamed, or its type/semantics change.
Additive optional fields do not bump the version -- consumers should ignore unknown fields and
treat absent optional fields as their documented default.

#### Banner constraints

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

#### Class blockers

Each file record also has an always-present `class_blockers` array. A class whose method sort is
blocked contributes `{"class": "C", "occurrence": 1, "blocker": "duplicates"}` while independent
safe changes may still give the file `changed` status. `occurrence` is one-based among same-name
classes. The array is empty when no class has a blocker.

## Warnings

Printed to stderr:

- `warning: no Python files found under the given paths` when collection selects no Python files.
- Safety check warnings (interstitial code, eager-order hazards, forward annotation refs, and
  duplicate names).
- Blocking interstitial code previews (truncated to 60 chars).
- Invalid or unsupported configured `banner-pattern` expressions. The pattern is ignored after the
  warning.
- Non-default policy values that have no effect under the resolved strategies.
- Git exclusion failures during a directory scan inside a worktree. The warning names the worktree
  and the reason, and every discovered Python file is kept.
- Private-label failures during a directory scan inside a worktree. The warning names the worktree
  and the reason; Git-ignored files stay excluded and every other discovered Python file is kept.
- An invalid `defsort.privateAttribute` name, once per run. defsort uses `private` instead.

Configuration diagnostics use
`warning: <file>: [tool.defsort].<key>: invalid value <raw>; expected <valid>; using <used>`.
Unknown keys use `warning: <file>: [tool.defsort]: unknown key <key>; ignoring`. The file is the
governing `pyproject.toml`; accepted values appear in their declared order, and the fallback names
the value actually used. For example, an invalid `method-order` uses the resolved `order` value.
Table-level configuration errors retain their existing wording and name the file.

Ineffective-policy warnings depend on the resolved value, not on whether it came from config or a
CLI flag. Default values never warn. defsort warns for:

- non-default `method-bodies` when method order is not `dependency`;
- `tie-break = source` when neither scope uses dependency order;
- non-default `method-visibility` or `method-dunders` when method order is `none`; and
- `main = sorted` when definition order is `none`.

These warnings do not reject the invocation. defsort ignores the ineffective value. Each uses
`warning: [tool.defsort] <key> has no effect under the resolved strategies; ignoring`.

In `barriers` mode, a constrained file also prints
`defsort: <path>: sort constrained by <n> banner(s)` to stderr. Preview and verbose output add one
line per boundary with the source line, banner label, held definitions or methods, and crossing
directions. JSON mode records the same information without writing this diagnostic.

## Exit codes

Closing stdout early preserves the status of completed work, as described under
[output behavior](#write-mode---write). Starting defsort with stdout already closed is an error: it
prints `error: standard output is unavailable` to stderr and exits 2 before reading or writing any
file, in every mode including `--help`, `--version`, and `hook`.

| Code | Meaning                                                                   |
| ---- | ------------------------------------------------------------------------- |
| 0    | No changes needed, or eligible writes succeeded with no skipped files     |
| 1    | Changes pending in a read-only mode, or files skipped in any sorting mode |
| 2    | Error (I/O, syntax, encoding, or Git required by the effective modes)     |

## File selection

### Explicit files and directory scans

Pass `.py` files or directories; no path means `.`. Existing non-Python files are ignored when
Python files are selected. Directory scans recurse into Python files, with these exclusions:

- Common non-source directories, including `.git`, `__pycache__`, `.venv`, `build`, and `dist`, are
  pruned before Git filtering, even if they contain tracked files.
- Git worktrees include tracked and untracked, non-ignored files. Nested worktrees use their own
  tracked state and exclusions. Git-ignored directories are not scanned.
- Files and nested worktrees marked with the `private` Git attribute are skipped. See
  [private files](hook.md#private-files) for attribute configuration and its pathname limits.
- Symlinks found during recursion are skipped.

An explicitly named Python file remains eligible even if Git ignores it or its parent has a pruned
name. Explicit symlinks are followed. See [platform paths](platform.md#paths) for path and
file-identity behavior.

### When Git filtering fails

| Situation                                                      | Directory-scan behavior                                     |
| -------------------------------------------------------------- | ----------------------------------------------------------- |
| Outside a worktree                                             | Scan the filesystem silently.                               |
| Git unavailable or exclusion filtering fails inside a worktree | Warn with the reason and keep every discovered Python file. |
| Only the private-label check fails                             | Warn with the reason; keep files not ignored by Git.        |

Directories already skipped as Git-ignored are not revisited after a later filtering failure. An
invalid `defsort.privateAttribute` name warns once per run and falls back to `private`.

### Multiple names for one file

Names that refer to one file, such as repeated operands, hard links, symlinks, and case variants,
are processed once under the first name. When their configurations or incremental eligibility would
give the file different results, every mode except `--detect` and `--show-policy` reports an error
naming them, processes none of them, and exits 2.
