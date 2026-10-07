# Changelog

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
