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
# D1xx (missing docstrings) applies to the public surface only. Docstring
# *style* rules still apply here — see "Docstrings: public surface only".
"src/jormungandr/runtime/**" = ["D1"]

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
      --junitxml=junit.xml -o junit_family=legacy \
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

And `.github/workflows/check.yml`:

```yaml
name: Run Checks and Tests

on:
  push:
  pull_request:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  tox:
    runs-on: ubuntu-latest
    name: Tox
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          version: "latest"
          enable-cache: true
      - name: Install tox
        run: uv tool install --python-preference only-managed --python 3.14 tox --with tox-uv --with tox-gh
      - name: Run Tox
        run: tox
      - name: Upload coverage reports to Codecov
        uses: codecov/codecov-action@v5
        with:
          token: ${{ secrets.CODECOV_TOKEN }}
          slug: AletheiaResearch/jormungandr
      - name: Upload test results to Codecov
        if: ${{ !cancelled() }}
        uses: codecov/test-results-action@v1
        with:
          token: ${{ secrets.CODECOV_TOKEN }}
          slug: AletheiaResearch/jormungandr
```

Three deliberate departures from the reference workflow:

- **`ubuntu-latest` with `setup-uv`'s own cache**, not Namespace runners with
  `nscloud-cache-action`. AletheiaResearch is not on that Namespace org, and a
  workflow naming a runner profile that does not exist never starts.
- **`pull_request` as well as `push`.** A gate that does not run on a pull
  request is not a gate. `concurrency` with `cancel-in-progress` stops a pushed
  PR branch from running the job twice.
- **No `env: TOX_RUNNER: "virtualenv"`.** The reference sets it, which bypasses
  `uv-venv-lock-runner` — plausibly to cooperate with the Namespace cache.
  Since that cache is not in use here, CI keeps the lock runner and therefore
  the `--locked` enforcement that is half the argument for tox. The
  `{env:TOX_RUNNER:…}` indirection stays in `tox.ini` so it remains overridable
  without editing config.

`--junitxml=junit.xml -o junit_family=legacy` in the base testenv is not
optional decoration: it is the input `codecov/test-results-action` consumes.
`--cov-report=xml` likewise feeds `codecov-action`.

Codecov requires `AletheiaResearch/jormungandr` to be onboarded with a
`CODECOV_TOKEN` repository secret. That is already in place.

The README gains the coverage badge, as the reference repo has on line 1.

### Docstrings: public surface only

Selecting `D` wholesale demanded 164 new docstrings. Reading what they would
actually say killed that: **75 of the 164 were on symbols where prose adds
nothing** — 15 `render()` methods on single-operation instruction dataclasses,
12 `identity()` implementations of a Protocol whose contract
(`modules/base.py:123-129`) is already stated in full, 22 one-line derived
properties (`ok`, `failed`, `reference`, `digest`), 17 one-line delegations to
`DockerCli`, 5 registry dunders, 4 singletons.

Writing those produces precisely the `"""Return the config."""` filler this
spec names as the failure mode, and it would dilute the genuinely good
why-focused prose that is the repo's actual asset.

So `D1xx` is scoped to the public surface — everything except
`src/jormungandr/runtime/**`, which is the internal machinery: image building,
container lifecycle, the module registry, Dockerfile instruction rendering.
What remains is the CLI, the config models, and `execute.py`, which is what
`__all__` exports and what a library caller touches.

**Docstring style rules are not narrowed.** Only the `D1` prefix is ignored, so
`D2xx`/`D3xx`/`D4xx` still hold every docstring in `runtime/` to the house
standard — 6 of the 11 style findings are in `runtime/` and still get fixed.

Measured effect: **164 missing docstrings → 23**, across 8 files
(`execute.py` 6, `config/models.py` 5, `cli_helpers.py` 4, `config/providers.py`
2, `config/prompts.py` 2, `__init__.py` 2, `cli.py` 1, `__main__.py` 1). Total
`ruff check` goes from 249 to **108**.

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
| `ruff check` total | **108** |
| — of which, missing docstrings (`D1xx`) | 23 |
| — of which, docstring style (`D2xx`/`D3xx`/`D4xx`) | 11 |
| — of which, substantive | 74 (29 auto-fixable, 45 hand edits) |
| `ruff format` | 25 of 39 files, ~556 changed lines |
| `mypy --strict src` (with `types-PyYAML`) | **33** in 10 files |
| `mypy --strict src tests` | 289 in 19 files |

Two earlier drafts of this table were wrong, both in the same direction —
counting work the configuration already removes:

- **260 / 85 substantive** was measured with ruff's default `max-args = 5`
  while this spec prescribes `max-args = 8`, counting 11 `PLR0913` findings the
  config silences. Corrected: 249 / 74.
- **164 docstrings** predates scoping `D1xx` to the public surface.
  Corrected: 23, and the total drops 249 → 108.

The figures above are what `ruff check` prints with the configuration as
specified. Both corrections came from running the config rather than reasoning
about it, which is the point.

The `src tests` figure requires `--with pytest` on the `uvx` invocation;
without it mypy cannot resolve `import pytest` and the count inflates to 320.

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
| 1 | `chore: ignore tool caches and coverage artifacts` | `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`, `.tox/`, plus `.coverage`, `coverage.xml`, `htmlcov/`, `junit.xml`. The first four are untracked *and* unignored today; the rest arrive with step 15. CLAUDE.md records `git add -A` having twice swept unrelated work into a commit. |
| 2 | `fix(execute): pass -- to git ls-remote and validate clone_url` | test first |
| 3 | `fix(execute): assign record ids before use` | test first |
| 4 | `fix(runtime): restore signal handlers Python did not install` | test first |
| 5 | `fix: import the names the annotations reference` | the three `F821` |
| 6 | `refactor(runtime): widen tuple annotations to tuple[str, ...]` | kills 5 `src` + 18 `tests` mypy errors and 14 `# type: ignore` |
| 7 | `style: format with ruff` | 25 files, nothing else in the commit. Run `ruff check --select I --fix` *before* `ruff format`; that order converges, the reverse needs a second format pass. |
| 8 | `fix: clear the substantive ruff findings` | the 74 — 29 auto-fixed, 45 by hand |
| 9 | `docs: document the public API` | the 23 docstrings + the 11 style fixes |
| 10 | `fix(runtime): drop the Field() call that only sets a default` | `spec.py:117` only — see below |
| 11 | `build!: require Python 3.14` | `requires-python`, `.python-version`, relock. Breaking, hence `!` |
| 12 | `build: add ruff and mypy configuration` | lands green |
| 13 | `build: run the gates under tox` | `tox.ini`, `test`/`lint`/`type` dependency groups, `pytest-cov` |
| 14 | `ci: run tox on push and pull request` | `.github/workflows/check.yml` with both Codecov uploads; makes `[gh.python]` live |
| 15 | `docs: add the coverage badge and record the tox commands` | README badge, CLAUDE.md commands |

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

### The house style the 23 docstrings are held to

Extracted from `execute.py` and `docs/notes.md`, which are where this repo's
prose is at its best:

1. **The summary line is one imperative sentence naming the job**, not the
   return value. Across all of `src` exactly two docstrings begin with
   "Return", and both are Protocol contracts where naming the return *is* the
   job.
2. **A body is warranted when there is a plausible alternative the reader would
   otherwise assume**, and the body's job is to kill it. The shape is always
   *chosen thing* → **rather than** → *obvious thing* → **because** → *the
   concrete failure*.
3. **Name the failure mode in units, not adjectives.** "a run of two hundred
   records against one repository otherwise makes two hundred identical network
   calls" — never "this is important for correctness". Same standard CLAUDE.md
   sets for commit messages.
4. **State the boundary**: what the thing deliberately does *not* do, and where
   that responsibility goes instead.
5. **Never restate the signature.** No `Args:`, `Returns:`, `Raises:` sections,
   ever. A grep for them across `src` returns zero matches today; types live in
   annotations, and pydantic field semantics live in the field's own attribute
   docstring.

Note that rule 1 has a cost: 6 of the 7 `D401` findings are noun-phrase
summaries, which is the house voice, not sloppiness. Selecting `D` commits the
repo to imperative summaries. Step 9's commit message should say so, or a later
reader will "restore" them.

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

Three further defects were found and reproduced while verifying the three
above. None is in scope here — each is its own concern with its own failure
mode, and CLAUDE.md is explicit about not widening a change to sweep them in.
They are recorded so they are not lost.

- **`ContainerRuntime` never uninstalls its signal handlers.** Measured: five
  runtimes constructed and garbage-collected leave five `atexit` callbacks and
  a five-deep handler chain, of which four levels do nothing — their weakrefs
  are dead. `_install_handlers` has no inverse, the class is not a context
  manager, and merely *constructing* one permanently repoints the interpreter's
  SIGINT and SIGTERM. Bounded today (the CLI builds one per run) but a real
  library-use defect. It also leaks into the pytest process via
  `tests/runtime/test_container.py:315`.
- **An inherited `SIG_IGN` is overridden.** Launched under `nohup` or a
  supervisor that ignores SIGTERM, a SIGTERM now tears down every container,
  then correctly honours the inherited `SIG_IGN` and does *not* kill the
  process — which keeps running with its containers silently gone. POSIX
  convention is not to handle a signal inherited as `SIG_IGN`. One-line guard.
- **Duplicate record ids are unchecked in `execute(records=...)`.** Two records
  sharing an id silently share one output directory, and the `shutil.rmtree` at
  `execute.py:261` means one clobbers the other. `load_prompts` guards this
  (`config/prompts.py:339-343`); `execute()` does not. Pre-existing, and step 3
  does not introduce it — but step 3's derived `prompt-NNNN` ids can now
  collide with an explicitly supplied one.

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
