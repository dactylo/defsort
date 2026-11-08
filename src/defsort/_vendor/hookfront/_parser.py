"""Provide strict option parsing for hook commands."""

import argparse
import sys
from collections.abc import Iterable
from typing import ClassVar, NoReturn, TypeVar, cast, overload, override

from . import _loader
from ._hints import closest
from ._tool import ToolIdentity

_Namespace = TypeVar('_Namespace')


class _StrictParser(argparse.ArgumentParser):
    """Parse options without abbreviations and suggest close long options."""

    def __init__(self, *, prog: str, usage: str, description: str) -> None:
        """Initialize the parser with its command name and usage text."""
        if sys.version_info >= (3, 14):
            super().__init__(
                prog=prog,
                usage=usage,
                description=description,
                allow_abbrev=False,
                suggest_on_error=True,
            )
        else:
            super().__init__(prog=prog, usage=usage, description=description, allow_abbrev=False)
        self._unknown: tuple[str, ...] = ()

    @overload
    def parse_args(
        self, args: Iterable[str] | None = None, namespace: None = None
    ) -> argparse.Namespace: ...

    @overload
    def parse_args(self, args: Iterable[str] | None, namespace: _Namespace) -> _Namespace: ...

    @overload
    def parse_args(self, *, namespace: _Namespace) -> _Namespace: ...

    @override
    def parse_args(
        self, args: Iterable[str] | None = None, namespace: _Namespace | None = None
    ) -> argparse.Namespace | _Namespace:
        """Parse all arguments while tracking mistyped long options.

        Args:
            args: Argument tokens, or process arguments when omitted.
            namespace: Existing namespace to fill when supplied.

        Returns:
            The parsed argument namespace, preserving a supplied namespace.

        Raises:
            SystemExit: With status 0 after help, or status 2 after a usage
                error.
            OSError: If help or error output cannot be written.
        """
        tokens = sys.argv[1:] if args is None else list(args)
        prefix = tokens[: tokens.index('--')] if '--' in tokens else tokens
        self._unknown = tuple(
            token
            for token in prefix
            if token.startswith('--')
            and ' ' not in token
            and token.partition('=')[0] not in self._option_string_actions
        )
        try:
            # argparse creates a Namespace when given None.
            return cast('argparse.Namespace | _Namespace', super().parse_args(tokens, namespace))
        finally:
            self._unknown = ()

    @override
    def error(self, message: str) -> NoReturn:
        """Print usage and an error, suggesting mistyped long options.

        Args:
            message: Error text supplied by argparse.

        Raises:
            SystemExit: Always with status 2 after the diagnostic is written.
            OSError: If the diagnostic cannot be written.
        """
        if self._unknown:
            names = {
                option[2:]: option
                for option in self._option_string_actions
                if option.startswith('--')
            }
            parts: list[str] = []
            for token in self._unknown:
                match = closest(token.partition('=')[0][2:], names)
                if match is None:
                    parts.append(token)
                else:
                    parts.append(f'{token} (did you mean {names[match]}?)')
            message = 'unrecognized arguments: ' + ' '.join(parts)
        super().error(message)


class RootParser(_StrictParser):
    """Render root options before commands with their own parsers.

    Subclasses supply `_command_groups` as ordered pairs of a section title and
    ordered `(command_name, help_text)` pairs. These groups affect help display
    only; callers dispatch commands separately.
    """

    _command_groups: ClassVar[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = ()

    @override
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        """Place command groups after option groups in the root help."""
        if formatter is None:
            formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        for action_group in self._action_groups:
            formatter.start_section(action_group.title)
            formatter.add_text(action_group.description)
            # argparse exposes no public accessor for a group's actions.
            formatter.add_arguments(action_group._group_actions)  # noqa: SLF001
            formatter.end_section()
        for title, commands in self._command_groups:
            formatter.start_section(title)
            formatter.add_arguments(
                [argparse.Action([], name, help=text) for name, text in commands]
            )
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


class HookParser(_StrictParser):
    """Parse shared hook options and list registered hosts in help."""

    def __init__(self, tool: ToolIdentity) -> None:
        """Build the hook parser for a tool.

        Args:
            tool: Tool whose name and registered hosts appear in help.
        """
        super().__init__(
            prog=f'{tool.name} hook',
            usage='%(prog)s --agent NAME [options]',
            description=(
                f"Run {tool.name} as a coding-agent hook, reading the agent's hook "
                'payload as JSON from stdin.'
            ),
        )
        self._tool = tool
        self.add_argument('-a', '--agent', required=True, metavar='NAME', help='coding agent')
        self.add_argument(
            '-e',
            '--event',
            metavar='NAME',
            help='hook event, for agents whose payload does not name it (agy)',
        )

    @override
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        """Include registered host names in hook help."""
        names = _loader.registered_host_names(self._tool)
        for action in self._actions:
            if action.dest == 'agent':
                help_text = f'coding agent: {", ".join(names) or "none"}'
                if action.help != help_text:
                    action.help = help_text
        if sys.version_info >= (3, 15):
            return super().format_help(formatter=formatter)
        return super().format_help()


def add_allow_on_failure(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Add a flag permitting hook failure without denying the operation.

    Args:
        parser: Parser that receives the flag.
        help_text: Description displayed in help.
    """
    parser.add_argument('--allow-on-failure', action='store_true', help=help_text)
