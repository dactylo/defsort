"""Light hook parsing, classification, adapter rendering, and engine dispatch."""

import argparse
import contextlib
import io
import json
import sys
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, override

from defsort._arguments import add_ordering_arguments, overrides_from_args
from defsort._hosts import AdapterLoadError, load_adapter
from defsort._terminal import escape_terminal_text
from defsort._types import EXIT_OK, FALLBACK_MODE_VALUES, SortOverrides
from defsort._vendor.hookshell._classify import Classification, classify_command
from defsort._vendor.hookshell._grammar import CommitPlan
from defsort._vendor.hookshell._shell import normalize_msys_path
from defsort.hooks import HostInput

if TYPE_CHECKING:
    from defsort.hooks import HookAdapter, HookEvent, HookInput

_LAST_RESORT_EXIT = 2
_REASON_PREFIX = 'defsort: unsupported commit command'


class _HookParser(argparse.ArgumentParser):
    """List the registered coding agents when hook help is shown."""

    @override
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        from defsort._hosts import registered_host_names  # noqa: PLC0415

        agents = ', '.join(registered_host_names()) or 'none'
        for action in self._actions:
            if action.dest == 'agent':
                action.help = f'coding agent: {agents}'
        if sys.version_info >= (3, 15):
            return super().format_help(formatter=formatter)
        return super().format_help()


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
        notes: Captured diagnostic lines for the adapter to render.
    """

    decision: Literal['allow', 'deny']
    reason: str | None = None
    notes: tuple[str, ...] = ()


def _build_hook_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the hook subcommand."""
    parser = _HookParser(
        prog='defsort hook',
        usage='%(prog)s --agent NAME [options]',
        description=(
            "Run defsort as a coding-agent hook, reading the agent's hook payload as JSON "
            'from stdin.'
        ),
        allow_abbrev=False,
    )
    parser.add_argument('-a', '--agent', required=True, metavar='NAME', help='coding agent')
    parser.add_argument(
        '-e',
        '--event',
        metavar='NAME',
        help='hook event, for agents whose payload does not name it (agy)',
    )
    incremental = parser.add_mutually_exclusive_group()
    incremental.add_argument(
        '-i',
        '--incremental',
        action='store_true',
        help='sort only definitions added since the last commit',
    )
    incremental.add_argument(
        '--no-incremental',
        action='store_true',
        help='sort every definition, even when configuration enables incremental mode',
    )
    parser.add_argument(
        '--incremental-fallback',
        choices=FALLBACK_MODE_VALUES,
        help='what to do when incremental sorting is blocked (default: warn)',
    )
    parser.add_argument(
        '--allow-on-failure',
        action='store_true',
        help='allow the command when defsort itself fails; safety blockers still deny',
    )
    add_ordering_arguments(parser)
    return parser


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
        return classify_command(command, normalized, reason_prefix=_REASON_PREFIX)
    except Exception as error:
        raise _ClassificationError('defsort: unexpected error during classification') from error


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


def _render(adapter: 'HookAdapter', host: str, result: Outcome) -> tuple[str, str, int]:
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


def _run_pre_tool_use(
    command: str,
    command_cwd: str | None,
    *,
    overrides: SortOverrides | None = None,
    allow_on_failure: bool = False,
) -> Outcome:
    """Classify a pre-tool command before repository access and sort a supported commit."""
    try:
        classification = _classify_for_hook(command, command_cwd)
    except _ClassificationError as error:
        traceback.print_exc()
        return Outcome('deny', str(error))
    if classification.outcome == 'unsupported':
        return Outcome('deny', classification.reason)
    plan = classification.plan
    if not isinstance(plan, CommitPlan):
        return allow()
    return _run_commit(plan, overrides=overrides, allow_on_failure=allow_on_failure)


def fail(reason: str, *, allow_on_failure: bool) -> Outcome:
    """Handle an infrastructure failure as a deny or a warned allow.

    Args:
        reason: Diagnostic text to escape before rendering or printing.
        allow_on_failure: Warn and allow instead of denying the operation.

    Returns:
        A silent allow after printing a warning, or a deny with the escaped
        reason.
    """
    reason = escape_terminal_text(reason)
    if allow_on_failure:
        print(f'WARNING: {reason}', file=sys.stderr)
        return allow()
    return Outcome('deny', reason)


def _run_commit(
    plan: CommitPlan,
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
) -> Outcome:
    """Load the engine and process a supported commit."""
    try:
        from defsort._hook import run_commit  # noqa: PLC0415
    except ImportError:
        traceback.print_exc()
        return fail('defsort: unexpected error during sort', allow_on_failure=allow_on_failure)

    return run_commit(plan, overrides=overrides, allow_on_failure=allow_on_failure)


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
            adapter, host, Outcome(outcome.decision, outcome.reason, notes)
        )
    except _LastResortError as error:
        return _last_resort(str(error))
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code


def _run_event(
    hook_input: 'HookInput',
    *,
    overrides: SortOverrides | None,
    allow_on_failure: bool,
) -> Outcome:
    """Dispatch neutral input to the pre-tool or post-tool core."""
    if hook_input.event == 'post_tool':
        return _run_post_tool(hook_input.edited_paths, hook_input.cwd, overrides=overrides)
    if hook_input.command is None:
        return allow()
    return _run_pre_tool_use(
        hook_input.command,
        hook_input.command_cwd,
        overrides=overrides,
        allow_on_failure=allow_on_failure,
    )


def _run_post_tool(
    edited_paths: Sequence[str],
    cwd: str,
    *,
    overrides: SortOverrides | None = None,
) -> Outcome:
    """Load the engine and process edited files after a tool call."""
    try:
        from defsort._hook import run_post_tool  # noqa: PLC0415
    except ImportError:
        traceback.print_exc()
        print('defsort: unexpected error during PostToolUse', file=sys.stderr)
        return allow()

    return run_post_tool(edited_paths, cwd, overrides=overrides)


def run_hook_command(argv: list[str]) -> int:
    """Handle the hook subcommand.

    Args:
        argv: Hook arguments after the `hook` command name.

    Returns:
        The hook exit status, or 0 when standard input is unavailable or a
        terminal.
    """
    hook_parser = _build_hook_parser()
    args = hook_parser.parse_args(argv)

    if sys.stdin.isatty():
        print(
            "defsort hook: reads the coding agent's hook payload as JSON from stdin",
            file=sys.stderr,
        )
        print('See docs/hook.md for configuration.', file=sys.stderr)
        return EXIT_OK

    try:
        stdin_data = sys.stdin.read()
    except (ValueError, OSError):
        return EXIT_OK

    return run_hook(
        stdin_data,
        host=args.agent,
        event=args.event,
        overrides=overrides_from_args(args),
        allow_on_failure=args.allow_on_failure,
    )
