# Agent hook guide

Use an agent hook to keep Python definitions ordered as you work. PreToolUse is recommended: it
sorts the content a supported commit will consume and preserves partial staging for normal staged
commits. PostToolUse sorts after edits, which makes changes visible sooner but can require the
agent to reread files.

Start with [configuration](#configuration) to choose a host and installation method. Then read
[supported commands](#command-detection), [failure behavior](#blocked-sort), and
[composition](#composing-with-other-hooks) if you use other formatters. The
[pre-commit integration](#pre-commit) runs independently of coding-agent hooks.

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

## Configuration

Choose one delivery mechanism and one hook mode.

Delivery mechanisms:

- **Plugin**: install the standalone `PreToolUse` hook package from this repository.
- **Manual config**: add the JSON below to the agent's hook config file.

Do not configure the same hook through both a plugin and manual JSON. If both sources register it,
the hook can run twice.

Hook modes:

- **PreToolUse (recommended)** intercepts `git commit` before execution. It reads staged content
  from the git index, sorts in memory, and writes back to the index. It then synchronizes clean
  working-tree copies while leaving divergent copies untouched, which preserves partial staging.
- **PostToolUse (alternative)** sorts each touched `.py` file immediately after file edits. The
  agent sees sorted code mid-turn, which enforces ordering at every step but forces re-reads and
  uses extra context.

**Do not enable both modes.** PostToolUse would modify files mid-turn, and PreToolUse would
redundantly re-process them at commit time.

### Plugin packages

Use this checkout as the marketplace source. The native plugin directories register host hook
configuration, so install the defsort distribution first and confirm that `defsort` is on `PATH`.
That one distribution includes every bundled adapter runtime; no host-specific Python package or
second adapter installation is required.

Install the Claude Code plugin from a defsort checkout:

```bash
claude plugin marketplace add .
claude plugin install defsort@defsort
```

Install the Codex plugin from a defsort checkout:

```bash
codex plugin marketplace add .
codex plugin add defsort@defsort
```

Install the Antigravity plugin from a defsort checkout:

```bash
agy plugin install ./plugins/agy
```

After installation, restart the host and review or trust the registered hook. In Codex, run
`/hooks` and confirm that `PreToolUse` is active. Do not retain a manual `defsort hook`
registration alongside the plugin; duplicate registration runs the hook twice.

The standalone plugins register only `PreToolUse`. If another hook modifies the same files or tool
input, follow the [composition guide](#composing-with-other-hooks) before registering both.

### Claude Code manual settings

Use `~/.claude/settings.json` when a plugin is not registering the hook.

**PreToolUse (recommended)** -- intercepts `git commit` commands before execution. Reads staged
content from the git index, sorts in memory, and writes back to the index. It then synchronizes
clean working-tree copies while leaving divergent copies untouched. Claude works uninterrupted and
partial staging is preserved.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent claude"
          }
        ]
      }
    ]
  }
}
```

**PostToolUse (alternative)**:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent claude"
          }
        ]
      }
    ]
  }
}
```

After changing settings, restart Claude Code and approve the hook if prompted.

For alphabetical ordering, change the command to `defsort hook --agent claude -o alphabetical`. For
independent control, use `--definition-order` and `--method-order` (e.g.,
`defsort hook --agent claude --definition-order alphabetical --method-order none`). Use `--banners`
in the same command when the hook needs a mode other than the default `barriers`.

### Codex manual settings

Use `~/.codex/hooks.json` or the hooks table in Codex `config.toml` when a plugin is not
registering the hook.

**PreToolUse (recommended)** -- intercepts `git commit` commands before execution. This uses the
same commit-time behavior as the Claude Code setup.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^Bash$",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent codex"
          }
        ]
      }
    ]
  }
}
```

**PostToolUse (alternative)** -- sorts `.py` files touched by Codex file edits. Codex reports file
edits with canonical `tool_name: "apply_patch"`.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "^apply_patch$",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent codex"
          }
        ]
      }
    ]
  }
}
```

After changing Codex hook config or enabling a hook plugin, restart Codex and run `/hooks`. Review
or trust the hook, then confirm the event shows `Active 1`. `Installed 1` with `Active 0` means
Codex discovered the hook but will not run it.

### Antigravity CLI manual settings

Manual Antigravity support is verified with installed `agy` 1.1.22. Use
`~/.gemini/config/hooks.json` when configuring hooks manually instead of through the plugin.

**PreToolUse (recommended)** -- intercepts `run_command` tool calls before execution. This uses the
same commit-time behavior as the Claude Code and Codex setups, but Antigravity denials are reported
as top-level `{"decision": "deny", "reason": "..."}` JSON with exit code 0.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "run_command",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent agy --event PreToolUse"
          }
        ]
      }
    ]
  }
}
```

**PostToolUse (alternative)** -- sorts `.py` files touched by Antigravity file edits. The supported
edit tools are `write_to_file` and `replace_file_content`.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "write_to_file|replace_file_content",
        "hooks": [
          {
            "type": "command",
            "command": "defsort hook --agent agy --event PostToolUse"
          }
        ]
      }
    ]
  }
}
```

Antigravity PostToolUse should be treated as edit-time normalization and failure reporting, not as
a dependable block, until local verification shows `decision: "deny"` blocks or surfaces reliably.

### Per-repo enable/disable

The hook checks the `defsort.hook` git config key. If the key is missing, the hook defaults to
enabled.

Values follow Git boolean semantics: true/yes/on/1 to enable, false/no/off/0 to disable.

To disable the hook for a single repository:

```text
git config defsort.hook false
```

To disable the hook globally and selectively re-enable it in one repository:

```text
git config --global defsort.hook false
git config defsort.hook true  # re-enable in this repo
```

### `[tool.defsort]` config

The hook reads `[tool.defsort]` from `pyproject.toml`, discovered by walking up from each processed
file and stopping at the git repository root, so nested projects govern their own files. Two
settings resolve once per invocation from the repository root: `incremental`, because one hook run
covers one repository, and `allow-on-failure`, because it governs the single allow-or-deny response
to the calling agent. CLI flags in the hook command override configuration values.

For example, disable method sorting while retaining the default top-level dependency order:

```toml
[tool.defsort]
method-order = "none"
```

Ordering flags, banner controls, and their defaults match the
[CLI ordering policy](cli.md#ordering-policy). Every matching hook flag overrides configuration.
`--order` selects both scope strategies; scoped flags override their own scopes and other policy
settings remain in force.

Use [banner handling](cli.md#banner-handling) for custom patterns and the destructive `strip` mode.
The hook reports invalid configured patterns and falls back to built-in recognition; an invalid
explicit pattern is a usage error. Non-default settings that cannot affect the selected strategy
produce the same [ineffective-policy warnings](cli.md#warnings) as the CLI.

Set `incremental = true` to sort only new definitions when the committed baseline conforms. See the
[incremental guide](incremental.md) for adoption, fallback, and reorder-guard choices.

### Incremental mode (`-i`)

Pass `-i` (or `--incremental`) to enable incremental mode. This limits sorting to new definitions
-- those not present in git HEAD. Existing definitions are left in their current positions.
Incremental mode is opt-in; without it, the hook fully sorts every file.

The hook reads incremental HEAD baselines with `git cat-file --batch -Z`. Every hook path that
performs a batch read requires Git 2.42 or newer.

Incremental PreToolUse commit processing detects and skips staged renames instead of applying a
full sort. PostToolUse does not detect renames; when the edited path has no `HEAD` baseline, it
treats the path as new and full-sorts it.

See the [incremental guide](incremental.md) for the full range of settings, fallback behavior, and
the large-reorder guard.

In `barriers` mode, the hook writes `defsort: <path>: sort constrained by <n> banner(s)` to stderr
when a banner prevents the unconstrained target order. Constraint-only no-ops and blocker or guard
outcomes still report the constraint. Other banner modes and unconstrained files stay silent.

### Future-import mutation

Full and incremental hook sorting never add `from __future__ import annotations`. They preserve the
import in every annotated module and remove it only when the module has no annotated assignment and
no function or async-function parameter or return annotation at any depth. A covered annotation
hazard without an existing import is reported as `FORWARD_REFS` before any write.

## Agent selection

`-a, --agent NAME` selects the agent adapter for a coding agent from the `defsort.hook_adapters`
entry-point group. The adapter translates the agent host's payload into defsort's neutral input and
translates defsort's decision into the host's response. Classification, Git operations, incremental
eligibility, sorting, and the allow or deny decision never depend on the adapter, and an adapter
cannot change a decision.

- Claude Code (`claude`) and Codex (`codex`) payloads carry `hook_event_name`; `PreToolUse` with
  `tool_name` `Bash` supplies the command and `PostToolUse` with `Edit`, `Write`, or `apply_patch`
  supplies the edited paths.
- Antigravity (`agy`) payloads carry no event name, so `--event PreToolUse` or
  `--event PostToolUse` is required. `run_command` supplies the command; `write_to_file` and
  `replace_file_content` supply the edited path.
- Any other event or tool is nothing defsort acts on: silent exit 0.

An unknown host, a host registered twice, an adapter that fails to import or reports a different
`defsort.hooks.API_VERSION`, a missing or unexpected `--event`, an adapter that raises while
parsing, or an adapter whose `render` and `render_failure` both raise is the last resort: one
`defsort:` line on stderr, nothing on stdout, exit 2. See Agent adapters below for what exit 2
means on each host.

### Adding a bundled agent

Each bundled integration owns one directory under `plugins/<agent>/`. That directory contains the
agent's native manifest, an `adapter.toml` entry point, an `_defsort` runtime package, and its
tests. The `plugins` directory and agent directory remain implicit namespace packages; only the
`_defsort` runtime leaf has an `__init__.py`.

The adapter implements `defsort.hooks.HookAdapter` at `defsort.hooks.API_VERSION`. Its runtime code
may import the standard library, its own package, and the supported `defsort.hooks` surface. It
must not import private defsort modules. Keep payload parsing and host response JSON inside the
owning plugin.

The build reads every `plugins/*/adapter.toml`, writes those entry points into the distribution,
and packages every `plugins/*/_defsort/` runtime leaf alongside the defsort core. Adding an agent
therefore requires only its new plugin directory. Do not add a root entry-point table or create a
separate Python distribution.

## PreToolUse

Every pre-tool shell command receives exactly one of three outcomes:

1. **Unrelated.** No commit invocation is detected. Silent allow, no repository work.
2. **Supported commit.** The complete command belongs to the closed grammar below. Defsort sorts
   the content the commit will consume, then allows. A grammar-accepted command that is proven not
   to commit, a status-only commit or a literal route whose target does not exist, allows with no
   repository work.
3. **Unsupported commit.** A commit invocation is detected and the command is outside the grammar.
   Defsort denies before any repository discovery, add replay, index read, index write, or
   working-tree write, with a reason that names the first unsupported element and the nearest
   accepted form, for example
   `defsort: unsupported commit command: segment 'git push'; supported form: [cd DIR &&] [git add ...;] git commit ...; run checks in an earlier tool call`.

An accepted committing shape whose execution directory is unknown and whose route is absent or
relative is an unsupported commit. That denial runs before hook enablement, configuration reads,
and `--allow-on-failure`, so neither `defsort.hook=false` nor the infrastructure-failure option can
convert it into an allow.

Silent passthrough for a detected commit does not exist. The grammar runs first; an accepted
command's message content is never inspected. Otherwise the detector runs.

### Skip conditions

The hook exits 0 with no output when:

- JSON on stdin is invalid or not an object.
- The payload describes another event or a tool that is not the shell.
- The command is unrelated: no commit invocation is detected.
- The command is a supported status-only commit (`--dry-run`, `--short`, `--porcelain`, `--long`,
  `--null`, or `-z`) or uses a literal route whose target does not exist. In both cases, the
  accepted command is proven not to reach a commit, so defsort performs no repository discovery.
- The hook is disabled in the target repository via git config for a supported commit. Disabling
  never suppresses the deny outcome: an unsupported command has no trusted route, so reading the
  payload repository's setting would let a disabled outer repository exempt a commit aimed at an
  enabled one.

When processing proceeds:

1. Reads any pathspec file after enablement and configuration resolution, then replays the
   supported `git add`, if any. Non-regular pathspec resources are denied before replay.
2. Determines the commit mode (normal, auto-stage, include, pathspec-only).
3. Discovers staged or modified `.py` files.
4. Reads file contents from the git index or working tree (depending on mode).
5. Sorts definitions in each file.
6. Writes results back to the index or working tree (depending on mode).
7. Syncs working-tree copies when appropriate.
8. Exits 0 with no output after a successful rewrite. The provider's normal permission prompt and
   allowlist evaluation still apply. Safety skips emit informational `defsort:` lines on stderr.

### Command detection

Commands are parsed into a structural tree by a POSIX shell parser that never executes, expands, or
predicts. The grammar and the detector both consume that tree; no text is rewritten before parsing.

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
- Run checks such as `git diff --check` in an earlier tool call and wait for success before
  submitting `git add sample.py && git commit -m 'fix: update sample'` in another call. Read-only
  segments before an actual commit are rejected, including checks after `git add`, so the hook
  cannot sort or stage files before the check runs. Standalone read-only commands pass through
  without sorting.
- For status-only commits (such as `git commit --dry-run`), read-only segments may precede them
  with no pathspecs or positional arguments and only these options: `status` accepts `--short`,
  `--porcelain`, `--porcelain=v1`, `--branch`, and `--untracked-files=all`; `diff` accepts
  `--cached`, `--check`, `--stat`, `--name-only`, and `--name-status`; `log` accepts `--oneline`,
  `--stat`, `--format=LITERAL`, `-N`, and `-n N`. `--output` and `--no-index` are excluded because
  a later add could stage what they produce.
- At most one `git add [--] PATHSPEC...` or
  `git add (-A | --all | -u | --update) [--] [PATHSPEC...]`.
- Exactly one final `git commit`. Its first word may be `GIT_COMMITTER_DATE=VALUE` with an unquoted
  name, where `VALUE` is literal text in any quoting or exactly `"$(git log -1 --format=%aI)"`.
- The `-m` or `--message` value may be the heredoc message idiom, at most once:

```text
git commit -m "$(cat <<'EOF'
subject

body
EOF
)"
```

The double quotes and the quoted `EOF` delimiter are part of the idiom. An unquoted `$(cat ...)` is
word-split by the shell and could add selection options to Git's argv, so it is unsupported.

The commit option inventory is closed. Selection options: `-a`, `--all`, `--no-all`, `-i`,
`--include`, `--no-include`, `-o`, `--only`, `--no-only`, `--`, `--pathspec-from-file FILE`,
`--pathspec-from-file=FILE`, `--pathspec-file-nul`, and positional pathspecs; last option wins, and
`--all` with a pathspec, `--include` with `--only`, `--include` or `--only` without a pathspec, and
`--pathspec-from-file` with positional pathspecs are unsupported. Value-taking metadata options:
`-m`, `--message`, `-F`, `--file`, `--author`, `--date`, `-c`, `--reedit-message`, `-C`,
`--reuse-message`, `--fixup` (except a value beginning with `reword:`), `--squash`, `--trailer`,
`--cleanup`, `-t`, `--template`, `--unified`, `--inter-hunk-context`. Boolean metadata options:
`--amend`, `-e`, `--edit`, `--no-edit`, `--reset-author`, `-s`, `--signoff`, `--no-signoff`, `-n`,
`--no-verify`, `--verify`, `--no-post-rewrite`, `-q`, `--quiet`, `--no-quiet`, `-v`, `--verbose`,
`--no-verbose`, `--allow-empty`, `--no-allow-empty`, `--allow-empty-message`,
`--no-allow-empty-message`, `--status`, `--no-status`, `--branch`, `--no-branch`, `--ahead-behind`,
`--no-ahead-behind`, `-S`, `-SKEY`, `--gpg-sign`, `--gpg-sign=KEY`, `--no-gpg-sign`, `-u`,
`-uMODE`, `--untracked-files`, `--untracked-files=MODE`, `--no-untracked-files`. Short boolean
options may be clustered. `--no-patch` and `--no-interactive` are accepted and turn interactive
mode off. Status-only options `--dry-run`, `--short`, `--porcelain`, `--long`, `-z`, and `--null`
are accepted and proven noncommitting. Everything else, including `-p`, `--patch`, `--interactive`,
`-h`, `--help`, `--pathspec-from-file=-`, every abbreviation, and every unlisted `--no-` spelling,
is unsupported.

The detector decides whether a rejected command contains a commit invocation that the shell, a
listed evaluator, or a listed interpreter executes: a word naming Git by basename (`git` or
`git.exe` in any letter case after the last `/` or `\`) followed later in the same segment by a
`commit` word; a word carrying an expansion followed later in the same segment by a `commit` word;
every command substitution, including inside double quotes and inside `${...}` expansions; every
heredoc body, scanned once for the substitutions an unquoted delimiter expands, once as a shell
script, and once as a quote-stripped token stream for a `git` token followed by a `commit` token;
the script arguments of `sh`, `bash`, `zsh`, `dash`, or `ksh` after a `-c` cluster; the joined
arguments of `eval`; and the inline script after `-c` or `-e` of `node`, `perl`, `ruby`, or any
`python*`, scanned as the same token stream. A parse that needed recovery (unbalanced quotes, an
unterminated substitution or heredoc, nesting deeper than eight substitutions) is never accepted
and is always classified by the detector. Only the bare spelling `git` can be accepted; a
path-qualified or `.exe` spelling denies with the element `executable '<spelling>'` and the hint
`use the bare executable name git; PATH resolves the installed git`.

Listed shells and interpreters are recognized by basename, ignoring letter case, using either `/`
or `\` as path separators, and allowing a trailing `.exe`. For example, `bash.exe` and
`C:\tools\BASH.EXE` receive the same detector checks as `bash`. These spellings do not make a
wrapped commit a supported command.

When an accepted commit's staged-index sort rewrites at least one index entry, the allow response
carries the reason `defsort: sorted N staged Python file for this commit` (or `files` for more than
one), which the host shows as hook feedback. Working-tree sorting paths and accepted commits that
rewrite nothing stay silent.

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

Commands where commit text is only data to another program, such as `grep 'git commit' file` or
`python -c "print('git commit')"`, are unrelated and allowed. A wrapper script that commits without
commit text in the outer command is outside the guarantee.

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
`Cwd` argument of `run_command`. Codex reports only the session directory, which can differ from
where a shell call with its own `workdir` executes, so the Codex adapter reports the directory as
unknown. With an unknown directory, a bare commit or a relative route is denied before any
repository access. For a bare commit, the reason is
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

### Commit modes

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

Pathspec files must be ordinary files; symlinks to ordinary files are accepted. Directories, FIFOs,
sockets, and devices are denied before any preceding `git add` is replayed, including with
`--allow-on-failure`. Missing or unreadable files use the normal infrastructure-failure policy. The
hook reads the file once and reuses its bytes for selection. Empty files keep Git's empty pathspec
selection semantics. A disabled hook does not open pathspec resources for supported commands.

### Security considerations

A successful standalone hook does not issue a permission decision for the intercepted command.
Unrelated commands, no-ops, and working-tree rewrites exit 0 without stdout. A staged-index rewrite
emits a `systemMessage` receipt with the number of sorted files. The host still applies its normal
permission rules. A sequencer has its own response contract; see
[composition](#composing-with-other-hooks).

| Outcome                                   | Claude Code                                       | Codex                                             | Antigravity                                                      |
| ----------------------------------------- | ------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| Unrelated, no-op, or working-tree rewrite | Exit 0, silent                                    | Exit 0, silent                                    | Exit 0, silent                                                   |
| Staged-index rewrite                      | `systemMessage`, exit 0                           | `systemMessage`, exit 0                           | `systemMessage`, exit 0                                          |
| Incremental-policy or reorder-guard skip  | Info on stderr, exit 0                            | Info on stderr, exit 0                            | Info on stderr, exit 0                                           |
| Hard file-level safety blocker            | Deny JSON, reason on stderr, exit 0               | Deny JSON, reason on stderr, exit 0               | Top-level deny JSON, reason on stderr, exit 0                    |
| Unsupported commit                        | Deny JSON on stdout, reason on stderr, exit 0     | Deny JSON on stdout, reason on stderr, exit 0     | Top-level deny JSON, reason on stderr, exit 0                    |
| Failure, fail-closed                      | Deny JSON on stdout, reason on stderr, exit 0     | Deny JSON on stdout, reason on stderr, exit 0     | Top-level deny JSON, reason on stderr, exit 0                    |
| Failure, `allow-on-failure`               | Warning on stderr, exit 0                         | Warning on stderr, exit 0                         | Warning on stderr, exit 0                                        |
| PostToolUse diagnostic                    | `additionalContext`, exit 0; notes also on stderr | `additionalContext`, exit 0; notes also on stderr | Existing stderr / `systemMessage`, exit 0; visibility unverified |
| Last resort (no functioning adapter)      | One line on stderr, exit 2: blocks                | One line on stderr, exit 2: blocks                | One line on stderr, exit 2: does not block                       |

Opting out with `git config defsort.hook false` disables sorting and its mutations for supported
commits in that repository. It does not suppress the deny outcome for unsupported commands, which
are denied before any repository is consulted.

A successful hook rewrite can remain in the index or working tree if the provider later rejects the
commit. Defsort cannot observe that later permission decision. For compound commands such as
`git add file.py && git commit`, the hook replays the parsed `git add` before sorting. Replayed
content therefore remains staged if either the provider or defsort's own later safety decision
denies the commit. A broad replay such as `git add -A` or `git add .` can also stage unrelated
non-Python files before that decision. Use separate `git add` and `git commit` tool calls when
clean failure behavior matters.

A compound command whose prefix segment is outside the grammar is denied, whatever commit mode
follows it. For example, `python generate.py && git commit -am test` and
`python generate.py && git commit -m test -- generated.py` are denied because the prefix could
change the working tree between sorting and staging, and `make check && git commit -m test` is
denied for the same reason even though a normal commit consumes the existing staged snapshot. Run
generation and commit as separate tool calls.

### File discovery

The hook selects Python files from the index or working tree according to the commit mode. Deleted
files, symlinks, and other non-regular Git entries are skipped. Incremental PreToolUse processing
also detects staged renames and leaves them unchanged.

### Index path

For a normal commit, the hook sorts staged content in memory, checks the whole batch before
writing, and updates the index atomically. Executable bits are preserved. The working-tree copy is
synchronized separately under the rules below.

### Working tree path

For auto-stage, pathspec-only, and include modes, the hook reads file contents from the working
tree, sorts in memory, and writes the result back to the working tree. Git then stages the modified
content as part of the commit.

### Working tree sync

After updating the index (normal commits), the hook syncs the working tree to keep it consistent:

- **No unstaged changes** (working tree matches staged content): the sorted content is written to
  the working tree. This applies to both bare `git commit` and compound `git add && git commit`
  commands.
- **Unstaged changes present** (working tree differs from staged content): the working tree is left
  untouched, preserving partial staging.
- **Compound commands** (`git add && git commit`): files in the `git add` arguments are always
  synced regardless of unstaged changes, so the subsequent `git add` stages the sorted content.

### Blocked sort

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
order after ordinary ordering. `allow-reordering` removes only that pin. `EAGER_ORDER` still blocks
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

### Infrastructure failures

By default, infrastructure failures (can't read staged content, can't create blobs, can't update
index) deny the commit. Pass `--allow-on-failure` to warn on stderr and return without a defsort
denial. Safety blockers (interstitial code, eager order, forward refs, duplicates, layout) always
deny regardless of this flag.

If Git is older than 2.42, defsort reports the detected version. PreToolUse denies by default or
warns and allows with `allow-on-failure`; PostToolUse warns, leaves the file unchanged, and exits
successfully.

### Syntax errors

Staged files with Python syntax errors are silently skipped. They do not block the commit.

### No staged `.py` files (exit 0, no output)

If no staged `.py` files are found, or all are already in order, the hook exits 0 with no output. A
successful staged-index rewrite instead includes a receipt naming the number of rewritten files in
the allow response; see [command detection](#command-detection).

## PostToolUse

The PostToolUse hook fires after each Claude Code Edit/Write tool use, Codex `apply_patch` file
edit, or Antigravity `write_to_file`/`replace_file_content` edit. The host adapter supplies the
edited paths: Claude Code payloads provide one `tool_input.file_path`, Antigravity payloads provide
one `TargetFile`, and Codex payloads provide an `apply_patch` command from which the adapter
extracts each touched path.

### Skip conditions (PostToolUse)

The hook exits 0 with no output when:

- Hook is disabled via git config.
- JSON on stdin is invalid or not an object.
- The payload describes another event or a tool the host adapter does not treat as a file writer.
- No touched path ends in `.py`.
- File does not exist on disk.

### Syntax errors (exit 0, JSON warning)

If the file has a Python syntax error, Claude Code and Codex receive the diagnostic in
`hookSpecificOutput.additionalContext`, with exit 0:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Cannot sort <path>: <error> (line N)."
  }
}
```

Antigravity retains its `systemMessage` response; its post-tool feedback visibility is unverified.

### Successful sort (exit 0, no output)

If the file needs sorting and all safety checks pass, the hook writes the sorted source directly to
the file and exits 0.

### Blocked sort (exit 0, diagnostic context and stderr)

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

## Composing with other hooks

Agent runtimes may run matching hooks concurrently. When two formatters modify the same files,
register one sequencer command and make each formatter an ordered step. For Codex, use this command
with the manual setup's event and matcher:

```bash
sequential-hooks --agent codex --step-timeout 30 defsort hook --agent codex -- docwrap hook --agent codex
```

For Claude Code, use `claude` in all three agent selections. Each process needs its own `--agent`;
children do not inherit the sequencer's selection. Remove their separate standalone registrations
so the same formatters do not also run concurrently.

The sequencer must honor a formatter's deny JSON even though that response exits 0. Exit 2 without
stdout is a separate adapter failure. A later hook failure cannot roll back source or index changes
already made by defsort.

**Antigravity limitation:** The current sequential-hooks Antigravity adapter rejects the
`systemMessage` receipt emitted by defsort and docwrap after staged rewrites. It stops with a deny
by default. Do not use these formatters as Antigravity sequencer children until their response
contracts are compatible. Registering both standalone formatters is not a serialization workaround;
they can still run concurrently.

## pre-commit

The pre-commit framework runs independently of coding agents. Use it when you want the formatter to
run for commits made from any editor or terminal. Install defsort in the environment where
pre-commit runs and make its CLI available on that environment's `PATH`. Then add a local hook to
`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: defsort
        name: defsort
        entry: defsort --write
        language: system
        types: [python]
```

Install pre-commit separately, then run `pre-commit install` in your project to activate the
registration. The shipped hook IDs are `defsort` (writing) and `defsort-check` (checking); for a
local checking registration, use `entry: defsort` with `id: defsort-check`. A `language: system`
registration runs the installed CLI, so it does not install defsort for you. These are CLI
invocations: they use the project's CLI configuration and the [CLI exit codes](cli.md#exit-codes),
not agent-hook response JSON.

## Exit codes

| Code | Meaning                                                            |
| ---- | ------------------------------------------------------------------ |
| 0    | Every rendered response: allow, deny, warning, PostToolUse         |
| 2    | Last resort: no functioning host adapter, one diagnostic on stderr |

A deny is carried by the host's JSON on stdout, not by the exit status. Exit 2 blocks on Claude
Code and Codex, which document exit 2 as blocking; Antigravity has no exit-code tier, so the last
resort does not block there. When defsort runs under a wrapper, the wrapper's own host adapter
decides what a child's nonzero exit means; that protection is the wrapper's contract, not
defsort's.

## Error handling

- Invalid JSON on stdin causes a silent exit (exit 0, no output).
- A supported commit in a repository with the hook disabled via git config causes a silent exit.
- An unsupported commit, every safety blocker, and every fail-closed infrastructure failure produce
  the host's deny JSON on stdout and the reason line on stderr with exit 0.
- `--allow-on-failure` turns infrastructure failures after a command is accepted into a warning on
  stderr and a silent allow. It never applies to unsupported commits, to safety blockers, or to the
  last resort.
- PostToolUse returns 0. Claude Code and Codex combine diagnostic notes and syntax-error feedback
  in one `hookSpecificOutput.additionalContext`, without duplicate lines, and retain notes on
  stderr. Antigravity retains notes on stderr and syntax-error feedback in `systemMessage`;
  visibility is unverified. Empty successful invocations remain silent. Adapter tests verify
  emitted JSON; live-host and wrapper acceptance must be verified separately.

## Agent adapters

Defsort reaches each host through an adapter that implements the public `defsort.hooks` contract
(see the [library reference](library.md)). Adapters are discovered through the
`defsort.hook_adapters` entry-point group; the built-in `claude`, `codex`, and `agy` adapters are
registered in that group like any other. An adapter translates: it turns the host payload into
neutral input (`event`, `command`, `cwd`, `command_cwd`, `edited_paths`) and turns defsort's
neutral result (`decision`, `reason`, `notes`) into the host response (`stdout`, `stderr`,
`exit_code`). It cannot change a decision.

`command_cwd` is the absolute directory a pre-tool command executes in, or `None` when the host
contract does not establish it. An adapter must never derive it from the process directory, a
workspace root, or the session directory; `defsort.hooks.absolute_directory` returns a payload
field only when it is a non-empty absolute path. An empty, relative, or non-string `command_cwd` is
an adapter failure that exits 2 without repository access. Post-tool input sets `command_cwd=None`
and uses `cwd` as event context.

To add a host, package a class that satisfies `defsort.hooks.HookAdapter`, set `api_version` to
`defsort.hooks.API_VERSION`, and register it:

```toml
[project.entry-points."defsort.hook_adapters"]
myhost = "defsort_host_myhost.adapter:MyHostAdapter"
```

Install the adapter package in the same Python environment as defsort, then register
`defsort hook --agent myhost`. A host whose payload carries no event name sets `requires_event` and
receives `--event`. Defsort refuses an adapter whose `api_version` differs from its own and a name
registered by two distributions.

Failure tiers:

1. Every deny, including safety blockers and infrastructure failures under fail-closed
   configuration, is rendered by the selected adapter's `render` or `render_failure`. The adapter
   encodes the host's real enforcement protocol; the core never chooses an exit code.
2. When no functioning adapter exists (unknown or duplicate name, import failure, wrong
   `api_version`, `parse` raising, or both `render` and `render_failure` raising), defsort writes
   one diagnostic line to stderr and exits 2 with nothing on stdout. Exit 2 blocks on Claude Code
   and Codex and does not block on Antigravity. Defsort cannot protect a chain from the failure of
   its own adapter.

## Differences from the CLI

| Feature              | CLI                            | PreToolUse                     | PostToolUse                    |
| -------------------- | ------------------------------ | ------------------------------ | ------------------------------ |
| Ordering mode        | Dependency, alphabetical, none | Dependency, alphabetical, none | Dependency, alphabetical, none |
| Policy axes          | All ordering-policy flags      | All ordering-policy flags      | All ordering-policy flags      |
| Incremental          | Off by default                 | Off by default                 | Off by default                 |
| Config file          | `[tool.defsort]`               | `[tool.defsort]`               | `[tool.defsort]`               |
| Constant hoisting    | `--hoist`                      | Not supported                  | Not supported                  |
| Force bypass         | `-f, --force`                  | Not supported                  | Not supported                  |
| Auto-add future      | Yes (default)                  | Never                          | Never                          |
| Remove future import | Only when annotation-free      | Only when annotation-free      | Only when annotation-free      |
| Method sorting       | All three modes                | All three modes                | All three modes                |
| Write mode           | `--write` flag required        | Always writes (to index)       | Always writes                  |
| Diff output          | `--diff` flag                  | Not supported                  | Not supported                  |
| Report output        | Summary table                  | Not supported                  | Not supported                  |
| Error reporting      | stderr + exit code 2           | Deny or warn (configurable)    | stderr (always returns 0)      |
| Scope                | Explicit paths/dirs            | Staged `.py` files             | Touched files from tool event  |
| Directory recurse    | Yes                            | No                             | No                             |
| Encoding             | Preserved                      | Preserved                      | Preserved                      |
| Newline style        | Preserved                      | Preserved                      | Preserved                      |
