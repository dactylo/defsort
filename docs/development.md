# Development

Use this guide to set up a development checkout, run checks, and update development dependencies.
It also describes the layout for adding a bundled agent.

## Local environment

Run `make setup` with uv 0.13.6 and a supported `python` on `PATH`. It creates or synchronizes
`.venv` from the hashed development lock and installs the checkout in editable mode. Project
commands refuse a missing or outdated setup.

Provide Biome, Taplo, and shfmt on `PATH` for formatting. Use `make install` for a CLI installation
through `uv tool`, or `make reinstall` to replace it; these do not set up a development checkout.

## Run checks

| Command                 | Purpose                                              |
| ----------------------- | ---------------------------------------------------- |
| `make format`           | Apply repository formatters.                         |
| `make format-check`     | Check formatting without rewriting files.            |
| `make whitespace-check` | Check whitespace.                                    |
| `make quality`          | Run all read-only quality checks.                    |
| `make test`             | Run the full test suite, serially by default.        |
| `make check`            | Run quality checks and the full test suite.          |
| `make lock-check`       | Check dependency locks.                              |
| `make type-check`       | Check types.                                         |
| `make dead-code-check`  | Check for unused code.                               |
| `make manifest-check`   | Validate the pre-commit manifest.                    |
| `make workflow-check`   | Audit GitHub Actions workflows with zizmor, offline. |

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality` first. The `test` and `hooks` jobs
then run in parallel. `package` starts only after those jobs pass; `hosted-validation` reports the
overall outcome, including failures. Pull requests and pushes run every job on GitHub-hosted
runners, with every test.

The `test` matrix covers Python 3.13 to 3.15 on Linux and Python 3.13 on macOS and Windows. All
test jobs have a one-hour hang guard. The `hooks` job runs both pre-commit hooks against a sample
file in a temporary repository, the way a user would, without building distributions. `package`
also loads the bundled plugin entry points from the installed wheel.

CI runs tests in parallel with pytest-xdist. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when a runner's
visible CPU count exceeds its CPU quota. For the same scheduling locally, run:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

Plain `make test` runs the suite serially.

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
`shfmt` on `PATH`; test jobs do not need them. Biome 2.6.4 is a separately installed JSON formatter
needed by `make quality` and `make format`.

## Adding a bundled agent

Adapter code lives in formatter-common's `hookfront`. To add a bundled agent, add its adapter to
`hookfront`, sync it into defsort, and add a directory under `plugins/<agent>/` containing:

- the agent's native manifests;
- an `adapter.toml` entry point;
- an `_defsort` package whose `_adapter.py` re-exports the shared adapter class;
- tool-specific tests.

The `plugins` directory and agent directory remain implicit namespace packages; only the `_defsort`
runtime leaf has an `__init__.py`.

The adapter implements `defsort.hooks.HookAdapter` at `defsort.hooks.API_VERSION`. Plugin code may
import the standard library, its own package, `defsort.hooks`, and `defsort._vendor.hookfront`.
Keep payload parsing and host response JSON in the shared adapter.

The build reads every `plugins/*/adapter.toml`, writes those entry points into the distribution,
and packages every `plugins/*/_defsort/` runtime leaf alongside the defsort core. Do not add a root
entry-point table or create a separate Python distribution.
