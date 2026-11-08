from tests._helpers import deep_expression_terms

_DEEP_EXPRESSION = ' + '.join(['a'] * deep_expression_terms())
TOO_DEEP = 'f = ' + 'lambda: ' * 3000 + '0'

DEEP_DEFINITIONS = {
    'default': f'def alpha(x={_DEEP_EXPRESSION}):\n    return x\n',
    'decorator': f'@decorate({_DEEP_EXPRESSION})\ndef alpha():\n    pass\n',
    'class': f'class alpha:\n    x = {_DEEP_EXPRESSION}\n',
}


def deep_source_case(kind: str) -> tuple[str, str]:
    prefix = 'a = 1\n\n\n'
    bravo = 'def bravo():\n    pass\n'
    alpha = DEEP_DEFINITIONS[kind]
    return prefix + bravo + '\n\n' + alpha, prefix + alpha + '\n\n' + bravo
