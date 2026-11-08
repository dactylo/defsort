"""Write existing source files in place with attempted restoration on write errors.

`write_in_place` replaces a file's bytes through the file's own descriptor and
attempts to restore the original on a write error. Main-thread writes defer
Ctrl+C and supported termination signals until the call finishes, only with
supported default handlers. If restoration fails or closing leaves the outcome
uncertain, the error names a private per-user recovery file containing the
original or says recovery could not be saved. The source file may retain
partial content. `group_paths` groups paths that name one file, so callers can
give that file one result. `FileChangedError` and `RestoreError` are the
writer's own failures; both subclass `OSError`.
`private_attribute`, `label_query`, and `labelled_paths` validate the name of
the git attribute that labels private paths, build the `git check-attr` query
for it, and read the paths its output reports as set; this package never
launches git.
`repository_key`, `SpellingIndex`, `tracked_query`, and `tracked_paths` match a
path to Git's spelling of it without launching Git.
`decode_git_path` and `encode_git_path` round-trip Git path bytes across
platforms.
`names_agree` and `ALIAS_CONFLICT` check results required by names of one file.

Consumers vendor this package and import these names from it; see the
repository README.
"""

from ._errors import FileChangedError, RestoreError
from ._keys import (
    KeyMatch,
    SpellingIndex,
    decode_git_path,
    encode_git_path,
    repository_key,
    tracked_paths,
    tracked_query,
)
from ._labels import DEFAULT_PRIVATE_ATTRIBUTE, label_query, labelled_paths, private_attribute
from ._paths import ALIAS_CONFLICT, group_paths, names_agree
from ._write import write_in_place

__all__ = [
    'ALIAS_CONFLICT',
    'DEFAULT_PRIVATE_ATTRIBUTE',
    'FileChangedError',
    'KeyMatch',
    'RestoreError',
    'SpellingIndex',
    'decode_git_path',
    'encode_git_path',
    'group_paths',
    'label_query',
    'labelled_paths',
    'names_agree',
    'private_attribute',
    'repository_key',
    'tracked_paths',
    'tracked_query',
    'write_in_place',
]
