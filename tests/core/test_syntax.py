import ast

import pytest

import defsort._core._syntax as syntax


@pytest.mark.parametrize('error_type', [RecursionError, MemoryError])
@pytest.mark.parametrize('stage', ['parse', 'compile'])
def test_resource_limit_is_invalid_source(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], stage: str
) -> None:
    error = error_type('resource limit')

    def fail(*_args: object, **_kwargs: object) -> ast.Module:
        raise error

    if stage == 'parse':
        monkeypatch.setattr(ast, 'parse', fail)
    else:
        monkeypatch.setattr(syntax, 'compile', fail, raising=False)
    with pytest.raises(
        SyntaxError, match=r"^source nests too deeply for Python's parser on this platform$"
    ) as caught:
        syntax.parse_source('pass', filename='input.py', check=True)
    assert caught.value.__cause__ is error
