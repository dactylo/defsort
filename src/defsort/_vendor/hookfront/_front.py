"""Parse, classify, render, and dispatch one tool's hook invocation."""

import contextlib
import importlib.metadata
import io
import json
import sys
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from typing import Literal, Protocol, cast

from defsort._vendor.hookshell._classify import (
    Classification,
    classify_command,
)
from defsort._vendor.hookshell._grammar import (
    CommitPlan,
)
from defsort._vendor.hookshell._shell import (
    normalize_msys_path,
)
from defsort._vendor.srcfiles import (
    FileChangedError,
    RestoreError,
)

from . import _loader
from ._contract import HookAdapter, HookEvent, HookInput, HostInput
from ._terminal import escape_terminal_text
from ._tool import ToolIdentity

_LAST_RESORT_EXIT = 2


class _ClassificationError(Exception):
    """Report that classification failed before a trusted plan existed."""


class _LastResortError(Exception):
    """Signal that no functioning host adapter exists for this invocation."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """Decision of the hook core before a host adapter renders it.

    Attributes:
        decision: Whether the triggering operation is allowed or denied.
        reason: Optional feedback or denial reason for the host adapter.
        notes: Diagnostic lines the front renders after any lines the engine
            wrote to standard error.
    """

    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


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


@cache
def _documentation_url(distribution: str) -> str | None:
    """Find the installed distribution's documentation URL, if available."""
    with contextlib.suppress(Exception):
        project_urls = importlib.metadata.metadata(distribution).get_all('Project-URL') or ()
        for entry in project_urls:
            label, separator, url = entry.partition(',')
            if separator and label.strip().casefold() == 'documentation':
                stripped_url = url.strip()
                if stripped_url:
                    return stripped_url
    return None


class _CommitRunner(Protocol):
    """Process a classified commit with tool-specific engine configuration."""

    def __call__(self, plan: CommitPlan, *, allow_on_failure: bool) -> Outcome:
        """Run the commit, warning on infrastructure failures when requested."""
        ...


class _PostToolRunner(Protocol):
    """Process paths edited by a completed tool call."""

    def __call__(self, edited_paths: Sequence[str], cwd: str) -> Outcome:
        """Process edited paths, resolving relative paths against `cwd`."""
        ...


@dataclass(frozen=True, slots=True)
class Engine:
    """Tool-specific callables loaded after parsing and classification.

    Attributes:
        run_commit: Process a supported commit plan.
        run_post_tool: Process paths edited by a completed tool call.
    """

    run_commit: _CommitRunner
    run_post_tool: _PostToolRunner


@dataclass(frozen=True, slots=True)
class HookOptions:
    """Shared options for one hook invocation.

    Attributes:
        host: Adapter name selected by `--agent`.
        event: `--event` value for a host whose payload carries no event.
        allow_on_failure: Warn instead of deny on infrastructure failures after
            a command is accepted. Safety blockers still deny.
    """

    host: str
    event: str | None = None
    allow_on_failure: bool = False


def _commit_hint(tool: ToolIdentity) -> str:
    """Point unsupported commit options at the installed hook guide."""
    with contextlib.suppress(Exception):
        url = _documentation_url(tool.name)
        if url:
            return f'supported commit options are listed in hook.md at {escape_terminal_text(url)}'
    return f'supported commit options are listed in the {tool.name} hook guide, hook.md'


def _last_resort(tool: ToolIdentity, diagnostic: str) -> int:
    """Write one diagnostic line and exit 2 when no functioning adapter exists."""
    print(f'{tool.name}: {escape_terminal_text(diagnostic)}', file=sys.stderr)
    return _LAST_RESORT_EXIT


def _post_tool_paths(cwd: object, paths: Iterable[object]) -> tuple[str, tuple[str, ...]]:
    """Validate the PostToolUse path fields consumed by the hook core."""
    if not isinstance(cwd, str):
        raise TypeError('post-tool cwd must be a string')
    if isinstance(paths, str):
        raise TypeError('post-tool edited paths must be a sequence of strings, not a string')
    edited_paths: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            raise TypeError('post-tool edited paths must be strings')
        edited_paths.append(path)
    return cwd, tuple(edited_paths)


def _response_fields(stdout: object, stderr: object, exit_code: object) -> tuple[str, str, int]:
    """Validate every response field before either output stream is written."""
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TypeError('adapter response streams must be strings')
    if not isinstance(exit_code, int):
        raise TypeError('adapter response exit_code must be an integer')
    return stdout, stderr, exit_code


def _render_response(adapter: 'HookAdapter', host: str, result: Outcome) -> tuple[str, str, int]:
    """Translate a native render exception into an adapter failure."""
    try:
        response = adapter.render(result)
        return _response_fields(response.stdout, response.stderr, response.exit_code)
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to render the response") from error


def _render(
    tool: ToolIdentity, adapter: 'HookAdapter', host: str, result: Outcome
) -> tuple[str, str, int]:
    """Render a result, falling back to the adapter's failure response."""
    try:
        return _render_response(adapter, host, result)
    except _LastResortError:
        try:
            response = adapter.render_failure(
                f"{tool.name}: host '{escape_terminal_text(host)}' failed to render the response"
            )
            return _response_fields(response.stdout, response.stderr, response.exit_code)
        except Exception as error:
            raise _LastResortError(
                f"host '{host}' failed to render the failure response"
            ) from error


def _select_adapter(tool: ToolIdentity, host: str, event: str | None) -> 'HookAdapter':
    """Load the selected adapter and enforce its `--event` requirement."""
    try:
        adapter = _loader.load_adapter(tool, host)
        requires_event = adapter.requires_event
    except _loader.AdapterLoadError as error:
        raise _LastResortError(str(error)) from error
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to load: {error}") from error
    if requires_event and event is None:
        raise _LastResortError(f"host '{host}' requires --event")
    if not requires_event and event is not None:
        raise _LastResortError(f"host '{host}' does not accept --event")
    return adapter


def _validated_adapter_fields(event: object, command: object) -> tuple['HookEvent', str | None]:
    """Check adapter values at the runtime boundary."""
    if event not in ('pre_tool', 'post_tool'):
        raise TypeError(f'invalid hook event {event!r}')
    if command is not None and not isinstance(command, str):
        raise TypeError(f'invalid hook command {command!r}')
    return event, command


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
        hook_event, command = _validated_adapter_fields(hook_input.event, hook_input.command)
        if hook_event == 'post_tool':
            cwd, edited_paths = _post_tool_paths(hook_input.cwd, hook_input.edited_paths)
            return HostInput(hook_event, None, cwd, command_cwd, edited_paths)
        return HostInput(hook_event, command, '', command_cwd)
    except _LastResortError:
        raise
    except Exception as error:
        raise _LastResortError(f"host '{host}' failed to parse the payload: {error}") from error


def allow() -> Outcome:
    """Return the silent allow outcome."""
    return Outcome('allow')


def _run_post_tool(
    edited_paths: Sequence[str],
    cwd: str,
    *,
    tool: ToolIdentity,
    load_engine: Callable[[], Engine],
) -> Outcome:
    """Load the engine and process edited files after a tool call."""
    try:
        engine = load_engine()
    except ImportError:
        traceback.print_exc()
        print(f'{tool.name}: unexpected error during PostToolUse', file=sys.stderr)
        return allow()
    return engine.run_post_tool(edited_paths, cwd)


def error_summary(error: BaseException) -> str:
    """Summarize an exception using its type and first message line."""
    message = str(error)
    name = type(error).__name__
    return f'{name}: {message.splitlines()[0]}' if message else name


def _classify_for_hook(
    command: str, command_cwd: str | None, tool: ToolIdentity
) -> Classification:
    """Translate classifier exceptions into the fail-closed hook boundary."""
    try:
        normalized = None if command_cwd is None else normalize_msys_path(command_cwd)
        return classify_command(
            command,
            normalized,
            reason_prefix=tool.reason_prefix,
            possible_reason_prefix=tool.possible_reason_prefix,
            commit_hint=partial(_commit_hint, tool),
        )
    except Exception as error:
        raise _ClassificationError(
            escape_terminal_text(
                f'{tool.name}: unexpected error during classification: {error_summary(error)}'
            )
        ) from error


def describe_error(error: BaseException) -> str:
    """Describe file failures without repeating paths or truncating recovery details."""
    if isinstance(error, (FileChangedError, RestoreError)):
        return str(error)
    if isinstance(error, UnicodeDecodeError):
        return error.reason
    if isinstance(error, SyntaxError):
        return error.msg
    if isinstance(error, OSError):
        return error.strerror if error.strerror is not None else str(error)
    return error_summary(error)


def fail(reason: str, *, allow_on_failure: bool) -> Outcome:
    """Handle an infrastructure failure as a deny or a warned allow.

    Args:
        reason: Diagnostic text to escape before rendering or printing.
        allow_on_failure: Warn and allow instead of denying the operation.

    Returns:
        A silent allow after printing a warning, or a deny with the escaped
        reason.

    Raises:
        OSError: If warning output cannot be written.
    """
    reason = escape_terminal_text(reason)
    if allow_on_failure:
        print(f'WARNING: {reason}', file=sys.stderr)
        return allow()
    return Outcome('deny', reason)


def _run_commit(
    plan: CommitPlan,
    *,
    tool: ToolIdentity,
    allow_on_failure: bool,
    load_engine: Callable[[], Engine],
) -> Outcome:
    """Load the engine and process a supported commit."""
    try:
        engine = load_engine()
    except ImportError as error:
        traceback.print_exc()
        return fail(
            f'{tool.name}: unexpected error during {tool.verb}: {error_summary(error)}',
            allow_on_failure=allow_on_failure,
        )
    return engine.run_commit(plan, allow_on_failure=allow_on_failure)


def _run_pre_tool_use(
    command: str,
    command_cwd: str | None,
    *,
    tool: ToolIdentity,
    options: HookOptions,
    load_engine: Callable[[], Engine],
) -> Outcome:
    """Classify a pre-tool command before repository access and process a supported commit."""
    try:
        classification = _classify_for_hook(command, command_cwd, tool)
    except _ClassificationError as error:
        traceback.print_exc()
        return Outcome('deny', str(error))
    if classification.outcome == 'unsupported':
        return Outcome('deny', classification.reason)
    plan = classification.plan
    if not isinstance(plan, CommitPlan):
        return allow()
    return _run_commit(
        plan, tool=tool, allow_on_failure=options.allow_on_failure, load_engine=load_engine
    )


def _run_event(
    hook_input: HookInput,
    *,
    tool: ToolIdentity,
    options: HookOptions,
    load_engine: Callable[[], Engine],
) -> Outcome:
    """Dispatch neutral input to the pre-tool or post-tool core."""
    if hook_input.event == 'post_tool':
        return _run_post_tool(
            hook_input.edited_paths, hook_input.cwd, tool=tool, load_engine=load_engine
        )
    if hook_input.command is None:
        return allow()
    return _run_pre_tool_use(
        hook_input.command,
        hook_input.command_cwd,
        tool=tool,
        options=options,
        load_engine=load_engine,
    )


def read_payload(tool: ToolIdentity) -> str | None:
    """Read a hook payload, or print configuration guidance for terminal input.

    Args:
        tool: Identity used in terminal guidance and documentation lookup.

    Returns:
        Standard input text, or `None` when input is a terminal or reading it
        raises `ValueError` or `OSError`.

    Raises:
        OSError: If terminal input cannot be inspected or configuration
            guidance cannot be written.
    """
    if sys.stdin.isatty():
        print(
            f"{tool.name} hook: reads the coding agent's hook payload as JSON from stdin",
            file=sys.stderr,
        )
        url = _documentation_url(tool.name)
        line = (
            f'See hook.md at {escape_terminal_text(url)} for configuration.'
            if url
            else f'See the {tool.name} hook guide, hook.md, for configuration.'
        )
        print(line, file=sys.stderr)
        return None
    try:
        return sys.stdin.read()
    except (ValueError, OSError):
        return None


def resolve_edited_paths(paths: Iterable[str], cwd: str) -> list[str]:
    """Resolve Python paths against the event directory in first-seen order.

    Args:
        paths: Paths reported by the completed tool call.
        cwd: Directory from which relative paths were reported.

    Returns:
        Distinct Python paths, preserving their first occurrence.
        Relative paths are joined to `cwd`, so a result is absolute when its
        path or `cwd` is.
    """
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


def run_hook(
    stdin_data: str,
    *,
    tool: ToolIdentity,
    options: HookOptions,
    load_engine: Callable[[], Engine],
) -> int:
    """Run one hook invocation through the selected host adapter.

    Args:
        stdin_data: Raw JSON text the host wrote to standard input.
        tool: Identity used to load adapters and name diagnostics.
        options: Host selection, event, and failure policy for this invocation.
        load_engine: Deferred engine factory, called only for a supported
            commit or post-tool event. Import failures follow the event failure
            policy.

    Returns:
        The exit status the adapter rendered. Invalid JSON and payloads the
        adapter reports as inapplicable exit 0 silently. When no functioning
        adapter exists, one diagnostic line goes to standard error and the
        status is 2.

    Raises:
        OSError: If the rendered response or last-resort diagnostic cannot be
            written.
        Exception: If the engine factory raises an exception other than
            `ImportError`, or an engine call raises. Engine exceptions retain
            their original types.
    """
    try:
        adapter = _select_adapter(tool, options.host, options.event)
        hook_input = _parse_input(adapter, options.host, stdin_data, options.event)
        if hook_input is None:
            return 0
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            outcome = _run_event(hook_input, tool=tool, options=options, load_engine=load_engine)
        notes = (*captured.getvalue().splitlines(), *outcome.notes)
        reason = outcome.reason
        if outcome.decision == 'deny' and not reason:
            reason = tool.deny_fallback
        stdout, stderr, exit_code = _render(
            tool, adapter, options.host, Outcome(outcome.decision, reason, notes)
        )
    except _LastResortError as error:
        return _last_resort(tool, str(error))
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code
