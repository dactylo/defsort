import sys
from collections.abc import Iterable

import pytest

from defsort._vendor.hookfront import (
    _loader,
)
from defsort._vendor.hookfront._parser import (
    HookParser,
    RootParser,
    _StrictParser,
    add_allow_on_failure,
)
from defsort._vendor.hookfront._tool import (
    ToolIdentity,
)


def _error(
    parser: _StrictParser, argv: Iterable[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    with pytest.raises(SystemExit) as error:
        parser.parse_args(argv)
    assert isinstance(error.value.code, int)
    return error.value.code, capsys.readouterr().err.splitlines()[-1]


def _hook() -> HookParser:
    parser = HookParser(ToolIdentity('acme', 'sort'))
    incremental = parser.add_mutually_exclusive_group()
    incremental.add_argument('-i', '--incremental', action='store_true')
    incremental.add_argument('--no-incremental', action='store_true')
    parser.add_argument('--fallback', choices=('warn', 'fail'))
    return parser


def _root() -> _StrictParser:
    parser = _StrictParser(prog='acme', usage='%(prog)s [options] [PATH ...]', description='x')
    parser.add_argument('paths', nargs='*', default=['.'])
    output = parser.add_mutually_exclusive_group()
    output.add_argument('-d', '--diff', action='store_true')
    output.add_argument('-w', '--write', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--length', type=int)
    return parser


def test_abbreviation_is_rejected_with_a_hint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_root(), ['--wri'], capsys) == (
        2,
        'acme: error: unrecognized arguments: --wri (did you mean --write?)',
    )


def test_allow_on_failure_option() -> None:
    parser = _hook()
    add_allow_on_failure(parser, 'allow hook failures')
    assert parser.parse_args(['--agent', 'x', '--allow-on-failure']).allow_on_failure is True
    assert any(
        line.split() == ['--allow-on-failure', 'allow', 'hook', 'failures']
        for line in parser.format_help().splitlines()
    )


def test_borrowed_option_gets_no_wrong_hint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_root(), ['--check'], capsys) == (
        2,
        'acme: error: unrecognized arguments: --check',
    )


def test_borrowed_option_gets_the_right_hint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_root(), ['--line-length', '80'], capsys) == (
        2,
        'acme: error: unrecognized arguments: --line-length (did you mean --length?)',
    )


def test_help_beside_a_typo_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        _hook().parse_args(['--help', '--agnt', 'claude'])
    assert error.value.code == 0
    assert capsys.readouterr().out.startswith('usage: acme hook --agent NAME [options]')


@pytest.mark.parametrize(
    ('names', 'expected'),
    [(['agy', 'claude'], 'coding agent: agy, claude'), ([], 'coding agent: none')],
)
def test_help_lists_registered_hosts(
    names: list[str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def registered_names(_tool: ToolIdentity) -> list[str]:
        return names

    monkeypatch.setattr(_loader, 'registered_host_names', registered_names)
    with pytest.raises(SystemExit) as error:
        _hook().parse_args(['--help'])
    assert error.value.code == 0
    assert expected in capsys.readouterr().out


def test_invalid_choice_hint_follows_python(capsys: pytest.CaptureFixture[str]) -> None:
    code, output = _error(_hook(), ['--agent', 'x', '--fallback', 'wran'], capsys)
    assert code == 2
    assert ('maybe you meant' in output) is (sys.version_info >= (3, 14))


def test_iterator_arguments_are_read_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert _hook().parse_args(iter(['--agent', 'claude'])).agent == 'claude'
    assert _error(_hook(), iter(['--agnt', 'claude']), capsys) == (
        2,
        'acme hook: error: unrecognized arguments: --agnt (did you mean --agent?)',
    )
    assert _root().parse_args(iter(['--', '--wirte'])).paths == ['--wirte']


def test_later_errors_keep_their_text(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _root()
    _error(parser, ['--wirte'], capsys)
    with pytest.raises(SystemExit) as error:
        parser.error('custom')
    assert error.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == 'acme: error: custom'


def test_missing_required_option_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_hook(), [], capsys) == (
        2,
        'acme hook: error: the following arguments are required: -a/--agent',
    )


def test_required_option_typo_gets_a_hint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_hook(), ['--agnt', 'claude'], capsys) == (
        2,
        'acme hook: error: unrecognized arguments: --agnt (did you mean --agent?)',
    )


def test_root_help_lists_command_groups() -> None:
    class Parser(RootParser):
        _command_groups = (('commands', (('hook', 'run the agent hook'),)),)

    parser = Parser(prog='tool', usage='%(prog)s [options]', description='Run tool.')
    parser.add_argument('--write', action='store_true', help='write files')

    help_text = parser.format_help()
    assert help_text.index('options:') < help_text.index('commands:')
    assert '  hook' in help_text
    assert 'run the agent hook' in help_text


def test_stray_argument_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_hook(), ['--agent', 'claude', 'stray'], capsys) == (
        2,
        'acme hook: error: unrecognized arguments: stray',
    )


def test_tokens_after_double_dash_and_with_spaces_are_paths() -> None:
    assert _root().parse_args(['--', '--wirte']).paths == ['--wirte']
    assert _root().parse_args(['--odd name.py']).paths == ['--odd name.py']


def test_typo_replaces_conflict_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_root(), ['--diff', '--write', '--verbos'], capsys) == (
        2,
        'acme: error: unrecognized arguments: --verbos (did you mean --verbose?)',
    )


def test_typo_replaces_missing_value_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_hook(), ['--agent', '--evnt', 'x'], capsys) == (
        2,
        'acme hook: error: unrecognized arguments: --evnt (did you mean --event?)',
    )


def test_typo_with_value_gets_a_hint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_hook(), ['--agnt=claude'], capsys) == (
        2,
        'acme hook: error: unrecognized arguments: --agnt=claude (did you mean --agent?)',
    )


def test_unknown_options_without_a_match_are_listed(capsys: pytest.CaptureFixture[str]) -> None:
    assert _error(_root(), ['--wirte', '--whatever'], capsys) == (
        2,
        'acme: error: unrecognized arguments: --wirte (did you mean --write?) --whatever',
    )


def test_usage_errors_do_not_read_the_registry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_tool: ToolIdentity) -> list[str]:
        raise AssertionError('registry read during an error')

    monkeypatch.setattr(_loader, 'registered_host_names', fail)
    for argv in ([], ['--agnt', 'x']):
        with pytest.raises(SystemExit) as error:
            _hook().parse_args(argv)
        assert error.value.code == 2
        assert capsys.readouterr().err.startswith('usage: acme hook --agent NAME [options]')
