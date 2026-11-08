from dataclasses import dataclass, fields
from pathlib import Path, PureWindowsPath

import pytest

from defsort._vendor.hookfront import (
    _contract,
)
from defsort._vendor.hookfront._contract import (
    API_VERSION,
    HookDecisionName,
    HostInput,
    HostResponse,
    absolute_directory,
    notes_text,
    object_field,
    payload_cwd,
)
from defsort._vendor.hookfront._tool import (
    ToolIdentity,
)
from defsort._vendor.hookshell import (
    _shell,
)


@dataclass(frozen=True)
class _Result:
    decision: HookDecisionName
    reason: str | None
    notes: tuple[str, ...]


def test_absolute_directory_accepts_msys_drive_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_contract, 'Path', PureWindowsPath)
    monkeypatch.setattr(_shell.sys, 'platform', 'win32')
    assert absolute_directory({'cwd': '/c/repo'}, 'cwd') == '/c/repo'
    assert absolute_directory({'cwd': 'relative'}, 'cwd') is None


def test_api_version_is_one() -> None:
    assert API_VERSION == 1


def test_identity_derives_names() -> None:
    tool = ToolIdentity('acme', 'sort')
    assert (tool.name, tool.verb) == ('acme', 'sort')
    assert tool.entry_point_group == 'acme.hook_adapters'
    assert tool.reason_prefix == 'acme: unsupported commit command'
    assert tool.possible_reason_prefix == 'acme: possible commit text'
    assert tool.deny_fallback == 'acme denied the operation'


def test_notes_text_ends_each_note_with_a_newline() -> None:
    assert notes_text(_Result('allow', None, ('a', 'b'))) == 'a\nb\n'


def test_object_field_ignores_non_objects() -> None:
    assert object_field({'a': [1]}, 'a') == {}


def test_payload_cwd_falls_back_to_process_directory() -> None:
    assert payload_cwd({}) == str(Path.cwd())
    assert payload_cwd({'cwd': ''}) == str(Path.cwd())


def test_record_fields_keep_their_order() -> None:
    assert [field.name for field in fields(HostInput)] == [
        'event',
        'command',
        'cwd',
        'command_cwd',
        'edited_paths',
    ]
    assert HostInput('pre_tool', 'ls', '/', None).edited_paths == ()
    assert [field.name for field in fields(HostResponse)] == ['stdout', 'stderr', 'exit_code']
