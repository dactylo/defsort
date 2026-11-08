"""Translate Antigravity hook events."""

import json
from collections.abc import Mapping
from typing import cast

from ._contract import (
    API_VERSION,
    HookResult,
    HostInput,
    HostResponse,
    absolute_directory,
    notes_text,
    object_field,
    payload_cwd,
)

_COMMAND_TOOL = 'run_command'
_EDIT_TOOLS = {
    'edit_file': 'Path',
    'replace_file_content': 'TargetFile',
    'write_file': 'Path',
    'write_to_file': 'TargetFile',
}
_EVENTS = {'PreToolUse': 'pre_tool', 'PostToolUse': 'post_tool'}


def _allow_response(result: HookResult) -> HostResponse:
    """Render Antigravity allow feedback."""
    stdout = (
        f'{json.dumps({"decision": "allow", "reason": result.reason})}\n' if result.reason else ''
    )
    return HostResponse(stdout, notes_text(result), 0)


def _base_cwd(payload: Mapping[str, object], args: Mapping[str, object]) -> str:
    """Resolve the target directory from `Cwd`, `workspacePaths`, `cwd`, or the process."""
    cwd = args.get('Cwd')
    if isinstance(cwd, str) and cwd:
        return cwd
    workspaces = payload.get('workspacePaths')
    if isinstance(workspaces, list):
        first = cast('list[object]', workspaces)[:1]
        if first and isinstance(first[0], str) and first[0]:
            return first[0]
    return payload_cwd(payload)


def _command_cwd(payload: Mapping[str, object], args: Mapping[str, object]) -> str | None:
    """Use explicit `Cwd`, or the first workspace when `Cwd` is absent or empty."""
    if 'Cwd' in args and args['Cwd'] != '':
        return absolute_directory(args, 'Cwd')
    workspaces = payload.get('workspacePaths')
    if isinstance(workspaces, list):
        first = cast('list[object]', workspaces)[:1]
        if first:
            return absolute_directory({'workspace': first[0]}, 'workspace')
    return None


def _deny(reason: str, stderr_prefix: str = '') -> HostResponse:
    """Render Antigravity's top-level deny object."""
    stdout = json.dumps({'decision': 'deny', 'reason': reason})
    return HostResponse(f'{stdout}\n', f'{stderr_prefix}{reason}\n', 0)


class AntigravityAdapter:
    """Translate Antigravity `toolCall` payloads, which carry no event name.

    Attributes:
        name: Host selector `agy`.
        api_version: Shared hook adapter API version implemented by this class.
        requires_event: `True`, because callers must supply the hook event.
    """

    name = 'agy'
    api_version = API_VERSION
    requires_event = True

    def __init__(self) -> None:
        """Initialize rendering for a pre-tool invocation until parsing sets the event."""
        self._post_tool = False

    def parse(self, payload: Mapping[str, object], event: str | None) -> HostInput | None:
        """Read `toolCall.name` and `toolCall.args` under the given event.

        For `run_command`, pre-tool `command_cwd` is `toolCall.args.Cwd` when
        it is an absolute path. When `Cwd` is absent or empty, the first
        `workspacePaths` entry is used if it is an absolute path. The process
        directory is never invocation evidence. Other tools report `None`.

        The event directory uses `Cwd`, the first `workspacePaths` entry,
        top-level `cwd`, or the process directory, in that order.

        Args:
            payload: Decoded Antigravity hook payload.
            event: `PreToolUse` or `PostToolUse` from `--event`.

        Returns:
            Neutral input; a tool other than `run_command` under `PreToolUse`
            or other than the file writers under `PostToolUse` yields input
            with nothing to act on.

        Raises:
            ValueError: If `event` is not an Antigravity hook event.
            OSError: If no usable event directory is supplied and the process
                directory cannot be determined.
        """
        neutral_event = _EVENTS.get(event or '')
        if neutral_event is None:
            raise ValueError(
                f'unknown Antigravity event {event!r}; expected PreToolUse or PostToolUse'
            )
        self._post_tool = neutral_event == 'post_tool'
        tool_call = object_field(payload, 'toolCall')
        args = object_field(tool_call, 'args')
        cwd = _base_cwd(payload, args)
        name = tool_call.get('name')
        if neutral_event == 'pre_tool':
            command = args.get('CommandLine') if name == _COMMAND_TOOL else None
            command_cwd = _command_cwd(payload, args) if name == _COMMAND_TOOL else None
            return HostInput(
                'pre_tool',
                command if isinstance(command, str) else None,
                cwd,
                command_cwd,
            )
        field = _EDIT_TOOLS.get(name) if isinstance(name, str) else None
        target = args.get(field) if field else None
        paths = (target,) if isinstance(target, str) and target else ()
        return HostInput('post_tool', None, cwd, None, paths)

    def render(self, result: HookResult) -> HostResponse:
        """Render allow feedback or a denial.

        Args:
            result: Decision, reason, and notes from the tool.

        Returns:
            Empty stdout for a silent allow, or an allow decision and reason
            for feedback; notes on stderr and exit 0. For deny, the deny JSON
            on stdout, the reason on stderr, and exit 0. Post-tool output is
            always empty stdout with notes and any reason on stderr.
        """
        notes = notes_text(result)
        if self._post_tool:
            return HostResponse('', notes + (f'{result.reason}\n' if result.reason else ''), 0)
        if result.decision == 'deny':
            return _deny(result.reason or '', notes)
        return _allow_response(result)

    def render_failure(self, reason: str) -> HostResponse:
        """Render a pre-tool denial or a post-tool diagnostic.

        Args:
            reason: Reason for the failure.

        Returns:
            The deny object on stdout for pre-tool events, the reason on
            stderr, and exit 0. Post-tool stdout is empty.
        """
        if self._post_tool:
            return HostResponse('', f'{reason}\n', 0)
        return _deny(reason)
