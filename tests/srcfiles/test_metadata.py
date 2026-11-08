import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from defsort._vendor.srcfiles import (
    write_in_place,
)

from ._support import (
    TOOL,
    ready_child,
)

# The child holds the file open for reading with the given Windows share mode until stdin closes.
_HOLDER = r"""
import ctypes, sys

generic_read, open_existing, normal = 0x80000000, 3, 0x80
share = int(sys.argv[2])
handle = ctypes.windll.kernel32.CreateFileW(
    sys.argv[1], generic_read, share, None, open_existing, normal, None
)
print('ready' if handle != -1 else 'failed', flush=True)
sys.stdin.readline()
"""
_NEW = b'x = 2\n'
_ORIGINAL = b'def f():\n    return 1\n'
_SHARE_READ = 1
_SHARE_WRITE = 2


def _target(tmp_path: Path) -> Path:
    path = tmp_path / 'target.py'
    path.write_bytes(_ORIGINAL)
    return path


def test_hidden_file_and_alternate_stream_survive_on_windows(tmp_path: Path) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows attributes and streams')
    target = _target(tmp_path)
    stream = Path(f'{target}:note')
    stream.write_bytes(b'kept')
    subprocess.run(['attrib', '+h', str(target)], check=True)
    write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == _NEW
    assert stream.read_bytes() == b'kept'
    assert target.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN


@pytest.mark.parametrize(
    ('share', 'result'), [(_SHARE_READ | _SHARE_WRITE, _NEW), (_SHARE_READ, _ORIGINAL)]
)
def test_holder_sharing_write_access_decides_on_windows(
    tmp_path: Path, share: int, result: bytes
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows share modes')
    target = _target(tmp_path)
    with ready_child(
        [sys.executable, '-c', _HOLDER, str(target), str(share)],
    ) as holder:
        try:
            if result == _NEW:
                write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
            else:
                with pytest.raises(PermissionError):
                    write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
        finally:
            holder.communicate('\n', timeout=60)
    assert target.read_bytes() == result


def test_set_user_id_bit_cleared_by_the_system_stays_cleared_on_linux(tmp_path: Path) -> None:
    if sys.platform != 'linux' or os.geteuid() == 0:
        pytest.skip('Linux, as a user without CAP_FSETID')
    target = _target(tmp_path)
    target.chmod(0o4755)
    if not target.stat().st_mode & stat.S_ISUID:
        pytest.skip('the filesystem refused the set-user-ID bit')
    write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == _NEW
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_user_xattr_survives_on_linux(tmp_path: Path) -> None:
    if sys.platform != 'linux':
        pytest.skip('Linux xattrs')
    target = _target(tmp_path)
    try:
        os.setxattr(target, 'user.note', b'kept')
    except OSError as error:
        pytest.skip(f'user xattrs unavailable: {error}')
    write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
    assert target.read_bytes() == _NEW
    assert os.getxattr(target, 'user.note') == b'kept'


def test_xattr_acl_and_hidden_flag_survive_on_macos(tmp_path: Path) -> None:
    if sys.platform != 'darwin':
        pytest.skip('macOS metadata')
    target = _target(tmp_path)
    subprocess.run(['xattr', '-w', 'com.example.note', 'kept', str(target)], check=True)
    subprocess.run(['chmod', '+a', 'everyone deny delete', str(target)], check=True)
    os.chflags(target, stat.UF_HIDDEN)
    try:
        write_in_place(target, _NEW, expected=_ORIGINAL, tool=TOOL)
        assert target.read_bytes() == _NEW
        note = subprocess.run(
            ['xattr', '-p', 'com.example.note', str(target)],
            capture_output=True,
            check=True,
            text=True,
        )
        assert note.stdout == 'kept\n'
        listing = subprocess.run(
            ['ls', '-le', str(target)], capture_output=True, check=True, text=True
        )
        # Directory services may render the everyone group as its UUID.
        assert (
            'everyone deny delete' in listing.stdout
            or 'ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C deny delete' in listing.stdout
        )
        assert target.stat().st_flags & stat.UF_HIDDEN
    finally:
        # The deny entry would stop pytest from removing the temporary directory.
        subprocess.run(['chmod', '-N', str(target)], check=True)
