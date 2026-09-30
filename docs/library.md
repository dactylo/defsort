# Library API

Use the library to sort source already held in memory, inspect proposed changes, or build your own
editor and automation integration. Sorting and checking do not read files or invoke Git; pass a
baseline string when you need incremental behavior.

```python
import defsort

source = 'def zebra(): pass\n\ndef alpha(): pass\n'
result = defsort.check(source)
if result.status == 'changed':
    source = defsort.sort(source)
```

Import the sorting API from `defsort`. The separate [hook adapter contract](#defsorthooks) supports
integrations with additional coding agents. See [concepts and safety](design.md) for ordering
choices and limits before enabling transformations in an application.

## `defsort.sort(source, *, baseline=None, config=None) -> str`

`sort()` returns source with definitions and methods sorted under `config`. A hard file-level
safety blocker returns the original source. A soft top-level incremental blocker can retain
top-level drift while independent method changes proceed. A method blocker leaves that class
unchanged while other safe changes may proceed. A `layout` blocker refuses the whole file: if any
scope cannot preserve source layout and comment ownership, every earlier in-memory change is
discarded and the exact original source is returned. Call `check()` for blocker details.

Ordinary sorting moves complete definition and method blocks with their decorators and owned
comments while preserving source layout. It does not enforce blank-line style. The exact
[source-preservation rules](cli.md#source-preservation) also cover grouped definitions, trailing
comments, mixed line endings, and final-newline state.

The library has two explicit non-goals: it does not hoist constants, and it does not add or remove
`from __future__ import annotations`. The `banners='strip'` policy is the explicit source-removal
exception. It removes only recognized banner spans, even when both scope orders are `none`.

```python
import defsort
from defsort import DefsortConfig

config = DefsortConfig(method_order='none')
source = defsort.sort(source, config=config)
```

Parameters:

- `source` -- Python source code as a string.
- `baseline` -- When non-empty, sort only definitions added since the baseline. `None`, an empty
  string, and a whitespace-only string select full sorting. A hard blocker returns the original
  source. A soft top-level blocker may preserve top-level order while returning independent method
  changes.
- `config` -- A `DefsortConfig` value. The default is `DefsortConfig()`.

Raises:

- `TypeError` -- `config.force` is not a `bool`, or `config.banner_pattern` is neither a string nor
  `None`.
- `ValueError` -- A vocabulary field contains an unknown value, `config.banner_pattern` does not
  compile or uses unsupported syntax, or source contains a lone Unicode surrogate. A compilation
  failure chains the original `re.error` as its cause. See [Banner patterns](#banner-patterns).
- `SyntaxError` -- Source is invalid Python, including source that contains a null byte.

Configuration validation precedes source parsing.

## `defsort.check(source, *, baseline=None, config=None) -> SortCheckResult`

`check()` reports what `sort()` would do without modifying source. It returns four statuses:

- `changed` -- `sort()` would change source.
- `invalid` -- Source analysis failed because source is invalid Python, contains a null byte, or
  contains a lone Unicode surrogate.
- `skipped` -- A safety check blocks a change, or blocked class methods are the only proposed
  change. A file-level `blocker` identifies its reason and `blocker_message` carries display text.
- `unchanged` -- Source already has the requested order.

Like `sort()`, `check()` never changes future imports. It reports a covered annotation inversion as
`forward_refs` unless the future-annotations import already exists. It reports a definition-time
read whose binding changes as `eager_order` regardless of the import or `config.force`.
`class_blockers` lists every class whose methods stayed in source order, including classes reported
alongside independent changes and a `changed` status.

Both APIs preserve the relative source order of eager top-level and method groups by default.
`DefsortConfig(evaluation_order='allow-reordering')` removes only that conservative pin in enabled
scopes and accepts changes to the relative execution order of decorators, defaults, and other
definition-time effects. It does not infer purity or maintain a decorator list. Binding-identity,
annotation, duplicate, dynamic `__all__`, banner, and structural checks remain in force.

```python
import defsort
from defsort import DefsortConfig

config = DefsortConfig(method_order='none')
result = defsort.check(source, config=config)
if result.status == 'changed':
    source = defsort.sort(source, config=config)
elif result.status == 'skipped':
    print(f'blocked: {result.blocker}')
```

`baseline` and `config` follow the `sort()` rules. Configuration errors raise the same `TypeError`
or `ValueError` as `sort()`. Source-analysis failures return `status='invalid'` instead of raising.

## `defsort.detect(source, *, banners='barriers', banner_pattern=None) -> DetectResult | None`

`detect()` classifies the current top-level definition order without modifying source. It returns
`None` when no section contains at least two definitions. Otherwise, `DetectResult.order` is:

- `both` when every comparable section matches both target orders;
- `dependency` when every comparable section matches dependency order and at least one misses
  alphabetical order;
- `alphabetical` for the symmetric case; or
- `unordered` for every other combination.

`detect()` retains its canonical comparison policy and does not accept or apply `evaluation_order`.
Use `check()` to test conformance with a complete `DefsortConfig`.

With `banners='barriers'`, detection compares definitions only within their current sections.
Sections with fewer than two definitions are neutral. `banners='ignore'` and `banners='strip'` both
classify the global order; `strip` means to classify as if recognized boundaries were absent and
does not mutate source.

The optional custom pattern follows the [supported banner-pattern subset](#banner-patterns) and
uses `re.fullmatch()` against each scope-relative comment line, including its leading `#` and
excluding its line ending. For each target order, detection sums the discordant and total pair
counts from comparable sections and computes `(total_pairs - 2 * discordant_pairs) / total_pairs`.
Each section therefore contributes according to its number of comparable pairs.

Raises:

- `TypeError` -- `banner_pattern` is neither a string nor `None`.
- `ValueError` -- `banners` is outside the `BannerMode` vocabulary, or `banner_pattern` does not
  compile or uses unsupported syntax, or source contains a lone Unicode surrogate. A compilation
  failure chains the original `re.error` as its cause. A lone surrogate can raise
  `UnicodeEncodeError`, a concrete `ValueError` subclass.
- `SyntaxError` -- Source is invalid Python.

Argument and pattern validation precedes source parsing in every banner mode.

## `defsort.resolve_config(path) -> DefsortConfig`

`resolve_config()` accepts `str | os.PathLike[str]`. An existing directory starts discovery at
itself. Every other path starts discovery at its parent, including a file that does not exist yet.
Discovery walks upward to the git repository root and includes that root in the search. The nearest
`pyproject.toml` wins outright. If that file has no usable `[tool.defsort]` table, resolution uses
defaults instead of inheriting from a more distant file.

For invalid, unreadable, or unknown file content, defsort emits `DefsortConfigWarning` and uses the
applicable default. Defsort does not explicitly raise these diagnostics; a caller's warning filters
can promote them to exceptions.

Results are cached per resolved directory for the process lifetime. A long-running embedder must
call `clear_config_cache()` after editing a `pyproject.toml`; otherwise, later calls for the same
directory keep returning the cached result.

```python
from pathlib import Path

import defsort

config = defsort.resolve_config(Path('src/mylib/module.py'))
```

## `defsort.clear_config_cache() -> None`

`clear_config_cache()` clears all configuration-resolution caches. Later `resolve_config()` calls
then read configuration from disk again. It also clears defsort's own warning registry, so a repeat
diagnostic for the same problem is reported again rather than suppressed as a duplicate by the
default warning filter.

```python
import defsort

# Edit pyproject.toml in this process.
defsort.clear_config_cache()
config = defsort.resolve_config('src/mylib/module.py')
```

## Banner patterns

`banner_pattern` adds regular-expression full-match recognition to the built-in banner rule. It
matches each scope-relative logical comment line, including the leading `#` and excluding the line
ending. `sort()`, `check()`, `detect()`, and `DefsortConfig` all accept the same bounded subset:

- literals, character classes, `.`, and escaped atoms other than decimal backreferences;
- an optional leading `^` and optional trailing `$`; and
- at most one `*` or `+` repetition, attached to an atom, in the entire expression.

For example, `^# Section: .+$` is supported. Grouping, alternation, lookaround, counted repetition,
`?`, backreferences, misplaced anchors, unattached repetition, and multiple repetitions are
rejected. A pattern that does not compile raises `ValueError` and chains the original `re.error`. A
pattern that compiles but uses unsupported syntax raises `ValueError` without an `re.error` cause.

## `defsort.DefsortConfig`

`DefsortConfig` is a frozen, slotted, keyword-only dataclass shared by `sort()` and `check()`.
Construction accepts these eleven fields:

| Field               | Type               | Default               |
| ------------------- | ------------------ | --------------------- |
| `definition_order`  | `SortScopeOrder`   | `'dependency'`        |
| `method_order`      | `SortScopeOrder`   | `'dependency'`        |
| `evaluation_order`  | `EvaluationOrder`  | `'preserve'`          |
| `force`             | `bool`             | `False`               |
| `banners`           | `BannerMode`       | `'barriers'`          |
| `banner_pattern`    | `str \| None`      | `None`                |
| `method_bodies`     | `MethodBodies`     | `'within-categories'` |
| `method_visibility` | `MethodVisibility` | `'mixed'`             |
| `method_dunders`    | `MethodDunders`    | `'before-regular'`    |
| `main`              | `MainPlacement`    | `'last'`              |
| `tie_break`         | `TieBreak`         | `'alphabetical'`      |

Construction validates every value. An unknown vocabulary value raises `ValueError`. A non-boolean
`force` or a `banner_pattern` value other than `str | None` raises `TypeError`. A pattern that does
not compile or uses unsupported syntax raises `ValueError`; only compilation failures chain the
original `re.error`. `sort()` and `check()` repeat this validation at their boundaries, including
for a hand-built instance that bypassed normal construction.

`banner_pattern` adds regular-expression full-match recognition to the built-in banner rule. It
matches each scope-relative logical comment line, including the leading `#` and excluding the line
ending. See [Banner patterns](#banner-patterns) for the exact accepted grammar.

`force` is a programmatic policy field for library callers. It can bypass eligible interstitial
checks, but it does not select `evaluation_order='allow-reordering'` and cannot bypass
duplicate-name, applicable eager-order, or forward-reference blockers. The `[tool.defsort]` table
has no `force` key; the CLI exposes its own `--force` flag.

CLI and hook operational settings are not `DefsortConfig` fields. This group comprises
`allow-on-failure`, `color`, `incremental`, `incremental-fallback`, `max-reorder-count`,
`max-reorder-percent`, and `show-skips`. Configuration files and CLI or hook processing own those
settings.

When a non-default transformation value has no effect, `sort()` and `check()` emit
`DefsortConfigWarning`. The ineffective combinations are:

- non-default `method_bodies` outside dependency method order;
- `tie_break='source'` when neither scope uses dependency order;
- non-default `method_visibility` or `method_dunders` when method order is `none`; and
- `main='sorted'` when definition order is `none`.

An explicitly supplied default value does not warn.

## `defsort.DefsortConfigWarning`

`DefsortConfigWarning` is a `UserWarning` subclass for configuration diagnostics.
`resolve_config()` emits it for invalid, unreadable, or unknown configuration input that falls back
to defaults. `sort()` and `check()` emit it for ineffective non-default transformation values.
Library calls use Python's `warnings` machinery and do not print these diagnostics directly.
Filters installed by the caller control whether Python ignores, renders, or promotes them. Python's
default warning renderer writes displayed warnings to stderr.

Use standard warning filters to ignore or promote the category:

```python
import warnings

from defsort import DefsortConfigWarning

warnings.filterwarnings('ignore', category=DefsortConfigWarning)
# Or make matching warnings exceptions.
warnings.filterwarnings('error', category=DefsortConfigWarning)
```

The CLI and hook capture this category and render `Warning: <message>` even when an external
filter, such as `PYTHONWARNINGS=ignore`, would silence a library warning.

## Vocabulary aliases

The eight vocabulary exports are `Literal` aliases. Callers pass plain strings; these names are
type annotations, not enum classes.

| Export             | Exact string values                                      |
| ------------------ | -------------------------------------------------------- |
| `BannerMode`       | `'barriers'`, `'ignore'`, `'strip'`                      |
| `EvaluationOrder`  | `'preserve'`, `'allow-reordering'`                       |
| `MainPlacement`    | `'last'`, `'sorted'`                                     |
| `MethodBodies`     | `'within-categories'`, `'ignore'`, `'across-categories'` |
| `MethodDunders`    | `'before-regular'`, `'after-regular'`                    |
| `MethodVisibility` | `'mixed'`, `'public-first'`                              |
| `SortScopeOrder`   | `'dependency'`, `'alphabetical'`, `'none'`               |
| `TieBreak`         | `'alphabetical'`, `'source'`                             |

`definition_order` and `method_order` select dependency, alphabetical, or no sorting for their
scope. `evaluation_order` controls relative source-order preservation for eager groups in enabled
scopes. `method_bodies` controls body-derived method edges. `method_visibility` and
`method_dunders` control method categories. `main` controls whether top-level `main()` stays last.
`tie_break` controls ready-item order during dependency sorting. `banners` preserves barriers,
ignores recognition, or removes recognized banner spans.

## `defsort.SortStatus`

`SortStatus` is the literal alias `Literal['changed', 'invalid', 'skipped', 'unchanged']`.
`SortCheckResult.status` uses the four outcomes documented under `check()`.

## `defsort.Blocker`

`Blocker` is the `Literal` string alias:

```python
Blocker = Literal[
    'duplicates',
    'forward_refs',
    'interstitial',
    'layout',
    'non_conforming',
    'target_incompatible',
    'user_reordered',
    'eager_order',
]
```

| Value                   | Meaning                                                                       |
| ----------------------- | ----------------------------------------------------------------------------- |
| `'duplicates'`          | Non-consecutive duplicate definitions block sorting                           |
| `'forward_refs'`        | Sorting would create an unresolved annotation read                            |
| `'interstitial'`        | Code between definitions blocks ordinary sorting                              |
| `'layout'`              | The requested reorder cannot preserve source layout and comment ownership     |
| `'non_conforming'`      | The incremental baseline misses its target order                              |
| `'target_incompatible'` | Existing positions conflict with the target order                             |
| `'user_reordered'`      | Existing definitions moved relative to the baseline                           |
| `'eager_order'`         | Sorting would change a covered binding, or eager-group order under `preserve` |

`layout` is a hard whole-file blocker in full and incremental modes, even when top-level definition
IDs are unchanged. Neither `force` nor hook `allow-on-failure` bypasses it. Separate attached
comments from section banners with blank lines before sorting.

Human-facing diagnostic text travels on the records that report a blocker:
`SortCheckResult.blocker_message` and `ClassBlocker.message`. Display those fields, but branch on
the classification value, for example `result.blocker == 'interstitial'`. Do not compare message
text; it is not a stable machine-readable interface. `Blocker` exposes no public forceability,
policy, or severity properties.

## `defsort.OrderKind`

`OrderKind` is the `Literal` string alias
`Literal['alphabetical', 'both', 'dependency', 'unordered']`. `DetectResult.order` carries one of
these plain strings, so `result.order == 'dependency'` is the intended comparison.

## `defsort.SortCheckResult`

`SortCheckResult` is a frozen, slotted, keyword-only dataclass with these fields:

| Field                | Type                       | Default  |
| -------------------- | -------------------------- | -------- |
| `status`             | `SortStatus`               | required |
| `blocker`            | `Blocker \| None`          | `None`   |
| `blocker_message`    | `str \| None`              | `None`   |
| `banner_constrained` | `bool`                     | `False`  |
| `class_blockers`     | `tuple[ClassBlocker, ...]` | `()`     |

`blocker` and `blocker_message` are populated together for a file-level safety blocker. A
class-only skip has `status='skipped'` and records in `class_blockers`, but both file-level fields
remain `None`. A `layout` blocker refuses the whole file, including independent changes already
calculated in memory; `blocker` is then `'layout'` and the exact original source is preserved.

`banner_constrained` is true when at least one recognized banner prevents the unconstrained target
order. It can be true for any non-invalid status. `class_blockers` can be non-empty with
`status='changed'` because independent best-effort changes still apply. When blocked class work is
the only proposed change, the status is `skipped`.

## `defsort.DetectResult`

`DetectResult` is a frozen, slotted, keyword-only dataclass with three required fields:

| Field         | Type        | Meaning                                         |
| ------------- | ----------- | ----------------------------------------------- |
| `order`       | `OrderKind` | Detected order classification                   |
| `dep_score`   | `float`     | Dependency-order Kendall tau from -1.0 to 1.0   |
| `alpha_score` | `float`     | Alphabetical-order Kendall tau from -1.0 to 1.0 |

## `defsort.ClassBlocker`

`ClassBlocker` is a frozen, slotted, keyword-only dataclass with four required fields:

| Field        | Type      | Meaning                                              |
| ------------ | --------- | ---------------------------------------------------- |
| `class_name` | `str`     | Name of the class whose methods stayed in place      |
| `occurrence` | `int`     | One-based occurrence among same-named classes        |
| `blocker`    | `Blocker` | Reason the class body could not be sorted            |
| `message`    | `str`     | Human-readable explanation of `blocker`, for display |

A blocked class keeps its methods in source order. Use `check()` to inspect these records; `sort()`
returns only best-effort source text.

## `defsort.hooks`

The public host adapter contract for `defsort hook`, versioned by `defsort.hooks.API_VERSION`
(currently `1`). `HookInput`, `HookResult`, `HookResponse`, and `HookAdapter` are structural
`typing.Protocol` classes. An adapter may implement its own classes or use the concrete convenience
records.

- `HookInput`: read-only `event` (`'pre_tool'` or `'post_tool'`), `command` (`str | None`, the
  shell command of a pre-tool payload), `cwd` (`str`), `command_cwd` (`str | None`, the absolute
  directory a pre-tool command executes in, or `None` when unknown), and `edited_paths`
  (`Sequence[str]`, the paths a post-tool payload reports as written).
- `HookResult`: read-only `decision` (`'allow'` or `'deny'`), `reason` (`str | None`), and `notes`
  (`Sequence[str]`, informational lines such as safety skips).
- `HookResponse`: read-only `stdout` (`str`), `stderr` (`str`), and `exit_code` (`int`).
- `HookAdapter`: read-only `name` (`str`), `api_version` (`int`), and `requires_event` (`bool`);
  `parse(payload, event) -> HookInput | None`, where `None` means the payload describes nothing
  defsort acts on; `render(result) -> HookResponse`; and `render_failure(reason) -> HookResponse`,
  the host's fail-closed response.
- `HostInput` and `HostResponse`: frozen, slotted concrete records with the corresponding protocol
  fields.
- `notes_text(result)`: render informational notes as newline-terminated stderr text.
- `object_field(payload, key)`: return a nested JSON object or an empty dictionary.
- `payload_cwd(payload, key='cwd')`: return the post-tool event directory, or the process
  directory; never a `command_cwd`.
- `absolute_directory(fields, key)`: return a non-empty absolute path string at `key`, else `None`.

Adapters are discovered through the `defsort.hook_adapters` entry-point group, each entry point
naming an adapter class, and selected by `defsort hook --agent NAME`. See the
[hook reference](hook.md#agent-adapters) for installation, `--event`, and the failure tiers.

```python
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MyInput:
    event: Literal['pre_tool', 'post_tool']
    command: str | None
    cwd: str
    command_cwd: str | None
    edited_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class MyResponse:
    stdout: str
    stderr: str
    exit_code: int


class MyHostAdapter:
    name = 'myhost'
    api_version = 1
    requires_event = False

    def parse(self, payload: Mapping[str, object], event: str | None) -> MyInput | None: ...

    def render(self, result: object) -> MyResponse: ...

    def render_failure(self, reason: str) -> MyResponse: ...
```
