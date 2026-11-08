# Hook reference

Use this reference when a hook command is denied or when you need exact selection, mutation, and
response rules. For installation and everyday use, start with the [agent hook guide](hook.md).

## Synopsis

```text
defsort hook (-a | --agent) NAME [-e | --event NAME]
             [-o ORDER] [--definition-order ORDER] [--method-order ORDER]
             [--evaluation-order MODE]
             [--method-bodies MODE] [--method-visibility MODE]
             [--method-dunders MODE] [--main MODE] [--tie-break MODE]
             [--banners MODE] [--banner-pattern REGEX | --no-banner-pattern]
             [-i | --no-incremental] [--incremental-fallback MODE] [--allow-on-failure]
```

The `hook` subcommand reads one host hook payload as JSON from stdin, translates it through the
agent adapter named by `-a, --agent NAME`, and either sorts for a supported commit or denies an
unsupported one. Agent selection is required; defsort never infers the coding agent from the
payload. `-e, --event` selects the event and is required for a host whose payload carries no event
name (Antigravity) and rejected for every other host. The built-in hosts are `claude`, `codex`, and
`agy`. Two hook events are supported: **PreToolUse** (recommended) and **PostToolUse**
(alternative). Choose one, not both.

By default, the hook uses dependency ordering. Pass `-o`/`--order` with `dependency`,
`alphabetical`, or `none`, or use `--definition-order` and `--method-order` for independent scope
control. The hook accepts every ordering-policy flag from the main CLI, including structural axes
and banner-pattern overrides. Pass `-i`/`--incremental` to sort only definitions added since git
HEAD.

### Host payloads

Claude Code and Codex provide `hook_event_name` and `tool_name`; Bash commands come from
`tool_input.command`. Antigravity takes the event from `--event` and the command from
`toolCall.args.CommandLine` for `run_command`. Edited paths are described under
[PostToolUse results](#posttooluse-results); execution-directory rules are under
[cross-repo commands](#cross-repo-commands).

## Silent skips

The hook exits 0 with no output when:

- JSON on stdin is invalid or not an object.
- The payload describes another event or a tool that is not the shell.
- The command is unrelated: no commit invocation is detected.
- The command is a supported status-only commit (`--dry-run`, `--short`, `--porcelain`, `--long`,
  `--null`, or `-z`) or uses a literal route whose target does not exist. In both cases, the
  accepted command is proven not to reach a commit, so defsort performs no repository discovery.
- The hook is disabled by a valid false Git config value in the target repository for a supported
  commit. Disabling never suppresses the deny outcome: an unsupported command has no trusted route,
  so reading the payload repository's setting would let a disabled outer repository exempt a commit
  aimed at an enabled one.

## Command detection

The hook accepts the literal command forms below. It does not execute shell syntax or predict
computed commands while deciding whether a command is supported.

The grammar accepts this sequence, where `SEP` is `&&`, `;`, or a line feed:

```text
[cd LITERAL &&] [GIT_ADD SEP] GIT_COMMIT
```

- Every word is literal: single quotes, double quotes without `$`, backticks, or backslash escapes,
  and backslash escapes outside quotes all produce literal text. A quoted `;` is a word, never a
  separator. A backslash before a line feed continues the line. A carriage return, a comment, a
  redirection, and a bare heredoc are rejected.
- A routing `cd` must be followed by `&&`, and its operand must be absolute or begin with `./` or
  `../`. Repository routing is uniform: one `cd`, or the same `-C ROUTE` on every git segment,
  never both.
- On Windows Python (`sys.platform == 'win32'`), a git segment is rejected if any literal word
  matches `/<single-letter>` or `/<single-letter>/...`. This conservative exclusion includes commit
  messages, `-C` routes, and add operands, even when quoted. Native Windows spelling such as
  `C:/repo` is outside this exclusion; the other grammar rules still apply. The hook does not
  reproduce Git Bash's native argument conversion for replay.
- Standalone `git status`, `git diff`, and `git log` segments before a real commit are denied,
  whether they precede or follow an optional add: hook staging and sorting would run before those
  observations and could change their results. The denial names the overtaken command, such as
  `read-only git diff before git commit`. Run the check or observation in a separate tool call,
  then submit the add/commit command only after success. For a check after staging, run the add and
  check without a real commit, then submit the commit separately. Standalone read-only commands
  pass through without sorting.
- Status-only commits and routes whose targets do not exist may be preceded by the read-only
  commands listed below; those commands accept no pathspecs or positional arguments.
- At most one `git add [--] PATHSPEC...` or
  `git add (-A | --all | -u | --update) [--] [PATHSPEC...]`.
- Exactly one final `git commit`. Its first word may be `GIT_COMMITTER_DATE=VALUE` with an unquoted
  name, where `VALUE` is literal text in any quoting or the substitution
  `$(git log -1 --format=%aI)` with or without double quotes.
- The `-m` or `--message` value may be the heredoc message idiom, at most once:

```text
git commit -m "$(cat <<'EOF'
subject

body
EOF
)"
```

The outer double quotes and a quoted heredoc delimiter are part of the idiom. The delimiter name is
arbitrary: after quote removal it must exactly match the closing line; `EOF` is only a familiar
example. An unquoted delimiter is unsupported because it could expand shell syntax in the body. An
unquoted `$(cat ...)` is also unsupported because the shell word-splits it.

The substitution must be its own word after `-m`. Attached `-m"$(cat <<'EOF' ...)"` and joined
`--message="$(cat <<'EOF' ...)"` forms are not supported.

### Read-only commands before a noncommitting command

These prefixes are accepted only when the final command is proven not to commit. Before a real
commit, run observations in a separate tool call so hook mutations cannot overtake them.

| Command      | Accepted options                                                                                                                              |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `git status` | `-s`, `-b`, nonempty clusters of `s`/`b` (including repeats), `--short`, `--porcelain`, `--porcelain=v1`, `--branch`, `--untracked-files=all` |
| `git diff`   | `--cached`, `--staged`, `--check`, `--stat`, `--name-only`, `--name-status`                                                                   |
| `git log`    | `--oneline`, `--stat`, `--format=LITERAL`, `-N`, `-n N`                                                                                       |

`--output` and `--no-index` are excluded because a later add could stage their output.

### Supported commit options

Only the options below are accepted. Use their full spelling; abbreviations and unlisted `--no-`
forms are unsupported. Short boolean options may be clustered.

| Purpose                  | Accepted spellings                                                                          |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| Stage tracked changes    | `-a`, `--all`, `--no-all`                                                                   |
| Include paths            | `-i`, `--include`, `--no-include`                                                           |
| Commit only paths        | `-o`, `--only`, `--no-only`                                                                 |
| Path operands            | `--`, positional pathspecs                                                                  |
| Read pathspecs           | `--pathspec-from-file FILE`, `--pathspec-from-file=FILE`, `--pathspec-file-nul`             |
| Message                  | `-m`, `--message`, `-F`, `--file`                                                           |
| Author and date          | `--author`, `--date`                                                                        |
| Reuse a message          | `-c`, `--reedit-message`, `-C`, `--reuse-message`                                           |
| Fixup and squash         | `--fixup` (except values starting with `reword:`), `--squash`                               |
| Message details          | `--trailer`, `--cleanup`, `-t`, `--template`                                                |
| Diff context             | `--unified`, `--inter-hunk-context`                                                         |
| Amend and edit           | `--amend`, `-e`, `--edit`, `--no-edit`, `--reset-author`                                    |
| Signoff                  | `-s`, `--signoff`, `--no-signoff`                                                           |
| Verification             | `-n`, `--no-verify`, `--verify`, `--no-post-rewrite`                                        |
| Verbosity                | `-q`, `--quiet`, `--no-quiet`, `-v`, `--verbose`, `--no-verbose`                            |
| Empty commits/messages   | `--allow-empty`, `--no-allow-empty`, `--allow-empty-message`, `--no-allow-empty-message`    |
| Status display           | `--status`, `--no-status`, `--branch`, `--no-branch`, `--ahead-behind`, `--no-ahead-behind` |
| Signing                  | `-S`, `-SKEY`, `--gpg-sign`, `--gpg-sign=KEY`, `--no-gpg-sign`                              |
| Untracked display        | `-u`, `-uMODE`, `--untracked-files`, `--untracked-files=MODE`, `--no-untracked-files`       |
| Disable interactive mode | `--no-patch`, `--no-interactive`                                                            |
| Status only; no commit   | `--dry-run`, `--short`, `--porcelain`, `--long`, `-z`, `--null`                             |

Message, author/date, message reuse, fixup/squash, message details, and diff-context options take
values. Selection options use the last effective value, subject to these restrictions:

- `--all` cannot accompany a pathspec.
- `--include` and `--only` cannot be combined; either requires a pathspec.
- `--pathspec-from-file` cannot accompany positional pathspecs.
- `--pathspec-file-nul` requires `--pathspec-from-file`.

Interactive forms (`-p`, `--patch`, `--interactive`), help (`-h`, `--help`), and
`--pathspec-from-file=-` are unsupported. Status-only options prove the command will not commit;
their unlisted negations are still unsupported.

### Possible commit text

The detector reports possible commit text when a rejected command contains a Git/commit word pair,
an expansion that could name Git, or a match in a command substitution, heredoc body, shell `-c`
script, `eval` argument, or inline interpreter script. It scans heredoc bodies as shell text and as
quote-stripped tokens unless a documented file-writing form exempts that body. It does not execute
the proposed command, resolve its live PATH, or predict computed subcommands. Recovered syntax and
unsupported forms remain subject to conservative detection. Only bare `git` can be accepted as a
commit command; path-qualified and `.exe` spellings are denied with guidance to use bare `git`.

### Quoted heredoc file writes

A quoted heredoc written by plain `cat` (`> FILE` or `>> FILE`) or `tee FILE` / `tee -a FILE` can
remain data in a chain of documented commands. This includes `<<-`, several writes, ordinary
directory setup, grouping, and inspection of the written file. For example:

```sh
mkdir -p notes && cat >notes/review.md <<'EOF'
See `docs/operations.md` (commit `e2e6dc8`).
EOF
wc -l notes/review.md
```

```sh
cat >"$TMPDIR/notes.md" <<'EOF'
Then run git commit when ready.
EOF
wc -l "$TMPDIR/notes.md"
```

#### Quoting and command chains

In a list of two or more commands, the body is exempt only when every command in the entire list is
a documented form whose arguments meet the rules below. Supported list separators are `&&`, `||`,
`;`, and newline, with comments. Balanced subshell groups, brace groups, `if`/`elif`/`else`, and
`while`/`until` are eligible when their contained commands meet the same rules. The command word
must be a bare literal name. Arguments and redirection targets may combine literal text with a
leading `~` (alone or followed by `/`) and double-quoted environment variables, `"$NAME"` or
`"${NAME}"`, that are followed by `/` or end the argument. `NAME` uses uppercase letters, digits,
and underscores and starts with a letter. These shell-managed names do not qualify:

- the `BASH_`, `ZSH_`, `COMP_`, `READLINE_`, and `TRY_BLOCK_` families;
- `ARGC`, `BASHPID`, `COLUMNS`, `COMPREPLY`, `COPROC`, `DIRSTACK`, `EPOCHREALTIME`, `EPOCHSECONDS`,
  `ERRNO`, `FUNCNAME`, `HISTCMD`, `LINENO`, `LINES`, `MAPFILE`, `MATCH`, `MBEGIN`, `MEND`,
  `OPTARG`, `OPTIND`, `PIPESTATUS`, `RANDOM`, `REPLY`, `SECONDS`, `SRANDOM`, and `TTYIDLE`.

`PWD` and `OLDPWD` qualify.

Options and their values, the `chmod` mode, the `printf` format, and the `grep` pattern must be
literal. A write target must end in a literal file name, except a `cp` or `mv` destination ending
in `/`. Substitutions, globs, braces, arithmetic, unquoted or lowercase variables, and modified
references such as `${NAME:-x}` do not qualify. Assignment prefixes, wrappers, pipes, background
commands, process substitution, and unknown or malformed syntax do not qualify.

A command that is only a `cat` or `tee` heredoc write to a file, with nothing before or after it,
keeps its quoted body exempt even when its options or targets fall outside these rules.

#### Supported file-writing and inspection commands

Only these forms qualify:

| Command               | Eligible form                                                          |
| --------------------- | ---------------------------------------------------------------------- |
| `mkdir`               | `[-p] [-m MODE] DIR...` with octal MODE                                |
| `cd`                  | `[-L\|-P] DIR`                                                         |
| `pwd`                 | `[-L\|-P]`                                                             |
| `true`                | No operands                                                            |
| `echo`                | `[-n] TEXT...`                                                         |
| `printf`              | `FORMAT [ARG...]`; only `%s`, `%%`, literal text, and `\n`             |
| `ls`                  | `[-aAdFhlnprRSt1] [PATH...]`                                           |
| `wc`                  | `[-clmw] [FILE...]`                                                    |
| `head`, `tail`        | `[-n N\|-c N] [FILE...]` with positive decimal N                       |
| `cat`                 | `[-benstuv] [FILE...]`; a heredoc sink also needs a file output target |
| `tee`                 | `[-ai] FILE...`                                                        |
| `touch`               | `[-acm] FILE...`                                                       |
| `cp`                  | `[-fip] SOURCE... DEST`                                                |
| `mv`                  | `[-fi] SOURCE... DEST`                                                 |
| `rm`                  | `[-firR] FILE...`                                                      |
| `chmod`               | `MODE FILE...`; three or four octal digits, `+x`, `u+x`, or `a+x`      |
| `grep`                | `[-EFivnqlc] [-e PATTERN] FILE...` or `[-EFivnqlc] PATTERN FILE...`    |
| `diff`                | `[-u] FILE FILE`                                                       |
| `sort`                | `[-nru] [FILE...]`                                                     |
| `basename`            | `PATH [SUFFIX]`                                                        |
| `dirname`, `realpath` | `PATH`                                                                 |

Options precede operands; unlisted options are ineligible. The `printf` format may not start with
`-`, and initial `echo` words that look like unsupported options are ineligible. `--` ends options
for supported external utilities except `chmod`, `basename`, `dirname` and `realpath`, which reject
it, and never for `echo` or `printf`. For an operand of those four commands that starts with `-`,
write `./` before it. File redirections `<`, `>`, and `>>` may have an adjacent numeric descriptor.
A chained heredoc sink needs a quoted delimiter, a standard-input heredoc, and a real file output;
additional literal `<` redirections are allowed. Descriptor duplication and unquoted delimiters do
not qualify. The hook compares all write-target basenames with every command name in the full list,
including commands before the write. A text-visible write to a command-name file with the same
basename keeps the body scanned. New link creation is ineligible.

> The hook checks command text; it does not inspect the shell environment. Aliases, shell
> functions, hook functions, traps, prompt hooks, what PATH finds, environment variable values, and
> existing file links are outside the guarantee.

Command-text effects remain in scope: writing, copying, or moving content that a later command
reads or runs, writing configuration that a command reads, and replacing a command-name file.
Commands such as `make`, `npm run`, an interpreter, and bare `git add` can consume written text as
instructions. Their presence with a write prevents the body exemption. The same applies to
unrecognized neighboring commands; the reason reports only that their behavior is unknown. The hook
does not perform general execution analysis.

For example, this command writes a Makefile recipe and then asks `make` to read it:

```sh
cat >Makefile <<'EOF'
all:
	git commit -m x
EOF
make
```

Unlike the `wc -l` inspection above, `make` can run the written recipe. The hook scans the quoted
body and denies the command with this reason:

```text
defsort: possible commit text: heredoc body line 2 '\x09git commit -m x'; scanned because 'make' can run the written file; to commit, use the supported form: [cd DIR &&] [git add ...;] git commit ...
```

Denials distinguish a possible text match from an identified outer `git commit`. The first possible
match in the source determines the reported body line and excerpt. The reason names the actual
blocker and gives a remedy for that cause. These are exact examples:

```text
defsort: possible commit text: heredoc body line 1 'See `docs/operations.md` (commit `e2e6dc8`).'; scanned because 'npx prettier --write notes/review.md' is not a recognized command; a quoted heredoc body skips scanning only when every command in the chain is a documented form
defsort: possible commit text: heredoc body line 2 'git commit -m x'; scanned because 'sh s.sh' can run the written file; to commit, use the supported form: [cd DIR &&] [git add ...;] git commit ...
defsort: possible commit text: heredoc body line 3 (git on line 1) 'fixed in commit e2e6dc8'; scanned because the heredoc delimiter is unquoted; quote it (<<'EOF') if the body is data
```

The second reason applies to a quoted `s.sh` body with a preceding comment line and then `sh s.sh`.
The third applies to `cat > notes <<EOF`, followed by body lines `git`, `context`, and
`fixed in commit e2e6dc8`. An outer commit such as `git commit -m x && git push` keeps the existing
`defsort: unsupported commit command` reason and its separate-call guidance.

#### Receipts and denied examples

When an accepted commit's staged-index sort rewrites at least one index entry, the allow response
carries the reason `defsort: sorted N staged Python file for this commit` (or `files` for more than
one), which the host shows as hook feedback. Working-tree sorting paths and accepted commits that
rewrite nothing stay silent when they have no notes.

For an accepted `git add ... && git commit ...` command, the hook runs the add before checking the
staged content. If defsort then blocks the commit, any files staged by that add remain staged, just
as they would after a failed commit in the shell. The denial explains this; the hook does not undo
the staging.

Detected commands outside the grammar are denied. Examples:

```text
make check && git add -A && git commit -m "message"
git init && git add -A && git commit -m "message"
git add first.py && git add second.py && git commit -m "message"
git add *.py && git commit -m "message"
FOO=1 git commit -m "message"
git --no-pager commit -m "message"
git commit -m "message" && git push
git commit -m "message" # note
git commit --dry-run --no-dry-run
sh -c 'git commit -m "message"'
```

A command with no possible commit match is unrelated and allowed. Text such as `echo git commit`
may be reported as possible commit text; that report does not claim the command executes a commit.
A wrapper script whose outer command contains no commit text is outside the guarantee.

### Cross-repo commands

When an agent session starts in one repository but the commit should run in another, put the target
repository in simple command text. The hook recognizes these command-visible forms:

```text
git -C <repo> commit ...
cd <repo> && git commit ...
git -C <repo> add ... && git -C <repo> commit ...
```

`git -C <repo> commit ...` is the clearest form for agent-driven cross-repo commits because the
target repository is visible in the command text.

On Windows Python, use native spelling such as `git -C C:/repo commit ...`; an MSYS spelling such
as `git -C /c/repo commit ...` is denied by the git-segment exclusion above. This exclusion also
applies to literal message words and add operands, not just routes.

For the `cd` form, `<repo>` must be a literal path that is absolute or begins with `./` or `../`,
and the `cd` must be connected to the commit by `&&`. Mixing `cd` and `git -C`, using cumulative
`-C` options, spelling the route differently between segments, or routing through a variable,
`cd -`, `;`, `||`, or a subshell makes the complete command unsupported, and it is denied.

A bare `git commit` is accepted only when the adapter establishes the command's execution
directory. Claude Code reports that directory as the payload `cwd`; Antigravity reports it as the
`Cwd` argument of `run_command`. When `Cwd` is absent or empty, Antigravity uses the first
`workspacePaths` entry if it is a non-empty absolute path. Later entries are never searched, and an
explicit relative or invalid `Cwd` does not use that fallback. Codex reports only the session
directory, which can differ from where a shell call with its own `workdir` executes, so the Codex
adapter reports the directory as unknown. With an unknown directory, a bare commit or a relative
route is denied before any repository access. For a bare commit, the reason is
`defsort: unsupported commit command: unknown working directory; supported form: git -C /absolute/repo commit ... or cd /absolute/repo && git commit ...`.
Retry with the absolute route; defsort never substitutes the session, process, or repository
directory and never chooses a repository for you.

### Compound bootstrap commands

PreToolUse runs once before the complete shell command. It cannot let `git init` and `git add` run
and then inspect the later `git commit` inside the same tool call. A compound bootstrap such as
`git init && git add -A && git commit` contains a commit invocation outside the grammar and is
denied before repository discovery, configuration loading, add replay, or index access.

Run `git init`, `git add`, and `git commit` as separate tool calls so the final standalone commit
reaches PreToolUse after the repository and index exist.

This rule affects only PreToolUse. PostToolUse continues to process files after supported edit
tools change them.

### Targets outside a worktree

After accepting a commit command, defsort checks the command's resolved target directory. A
confirmed non-worktree target allows with exit 0 and no output. Git therefore owns the error for a
bare `git commit` outside a repository.

A literal route whose target does not exist was already proven noncommitting during grammar
resolution and allowed silently before this check. Git execution failures, unreadable repository
metadata, and other errors while discovering an otherwise viable target remain infrastructure
failures. The normal `allow-on-failure` policy applies to those errors.

## Commit modes

The hook detects which git commit mode is in use and dispatches to the appropriate sort path:

| Mode               | Flags                                | Sort path                                           |
| ------------------ | ------------------------------------ | --------------------------------------------------- |
| Normal             | (no mode flags)                      | Index                                               |
| Auto-stage         | `-a`, `--all`                        | Working tree                                        |
| Include            | `-i`, `--include`                    | Working tree (pathspecs) + index (remaining staged) |
| Only / pathspec    | `-o`, `--only`, or bare `<pathspec>` | Working tree                                        |
| Interactive        | `-p`, `--patch`, `--interactive`     | Unsupported (denied)                                |
| Pathspec from file | `--pathspec-from-file=<file>`        | Working tree                                        |

For auto-stage and pathspec modes, the hook sorts files on the working tree so git stages the
sorted content naturally. The `--pathspec-from-file=-` (stdin) case is unsupported and denied
because the hook's stdin is consumed by the JSON payload.

### Pathspec files

Pathspec files must be regular files; symlinks to regular files are accepted. Directories, FIFOs,
sockets, and devices are denied with `defsort: pathspec resource must be a regular file`, including
with `--allow-on-failure`. The hook reads and parses the file once, after enablement and
configuration resolution and before replaying any preceding `git add` or changing any file. A
disabled hook does not open pathspec resources for supported commands.

Newline-delimited records accept CRLF endings and Git's C-style quoting. Bytes after a closing
quote are ignored. A raw NUL ends a newline record, and a decoded NUL ends a quoted name.
`--pathspec-file-nul` instead preserves each record literally, including quotes and newlines. An
empty file supplies no pathspecs, so ordinary commits still process staged files. Empty records and
malformed quoted records fail before any add replay or write; a final delimiter does not create an
extra record. `--pathspec-from-file=-` remains unsupported.

Read failures use `defsort: cannot read pathspec file <name>: <reason>`. Parsing failures use
`defsort: cannot use pathspec file <name>: <detail>`, with `empty pathspec record` or
`malformed quoted pathspec record`. Git selection failures use
`defsort: cannot use pathspecs: <detail>`. These failures follow the normal failure policy, and
filenames and details are escaped for terminal display.

### File discovery

The hook selects Python files from the index or working tree according to the commit mode. Deleted
files, symlinks, and other non-regular Git entries are skipped. Renames paired by Git keep the
source file's baseline; files Git does not pair are new.

Every commit mode skips Python files whose `private` git attribute is set, in the index and on the
working tree, wherever the commit runs from. A commit that stages only private Python files is
allowed without output. If Git cannot report the labels, the hook denies with
`defsort: failed to read private labels: <reason>`, or warns and allows under `--allow-on-failure`.

### Index path

For a normal commit, the hook sorts staged content in memory, checks the whole batch before
writing, and updates the index atomically. Executable bits are preserved. The working-tree copy is
synchronized separately under the rules below.

### Working tree path

For auto-stage, pathspec-only, and include modes, the hook reads file contents from the working
tree, sorts in memory, and writes the result back to the working tree. Git then stages the modified
content as part of the commit.

If a write fails, the hook writes back the files it already changed and denies the commit. The
denial names every file it could not restore, such as one edited again after the hook wrote it.
Several names of one file whose results differ deny the commit before any file is written; a name
that cannot be read counts as differing. With `--allow-on-failure`, alias conflicts warn and leave
the affected file untouched; write failures warn and continue, retaining earlier successful writes.
The writer restores the original bytes when possible; if restoration fails, the error names the
recovery file or says recovery could not be saved.

### Working tree sync

After updating the index for a normal commit, the hook writes a sorted working-tree copy only when
the working-tree text, with CRLF and CR line endings normalized to LF, equals the staged text that
was sorted. When the texts are exactly equal, the working tree receives the sorted staged text.
When only line endings differ, it receives the sort of its own text, which keeps its line endings.
Any other difference leaves the working tree untouched, preserving partial staging.

Preceding `git add` commands are replayed so the hook can inspect the content the commit would use,
but neither the replay nor Git's path selection authorizes a working-tree write. An ordinary
unfiltered file added immediately before a commit normally satisfies the equality check and is
synchronized.

Git attributes can make the staged and working-tree representations differ even without a user
edit. For example, an `ident` attribute stores `$Id$` in the index while expanding it in the
working tree. defsort still sorts the index entry, but the representation difference prevents a
working-tree write. When the original compound command later runs its real `git add`, Git may
replace defsort's sorted index entry with the clean-side form of the unchanged, unsorted
working-tree file, leaving that file unsorted for the commit. This safe no-op is preferred to
overwriting a representation defsort cannot prove equivalent.

When several staged names refer to one working-tree file, such as hard links, defsort synchronizes
the file only when every name passes the equality check and all names need the same bytes. Each
name's index entry is still sorted when the working tree cannot be updated.

After the index update succeeds and all synchronization writes have been attempted, each file that
could not be updated gets one line on stderr, in staging and group order:

```text
defsort: did not update <names> in the working tree: <reason>; the commit has the sorted version
```

The reason is one of:

- `it has unstaged changes`: the copy differs from the staged content, is missing, cannot be
  decoded with the staged encoding, or is now a symbolic link. For a file with multiple names, the
  reason identifies the affected names: `<name> has unstaged changes` or
  `<names> have unstaged changes`.
- `names of one file need different results`: aliases require different bytes, or a staged name
  could not be processed.
- `cannot read it: <error>`: another failure while reading the working copy.
- `cannot sort it: <error>`: re-sorting a copy that differs only in line endings failed. A re-sort
  that skips or blocks remains silent.
- `cannot write it: <error>`: any failure during synchronization, including a concurrent edit or a
  failed restore. If restoration fails or closing leaves the outcome uncertain, the reason names
  the recovery file holding the original bytes or says recovery could not be saved. The file may
  retain partial content.

Nothing in working-tree synchronization denies the commit. The receipt still reports the updated
index. If the index update fails, no synchronization note claims the commit has the new version.

## Banner constraints

In `barriers` mode, the hook reports `defsort: <path>: sort constrained by <n> banner(s)` on stderr
whenever a banner prevents the unconstrained target, including no-ops and blocked or guarded
outcomes. Other modes and unconstrained files stay silent.

## Blocked sort

A blocked class never denies a commit. For code between methods or duplicate method names, the hook
leaves that class unchanged, sorts the rest of the file when possible, and reports each blocked
class as `defsort: <path>: class <name>: skipped (<label>)`. The labels are `code between methods`
and `duplicate method names`. If nothing else changes, the file stays unchanged. Claude Code and
Codex receive these notes through `systemMessage` in every commit mode.

If any staged file needs sorting but a safety check fails, the hook emits deny JSON on stdout with
a reason. The host receives the denial. For Claude Code and Codex, the response has this shape:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "ERROR: <path> has code between definitions. Move constants/type aliases to preamble to enable sorting."
  }
}
```

For a full-sort candidate, safety check priority is interstitial code > eager order > forward
annotation refs > non-consecutive duplicates. In incremental mode, a structural or policy blocker
that prevents an actual candidate remains authoritative; once actual top-level and method
candidates exist, eager order precedes forward annotation refs across both scopes.

With `evaluation-order = preserve`, eager top-level and method groups retain their relative source
order after ordinary ordering. `allow-reordering` removes only that pin. `eager_order` still blocks
a candidate that changes the binding seen by a covered decorator, default value, base class, class
keyword, or class-suite read. A future import cannot repair it.

A `layout` blocker means a requested reorder cannot preserve source layout and comment ownership.
It refuses the whole file, discarding any earlier in-memory scope changes and preserving the
original bytes. Separate attached comments from section banners with blank lines before sorting.
Incremental fallback and `allow-on-failure` cannot bypass this blocker.

PreToolUse checks each ordinary mutation batch before its first source or index write. Include mode
uses two mutation batches: it prepares the remaining staged files, checks and writes the included
worktree files, then analyzes and writes the remaining staged batch. A blocker discovered in that
last analysis denies the commit after included worktree files may have changed; those worktree
changes remain. Each batch retains its own rollback and partial-staging protections, but include
mode is not atomic across both batches.

## Infrastructure failures

On Git older than 2.42, incremental mode and the reorder guard on the command line, after an
agent's edit, and in commits that sort working-tree files report a version error. Sorting staged
files at commit time works with older Git.

Failure messages name the operation and its cause on one line. Git failures include the first
nonblank stderr line, or the exit status if stderr is empty. Launch failures name the operating
system reason; unusable Git output names what was wrong. File failures include the operating system
or decoding reason. Unexpected exceptions include their type and first message line. Control
characters are escaped. Commit working-tree sync notes, PostToolUse skip notes, and their
discovery/configuration skip lines use repository-relative paths when a worktree is known. Other
PostToolUse baseline, guard, and blocker diagnostics retain their absolute lexical paths. Recovery
messages retain the recovery file and restoration details.

`--allow-on-failure` uses the same message after `WARNING: `. PostToolUse reports read failures as
skips with a cause; failed baseline batches leave affected files unchanged with the Git failure
reason.

By default, infrastructure failures (can't read staged content or a required baseline, can't create
blobs, can't update index) deny the commit. Pass `--allow-on-failure` to warn on stderr and return
without a defsort denial. In PreToolUse, a failed baseline read skips that file under
`--allow-on-failure` and continues sorting the remaining files. A missing baseline is a failure
when the staged record names an existing old blob; a new file can still receive a full sort. An
undecodable baseline, including an invalid encoding cookie, skips the file with a
`baseline cannot be decoded: <cause>` note before the reorder guard is considered. PostToolUse
baseline behavior is unchanged.

Safety blockers (interstitial code, eager order, forward refs, duplicates, layout) always deny
regardless of this flag.

If Git is older than 2.42, defsort reports the detected version. PreToolUse denies by default or
warns and allows with `allow-on-failure`; PostToolUse warns, leaves the file unchanged, and exits
successfully.

### Syntax errors

Staged files with Python syntax errors are silently skipped. They do not block the commit.

### No staged `.py` files (exit 0, no output)

If no staged `.py` files are found, or all are already in order, the hook exits 0 with no output. A
successful staged-index rewrite instead includes a receipt naming the number of rewritten files in
the allow response; see [host responses](#host-responses).

## PostToolUse results

The PostToolUse hook fires after each Claude Code Edit/Write tool use, Codex `apply_patch` file
edit, or Antigravity `write_to_file`/`replace_file_content` or `write_file`/`edit_file` edit. The
host adapter supplies the edited paths: Claude Code payloads provide one `tool_input.file_path`,
Antigravity payloads provide one `TargetFile` for `write_to_file`/`replace_file_content` or `Path`
for `write_file`/`edit_file`, and Codex payloads provide an `apply_patch` command from which the
adapter extracts each touched path.

File-processing failures use `defsort: PostToolUse skipped <path>: <reason>` with these reasons:

| Failure                                     | Reason                                                                                |
| ------------------------------------------- | ------------------------------------------------------------------------------------- |
| Read or decode failure                      | `cannot read it: <error>`                                                             |
| Python syntax error                         | `syntax error: <message>`, followed by ` (line <n>)` only when Python supplies a line |
| File changed before writing                 | `changed since it was read; not written`                                              |
| File deleted before writing                 | `no longer exists; not written`                                                       |
| Write or restoration failure                | `cannot write it: <error>`                                                            |
| Names of one file require different results | `names of one file need different results`                                            |
| Other transform or write failure            | `unexpected error: <Type: message>`                                                   |

Paths are repository-relative when a worktree is known; outside a worktree they retain the
payload's spelling. Discovery and hook-setting skip lines use the same path rule. Notes follow
payload order, including when an earlier file fails to write and a later file fails to read. Names
of one physical file are listed together in payload order for an alias conflict. Unexpected
failures put their traceback immediately after the note on stderr.

Syntax errors become the hook's reason. Other notes remain on stderr. Claude Code and Codex receive
the notes followed by the syntax-error reasons, without duplicate lines, in
`hookSpecificOutput.additionalContext`. Antigravity emits empty stdout and sends notes and
syntax-error reasons to stderr only. Every rendered PostToolUse response exits 0; invocations
without notes or reasons remain silent.

### Skip conditions (PostToolUse)

The hook exits 0 with no output when:

- Hook is disabled by a valid false Git config value.
- JSON on stdin is invalid or not an object.
- The payload describes another event or a tool the host adapter does not treat as a file writer.
- No touched path ends in `.py`.
- File does not exist on disk.

If the hook setting cannot be read, the file is skipped with a diagnostic that names the key and
the cause. PostToolUse still exits 0.

When incremental mode or a reorder guard needs Git and a baseline query fails, the file is left
unchanged. Stderr names the repository-relative file and Git's reason:

```text
defsort: PostToolUse skipped NAME: incremental mode cannot use Git: DETAIL
defsort: PostToolUse skipped NAME: the reorder guard cannot use Git: DETAIL
```

When both modes apply, the reason starts `incremental mode and the reorder guard cannot use Git`. A
file outside a repository is left unchanged when either mode applies, with the payload path in
`defsort: PostToolUse skipped NAME: not in a Git repository`. Ordinary full sorting outside a
repository still works. All these PostToolUse skips exit 0.

### Syntax errors (exit 0, JSON warning)

If the file has a Python syntax error, Claude Code and Codex receive the diagnostic in
`hookSpecificOutput.additionalContext`, with exit 0:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "defsort: PostToolUse skipped <path>: syntax error: <message> (line <n>)"
  }
}
```

The line is omitted when Python reports none, as for source that nests too deeply.

Antigravity emits empty stdout and sends syntax-error feedback to stderr only.

### Successful sort (exit 0, no output)

If the file needs sorting and all safety checks pass, the hook writes the sorted source directly to
the file and exits 0. If the write fails, defsort tries to restore the original bytes and notes the
failure on stderr. When restoration fails, the note names the recovery file or says recovery could
not be saved. A file that changed after defsort read it, such as one edited again in the meantime,
is skipped with a note on stderr. When one event reports several names of one file whose results
differ, or one that cannot be processed, defsort skips the file and says so. If Git cannot
determine a file's repository, the hook skips the file and prints
`defsort: PostToolUse skipped NAME: failed to discover git work tree`, because without Git it
cannot read the repository's `defsort.hook` setting. The line repeats on each edit of the file
until Git works again.

### Blocked sort (exit 0, diagnostic context and stderr)

A class blocked by code between methods or duplicate method names stays unchanged while the hook
sorts the rest of the file when possible. Each blocked class is reported as
`defsort: <path>: class <name>: skipped (<label>)` on stderr and, for Claude Code and Codex, in
`additionalContext`. If nothing else changes, the file stays unchanged.

If the file needs sorting but a safety check fails, the hook preserves the source, prints an error
to stderr, and exits 0. Claude Code and Codex also receive the diagnostic through
`hookSpecificOutput.additionalContext` so the agent can address it:

- **Interstitial code**:
  `ERROR: <path> has code between definitions. Move constants/type aliases to preamble to enable sorting.`
- **Eager order**:
  `ERROR: <path> would move a definition-time reference before the definition it uses. Restructure that reference or choose an order that keeps the referenced definition before the reader.`
- **Forward annotation refs**: "ERROR: <path> needs sort with forward annotation references. Add
  `from __future__ import annotations` to enable auto-sort."
- **Layout preservation**:
  `ERROR: <path>: requested reorder cannot preserve source layout and comment ownership. Separate attached comments from section banners with blank lines before sorting.`
  The whole original file is preserved, including when another scope sorted successfully in memory.
- **Non-consecutive duplicates**:
  `ERROR: <path> has non-consecutive definitions with the same name. Consolidate or rename them to enable auto-sort.`

These examples show current display text; classify blockers by their values rather than parsing the
diagnostic wording.

The blocker is reported before the file write. Full-sort checks use this priority: interstitial >
eager order > forward refs > duplicates. Incremental structural blockers remain authoritative;
across actual candidates, eager order precedes forward refs.

### No changes needed (exit 0, no output)

If the file is already in order, the hook exits 0 silently.

## Host responses

The hook classifies commands before repository access: unrelated commands pass silently, supported
commits format their selected content, and detected unsupported commits are denied.
Unsupported-command denial happens before hook enablement and `allow-on-failure`; neither setting
can exempt one. An accepted message is not scanned for commit text.

Unrelated commands and no-ops without notes exit 0 without stdout. A staged-index rewrite emits a
`systemMessage` receipt with the number of sorted files under Claude Code and Codex. Antigravity
receives `{"decision": "allow", "reason": "<receipt>"}`; its protocol requires a decision whenever
a receipt is emitted. Claude Code and Codex also show PreToolUse warning and informational notes to
the user in `systemMessage`; the agent does not see them. Antigravity receives those notes only on
stderr. The native Antigravity `allow` decision can authorize the tool call without a prompt. A
sequencer has its own response contract; see [composition](hook.md#composing-with-other-hooks).

| Outcome                                  | Claude Code and Codex                                              | Antigravity                                                                  |
| ---------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| Unrelated or no-op without notes         | Exit 0, silent                                                     | Exit 0, silent                                                               |
| Working-tree rewrite without notes       | Exit 0, silent                                                     | Exit 0, silent                                                               |
| Staged-index rewrite                     | `systemMessage`, exit 0                                            | `allow` decision and receipt in `reason`, exit 0                             |
| Incremental-policy or reorder-guard skip | `systemMessage` to user; info on stderr, exit 0                    | Info on stderr, exit 0                                                       |
| Hard file-level safety blocker           | Deny JSON, reason on stderr, exit 0                                | Top-level deny JSON, reason on stderr, exit 0                                |
| Unsupported commit                       | Deny JSON on stdout, reason on stderr, exit 0                      | Top-level deny JSON, reason on stderr, exit 0                                |
| Failed `git add` replay (default)        | Deny JSON; Git's first error, exit status, or launch cause, exit 0 | Top-level deny JSON; Git's first error, exit status, or launch cause, exit 0 |
| Failure, fail-closed                     | Deny JSON on stdout, reason on stderr, exit 0                      | Top-level deny JSON, reason on stderr, exit 0                                |
| Failure, `allow-on-failure`              | `systemMessage` to user; warning on stderr, exit 0                 | Warning on stderr, exit 0                                                    |
| PostToolUse diagnostic                   | `additionalContext`, exit 0; notes also on stderr                  | Empty stdout; diagnostics on stderr, exit 0                                  |
| Last resort (no functioning adapter)     | One line on stderr, exit 2: blocks                                 | One line on stderr, exit 2: does not block                                   |
| Standard output closed at start          | One line on stderr, exit 2: blocks                                 | One line on stderr, exit 2: does not block                                   |

### Mutation and permission timing

Opting out with `git config defsort.hook false` disables sorting and its mutations for supported
commits in that repository. It does not suppress the deny outcome for unsupported commands, which
are denied before any repository is consulted.

A successful hook rewrite can remain in the index or working tree if the provider later rejects the
commit. defsort cannot observe that later permission decision. For compound commands such as
`git add file.py && git commit`, the hook replays the parsed `git add` before sorting. Replayed
content therefore remains staged if either the provider or defsort's own later safety decision
denies the commit. If the replay fails, defsort stops before sorting and denies with Git's first
error line, or `git exited with status N` when stderr has no nonblank line (or the launch failure's
cause). With `--allow-on-failure`, it warns and allows without sorting. Any content staged before
the failure remains staged. A broad replay such as `git add -A` or `git add .` can also stage
unrelated non-Python files before that decision. Use separate `git add` and `git commit` tool calls
when clean failure behavior matters.

A compound command whose prefix segment is outside the grammar is denied, whatever commit mode
follows it. For example, `python generate.py && git commit -am test` and
`python generate.py && git commit -m test -- generated.py` are denied because the prefix could
change the working tree between sorting and staging, and `make check && git commit -m test` is
denied for the same reason even though a normal commit consumes the existing staged snapshot. Run
generation and commit as separate tool calls.

## Exit codes

| Code | Meaning                                                             |
| ---- | ------------------------------------------------------------------- |
| 0    | Every rendered response: allow, deny, warning, PostToolUse          |
| 2    | Hook usage error or last resort; stderr only, no host response JSON |

A deny is carried by the host's JSON on stdout, not by the exit status. Exit 2 blocks on Claude
Code and Codex, which document exit 2 as blocking; Antigravity has no exit-code tier, so exit 2
does not block there. When defsort runs under a wrapper, the wrapper's own host adapter decides
what a child's nonzero exit means; that protection is the wrapper's contract, not defsort's.

Hook usage errors, such as a missing `--agent`, an unknown flag, or an invalid explicit
`--banner-pattern`, print argparse usage on stderr and exit 2 before loading an adapter. Check the
registered hook command. The last resort also exits 2 with no host response JSON when:

- stdout is already closed at start;
- the selected adapter is unknown, registered twice, fails to import, or reports a different
  `defsort.hooks.API_VERSION`;
- `--event` is missing when required or supplied when unsupported;
- the adapter raises while parsing or supplies an invalid event, command, `command_cwd`, or
  post-tool path data; or
- both `render` and `render_failure` raise.

The last resort writes one diagnostic line to stderr. When stdout is already closed, that line is
`error: standard output is unavailable`; defsort exits before reading the payload or loading an
adapter. Nothing is sorted, and `--allow-on-failure` does not apply.

## Differences from the CLI

| Capability                 | CLI                               | Agent hooks                                              |
| -------------------------- | --------------------------------- | -------------------------------------------------------- |
| Ordering and banner policy | All policy flags                  | Same policy flags                                        |
| Incremental mode           | Off by default                    | Off by default                                           |
| Hoisting and force bypass  | `--hoist`, `--force`              | Not supported                                            |
| Future import              | May add or remove                 | Never adds; may remove under cleanup rules               |
| Writes                     | Requires `--write`                | Automatic; destination follows commit mode or edit event |
| Output                     | Summary, preview, diff, JSON      | Host responses and diagnostics                           |
| Selection                  | Explicit files or directory scans | Commit selection or touched edit paths; no recursion     |

Configuration comes from `[tool.defsort]`. Both surfaces preserve source encoding and follow the
[file newline policy](platform.md#source-encoding-and-line-endings). PreToolUse may deny;
PostToolUse reports diagnostics with exit 0. See [exit codes](#exit-codes).
