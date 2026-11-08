import errno
import os
import subprocess
from pathlib import Path

import pytest

from defsort._vendor.gitfiles._run import (
    _git_executable,
)


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    executable = _git_executable()
    assert executable is not None
    return subprocess.run([executable, *args], cwd=cwd, capture_output=True, check=True)


def latin1_decode(raw: bytes) -> str:
    return raw.decode('latin-1')


def latin1_encode(path: str) -> bytes:
    return path.encode('latin-1')


def write_bytes_file(root: Path, name: bytes, content: bytes) -> None:
    try:
        descriptor = os.open(
            os.fsencode(root / os.fsdecode(name)), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
    except OSError as error:
        if error.errno == errno.EILSEQ:
            pytest.skip('filesystem cannot create non-UTF-8 filenames')
        raise
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
