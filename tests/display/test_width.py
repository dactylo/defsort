from defsort._display._width import display_width, pad_cells


class TestDisplayWidth:
    def test_pad_cells_aligns_by_cells(self) -> None:
        assert pad_cells('ab', 4) == 'ab  '
        assert pad_cells('文件', 5) == '文件 '
        assert pad_cells('abcdef', 4) == 'abcdef'

    def test_wide_and_fullwidth_count_two_cells(self) -> None:
        assert display_width('abc') == 3
        assert display_width('文件') == 4
        assert display_width('a文b') == 4
