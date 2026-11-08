"""Translate Codex hook events."""

import json
from collections.abc import Mapping

from ._contract import (
    API_VERSION,
    HookResult,
    HostInput,
    HostResponse,
    notes_text,
    object_field,
    payload_cwd,
)

_APPLY_PATCH_FILE_PREFIXES = (
    '*** Add File: ',
    '*** Delete File: ',
    '*** Move to: ',
    '*** Update File: ',
)
_CUT = '\n... (more on stderr)'
_EDIT_TOOL = 'apply_patch'
_MAX_MESSAGE = 10_000


def _allow_response(result: HookResult) -> HostResponse:
    """Render Codex allow feedback."""
    lines = '\n'.join((result.reason or '', *result.notes)).split('\n')
    message = '\n'.join(dict.fromkeys(line for line in lines if line))
    stderr = notes_text(result)
    if len(message) > _MAX_MESSAGE:
        message = message[: _MAX_MESSAGE - len(_CUT)] + _CUT
        if result.reason:
            stderr = f'{result.reason}\n{stderr}'
    stdout = f'{json.dumps({"systemMessage": message})}\n' if message else ''
    return HostResponse(stdout, stderr, 0)


def _apply_patch_paths(command: str) -> tuple[str, ...]:
    """Extract touched file paths from an `apply_patch` command."""
    paths: list[str] = []
    for line in command.splitlines():
        for prefix in _APPLY_PATCH_FILE_PREFIXES:
            if line.startswith(prefix):
                path = line.removeprefix(prefix).strip()
                if path and path not in paths:
                    paths.append(path)
                break
    return tuple(paths)


def _permission_deny(reason: str, stderr_prefix: str = '') -> HostResponse:
    """Render Codex's `hookSpecificOutput` deny shape."""
    payload = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        },
    }
    return HostResponse(f'{json.dumps(payload)}\n', f'{stderr_prefix}{reason}\n', 0)


class CodexAdapter:
    """Translate Codex PreToolUse and PostToolUse payloads.

    Call `parse()` before `render()` on the same instance. Parsing each payload
    replaces the event context used to choose the response format.

    Attributes:
        name: Host selector `codex`.
        api_version: Shared hook adapter API version implemented by this class.
        requires_event: `False`, because the payload supplies its event.
    """

    name = 'codex'
    api_version = API_VERSION
    requires_event = False
    _post_tool = False

    def parse(self, payload: Mapping[str, object], event: str | None) -> HostInput | None:
        """Read `hook_event_name`, `tool_name`, `tool_input`, and `cwd`.

        Codex defines top-level `cwd` as the session directory and exposes no
        execution directory for a shell call, so pre-tool `command_cwd` is
        always `None`. Undocumented directory-like fields are ignored.

        Args:
            payload: Decoded Codex hook payload.
            event: Always `None`; Codex payloads carry their event.

        Returns:
            Neutral input for a `PreToolUse` or `PostToolUse` payload, else
            `None`. Post-tool paths come from the `apply_patch` command text.
            A missing or empty event directory falls back to the process
            directory.

        Raises:
            OSError: If the process directory is needed but cannot be
                determined.
        """
        del event
        self._post_tool = False
        hook_event = payload.get('hook_event_name')
        tool_name = payload.get('tool_name')
        tool_input = object_field(payload, 'tool_input')
        cwd = payload_cwd(payload)
        if hook_event == 'PreToolUse':
            command = tool_input.get('command') if tool_name == 'Bash' else None
            return HostInput('pre_tool', command if isinstance(command, str) else None, cwd, None)
        if hook_event == 'PostToolUse':
            self._post_tool = True
            command = tool_input.get('command') if tool_name == _EDIT_TOOL else None
            paths = _apply_patch_paths(command) if isinstance(command, str) else ()
            return HostInput('post_tool', None, cwd, None, paths)
        return None

    def render(self, result: HookResult) -> HostResponse:
        """Render feedback using the event recorded by `parse()`.

        Args:
            result: Decision, reason, and notes from the tool.

        Returns:
            For pre-tool input, empty stdout for a silent allow, a system
            message containing distinct nonempty feedback lines, or deny JSON
            with the denial reason on stderr. Allow messages are capped at
            10,000 characters; when truncated, the complete reason is also
            written to stderr.

            For post-tool input, distinct nonempty diagnostic lines become
            additional context without that cap, or stdout is empty when there
            are none. Notes remain complete on stderr and the exit status is 0.
        """
        if self._post_tool:
            return _post_tool_response(result)
        notes = notes_text(result)
        if result.decision == 'deny':
            return _permission_deny(result.reason or '', notes)
        return _allow_response(result)

    def render_failure(self, reason: str) -> HostResponse:
        """Render the deny JSON that blocks the tool call.

        Args:
            reason: Reason for the failure.

        Returns:
            The deny JSON on stdout, the reason on stderr, and exit 0.
        """
        return _permission_deny(reason)


def _post_tool_response(result: HookResult) -> HostResponse:
    """Render distinct diagnostic lines as post-tool context."""
    lines = '\n'.join((*result.notes, result.reason or '')).split('\n')
    context = '\n'.join(dict.fromkeys(line for line in lines if line))
    payload = {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': context,
        },
    }
    stdout = f'{json.dumps(payload)}\n' if context else ''
    return HostResponse(stdout, notes_text(result), 0)
