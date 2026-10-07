# Platform compatibility

Use this guide to check runtime requirements and understand how defsort handles files on your
platform. The [CLI reference](cli.md) covers file selection; the [hook guide](hook.md) covers agent
registration and commit behavior.

## Requirements

- CPython 3.13 or newer. Alternative Python implementations are unsupported because defsort relies
  on CPython's AST behavior.
- A Python version that can parse your source. For example, use Python 3.14 to sort files
  containing syntax introduced in Python 3.14.
- Git 2.42 or newer for agent hooks, CLI incremental mode, or a configured reorder guard. Ordinary
  CLI and library sorting work without Git.

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
[source preservation](cli.md#source-preservation).

The same encoding rules apply to staged Git content. When Git attributes or filters give a clean
working-tree file different text from its staged version, defsort sorts the working-tree text
independently when synchronization is safe. It does not replace that representation with the staged
one. Divergent unstaged edits are protected by the
[hook synchronization rules](hook.md#working-tree-sync).

Invalid or unknown source encodings are reported according to the entry point's error policy.
Review CLI errors or hook diagnostics rather than assuming every selected file was processed.

## Writes and file metadata

CLI and working-tree writes use a temporary file in the target directory and atomically replace the
target. This prevents a failed write from truncating the existing source. The target directory must
be writable, even when the file itself is writable.

Replacement has these consequences:

- Permission bits are preserved.
- Other inode metadata, including ownership, ACLs, extended attributes, and filesystem flags, is
  not preserved.
- Hard links no longer share the updated content because the replaced target gets a new inode.
- A temporary `.defsort-*.tmp` file left by an interrupted process may be removed once that process
  is no longer running.

An explicitly selected symlink is followed to its target. Recursive file discovery skips symlinks,
and hooks skip staged symlinks and other non-regular Git entries. Git index updates preserve the
file's regular mode, including its executable bit.

## Paths

CLI paths follow the current operating system's conventions. Directory scans prune common
non-source directories, honor Git exclusions, and use each nested worktree's own tracked state.
Explicitly named Python files remain eligible even when Git ignores them. See
[file selection](cli.md#synopsis) for details.

### Git Bash paths

For hook commit commands on Windows Python, use native Windows spelling:

```text
git -C C:/repo commit -m "Update helpers"
```

A detected commit is denied if a Git segment contains a literal word matching `/<single-letter>` or
`/<single-letter>/...`, such as `/c` or `/c/repo`. The restriction includes quoted words, commit
messages, and add operands. defsort cannot reproduce Git Bash's argument conversion when it replays
an add, so it rejects those ambiguous commands before touching the repository.

Host payload paths and a supported routing `cd` are normalized separately: `/c/repo` becomes
`C:/repo`, and `/c` becomes `C:/`. Paths such as `/home/...` and `/tmp/...` are not rewritten. This
normalization does not exempt MSYS-looking words inside a Git segment from the restriction.

A cross-drive or incompatible UNC path is not redirected to a guessed location. If Git cannot
resolve an add path for synchronization, the hook uses its staged-Python fallback.

### Unusual filenames and pathspec files

On POSIX, filenames may contain bytes that cannot be represented as strict UTF-8. Incremental and
reorder-guard modes cannot request a Git baseline for such a path and skip that file; other
encodable files continue normally. Ordinary file processing is unaffected by that baseline
limitation.

If Git cannot return a repository root as strict UTF-8, incremental and reorder-guard modes skip
files whose baseline requires that discovery. PostToolUse also skips affected files rather than
guessing a repository root. Ordinary CLI sorting does not require Git repository discovery.

For `--pathspec-from-file`, newline-delimited records accept CRLF endings. NUL-delimited records
preserve their bytes. Passing pathspecs through a file can avoid shell quoting problems, but the
[hook command restrictions](hook.md#command-detection) still apply.

## Git executable lookup

defsort resolves Git from `PATH` before each Git launch, so later `PATH` changes take effect.

| Platform       | Lookup behavior                                                                                                                                                                                               |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POSIX          | Uses the child environment's `PATH`; relative and empty entries are relative to the child working directory. An absent `PATH` uses Python's default search path; an empty one searches the working directory. |
| Native Windows | Uses the parent process's `PATH` and working directory; searches only `git.exe`. An absent or entirely empty `PATH` fails lookup.                                                                             |

Windows does not implicitly search the application or system directories or expand `PATHEXT`.
Explicit entries retain their order, including relative and empty components within a nonempty
`PATH`. Wrappers and symlinks are allowed; choose a trusted `PATH`.

For a replayed add, the execution directory determines POSIX executable lookup. Git's subsequent
`-C` target does not change that lookup directory. Hook command text must still use the bare name
`git`; a path-qualified Git executable is outside the supported commit grammar.

Environment-prefixed detected commits are denied except for the documented `GIT_COMMITTER_DATE`
form. In particular, `GIT_DIR` and `GIT_WORK_TREE` cannot redirect a supported hook command. See
[command detection](hook.md#command-detection).

## Terminal output

Human-readable file paths use forward slashes on every platform, including Windows under Git Bash
or PowerShell. Drive letters and relative paths retain their meaning. Special Windows namespace
paths keep their original spelling. Literal backslashes in POSIX filenames are preserved. This
display convention does not change JSON paths, redirected patches, filesystem operations, or shell
quoting.

Windows standard input, output, and error use UTF-8. On every platform, stdout escapes characters
that its encoding cannot represent, including undecodable POSIX filename bytes.

Set `NO_COLOR` to disable automatic color. Explicit `--color always` or `--color never` takes
precedence; see [CLI configuration](cli.md#configuration-tooldefsort) for the complete precedence.
