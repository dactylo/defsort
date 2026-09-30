from defsort._core._layout import SourceLayout
from defsort._core._parse import parse_definitions
from defsort._core._reassemble import reassemble_source
from defsort._types import Definition


class TestReassembleSource:
    def test_banner_layout_reassembles_adjacent_empty_section_losslessly(self) -> None:
        source = (
            'def alpha():\n'
            '    pass\n'
            '\n\n'
            '# --------\n'
            '\n'
            '# ordinary block\n'
            '\n'
            '# ========\n'
            '\n\n'
            'def bravo():\n'
            '    pass\n'
        )
        parsed = parse_definitions(source, banners='barriers')

        result = reassemble_source(parsed.layout, parsed.blocks, parsed.definitions)

        assert result == source

    def test_empty_layout_reassembles_exact_prefix(self) -> None:
        layout = SourceLayout('import os\r\nvalue = 1', (), '')

        assert reassemble_source(layout, {}, []) == 'import os\r\nvalue = 1'

    def test_exact_concatenation(self) -> None:
        definitions = [_definition('alpha', 0), _definition('bravo', 1)]
        layout = SourceLayout('prefix\r\n', ('\n \t\r# fixed\r\n',), '\n# suffix')
        blocks = {0: 'ALPHA', 1: 'BRAVO'}

        result = reassemble_source(layout, blocks, definitions)

        assert result == 'prefix\r\nALPHA\n \t\r# fixed\r\nBRAVO\n# suffix'

    def test_final_slot_state_moves_out_and_in(self) -> None:
        definitions = [_definition('alpha', 0), _definition('bravo', 1)]
        layout = SourceLayout('', ('\r\n \t\r',), '')
        blocks = {0: 'ALPHA\nbody', 1: 'BRAVO\r\nbody'}

        original = reassemble_source(layout, blocks, definitions)
        reversed_source = reassemble_source(layout, blocks, list(reversed(definitions)))

        assert original == 'ALPHA\nbody\r\n \t\rBRAVO\r\nbody'
        assert reversed_source == 'BRAVO\r\nbody\r\n \t\rALPHA\nbody'


def _definition(name: str, index: int, *, section: int = 0) -> Definition:
    return Definition(
        index=index,
        name=name,
        start_line=index + 1,
        end_line=index + 1,
        kind='function',
        source=f'def {name}(): pass',
        section=section,
    )
