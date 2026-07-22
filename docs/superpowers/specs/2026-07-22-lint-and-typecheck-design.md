# Lint and type checking

Adopt Ruff (lint + format) and mypy as blocking gates. Run them with `uv run`.

All numbers below were measured on this tree at `40d110f` with `ruff 0.15.22`
and `mypy 2.3.0`, not estimated.

## Why

The repo has no lint, no type checking and no CI. That is not a style problem.
Three defects and three `NameError`s are sitting in `main` today, and every one
of them is caught by a tool that was never run:

| Where | Defect |
| --- | --- |
| `execute.py:113` | `["git", "ls-remote", clone_url, target]` has no `--` separator, and `clone_url` is validated only as `min_length=1` (`config/prompts.py:92`). A `clone_url` of `--upload-pack=<cmd>` in `prompts.jsonl` runs `<cmd>` on the host. Reproduced end to end. |
| `execute.py:257` | `output_dir / record.id`, where `id: str \| None = None` (`config/prompts.py:172`). `Path / None` raises inside a worker, and `future.result()` then kills the whole run rather than one record. |
| `container.py:406` | `previous.get(signum, signal.SIG_DFL)`. `signal.getsignal` *stores* `None` when Python did not install the handler, so the key exists and the `.get` default never fires. `signal.signal(signum, None)` raises inside the SIGINT handler, `os.kill` is never reached, and the shell sees the wrong exit cause. |

Plus three `F821`s: `Any` twice in `execute.py`, and `Path` at
`tests/runtime/test_docker_integration.py:457` — in the docker-deselected file,
so nobody has run it.

That is the justification for this work. The style enforcement is a side
effect.

## What is adopted

**Ruff**, for lint and formatting. **mypy**, strict, on `src/`. **tox**, as the
task runner and CI entry point. **GitHub Actions**, calling tox. **Coverage**,
via `pytest-cov`.

Ruff and mypy are configured in `pyproject.toml`; tox gets its own `tox.ini`.

### On tox — a reversed decision

An earlier draft of this spec dropped tox, on the reasoning that at a single
Python version it is ~40 lines of configuration replacing four `uv run` lines,
and that its benefits did not carry that weight *while there was no CI to call
it from*.

That reasoning was wrong because its premise was. The established pattern in
this author's other projects is tox as precisely that CI entry point —
`.github/workflows/check.yml` installs `tox --with tox-uv --with tox-gh` and
runs bare `tox`, with `[gh.python]` mapping the interpreter to its env list.
Under that pattern tox is not ceremony wrapping `uv run`; it is the single
command that CI and a developer both invoke, and per-env `dependency_groups`
give each gate an isolated environment that one shared `.venv` cannot.

The three consequences: `tox.ini` rather than `[tool.tox]` in `pyproject.toml`
(matching the reference project), separate `test`/`lint`/`type` dependency
groups with `dev` composing them via `include-group`, and a CI workflow in the
same change — without it, `[gh.python]` is inert and the gates run only when
someone remembers.

### Python floor moves to 3.14

Rather than a 3.13/3.14 matrix, `requires-python` moves to `>=3.14` and there
is one interpreter env. Verified before adopting: all dependencies have cp314
wheels (`pyarrow` 25.0.0, `pydantic` 2.13.4), and the full fast suite is **419
passed, 38 deselected on 3.14.5**. Ruff's `target-version = "py314"` produces
byte-identical output to `py313` on this tree — 260 findings either way — so
the bump costs nothing in lint churn.

This also retires the `pyarrow` cp315 concern noted earlier; it becomes live
again at 3.15. `pyarrow` remains declared and imported nowhere.

### What is deliberately not adopted

**flake8 and flake8-pydantic.** Measured return is 4 findings, all `PYD003`,
all in `runtime/spec.py`, and arguably 0 actionable — lines 69/70/72 sit in a
block whose neighbours *must* use `Field(...)` because they carry `gt=0`, so
"fixing" them makes the block inconsistent. The cost is two dependencies, one
of them 19 months without a commit, plus a second configuration language:
flake8 cannot read `pyproject.toml`, and a `[tool.flake8]` table is silently
ignored rather than rejected. The four lines are fixed by hand instead.

As a standing replacement, `plugins = ["pydantic.mypy"]` is enabled. It finds
nothing today — measured as completely inert on this tree, byte-identical mypy
output with and without it, over both `src` alone and `src` plus `tests` — but
it covers `PYD002`, the only PYD rule tied to a hard runtime error, and it
costs nothing.

The `[tool.pydantic-mypy]` settings usually paired with it (`init_typed`,
`init_forbid_extra`, `warn_required_dynamic_aliases`) are deliberately omitted:
they were measured as inert on pydantic v2, so including them would be cargo
cult rather than forward compatibility.

## Configuration

```toml
[project]
requires-python = ">=3.14"

[dependency-groups]
dev = [
  { include-group = "test" },
  { include-group = "lint" },
  { include-group = "type" },
]
test = ["pytest>=8", "pytest-cov>=6"]
lint = ["ruff>=0.15"]
type = ["mypy>=2", "types-PyYAML"]

[tool.ruff]
line-length = 88
target-version = "py314"
src = ["src"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM",
          "RET", "ARG", "PTH", "ERA", "TID", "RUF", "S", "PL", "D"]
ignore = [
  "E501",                                          # the formatter owns line length
  "W191", "E111", "E114", "E117", "D206", "D300",  # conflict with `ruff format`
  "PLC0415",                                       # deferred imports are deliberate
  "S607",                                          # partial paths are deliberate
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S108", "S603", "S607", "D", "ARG", "PLR2004"]

[tool.ruff.lint.pydocstyle]
convention = "pep257"

[tool.ruff.lint.pylint]
max-args = 8

[tool.mypy]
files = ["src"]
python_version = "3.14"
strict = true
show_error_codes = true
plugins = ["pydantic.mypy"]
local_partial_types = true   # stated explicitly: mypy 2.0 made these defaults,
strict_bytes = true          # so a downgrade cannot silently relax them
```

And `tox.ini`, modelled on the reference project:

```ini
[tox]
requires =
    tox>=4.24.1
    tox-uv>=1.23
env_list =
    3.14
    lint
    type
skip_missing_interpreters = true

[testenv]
runner = {env:TOX_RUNNER:uv-venv-lock-runner}
description = run the unit tests with pytest under {base_python}
pass_env =
    PYTEST_*
commands =
    python -m pytest {tty:--color=yes} \
      --cov=jormungandr --cov-branch \
      --cov-report=xml --cov-report=html \
      --cov-report term-missing:skip-covered \
      {posargs:tests}
dependency_groups = test

[testenv:docker]
runner = {env:TOX_RUNNER:uv-venv-lock-runner}
description = run the tests that need a Docker daemon
pass_env =
    PYTEST_*
    DOCKER_HOST
commands =
    python -m pytest {tty:--color=yes} -m docker {posargs:tests}
dependency_groups = test

[testenv:lint]
runner = {env:TOX_RUNNER:uv-venv-lock-runner}
description = lint and format-check the code base
commands =
    ruff check {posargs:.}
    ruff format --check {posargs:.}
dependency_groups = lint

[testenv:type]
runner = {env:TOX_RUNNER:uv-venv-lock-runner}
description = run type check on code base
commands =
    mypy {posargs:src}
dependency_groups = type

[gh.python]
"3.14" = ["3.14", "lint", "type"]
```

`docker` is deliberately outside `env_list`: bare `tox` must not require a
daemon. It stays opt-in as `tox -e docker`, consistent with the existing
`addopts = "-m 'not docker'"`. A command-line `-m docker` overrides that
`addopts` value, which is the mechanism CLAUDE.md already documents.

Coverage measures `--cov=jormungandr` (the installed package) rather than
`--cov=src`, and no `fail_under` threshold is set. Establishing a threshold
against an unmeasured baseline would either be meaningless or immediately
block; setting one is its own decision, once a number exists.

And `.github/workflows/check.yml`, mirroring the reference project: `setup-uv`,
then `uv tool install --python-preference only-managed --python 3.14 tox --with
tox-uv --with tox-gh`, then bare `tox`.

### Why each ignore earns its line

- **`PLC0415`** (119 findings) — `cli.py` defers every subcommand import, and
  `commands/jobs.py` defers 18 more, to keep `jormungandr --help` from
  importing the compose and container machinery. Enforcing this rule would undo
  a deliberate design.
- **`S607`** (4) — `git` and `docker` are invoked by bare name on purpose;
  `PATH` lookup is the intended behaviour for a tool that shells out to the
  user's own CLIs.
- **`E501`** (80) — `ruff format` decides line length. Keeping the rule
  selected only duplicates the formatter and creates disagreements.
- **`W191`, `E111`, `E114`, `E117`, `D206`, `D300`** — Ruff documents these as
  incompatible with its formatter. Also never select `COM`, `Q`, `D203` or
  `W191`: `COM812` was measured going **198 → 247** *after* formatting, so the
  formatter and that rule actively fight.

**`S603` stays selected.** After the `tests/` ignore it is 4 sites in `src`.
Each gets a `noqa` carrying the reason that input is trusted — which is exactly
the documentation that was missing at `execute.py:113`.

**`max-args = 8`** clears 9 of the 12 `PLR0913` findings in `src`. The
remaining three (two 9-argument, one 14-argument at `runtime/run.py:96`) get a
`noqa` with a reason. Splitting a 14-argument function is a refactor nobody
asked for and is out of scope here; the `noqa` records the debt.

## Measured cost

With the configuration above:

| | Findings |
| --- | --- |
| `ruff check` total | **260** (`src` 237, `tests` 23) |
| — of which, missing docstrings (`D1xx`) | 164 |
| — of which, docstring style (`D2xx`/`D4xx`) | 11 |
| — of which, substantive | 85 |
| `ruff format` | 25 of 39 files, ~556 changed lines |
| `mypy --strict src` (with `types-PyYAML`) | **33** in 10 files |

`types-PyYAML` is the only stub package needed; `pydantic` and `cyclopts` both
ship `py.typed`.

Both figures are invariant across the Python floor change: ruff gives 260 at
`py313` and `py314`, mypy gives 33 at `--python-version 3.13` and `3.14`. The
3.14 bump therefore adds no work to any task below.

Of the 33 mypy errors, roughly 2 are real bugs, 2 are latent, and the rest is
annotation debt — of which 11 die from 9 one-line edits, the largest being
three `tuple[str, ...]` widenings that also delete 18 errors in `tests/` and 14
existing `# type: ignore` comments.

## Sequence

One concern per commit. Every commit is green. The defects come first: they
exist today and are not lint fallout, and CLAUDE.md requires reproducing a
defect before fixing it.

| # | Commit | Notes |
| --- | --- | --- |
| 1 | `chore: ignore tool cache directories` | `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`. All three are untracked *and* unignored today, and CLAUDE.md records `git add -A` having twice swept unrelated work into a commit. |
| 2 | `fix(execute): pass -- to git ls-remote and validate clone_url` | test first |
| 3 | `fix(execute): assign record ids before use` | test first |
| 4 | `fix(runtime): restore signal handlers Python did not install` | test first |
| 5 | `fix: import the names the annotations reference` | the three `F821` |
| 6 | `refactor(runtime): widen tuple annotations to tuple[str, ...]` | kills 5 `src` + 18 `tests` mypy errors and 14 `# type: ignore` |
| 7 | `style: format with ruff` | 25 files, nothing else in the commit. Run `ruff check --select I --fix` *before* `ruff format`; that order converges, the reverse needs a second format pass. |
| 8 | `fix: clear the ruff findings that are not docstrings` | `F401`, `I001`, `RUF022`, `RUF100`, `PLW1510`, `B017`, `RUF043`, `F841`, `E741`, and the remainder of the 85 |
| 9 | `docs: document the config API` | part of the 164 docstrings |
| 10 | `docs: document the runtime API` | " |
| 11 | `docs: document the cli and commands API` | " |
| 12 | `fix(runtime): drop the Field() call that only sets a default` | `spec.py:117` only — see below |
| 13 | `build!: require Python 3.14` | `requires-python`, `.python-version`, relock. Breaking, hence `!` |
| 14 | `build: add ruff and mypy configuration` | lands green |
| 15 | `build: run the gates under tox` | `tox.ini`, `test`/`lint`/`type` dependency groups, `pytest-cov` |
| 16 | `ci: run tox on push and pull request` | `.github/workflows/check.yml`; makes `[gh.python]` live |
| 17 | `docs: record the tox commands in CLAUDE.md` | |

### Task 12 is smaller than it looked

The spec originally said "fix the 4 `PYD003` by hand". Reading the code changes
that. The four sites are `runtime/spec.py:69`, `:70`, `:72` and `:117`.

Lines 68–73 are one block:

```python
cpus: float | None = Field(default=2.0, gt=0)      # must keep Field — gt=0
memory: str | None = Field(default="4g")           # PYD003
memory_swap: str | None = Field(default=None)      # PYD003
pids: int | None = Field(default=512, gt=0)        # must keep Field — gt=0
shm_size: str | None = Field(default=None)         # PYD003
nofile: int | None = Field(default=4096, gt=0)     # must keep Field — gt=0
```

"Fixing" 69/70/72 leaves three fields using `Field(...)` and three bare, in an
alternating pattern, for no benefit any tool now checks — flake8 was dropped.
Leave them.

Line 117 is different: `tier_split: int = Field(default=20)` sits beside
`modules: tuple[ModuleDeclaration, ...] = ()`, which is already a bare default.
That one is fixed, and the block gets more consistent rather than less.

One line changes, not four.

Configuration lands last so that every preceding commit is independently green
and the gate is green the moment it exists. The alternative — land the config
first and let the tree stay red for a dozen commits — is faster to write and
worse to bisect.

### Risk on steps 9–11

The 164 docstrings are the bulk of this work and the part most likely to go
wrong. 103 are `D102` on methods. The failure mode is `"""Return the
config."""` filler that dilutes the genuinely good why-focused prose already in
`execute.py` and `docs/notes.md`. Each docstring must say why the thing exists
or what it guarantees, not restate its signature. Where a method truly has
nothing to add, raise it rather than padding it.

## Verification

`uv run pytest` after every commit. Before declaring the work done:

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -m ""          # including the docker-marked tests
```

Once tox lands (step 15), the same gates are one command — and it is the
command CI runs, so a green `tox` locally means a green CI:

```sh
tox                          # 3.14 + lint + type
tox -e docker                # opt-in, needs a daemon
```

Then the Docker hygiene check from CLAUDE.md, since `pytest -m ""` starts
containers:

```sh
docker images --filter 'label=dev.jormungandr.managed=true' -q | wc -l
docker ps -a --filter 'label=dev.jormungandr.session' -q | wc -l
```

## Deferred

- **mypy over `tests/`.** Strict on `src` is 33 errors; adding `tests/` is
  +256. Doing both in one change guarantees blanket `# type: ignore`. Worth
  doing later behind `[[tool.mypy.overrides]]` with `disallow_untyped_defs` and
  `disallow_untyped_calls` disabled, which is the setting that surfaces the
  useful signal — whether the fakes have drifted from the real signatures — at
  around 110–150 errors rather than 256.
- **A coverage threshold.** `pytest-cov` reports; nothing fails on a number.
  Setting `fail_under` against a baseline nobody has measured would either be
  meaningless or block immediately. Pick one once a real figure exists.
- **`uv check` runs `ty`, not mypy.** A contributor typing the obvious command
  gets a different type checker with different answers. Step 17 names the real
  commands in CLAUDE.md; that is the mitigation.
- **`pyarrow`** is declared as a dependency and imported nowhere. It constrains
  the Python floor (no cp315 wheels) for no benefit. Removing it is its own
  concern, and wants a check of whether the trace-reading boundary described in
  `docs/notes.md` is expected to need it.
- **Python 3.15.** Blocked on `pyarrow` cp315 wheels for as long as `pyarrow`
  stays declared.
