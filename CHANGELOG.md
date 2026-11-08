# Changelog

## 0.3.0 (2026-11-07)

### Breaking changes

- In incremental mode, a file with code between top-level definitions is now skipped whenever
  sorting would change it, so the commit hook denies such a commit. Move that code above the first
  definition or below the last, or run `defsort --force` on the file.
- The commit hook no longer re-sorts the working-tree copy of a file with an `ident` or `filter`
  attribute. Run `defsort --write` on such a file before you commit.
- A `defsort.hook` value that is not a Git boolean now makes the hook deny commits. Set it to
  `true` or `false`, or remove it.

### Added

- The commit hook allows a quoted heredoc written to a file in a chain of simple commands, such as
  `mkdir -p notes && cat >notes/review.md <<'EOF'`.
- Directory scans and commit checks skip files whose `private` Git attribute is set. Choose another
  attribute with `git config defsort.privateAttribute NAME`.

### Changed

- The commit hook works with any Git version; Git 2.42 or newer is needed only for incremental mode
  and the reorder guard.
- Files are written in place, keeping hard links and other metadata, and a file that changed after
  defsort read it is left alone.
- Uncolored `--diff` output is a patch that `git apply -p0` applies exactly.
- Error and warning messages name the cause and the fix, and Claude Code and Codex show the hook's
  warnings to you.
- Directory scans skip Git-ignored folders, which is much faster in repositories with large ignored
  trees.

### Fixed

- A class with code between methods or duplicate method names no longer stops the rest of the file
  from being sorted.
- Deeply nested expressions no longer fail with `maximum recursion depth exceeded`.
- In Antigravity, the commit hook no longer blocks the commits it sorts or a plain `git commit`,
  and the PostToolUse hook sorts files after `write_file` and `edit_file` edits.
- On Windows, the hook accepts Git Bash directories such as `/c/repo`.

### Security

- defsort looks for Git only in absolute `PATH` entries, so an entry such as `.` can no longer make
  it run a `git` program from the current directory.

## 0.2.0 (2026-10-06)

### Breaking changes

- The hook now denies commit commands that use ANSI-C quoting (`$'...'`), such as
  `git commit -m $'first\nsecond'`. Use ordinary quotes or the heredoc message idiom.

### Added

- The heredoc commit-message idiom accepts any quoted delimiter name, not only `EOF`.
- Before a command that cannot create a commit, such as `git commit --dry-run`, the hook accepts
  `git status -s`, `-b`, or `-sb`, and `git diff --staged`.

### Changed

- When a `git status`, `git diff`, or `git log` check comes before a commit, the denial names the
  check and explains how to split the command.
- `defsort --help` is grouped and shorter, and `defsort hook --help` lists the installed agent
  adapters.
- On Windows, human-readable output shows paths with forward slashes.

### Fixed

- On Python 3.15, a dictionary unpacking comprehension such as `{**d for d in items}` no longer
  causes an error.
- Decorated and other eager methods keep their source order, so classes that 0.1.0 skipped with an
  `eager_order` blocker now sort.
- `git add -A` or `git add -u` with pathspecs before a commit no longer makes the hook rewrite
  partially staged files outside those pathspecs.
- The hook no longer denies a plain `cat` or `tee` command that writes a quoted heredoc mentioning
  `git commit` to a file.
- When the hook cannot update the working-tree copy after sorting the index, it says so instead of
  failing silently.

## 0.1.0 (2026-09-29)

Initial release.
