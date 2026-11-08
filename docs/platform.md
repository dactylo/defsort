# Platform compatibility

Use this page to check defsort's runtime requirements and file behavior on your platform. It also
covers path handling and recovery after a failed write.

## Requirements

- CPython 3.13 or newer. Alternative Python implementations are unsupported because defsort relies
  on CPython's AST behavior.
- A Python version that can parse your source. For example, use Python 3.14 to sort files
  containing syntax introduced in Python 3.14.
- Git 2.42 or newer for incremental mode and the reorder guard on the command line, after an
  agent's edit, and in commits that sort working-tree files. Sorting staged files at commit time
  works with older Git. Ordinary CLI and library sorting work without Git.

## Supported environments

| Environment           | Python runtime           |
| --------------------- | ------------------------ |
| Linux                 | Native Linux CPython     |
| macOS                 | Native macOS CPython     |
| Windows with Git Bash | Native Windows CPython   |
| Windows with WSL      | Linux CPython inside WSL |

Git Bash does not turn Windows Python into a POSIX runtime. Windows path and process rules still
apply. Use native Windows CPython rather than MSYS2 Python.

Choose the adapter for your coding agent; see [agent selection](hook.md#agent-selection).

## Source encoding and line endings

defsort reads Python source using its encoding declaration, including PEP 263 coding cookies and a
UTF-8 byte-order mark. Files without a declaration use Python's UTF-8 default. Writes use the same
encoding, and a UTF-8 BOM is preserved.

Ordinary sorting retains the exact decoded source, including LF, CRLF, lone CR, mixed line endings,
and whether the file ends with a newline. Binary file and Git I/O prevent the operating system from
converting those endings. A narrow boundary adjustment may relocate an authored blank line or swap
an adjacent lone CR and LF to keep two endings distinct; see
[source preservation](design.md#source-preservation).

The same encoding rules apply to staged Git content. When Git attributes or filters give a clean
working-tree file different text from its staged version, defsort sorts the working-tree text
independently when synchronization is safe. It does not replace that representation with the staged
one. Divergent unstaged edits are protected by the
[hook synchronization rules](hook-reference.md#working-tree-sync).

Invalid or unknown source encodings are reported according to the entry point's error policy.
Review CLI errors or hook diagnostics rather than assuming every selected file was processed.

## Writes and file metadata

defsort writes an existing file in place: it opens the file without truncating it, checks that the
file still holds the bytes defsort read, and writes the new bytes over them. defsort never creates
a file.

The file keeps its identity, so hard links, symlinks, ownership, permissions, ACLs, extended
attributes, filesystem flags, and other programs' open handles are unaffected, and the directory
need not be writable. The operating system still updates the modification time, may clear
set-user-ID and set-group-ID bits (Linux also removes file capabilities), and sets the Windows
archive attribute; defsort does not restore them.

### Concurrent changes

A file that changed after defsort read it, or was removed, is left alone and reported as changed.
That check is best effort: a write by another program between the check and defsort's own write is
not detected.

On Windows, a file that another program holds open without sharing write access cannot be written,
and defsort reports the error.

### Failed writes and recovery

If writing fails partway, for example on a full disk, defsort tries to write the original bytes
back before reporting the error. For main-thread writes with supported default handlers, Ctrl+C,
SIGTERM, and SIGHUP, or Ctrl+Break on Windows, take effect only after the write or attempted
restore finishes. Writes in other threads and custom signal handlers receive no signal deferral.
Failed restoration or uncertain close can still leave partial content. A stop that cannot be
caught, such as SIGKILL, a Windows job termination, or power loss, can also leave a partial file;
recover it from Git. defsort does not call `fsync`.

### Recovery files

If the original bytes cannot be written back, or a close error leaves the outcome uncertain,
defsort tries to save the original bytes to a recovery file. The error names that file or says
recovery could not be saved. Recovery files are never deleted automatically. Each is stored in a
new private directory under `%LOCALAPPDATA%\defsort\recovery` on Windows,
`~/Library/Application Support/defsort/recovery` on macOS, and `$XDG_STATE_HOME/defsort/recovery`
(by default `~/.local/state/defsort/recovery`) elsewhere. Other accounts, apart from
administrators, cannot read them. If that location is inside a Git worktree, for example a home
directory kept in Git, `git add -A` there can stage a recovery file.

An explicitly selected symlink is followed to its target. Recursive file discovery skips symlinks,
and hooks skip staged symlinks and other non-regular Git entries. Git index updates preserve the
file's regular mode, including its executable bit.

## Paths

CLI paths follow your operating system. See [file selection](cli.md#file-selection) for explicit
files, directory exclusions, symlinks, private labels, and Git-filtering failures.

Names that refer to one file, such as repeated operands, hard links, symlinks, and case variants on
a case-insensitive filesystem, are processed once. When their configurations or incremental
eligibility would give that file different results, defsort processes none of them and reports the
names, because one file cannot hold two results. A write through one name changes every name of the
file, including names defsort was not asked to process.

### Git Bash paths

For Git commands under Windows Python, use native Windows spelling:

```text
git -C C:/repo commit -m "Update helpers"
```

A detected commit is denied if a Git segment contains a literal word matching `/<single-letter>` or
`/<single-letter>/...`, such as `/c` or `/c/repo`. This includes quoted words, commit messages, and
add operands. The hook cannot reproduce Git Bash's argument conversion when replaying an add.

Host payload paths, literal `cd` routes, and attached `--pathspec-from-file=/c/repo/paths` values
are normalized at their supported boundaries: `/c/repo` becomes `C:/repo`, and `/c` becomes `C:/`.
Paths such as `/home/...` and `/tmp/...` are not rewritten. This does not exempt a standalone
MSYS-looking word inside a Git segment.

A literal `cd /c/repo && git commit ...` can route a Windows commit. Relative routes require a host
that supplies the command's working directory; Codex requires an absolute route. A cross-drive or
incompatible UNC path is not redirected to a guessed location. If Git cannot resolve an add path
for synchronization, the hook uses its staged-Python fallback.

### Unusual filenames and pathspec files

On POSIX, filenames may contain bytes that cannot be represented as strict UTF-8. Incremental and
reorder-guard modes cannot request a Git baseline for such a path and skip that file; other
encodable files continue normally. Ordinary file processing is unaffected by that baseline
limitation.

defsort reads the repository root that Git reports with the same path codec it uses for every name
from Git, removing only Git's final newline. A repository whose path ends in a space, or on Linux
is not valid UTF-8, works in incremental and reorder-guard modes. When Git cannot report a root,
PostToolUse skips the affected files rather than guessing one. Ordinary CLI sorting does not
require Git repository discovery.

For `--pathspec-from-file`, newline-delimited records accept CRLF endings and Git's C-style
quoting; NUL-delimited records preserve literal bytes. On Windows, a record that is not valid UTF-8
fails before any preceding add replay or write with
`defsort: cannot use pathspec file <name>: pathspec-file entry <entry> is not valid UTF-8; Git on Windows cannot receive it`.
The entry is escaped for terminal display. On POSIX, undecodable path bytes are preserved. See
[commit modes](hook-reference.md#commit-modes) for reading, parsing, and failure rules; the
[hook command restrictions](hook-reference.md#command-detection) still apply.

## Git executable lookup

defsort looks Git up in `PATH` before each Git launch, so a later `PATH` change takes effect. It
reads its own process's `PATH`, considers the entries in order, and skips every entry that is empty
or not absolute, including `.`; the current directory is never searched.

| Platform       | Rule                                                                                                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| POSIX          | The first entry holding an executable file named `git`. With `PATH` unset, Python's default search path is used; an empty `PATH` finds nothing.              |
| Native Windows | The first entry holding `git.exe`. `PATHEXT` is not consulted, so `git.cmd`, `git.bat` and `git.com` are never used. An unset or empty `PATH` finds nothing. |

The executable is used as found, without resolving symlinks or wrappers, so choose a trusted
`PATH`. Hook command text must still use the bare name `git`; a path-qualified Git executable is
outside the supported commit grammar.

Environment-prefixed detected commits are denied except for the documented `GIT_COMMITTER_DATE`
form. In particular, `GIT_DIR` and `GIT_WORK_TREE` cannot redirect a supported hook command. See
[command detection](hook-reference.md#command-detection).

## Deeply nested source

Python's parser limits how deeply source can nest. The limit depends on the Python version and
platform. Python 3.13 parses about 1,000 levels on Windows ARM64 and about 3,000 on other Windows
builds, against about 10,000 on macOS and Linux. Python 3.14 and later set the limit from the
available stack and accept far deeper nesting. A long chain of operators such as `a + b + c + ...`
nests one level per operator.

defsort reports a file beyond the limit as
`source nests too deeply for Python's parser on this platform` and leaves it unchanged. To sort
such a file, use Python 3.14 or later.

## Terminal output

Human-readable file paths use forward slashes on every platform, including Windows under Git Bash
or PowerShell. Drive letters and relative paths retain their meaning. Special Windows namespace
paths keep their original spelling. Literal backslashes in POSIX filenames are preserved. This
display convention does not change JSON paths, filesystem operations, or shell quoting. Diff
headers use forward slashes for ordinary Windows paths, keep namespace paths as spelled, and quote
names using Git's patch syntax.

Standard input, output, and error use UTF-8 on every platform. Stdout escapes characters that UTF-8
cannot represent, including undecodable POSIX filename bytes.

Set `NO_COLOR` to disable automatic color. Explicit `--color always` or `--color never` takes
precedence; see [CLI configuration](cli.md#color) for the complete precedence.
