# Concepts and safety

defsort makes definition order a deterministic, configurable project policy. As people and coding
agents add functions, placement can reflect the sequence of edits. defsort applies an ordering
convention across those edits, with dependency and alphabetical strategies and separate controls
for top-level definitions and class methods.

Start with the [quick start](../README.md#quick-start). This guide explains how to choose a policy,
what sorting preserves, and where its safety checks stop. The [CLI reference](cli.md) lists every
flag and configuration value.

## Choose an ordering policy

Top-level definitions and class methods have independent settings:

| Strategy       | Ordering rule                                                              | Tradeoff                                                              |
| -------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `dependency`   | Place dependencies before dependents, with alphabetical ties by default    | Dependency changes can move many definitions and produce large diffs  |
| `alphabetical` | Sort classes before functions, then by name; method categories still apply | Name order can separate definitions that are used together            |
| `none`         | Leave that scope's definition order unchanged                              | Authors retain placement decisions; defsort does not enforce an order |

Automatic ordering trades control over individual placement for a repeatable convention. Neither
strategy guarantees an intuitive reading order or small diffs. Banners preserve section boundaries
while allowing sorting within each section; disabling a scope preserves its authored order.

Deterministic ordering means repeatable results for the same source, policy, and applicable
baseline. It does not mean that changing a dependency or renaming a function leaves other
definitions in place. Source order can also remain significant through banners, tie-breaking,
incremental baselines, and safety constraints.

The `order` setting selects both scopes. `definition-order` and `method-order` override their
individual scopes. For example, preserve method order while organizing a module's definitions:

```toml
[tool.defsort]
definition-order = "dependency"
method-order = "none"
```

`main = "last"` keeps a top-level `main()` after other definitions unless a definition-time read
requires it earlier. Under `evaluation-order = "preserve"`, an eager `main()` keeps its source
order relative to other eager groups, so it can remain earlier. Set `main = "sorted"` to treat it
like other functions. In dependency order, `tie-break = "source"` keeps unrelated ready definitions
in their authored order; the default `alphabetical` breaks those ties by name.

### Dependencies and cycles

For functions, dependency ordering considers references throughout the function body. For classes,
it considers definition-time components, including bases, decorators, class statements, and method
signatures. Method bodies do not create dependencies for their containing top-level class: doing so
would make large class relationships dominate module order.

Mutually dependent definitions form a cycle, so neither can precede every dependency. defsort drops
ordering edges within each cycle, then uses the configured tie-break. Dependencies between separate
cycles still constrain their order. Safety checks apply to the resulting candidate.

## Method sorting

The default category order is `__new__`, `__init__`, other dunder methods, then regular methods.
Each category uses dependency ordering with alphabetical ties.

| Setting                              | Effect                                                    |
| ------------------------------------ | --------------------------------------------------------- |
| `method-visibility = "mixed"`        | Keep public and private regular methods in one category   |
| `method-visibility = "public-first"` | Put public regular methods before private regular methods |
| `method-dunders = "before-regular"`  | Put other dunders before regular methods                  |
| `method-dunders = "after-regular"`   | Put other dunders after regular methods                   |

`__new__` and `__init__` remain the first two categories. A private regular method begins with an
underscore and is not a dunder. Property getters, setters, and deleters stay together, as do
consecutive same-name overload definitions within a section.

Definition-time dependencies, such as a decorator referring to another method, take precedence over
categories. Method-body relationships supply ordering preferences, controlled by `method-bodies`:

- `within-categories` uses body relationships only inside each category. This is the default.
- `ignore` omits body relationships while retaining definition-time dependencies. Categories and
  the configured tie-break determine the order where those dependencies leave a choice.
- `across-categories` lets body relationships cross category boundaries. Categories then break ties
  rather than separating those relationships.

If a cycle combines body relationships with definition-time dependencies, body relationships yield
first. An ordering preference cannot override a definition-time requirement.

### Which body relationships count

defsort recognizes literal attribute reads through a method's own receiver, such as `self.helper`
or `cls.factory`, using the actual first parameter name. Calls, stored references, and property
reads count; assignments and deletions do not. Static methods recognize explicit
`ContainingClass.member` reads. References to a property or overload member apply to its whole
group, and references never cross a banner barrier.

Aliases, `getattr`, `type(self)`, computed attributes, subscripts, strings, and inherited members
are not resolved. A class-name access inside an instance method does not count as a receiver read.
Nested scopes contribute references only while they retain the enclosing receiver binding; a local
binding of that name stops their contribution. These rules may omit a useful relationship, but they
avoid inventing a dependency on a different object.

## Preserve meaningful sections

Comment banners let a file express a reading order that sorting alone cannot infer. With the
default `banners = "barriers"`, definitions and methods sort only within their current sections.
Use banners to define sections within which the configured ordering policy applies.

```python
# ----
# Input handling
# ----
def read_input():
    pass


# ----
# Output handling
# ----
def write_output():
    pass
```

Built-in recognition requires a comment-only block with a rule line of at least four identical `-`,
`=`, `*`, `#`, or `~` characters. A custom `banner-pattern` can recognize another convention. See
[banner handling](cli.md#banner-handling) for the supported regular-expression subset.

`banners = "ignore"` sorts across sections without recognizing boundaries. `banners = "strip"` also
removes recognized banner blocks, even in a scope whose ordering is `none`. Inspect a diff before
using `strip`: a custom pattern that matches ordinary comments can remove their entire comment
block.

Barriers preserve section boundaries, not the complete order inside a section. Use
`method-order = "none"` when a class requires its full authored method order. The CLI reports when
a banner holds an item away from its otherwise preferred position.

## Safety model

Python can observe definition order. Decorators and default arguments run when a function is
defined; class bodies, bases, and class-creation hooks can read names immediately. Reordering those
definitions can change which value a name refers to or whether that value exists yet.

defsort checks the actual proposed order against a bounded model of these reads. It preserves the
binding seen by covered definition-time reads, keeps grouped definitions together, and requires
valid output. Sorting is idempotent: sorting the result again produces the same source. These
checks do not prove that arbitrary Python programs behave identically after sorting.

### Evaluation order

By default, `evaluation-order = "preserve"` keeps the relative source order of groups with
decorators, defaults, or other covered definition-time evaluation. This can prevent an otherwise
preferred alphabetical or dependency order, but avoids changing the order of those effects.

Use `evaluation-order = "allow-reordering"` only when your project accepts changing their relative
execution order. It removes that conservative constraint for enabled scopes; it does not infer
purity or trust particular decorators. Binding checks, annotation checks, duplicate detection,
dynamic `__all__` guards, and structural checks remain in force.

Either setting can change class namespace iteration and the observation order of `__set_name__` or
`__init_subclass__`. Use `method-order = "none"` for classes whose behavior depends on complete
method order. Banners can protect boundaries between stable sections.

### What happens when sorting is blocked

| Condition                       | Why sorting stops                                                 | Typical response                                                                       |
| ------------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Code between definitions        | Moving a definition across a statement can change execution order | Move appropriate constants or type aliases to the preamble, or preserve authored order |
| Changed definition-time binding | A covered read would see a different value or lose its binding    | Keep the dependency before its reader or restructure the reference                     |
| Forward annotation reference    | A moved annotation needs a definition that is not yet available   | Use future annotations when their semantics suit the project                           |
| Non-consecutive duplicate names | Reordering could change which definition wins                     | Consolidate or rename the definitions                                                  |

The CLI skips blocked candidates and reports why. A hard file-level blocker causes the PreToolUse
hook to deny the commit; incremental-policy and reorder-guard skips warn instead. PostToolUse
reports the problem after the edit. See [hook failure behavior](hook.md#blocked-sort) and
[incremental skip conditions](incremental.md#skip-conditions) for the distinctions.

A class-local blocker leaves that class's methods in place while independent safe changes may
proceed. CLI, JSON, and library reports include these partial outcomes.

`--force` bypasses interstitial-code and large-reorder guards. It never bypasses duplicate-name,
applicable eager-order, or unresolved annotation blockers, and it does not select
`allow-reordering`. Preview a forced change before writing it.

### What the checks cover

Covered eager reads include decorators, positional and keyword-only defaults, class bases and
keywords, and executable class-suite expressions. The comparison includes original bindings from
imports and assignments before the definitions. An unresolved read that the sort does not worsen
does not itself block the change.

The model follows direct bare-name calls and decorators into ordinary sibling functions. It follows
a sibling generator when a class-suite loop or eager comprehension iterates a direct call, or when
an unshadowed built-in consumer directly consumes one. Supported consumers are `all`, `any`,
`dict`, `frozenset`, `list`, `max`, `min`, `next`, `set`, `sorted`, `sum`, and `tuple`. Each
matched sibling expands once.

The model also covers reads from a directly named earlier local base's `__init_subclass__`, a
directly named local metaclass's `__new__`, and a directly constructed local descriptor's
`__set_name__`. Rebinding the provider invalidates that relationship.

Aliases, attributes, computed calls, async functions, inherited class-creation hooks, arbitrary
descriptors or metaclasses, and deeper interprocedural execution remain outside this model. Lazy
type-alias payloads and type-parameter bounds or defaults are excluded. Lambda payloads below an
eager expression are conservatively included; a generator expression contributes only its outermost
iterable. Conservative treatment of untaken paths can block a safe change, while unmodeled dynamic
behavior can allow an unsafe one. Preserve authored order when those limits matter to your code.

## Source preservation

defsort moves complete definitions with their decorators and owned comments into the existing
source layout. It preserves surrounding whitespace, line endings, class headers and footers, and
the final-newline state. Consecutive overload and property groups move together. Sorting does not
impose blank-line style; run a formatter for that purpose.

Trailing comments sometimes require a blank line to remain distinct from the next definition.
defsort may relocate one authored blank line per affected boundary, preserving that line's bytes.
See [source preservation in the CLI reference](cli.md#source-preservation) for exact separator and
mixed-ending behavior, and [platform compatibility](platform.md) for encoding and file metadata.

Three optional transformations change more than position: constant hoisting, future-import
management, and banner stripping. Each changes only its own source spans.

### Constant hoisting

CLI `--hoist` can move literal-only `UPPER_SNAKE_CASE` assignments from between definitions into
the preamble area. This can remove an interstitial-code blocker when the constant has one binding
and is not referenced before its original position. Values containing calls or name references are
not eligible.

Hoisting preserves the assignment's exact source and adjacent authored blank lines. Hooks and the
public library do not hoist constants.

### Future annotations management

Annotations have their own ordering requirements. When CLI sorting creates a covered annotation
hazard, defsort can add `from __future__ import annotations`. `--no-future` disables addition.
Hooks and the library never add the import, so a covered hazard without an existing import blocks
sorting there.

| Surface                                | Add the import                    | Remove an unused import                |
| -------------------------------------- | --------------------------------- | -------------------------------------- |
| CLI, full or incremental               | When needed, unless `--no-future` | When the cleanup conditions below hold |
| Agent hooks, full or incremental       | Never                             | When the cleanup conditions below hold |
| `defsort.sort()` and `defsort.check()` | Never                             | Never                                  |

Cleanup requires no annotations anywhere in the module and no occurrence of the name `annotations`
as a load, store, or deletion. It preserves combined or aliased future imports and imports sharing
their physical lines with comments or other statements. It removes every eligible standalone,
annotations-only import in one sort. Reflective use through strings is not detected.

On Python 3.14 and later, adding this import selects stringized annotations instead of the default
deferred annotation values. defsort applies this cross-version policy without inferring your
project's target Python version. Choose `--no-future` when that representation change is
unsuitable. A future import cannot repair an eager-order blocker.

## Incremental sort

Full sorting is the default and gives each eligible file the configured order. Incremental mode
uses Git HEAD as a baseline and inserts new definitions while preserving the relative order of
existing ones. This limits later churn after a baseline already conforms.

Incremental mode does not gradually repair an unsorted baseline. Establish the baseline with a full
sort, or configure the hook's full-sort fallback to adopt files as they are touched. Policy changes
can make a previously conforming baseline unsuitable. The [incremental guide](incremental.md)
explains adoption, rename handling, fallback choices, and reorder guards.

## Order detection

Use `--detect` or the library's `detect()` to describe current top-level order without changing
source. Results are `dependency`, `alphabetical`, `both`, or `unordered`. Sections separated by
banner barriers are compared independently; sections with fewer than two definitions are neutral.
If no section has a comparable pair, detection has no result.

Detection compares against the canonical defaults, rather than every configured policy axis. A file
can follow a custom policy and still be classified as `unordered`. To check your effective policy,
use an ordinary CLI run or the library's `check()`.

Verbose detection includes Kendall tau scores from -1 to 1: higher values indicate closer ordering
agreement. Scores count only pairs inside comparable sections and weight each section by its number
of pairs. They describe similarity, not whether a proposed sort is safe.

## Boundaries

Only direct module-body definitions and sortable class methods move. Conditional or dynamically
created definitions are outside that set. Code between movable definitions remains subject to the
interstitial guard, and trailing directive comments receive separate protection.

defsort provides structural ordering rather than framework-specific placement rules, arbitrary
name-priority rules, or guesses about which definitions are most important. Use the supported
policy axes, banners, and disabled scopes to express the organization your readers need.
