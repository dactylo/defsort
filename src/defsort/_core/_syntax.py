"""Normalize parser resource limits into invalid-source errors."""

import ast

NESTING_LIMIT_MESSAGE = "source nests too deeply for Python's parser on this platform"


def parse_source(source: str, *, filename: str = '<unknown>', check: bool = False) -> ast.Module:
    """Parse source, optionally checking that it compiles as a module.

    Parser and compiler recursion or memory limits raise `SyntaxError` with the
    original exception as their cause, like other invalid input.

    Args:
        source: Python source text to parse.
        filename: Filename used in parser and compiler diagnostics.
        check: Also compile the AST to reject constructs invalid at module
            scope.

    Returns:
        The parsed module AST.

    Raises:
        SyntaxError: If parsing fails, or compilation fails when `check` is
            true, including normalized parser or compiler resource limits.
        ValueError: If source contains a lone Unicode surrogate.
    """
    try:
        tree = ast.parse(source, filename=filename)
        if check:
            compile(tree, filename, 'exec')
    except (RecursionError, MemoryError) as error:
        raise SyntaxError(NESTING_LIMIT_MESSAGE) from error
    return tree
