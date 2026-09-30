# Development

## Local environment

Run `make setup` from the checkout with uv 0.12.13 and a supported `python` on `PATH`. It
synchronizes the hashed development lock into `.venv` and installs the checkout in editable mode.
`make quality` runs the read-only formatting, lock, type, dead-code, manifest, and whitespace
checks; `make check` adds the full test suite. `make test` runs that suite alone, and `make format`
applies the repository formatters. Project commands refuse a missing or outdated setup. Biome,
Taplo, and shfmt are separate native tools on `PATH` for formatting.

Use `make install` to install the command-line tool with `uv tool`, or `make reinstall` to replace
it. `make format-check`, `make lock-check`, `make type-check`, `make dead-code-check`,
`make manifest-check`, and `make whitespace-check` run the corresponding read-only checks.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality` first. The `test` and `hooks` jobs
then run in parallel. `package` starts only after those jobs pass; `hosted-validation` reports the
outcome of a hosted run, including failures.

CI uses GitHub-hosted runners unless a run explicitly requests local runners. In a private
repository, the Actions variables `CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER`
select local runners for runs started from Actions > CI > Run workflow with the hosted option
disabled. If a variable is absent, that operating system uses a hosted runner. Pushes, pull
requests, public repositories, and runs with the hosted option enabled use hosted runners for every
job and run every test.

A staging repository sets the Actions variable `CI_RUN_ON_PUSH` to `false`. Its pushes then appear
as `CI / push skipped` and run no jobs; CI runs only on request. A newer requested run on a branch
cancels an older one. Push runs are never cancelled, because the Release workflow needs the tagged
commit's push run.

| Requested run                                                 | Actions > CI > Run workflow | Title               |
| ------------------------------------------------------------- | --------------------------- | ------------------- |
| Quick: local runners, slow tests omitted on macOS and Windows | hosted off, slow tests off  | `CI / local`        |
| Full: local runners, every test                               | hosted off, slow tests on   | `CI / local (full)` |
| Hosted: GitHub-hosted runners, every test                     | hosted on                   | `CI / hosted`       |

The `test` matrix covers Python 3.13 to 3.15 on Linux and Python 3.13 on macOS and Windows. All
test jobs have a one-hour hang guard, on local and hosted runners, in quick and full runs. The
`hooks` job runs both pre-commit hooks against a sample file in a temporary repository, the way a
user would, without building distributions. `package` also loads the bundled plugin entry points
from the installed wheel.

CI runs tests in parallel with pytest-xdist. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when a runner's
visible CPU count exceeds its CPU quota. For the same scheduling locally, run:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

The manual [Release workflow](../.github/workflows/release.yml) publishes the package to PyPI after
checking its version tag and a successful push CI run for the tagged commit.

## Dependency updates

`pyproject.toml` declares dependency ranges. `requirements-ci.txt` pins and hashes the development
and build dependencies for quality, consumer, and package jobs. `requirements-test.txt` selects the
test and build dependencies from the same pins, including Hatchling for packaging tests. Test jobs
install this smaller set. Published dependency requirements remain the ranges in `pyproject.toml`.

Use the uv version pinned in [the CI workflow](../.github/workflows/ci.yml) when regenerating the
files. After changing dependencies, run:

```sh
make lock-update
```

This retains existing compatible pins. Use `make lock-update LOCK_FLAGS=--upgrade` only for an
intentional refresh. Review and commit both generated files. `make lock-check` checks them without
rewriting them, and `make check` includes that check. Generation and checking resolve dependencies
but do not install them.

The `ci-build` dependency group supplies Cython and setuptools for source builds on supported CI
interpreters. The full pin set retains these tools for quality and package jobs. The Markdown
formatter uses Taplo for TOML code blocks and shfmt for shell code blocks. CI downloads their
official Linux binaries for the quality runner's architecture and checks pinned SHA-256 hashes. The
native Taplo executable replaces `mdformat-config`'s Taplo Python dependency because that
distribution lacks Linux and Windows ARM wheels. Local Markdown formatting requires `taplo` and
`shfmt` on `PATH`; test jobs do not need them. Biome 2.5.14 is a separately installed JSON
formatter needed by `make quality` and `make format`.

The local and CI quality commands select the same checks. This command parity does not establish
tool-version, Python-version, or native-platform parity with the CI matrix.

## Test and runner notes

The `hooks` job checks both pre-commit hooks in a temporary repository without building or
downloading distributions. The required checks and hooks job run in either mode. Hosted jobs run on
separate GitHub-hosted runners; local runners may share a workspace and need their own workspace
isolation. Linux and hosted runners run the full suite; local macOS and Windows omit tests marked
`slow` in quick runs. Test jobs allow 30 minutes on Linux and hosted macOS, and 45 minutes on
hosted Windows. `make test` remains serial. The local and CI quality commands select the same
checks; this does not establish tool-version, Python-version, or native-platform parity with the CI
matrix.
