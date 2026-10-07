"""Shared configuration, result, and internal processing records."""

import enum
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal, NamedTuple, cast

BannerMode = Literal['barriers', 'ignore', 'strip']

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

type BoundaryLayoutPart = str | SectionBoundary

ColorMode = Literal['auto', 'always', 'never']

DefinitionKind = Literal['class', 'function']

EvaluationOrder = Literal['preserve', 'allow-reordering']

FallbackMode = Literal['warn', 'full-sort']

JsonStatus = Literal['changed', 'error', 'invalid', 'skipped', 'unchanged']

MainPlacement = Literal['last', 'sorted']

MethodBodies = Literal['within-categories', 'ignore', 'across-categories']

MethodDunders = Literal['before-regular', 'after-regular']

MethodVisibility = Literal['mixed', 'public-first']

OrderKind = Literal['alphabetical', 'both', 'dependency', 'unordered']

PropertyKind = Literal['getter', 'setter', 'deleter']

SortScopeOrder = Literal['dependency', 'alphabetical', 'none']

# 'changed'   -- sort() would change the source
# 'invalid'   -- source cannot be parsed or validated as executable Python
# 'skipped'   -- file-level sorting was blocked, or only blocked class work remains
# 'unchanged' -- already in correct order
SortStatus = Literal['changed', 'invalid', 'skipped', 'unchanged']

TieBreak = Literal['alphabetical', 'source']

BANNER_MODE_VALUES: tuple[BannerMode, ...] = ('barriers', 'ignore', 'strip')
COLOR_MODE_VALUES: tuple[ColorMode, ...] = ('auto', 'always', 'never')
EVALUATION_ORDER_VALUES: tuple[EvaluationOrder, ...] = ('preserve', 'allow-reordering')
EXIT_CHANGED = 1
EXIT_ERROR = 2
EXIT_OK = 0
FALLBACK_MODE_VALUES: tuple[FallbackMode, ...] = ('warn', 'full-sort')
MAIN_PLACEMENT_VALUES: tuple[MainPlacement, ...] = ('last', 'sorted')
METHOD_BODIES_VALUES: tuple[MethodBodies, ...] = (
    'within-categories',
    'ignore',
    'across-categories',
)
METHOD_DUNDERS_VALUES: tuple[MethodDunders, ...] = ('before-regular', 'after-regular')
METHOD_VISIBILITY_VALUES: tuple[MethodVisibility, ...] = ('mixed', 'public-first')
SORT_SCOPE_ORDER_VALUES: tuple[SortScopeOrder, ...] = ('dependency', 'alphabetical', 'none')
TIE_BREAK_VALUES: tuple[TieBreak, ...] = ('alphabetical', 'source')

_BLOCKER_MESSAGES: dict[str, str] = {
    'duplicates': 'non-consecutive duplicate definitions',
    'eager_order': (
        'sort would move a decorator, default value, base class, or class-body '
        'reference before the definition it uses'
    ),
    'forward_refs': 'forward annotation references require future annotations',
    'interstitial': 'code between definitions',
    'layout': 'requested reorder cannot preserve source layout and comment ownership',
    'non_conforming': 'baseline not in target order',
    'target_incompatible': 'target order conflicts with existing positions',
    'user_reordered': 'existing definitions reordered from baseline',
}

_SOFT_BLOCKER_VALUES: frozenset[str] = frozenset(
    {
        'non_conforming',
        'target_incompatible',
        'user_reordered',
    }
)


class BlobReadStatus(enum.Enum):
    """Disposition of one Git blob read.

    Attributes:
        CONTENT: Blob text and its encoding are available.
        DECODE_FAILED: The blob exists but cannot be decoded as Python source.
        INFRA_ERROR: Git could not complete the blob read.
        MISSING: Git reports no object for the requested key.
    """

    CONTENT = 'content'  # blob text available; BlobReadResult.content is set
    DECODE_FAILED = 'decode_failed'  # blob exists but cannot be decoded
    INFRA_ERROR = 'infra_error'  # git call failed / not a repo
    MISSING = 'missing'  # no blob at that key (cat-file header 'missing')


@dataclass(frozen=True, slots=True, kw_only=True)
class BlobReadResult:
    """Result of reading one Git blob.

    Attributes:
        status: Disposition of the read operation.
        content: Decoded blob text for `BlobReadStatus.CONTENT`, otherwise
            `None`.
        encoding: Detected source encoding, defaulting to `utf-8`.

    `content` is a string exactly when `status` is `BlobReadStatus.CONTENT`.

    Raises:
        ValueError: If `content` disagrees with `status`.
    """

    status: BlobReadStatus
    content: str | None = None
    encoding: str = 'utf-8'

    def __post_init__(self) -> None:
        """Validate that content matches the blob read status.

        Raises:
            ValueError: If a `CONTENT` result lacks string content or a
                non-content result carries content.
        """
        if self.status is BlobReadStatus.CONTENT:
            if not isinstance(self.content, str):
                raise ValueError('CONTENT blob reads require string content')
        elif self.content is not None:
            raise ValueError('non-content blob reads cannot carry content')


@dataclass(frozen=True, slots=True)
class BlockingCode:
    """Preview of unsafe code between top-level definitions.

    Attributes:
        line: One-based line containing the unsafe code.
        text: Bounded source preview for diagnostics.
    """

    line: int
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassBlocker:
    """One class whose method sort was blocked.

    Attributes:
        class_name: Name of the blocked class.
        occurrence: 1-based occurrence index among same-named classes.
        blocker: Why the class body could not be sorted.
        message: Human-readable explanation of `blocker`, for display only.
    """

    class_name: str
    occurrence: int
    blocker: Blocker
    message: str


@dataclass(frozen=True, slots=True)
class ClassSlice:
    """Source lines and position of a class definition.

    Attributes:
        lines: Exact physical source lines occupied by the class.
        start_line: One-based line where the class begins.
        source: Exact class source derived from `lines`.
    """

    lines: list[str]
    start_line: int
    source: str = field(init=False)

    def __post_init__(self) -> None:
        """Derive `source` from `lines`."""
        object.__setattr__(self, 'source', ''.join(self.lines))


@dataclass
class Definition:
    """A top-level class or function definition with owned source.

    Attributes:
        index: Stable source-order identity.
        name: Defined class or function name.
        start_line: One-based first source line.
        end_line: One-based final source line.
        kind: Whether the definition is a class or function.
        source: Exact source occupied by the definition itself.
        trailing: Safe trailing source owned by the definition.
        leading: Safe leading source owned by the definition.
        dependencies: Top-level names required before the definition.
        annotation_refs: Top-level names referenced by annotations.
        had_interstitial_code: Whether unsafe code followed the definition.
        is_overload: Whether the definition is an overload declaration.
        section: Stable section identity assigned by banner analysis.
        eager_refs: Names read while the definition is created.
        has_eager_evaluation: Whether creating the definition can observe
            order.
    """

    index: int
    name: str
    start_line: int
    end_line: int
    kind: DefinitionKind
    source: str
    trailing: str = ''
    leading: str = ''
    dependencies: set[str] = field(default_factory=set[str])
    annotation_refs: set[str] = field(default_factory=set[str])
    had_interstitial_code: bool = False
    is_overload: bool = False
    section: int = 0
    eager_refs: set[str] = field(default_factory=set[str])
    has_eager_evaluation: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DetectResult:
    """Detected definition order and its agreement with the target orders.

    Attributes:
        order: The detected order classification.
        dep_score: Kendall tau for dependency order (-1.0 to 1.0).
        alpha_score: Kendall tau for alphabetical order (-1.0 to 1.0).
    """

    order: OrderKind
    dep_score: float
    alpha_score: float


@dataclass(frozen=True, slots=True)
class HeldItem:
    """One definition or method held on one side of a banner.

    Attributes:
        name: Display name of the held item.
        kind: Whether the item is a class, function, or method.
        occurrence: One-based occurrence among same-named items.
        line: One-based source line where the item begins.
        direction: Side toward which the unconstrained target would move the
            item across the banner, opposite the side where it remains.
    """

    name: str
    kind: Literal['class', 'function', 'method']
    occurrence: int
    line: int
    direction: Literal['before', 'after']


@dataclass(frozen=True, slots=True)
class HookContext:
    """Execution context threaded through hook handlers.

    Attributes:
        cwd: Effective working directory for the triggering command.
        repo_root: Resolved Git worktree root.
        allow_on_failure: Whether hook infrastructure failures allow the
            operation.
        incremental: Whether the hook uses incremental sorting.
        overrides: Command-line transformation overrides.
    """

    cwd: str
    repo_root: str
    allow_on_failure: bool
    incremental: bool
    overrides: 'SortOverrides'


@dataclass
class MethodDefinition:
    """A method definition within a class.

    Attributes:
        index: Stable source-order identity within the class.
        name: Defined method name.
        start_line: One-based first source line.
        end_line: One-based final source line.
        source: Exact source occupied by the method.
        semantic_deps: Class-local names required by definition-time semantics.
        property_group: Property name shared by related accessors, or `None`.
        property_kind: Getter, setter, or deleter role, or `None`.
        is_overload: Whether the method is an overload declaration.
        section: Stable section identity assigned by banner analysis.
        annotation_refs: Class-local names referenced by annotations.
        safe_gap_after: Whether the following gap permits grouping.
        body_refs: Class-local names read by the method body.
        eager_reads: Class-local names read while the method is created.
        has_eager_evaluation: Whether creating the method can observe order.
    """

    index: int
    name: str
    start_line: int
    end_line: int
    source: str
    semantic_deps: set[str] = field(default_factory=set[str])
    property_group: str | None = None
    property_kind: PropertyKind | None = None
    is_overload: bool = False
    section: int = 0
    annotation_refs: set[str] = field(default_factory=set[str])
    safe_gap_after: bool = True
    body_refs: set[str] = field(default_factory=set[str])
    eager_reads: set[str] = field(default_factory=set[str])
    has_eager_evaluation: bool = False


@dataclass
class MethodSortInfo:
    """Before-and-after method order for a single class.

    Attributes:
        class_name: Name of the sorted class.
        original_order: Methods in their original order.
        new_order: The same methods in proposed order.
    """

    class_name: str
    original_order: list[MethodDefinition]
    new_order: list[MethodDefinition]


class ClassSortResult(NamedTuple):
    """Per-class result from method sorting.

    Attributes:
        new_source: Rewritten class source, or `None` when unchanged.
        blocker: Reason sorting was blocked, or `None`.
        method_sort: Movement metadata when methods were reordered.
        banner_constraints: Banner constraints found within the class.
    """

    new_source: str | None  # rewritten class source, or None if unchanged
    blocker: Blocker | None
    method_sort: MethodSortInfo | None
    banner_constraints: 'tuple[BannerConstraint, ...]' = ()


@dataclass(frozen=True, slots=True)
class SectionBoundary:
    """A source banner that introduces one definition section.

    Attributes:
        section: Identity assigned to definitions after the banner.
        start_line: One-based first banner line.
        end_line: One-based final banner line.
        raw: Exact banner source.
        text: Normalized banner text for diagnostics.
    """

    section: int
    start_line: int
    end_line: int
    raw: str
    text: str


@dataclass(frozen=True, slots=True)
class BannerConstraint:
    """Items whose unconstrained target would cross one banner.

    Attributes:
        scope: Whether the constraint governs definitions or methods.
        class_name: Containing class for a method constraint, otherwise `None`.
        boundary: Banner that constrains the items.
        held: Items retained on their original side of the banner.
    """

    scope: Literal['definitions', 'methods']
    class_name: str | None
    boundary: SectionBoundary
    held: tuple[HeldItem, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SortCheckResult:
    """Result of check().

    Attributes:
        status: The sort status of the source.
        blocker: File-level blocker, or `None` for a class-only skip and when
            no file-level blocker applies.
        blocker_message: Human-readable explanation of `blocker`, for display
            only. Populated exactly when `blocker` is populated.
        banner_constrained: Whether a banner prevented the unconstrained
            target.
        class_blockers: Classes whose method sort was blocked.
    """

    status: SortStatus
    blocker: Blocker | None = None
    blocker_message: str | None = None
    banner_constrained: bool = False
    class_blockers: tuple[ClassBlocker, ...] = ()


def _scan_banner_character_class(
    char: str,
    escaped: bool,
    atom_seen: bool,
    at_start: bool,
) -> tuple[bool, bool, bool, bool]:
    """Advance the scanner state for one character-class character."""
    if escaped:
        return False, True, False, False
    if char == '\\':
        return True, atom_seen, at_start, False
    if char == '^' and at_start:
        return False, atom_seen, False, False
    if char == ']' and atom_seen:
        return False, atom_seen, False, True
    return False, True, False, False


def _unsupported_banner_pattern(detail: str) -> ValueError:
    """Build an error for unsupported banner-pattern syntax."""
    return ValueError(f'banner_pattern uses unsupported regular-expression syntax: {detail}')


def _scan_banner_pattern_special(
    char: str,
    index: int,
    pattern_length: int,
    atom_available: bool,
    repeat_seen: bool,
) -> tuple[bool, bool]:
    """Validate one unescaped special character and advance repeat state."""
    if char == '^':
        if index != 0:
            raise _unsupported_banner_pattern('^ is allowed only at the start')
        return False, repeat_seen
    if char == '$':
        if index != pattern_length - 1:
            raise _unsupported_banner_pattern('$ is allowed only at the end')
        return False, repeat_seen
    if char in '()|{}?':
        raise _unsupported_banner_pattern(f'{char!r} is not supported')
    if not atom_available:
        raise _unsupported_banner_pattern('repetition requires an atom')
    if repeat_seen:
        raise _unsupported_banner_pattern('only one repetition is supported')
    return False, True


def _validate_banner_pattern_subset(pattern: str) -> None:
    """Validate the bounded regular-expression subset used for custom banners."""
    escaped = False
    in_class = False
    class_atom_seen = False
    class_at_start = False
    atom_available = False
    repeat_seen = False

    for index, char in enumerate(pattern):
        if in_class:
            escaped, class_atom_seen, class_at_start, class_closed = _scan_banner_character_class(
                char,
                escaped,
                class_atom_seen,
                class_at_start,
            )
            if class_closed:
                in_class = False
                atom_available = True
            continue

        if escaped:
            if char.isdecimal():
                raise _unsupported_banner_pattern('backreferences are not supported')
            escaped = False
            atom_available = True
        elif char == '\\':
            escaped = True
            atom_available = False
        elif char == '[':
            in_class = True
            class_atom_seen = False
            class_at_start = True
            atom_available = False
        elif char in '^$()|{}?*+':
            atom_available, repeat_seen = _scan_banner_pattern_special(
                char,
                index,
                len(pattern),
                atom_available,
                repeat_seen,
            )
        else:
            atom_available = True


def blocker_message(blocker: Blocker) -> str:
    """Human-readable explanation text for one blocker value.

    Args:
        blocker: The blocker to describe.

    Returns:
        The display text from the internal blocker-message mapping.
    """
    return _BLOCKER_MESSAGES[blocker]


def is_soft_blocker(blocker: Blocker) -> bool:
    """Whether `blocker` is a soft incremental-policy condition.

    Args:
        blocker: The blocker to classify.

    Returns:
        True when the blocker only constrains incremental sorting rather than
        indicating an unsafe transformation.
    """
    return blocker in _SOFT_BLOCKER_VALUES


def validate_banner_pattern(banner_pattern: object) -> None:
    """Validate a `banner_pattern` value against the public contract.

    Args:
        banner_pattern: Candidate pattern value.

    Raises:
        TypeError: If `banner_pattern` is neither a string nor `None`.
        ValueError: If the pattern does not compile or uses unsupported syntax.
            Compilation failures attach the underlying `re.error` as the cause.
    """
    if banner_pattern is None:
        return
    if not isinstance(banner_pattern, str):
        msg = f'banner_pattern must be a string or None, got {type(banner_pattern).__name__}'
        raise TypeError(msg)
    try:
        re.compile(banner_pattern)
    except re.error as exc:
        msg = f'banner_pattern is not a valid regular expression: {exc}'
        raise ValueError(msg) from exc
    _validate_banner_pattern_subset(banner_pattern)


def validate_vocabulary(name: str, value: object, allowed: Collection[str]) -> None:
    """Validate one closed-vocabulary configuration value.

    Args:
        name: Field name used in the error message.
        value: Candidate value.
        allowed: The closed set of accepted string values.

    Raises:
        ValueError: If `value` is not one of `allowed`.
    """
    if not isinstance(value, str) or value not in allowed:
        expected = ', '.join(repr(item) for item in allowed)
        msg = f'{name} must be one of {expected}, got {value!r}'
        raise ValueError(msg)


def validate_config(config: 'DefsortConfig') -> None:
    """Validate every public configuration construction invariant.

    Shared by `__post_init__` and the public operations' boundary defense so
    both raise identical exceptions.

    Args:
        config: Configuration object to validate.

    Raises:
        TypeError: If `force` is not a `bool`, or `banner_pattern` is neither a
            string nor `None`.
        ValueError: If a vocabulary field holds a value outside its alias, or
            `banner_pattern` does not compile or uses unsupported syntax.
    """
    validate_vocabulary('definition_order', config.definition_order, SORT_SCOPE_ORDER_VALUES)
    validate_vocabulary('method_order', config.method_order, SORT_SCOPE_ORDER_VALUES)
    validate_vocabulary('evaluation_order', config.evaluation_order, EVALUATION_ORDER_VALUES)
    if not isinstance(cast('object', config.force), bool):
        msg = f'force must be a bool, got {type(config.force).__name__}'
        raise TypeError(msg)
    validate_vocabulary('banners', config.banners, BANNER_MODE_VALUES)
    validate_banner_pattern(config.banner_pattern)
    validate_vocabulary('method_bodies', config.method_bodies, METHOD_BODIES_VALUES)
    validate_vocabulary('method_visibility', config.method_visibility, METHOD_VISIBILITY_VALUES)
    validate_vocabulary('method_dunders', config.method_dunders, METHOD_DUNDERS_VALUES)
    validate_vocabulary('main', config.main, MAIN_PLACEMENT_VALUES)
    validate_vocabulary('tie_break', config.tie_break, TIE_BREAK_VALUES)


# ---------------------------------------------------------------------------
# Section: configuration object.
# This banner is an ordering barrier, not decoration. `DefsortConfig` calls
# `validate_config` from `__post_init__`, and the sorter deliberately ignores
# method-body references, so without this barrier alphabetical tie-breaking
# would hoist the class above the validators it depends on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class DefsortConfig:
    """Public transformation configuration shared by `sort()` and `check()`.

    Values are validated at construction, including the bounded
    regular-expression subset accepted by `banner_pattern`.

    Attributes:
        definition_order: Ordering strategy for top-level definitions.
        method_order: Ordering strategy for methods within classes.
        evaluation_order: Whether definition-time effects preserve source order
            or may be reordered.
        force: Whether eligible safety checks may be overridden.
        banners: Treatment of recognized section banners.
        banner_pattern: Additional safe full-match banner pattern, or `None`.
        method_bodies: Scope in which method-body dependencies constrain order.
        method_visibility: Relative placement of public and private methods.
        method_dunders: Relative placement of non-lifecycle dunder methods.
        main: Whether `main()` is anchored last or sorted normally.
        tie_break: Tie-breaking strategy for dependency ordering.

    Raises:
        TypeError: If `force` or `banner_pattern` has an invalid type.
        ValueError: If a vocabulary value or `banner_pattern` is invalid.
    """

    definition_order: SortScopeOrder = 'dependency'
    method_order: SortScopeOrder = 'dependency'
    evaluation_order: EvaluationOrder = 'preserve'
    force: bool = False
    banners: BannerMode = 'barriers'
    banner_pattern: str | None = None
    method_bodies: MethodBodies = 'within-categories'
    method_visibility: MethodVisibility = 'mixed'
    method_dunders: MethodDunders = 'before-regular'
    main: MainPlacement = 'last'
    tie_break: TieBreak = 'alphabetical'

    def __post_init__(self) -> None:
        """Validate every construction invariant.

        Raises:
            TypeError: If `force` is not a boolean or `banner_pattern` is
                neither a string nor `None`.
            ValueError: If a vocabulary value is unknown or `banner_pattern`
                does not compile or uses unsupported syntax.
        """
        validate_config(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class SortOverrides:
    """CLI flag overrides applied on top of a resolved DefsortConfig.

    Nullable fields are `None` when not overridden. `clear_banner_pattern`
    remains false unless the caller requests a reset.

    Attributes:
        banners: Override for recognized banner treatment.
        banner_pattern: Override for the custom banner pattern.
        clear_banner_pattern: Whether to discard a configured banner pattern.
        definition_order: Override for top-level definition order.
        evaluation_order: Override for definition-time evaluation order.
        incremental: Override for incremental sorting.
        incremental_fallback: Override after `non_conforming` or
            `target_incompatible`. Missing baselines use full sorting;
            `user_reordered` remains warning-only.
        main: Override for `main()` placement.
        method_bodies: Override for method-body dependency scope.
        method_dunders: Override for dunder-method placement.
        method_order: Override for class-method order.
        method_visibility: Override for method visibility placement.
        order: Shared override for definition and method order.
        show_skips: Override for skipped-file diagnostics.
        tie_break: Override for dependency tie-breaking.
    """

    banners: BannerMode | None = None
    banner_pattern: str | None = None
    clear_banner_pattern: bool = False
    definition_order: SortScopeOrder | None = None
    evaluation_order: EvaluationOrder | None = None
    incremental: bool | None = None
    incremental_fallback: FallbackMode | None = None
    main: MainPlacement | None = None
    method_bodies: MethodBodies | None = None
    method_dunders: MethodDunders | None = None
    method_order: SortScopeOrder | None = None
    method_visibility: MethodVisibility | None = None
    order: SortScopeOrder | None = None
    show_skips: bool | None = None
    tie_break: TieBreak | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SortPolicy:
    """Resolved ordering policy shared across engine scopes.

    Attributes:
        definition_order: Ordering strategy for top-level definitions.
        method_order: Ordering strategy for methods within classes.
        evaluation_order: Whether definition-time effects preserve source order
            or may be reordered.
        method_bodies: Scope in which method-body dependencies constrain order.
        method_visibility: Relative placement of public and private methods.
        method_dunders: Relative placement of non-lifecycle dunder methods.
        main: Whether `main()` is anchored last or sorted normally.
        tie_break: Tie-breaking strategy for dependency ordering.
        banners: Treatment of recognized section banners.
        banner_pattern: Additional safe full-match banner pattern, or `None`.
    """

    definition_order: SortScopeOrder = 'dependency'
    method_order: SortScopeOrder = 'dependency'
    evaluation_order: EvaluationOrder = 'preserve'
    method_bodies: MethodBodies = 'within-categories'
    method_visibility: MethodVisibility = 'mixed'
    method_dunders: MethodDunders = 'before-regular'
    main: MainPlacement = 'last'
    tie_break: TieBreak = 'alphabetical'
    banners: BannerMode = 'barriers'
    banner_pattern: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class EngineOptions:
    """Concrete policy plus source-transformation settings for the engine.

    Attributes:
        policy: Resolved ordering and banner policy.
        force: Whether eligible safety checks may be overridden.
        hoist: Whether safe constants may move into the preamble.
        add_future: Whether needed future-annotations imports may be added.
        remove_future: Whether unnecessary future-annotations imports may be
            removed.
    """

    policy: SortPolicy
    force: bool = False
    hoist: bool = False
    add_future: bool = True
    remove_future: bool = True


@dataclass
class SortResult:
    """Result of sorting a file.

    Attributes:
        original_source: Source supplied to the sorter.
        new_source: Sorted source, or the original source when unchanged.
        original_order: Definitions in their original order.
        new_order: Definitions in their proposed order.
        warning: Optional human-readable diagnostic.
        blocking_code: Unsafe interstitial code found during parsing.
        blocker: File-level reason sorting was blocked, or `None`. Class-only
            skips are recorded in `class_blockers`.
        skipped: Whether a safety condition prevented sorting.
        guarded: Whether the large-reorder guard prevented sorting.
        method_sorts: Movement metadata for sorted classes.
        total_methods: Number of sortable methods encountered.
        banner_constraints: Banner constraints affecting proposed order.
        class_blockers: Classes whose method sorting was blocked.
    """

    original_source: str
    new_source: str
    original_order: list[Definition]
    new_order: list[Definition]
    warning: str | None
    blocking_code: list[BlockingCode]
    blocker: Blocker | None = None
    skipped: bool = False
    guarded: bool = False
    method_sorts: list[MethodSortInfo] = field(default_factory=list[MethodSortInfo])
    total_methods: int = 0
    banner_constraints: list[BannerConstraint] = field(
        default_factory=list[BannerConstraint],
    )
    class_blockers: tuple[ClassBlocker, ...] = ()


def sort_policy_from_config(config: DefsortConfig) -> SortPolicy:
    """Build the concrete engine policy from public configuration.

    Args:
        config: Public transformation configuration to resolve.

    Returns:
        The concrete ordering policy carried by `EngineOptions`.
    """
    return SortPolicy(
        definition_order=config.definition_order,
        method_order=config.method_order,
        evaluation_order=config.evaluation_order,
        method_bodies=config.method_bodies,
        method_visibility=config.method_visibility,
        method_dunders=config.method_dunders,
        main=config.main,
        tie_break=config.tie_break,
        banners=config.banners,
        banner_pattern=config.banner_pattern,
    )
