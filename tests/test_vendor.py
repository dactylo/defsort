import hashlib
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_VENDORED_SUFFIXES = frozenset({'.json', '.py'})


def _vendored_paths(name: str, manifest: Path) -> set[str]:
    return {
        path.relative_to(_ROOT).as_posix()
        for root in (manifest.parent, _ROOT / 'tests' / name)
        for path in root.rglob('*')
        if path.is_file()
        and path.suffix in _VENDORED_SUFFIXES
        and '__pycache__' not in path.parts
        and path != manifest
    }


@pytest.mark.parametrize('name', ['hookshell', 'srcfiles', 'hookfront', 'gitfiles'])
def test_vendored_package_matches_its_manifest(name: str) -> None:
    manifest = _ROOT / 'src' / 'defsort' / '_vendor' / name / 'manifest.json'
    files: dict[str, str] = json.loads(manifest.read_text(encoding='utf-8'))['files']
    assert set(files) == _vendored_paths(name, manifest)
    for path, digest in files.items():
        assert hashlib.sha256((_ROOT / path).read_bytes()).hexdigest() == digest, path
