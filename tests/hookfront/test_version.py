import importlib.metadata

import pytest

from defsort._vendor.hookfront._version import (
    print_version,
)


def test_print_version_prints_distribution_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def version(name: str) -> str:
        assert name == 'tool'
        return '1.2.3'

    monkeypatch.setattr(importlib.metadata, 'version', version)

    assert print_version('tool') == 0
    assert capsys.readouterr() == ('tool 1.2.3\n', '')


def test_print_version_reports_missing_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, 'version', missing)

    assert print_version('tool') == 2
    assert capsys.readouterr() == (
        '',
        'error: installed distribution metadata for tool is unavailable\n',
    )
