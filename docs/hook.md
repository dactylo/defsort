# Agent hook guide

Use this guide to install defsort for your coding agent and understand what happens at edits and
commits. For exact command rules, see
[supported commit options](hook-reference.md#supported-commit-options) and the
[hook reference](hook-reference.md).

## Configuration

Install the CLI using the [README](../README.md#installation), then choose one integration:

| Choice                   | When it runs               | What to expect                                                                               |
| ------------------------ | -------------------------- | -------------------------------------------------------------------------------------------- |
| PreToolUse (recommended) | Before supported commits   | Formats the content the commit will consume; normal staged commits preserve partial staging. |
| PostToolUse              | After supported file edits | Formats touched Python files immediately; the agent may need to reread them.                 |

Choose either a plugin or manual registration, and one event mode; registering both can run the
formatter twice. Antigravity staged-rewrite receipts include an `allow` decision that can bypass
its permission prompt; see [host responses](hook-reference.md#host-responses).

### Plugin packages

Use this checkout as the marketplace source. The native plugin directories register host hook
configuration, so install the `defsort` distribution first and confirm that `defsort` is on `PATH`.
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

The plugins register PreToolUse only. If another hook modifies the same files, use
[one sequencer](#composing-with-other-hooks).

### Claude Code manual settings

Use `~/.claude/settings.json` when a plugin is not registering the hook.

**PreToolUse (recommended):**

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

**PreToolUse (recommended):**

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

### Antigravity manual settings

Use `~/.gemini/config/hooks.json` for manual Antigravity registration.

**PreToolUse (recommended):**

```json
{
  "defsort": {
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
edit tools are `write_to_file` and `replace_file_content` (`TargetFile`), plus `write_file` and
`edit_file` (`Path`).

```json
{
  "defsort": {
    "PostToolUse": [
      {
        "matcher": "write_to_file|replace_file_content|write_file|edit_file",
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

Antigravity PostToolUse sends diagnostics only to stderr; their visibility depends on the host.

## PreToolUse

Stage your changes, run your checks, then submit the commit in a separate tool call. Use an
absolute repository path under Codex, whose adapter cannot establish a shell call's working
directory:

```text
git -C /absolute/repo commit -m "Describe the change"
```

A normal commit sorts staged Python content. The hook updates the index, then updates matching
working-tree copies; it leaves divergent unstaged edits alone. A receipt reports how many staged
files changed. A commit with no changes or notes is silent.

Other commit modes can write working-tree files; consult
[commit modes](hook-reference.md#commit-modes) before using `--all`, `--include`, or commit
pathspecs.

### Checks before commits

Run checks such as `git diff --check` in their own tool call. If a check must follow staging, run
the add and check together without a commit, then commit after success. Separate lines in one tool
call still reach PreToolUse together. A later hook may format staged content, so an earlier check
does not certify the final formatted bytes.

The hook accepts a single add followed by a commit, but replayed staging can remain after a denial;
see [mutation and permission timing](hook-reference.md#mutation-and-permission-timing).

```text
git -C /absolute/repo add file.py && git -C /absolute/repo commit -m "Describe the change"
```

Run initialization, generation, checks, and pushes in separate tool calls. The
[command reference](hook-reference.md#command-detection) gives the supported sequence and
exceptions.

### Cross-repo commands

Use `git -C /absolute/repo` on every Git segment, or `cd /absolute/repo && git commit ...`. Do not
combine the two routing forms. Relative routes require a host that supplies the command's execution
directory; Codex requires an absolute route. On Windows, use `C:/repo` in Git commands. See
[routing details](hook-reference.md#cross-repo-commands).

### Working tree sync

For normal staged commits, synchronization requires matching newline-normalized text and an
unchanged byte snapshot. A skipped synchronization does not deny the commit: the index already has
the formatted version, and a note explains why the working copy stayed unchanged. See
[synchronization rules](hook-reference.md#working-tree-sync) for aliases, Git filters, and
recovery.

## PostToolUse

Choose the PostToolUse manual configuration above when you want formatting after each edit. The
hook formats touched `.py` files and is silent when no diagnostics are needed. It does not block an
edit that has already happened.

Claude Code and Codex receive diagnostics as `additionalContext` and on stderr. Antigravity
receives stderr only. Syntax errors, changed files, and failed writes produce skip diagnostics; see
[PostToolUse results](hook-reference.md#posttooluse-results) for exact conditions and recovery.

## Composing with other hooks

Matching agent hooks may run concurrently. Register one sequencing hook when multiple steps modify
the same files or tool input, and remove their standalone registrations. Each step needs its own
`--agent` selection and the same incoming payload. Concurrent add replay or index updates can fail
on Git's `index.lock`.

The sequencer must honor deny JSON even when the formatter exits 0. A later failure cannot undo a
completed source or index rewrite. For Antigravity, choose a sequencer that composes native
`allow`/`deny` responses and string `systemMessage` receipts. The sequential-hooks Antigravity
adapter accepts both forms and folds string receipts into its composed reason. Keep deny handling
enabled. See [host responses](hook-reference.md#host-responses).

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
        files: \.py$
```

Install pre-commit separately, then run `pre-commit install` in your project to activate the
registration. The shipped hook IDs are `defsort` (writing) and `defsort-check` (checking); for a
local checking registration, use `entry: defsort` with `id: defsort-check`. A `language: system`
registration runs the installed CLI, so it does not install defsort for you. These are CLI
invocations: they use the project's CLI configuration and the [CLI exit codes](cli.md#exit-codes),
not agent-hook response JSON.

`defsort --write` exits 1 if any passed file is skipped because of a file-level blocker, a
class-only skip, the reorder guard, or an unreadable incremental baseline. This makes pre-commit
fail even when no file was written. `--hide-skips` hides skipped-file messages but does not change
the exit status. Agent hooks allow class-only skips. To make the pre-commit hook pass, fix the
blocker or exclude the file in the pre-commit configuration.

## Troubleshooting

| Symptom                                  | What to do                                                                                               |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Hook installed but not running           | Restart the host and review/trust the registration; in Codex confirm `Active 1` in `/hooks`.             |
| Unsupported commit command               | Use the simple commit form above; check [supported options](hook-reference.md#supported-commit-options). |
| Unknown working directory                | Use an absolute `git -C` route.                                                                          |
| Git, index, or file operation failed     | Read the operation and cause; see [infrastructure failures](hook-reference.md#infrastructure-failures).  |
| Working copy was not updated             | Keep your unstaged edits; the note describes the [sync result](hook-reference.md#working-tree-sync).     |
| Hooks contend on `index.lock`            | Serialize modifying hooks under one registration.                                                        |
| Recovery file named after a failed write | Recover the original bytes as described in [file writes](platform.md#writes-and-file-metadata).          |

A file-level safety blocker denies a commit; a blocked class or incremental-policy skip can allow
it with a note. See [blocked sorts](hook-reference.md#blocked-sort) for remedies.

## Hook settings

### Per-repo enable/disable

The hook checks the `defsort.hook` git config key. If the key is missing, the hook defaults to
enabled.

Values follow Git boolean semantics: true/yes/on/1 to enable, false/no/off/0 to disable. Only a
valid false value disables the hook. A value Git cannot read as a boolean, or configuration Git
cannot read, is a failure that names `defsort.hook` and Git's error. PreToolUse denies the commit,
or warns and allows without formatting under `--allow-on-failure` or the applicable
`allow-on-failure = true` configuration. PostToolUse skips each affected file.

To disable the hook for a single repository:

```text
git config defsort.hook false
```

To disable the hook globally and selectively re-enable it in one repository:

```text
git config --global defsort.hook false
git config defsort.hook true  # re-enable in this repo
```

### Private files

Directory scans and commit checks skip files whose `private` git attribute is set, such as
everything in a folder whose `.gitattributes` file contains `* private`. To use a different
attribute, name it in git config:

```text
git config --global defsort.privateAttribute unpublished
git config defsort.privateAttribute unpublished   # this repository only
```

A name uses letters, digits, `.`, `_`, or `-`, and does not start with `-` or `builtin_`. An
invalid name prints one warning per run, and defsort uses `private`. Selection is by pathname: a
write through a selected hard-link alias can change working-tree contents shared with an excluded
private pathname. Formatting a file an agent edits (PostToolUse) does not check the label.

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

### Incremental mode and future imports

Full sorting is the default. Use `-i` or project configuration for incremental insertion, and
`--no-incremental` to override it. See [incremental mode](incremental.md) for baselines, fallbacks,
Git requirements, and reorder guards.

Hooks never add future imports or hoist constants. They can remove an unused standalone annotations
import under the [cleanup conditions](design.md#future-annotations-management). There is no hook
equivalent of `--force`.

## Agent selection

`--agent` (`-a`) names the coding agent, not a machine. It is required; the payload is never used
to guess it. `--event` (`-e`) is required for Antigravity and rejected for the other built-in
hosts.

| Host        | Agent    | PreToolUse tool | PostToolUse tools                                                  |
| ----------- | -------- | --------------- | ------------------------------------------------------------------ |
| Claude Code | `claude` | `Bash`          | `Edit`, `Write`                                                    |
| Codex       | `codex`  | `Bash`          | `apply_patch`                                                      |
| Antigravity | `agy`    | `run_command`   | `write_to_file`, `replace_file_content`, `write_file`, `edit_file` |

Other events and tools produce no action. See the [synopsis](hook-reference.md#synopsis) and
[exit codes](hook-reference.md#exit-codes) for invalid registrations and adapter failures.

## Agent adapters

Install third-party adapters in the same Python environment as defsort. Adapter authors should use
the [public hooks contract](library.md#defsorthooks).

### Adding a bundled agent

See [Adding a bundled agent](development.md#adding-a-bundled-agent) in the development guide.
