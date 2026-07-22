# Lint and Type Checking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt Ruff (lint + format) and mypy as blocking gates, run under tox and GitHub Actions with Codecov, fixing the three defects the gates expose first.

**Architecture:** Fifteen commits, one concern each, every one independently green. Defects first (test-first, reproduced before fixed), then mechanical cleanup, then documentation, then the Python floor bump, then configuration last so the gate is green the moment it exists.

**Tech Stack:** ruff 0.15.22, mypy 2.3.0, tox 4.24 + tox-uv + tox-gh, pytest + pytest-cov, uv 0.11.23, Python 3.14.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-22-lint-and-typecheck-design.md`. It is authoritative; this plan implements it.
- Commit author is Nejc Drobnic <nejc@nejc.dev>. **No co-author trailers.**
- Stage explicitly — never `git add -A`. It has twice swept unrelated work into a commit here.
- Commit messages say *why*, and name the failure mode the change prevents.
- Use Read + Edit for file changes. Never `sed`, `perl`, `python -c`, or a heredoc'd script.
- Run `uv run pytest` after every commit. Baseline before any change: **419 passed, 38 deselected**.
- Docker-marked tests are opt-in and are not run here (no daemon required by any task).
- Until Task 12 lands the config, verify ruff with the scratch config:
  `uvx ruff@latest check . --config <scratch>/ruff.toml --no-cache`
- Do not add capability that was not asked for. Three further defects are listed in the spec's Deferred section — leave them there.

---

### Task 1: Ignore tool caches and coverage artifacts

**Files:** Modify `.gitignore`

- [ ] **Step 1: Confirm the directories are unignored**

```bash
for d in .ruff_cache .mypy_cache .pytest_cache .tox; do
  printf "%-14s " "$d"; git check-ignore -q "$d" && echo ignored || echo "NOT ignored"; done
```
Expected: all four `NOT ignored`.

- [ ] **Step 2: Append to `.gitignore`**

```
.ruff_cache/
.mypy_cache/
.pytest_cache/
.tox/

.coverage
coverage.xml
htmlcov/
junit.xml
```

- [ ] **Step 3: Verify**

Run: `git check-ignore -q .ruff_cache && echo ok`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit
```
Message names the failure mode: these are untracked *and* unignored, and `git add -A` has twice swept unrelated files into a commit in this repo.

---

### Task 2: Stop a clone_url becoming a git option

**Files:**
- Modify: `src/jormungandr/execute.py` (the `subprocess.run` in `resolve_commit`)
- Modify: `src/jormungandr/config/prompts.py` (new `CLONE_URL` pattern + validator)
- Modify: `tests/config/test_execute.py` (injection test), `tests/config/test_config.py` (validation tests + 9 placeholder urls)
- Modify: `docs/config-spec.md` (clone_url description)

**Interfaces:**
- Produces: `CLONE_URL` regex in `config/prompts.py`; `GitSource._validate_clone_url`.

- [ ] **Step 1: Write the injection test**

In `tests/config/test_execute.py`, in class `TestRefResolution`, after `test_an_unresolvable_repo_is_a_clear_error`:

```python
    def test_a_clone_url_cannot_become_a_git_option(self, tmp_path: Path) -> None:
        # `git ls-remote <url> <ref>` with no `--` lets a url beginning with a
        # dash be parsed as an option, and `--upload-pack=<cmd>` executes <cmd>
        # on the host. clone_url comes straight out of prompts.jsonl.
        #
        # The assertion is the side effect, not the message: git exits non-zero
        # either way, so only the absence of the file proves nothing ran.
        from jormungandr.execute import ExecutionError, resolve_commit

        sentinel = tmp_path / "pwned.txt"
        # `sh -c '...'` rather than a bare `touch`: git appends the repository
        # name to the upload-pack command, so a bare touch also creates a file
        # called HEAD in the working directory. Here it lands harmlessly in $0.
        payload = f"--upload-pack=sh -c 'touch {sentinel}'"
        resolve_commit.cache_clear()
        try:
            with pytest.raises(ExecutionError):
                resolve_commit(payload, None)
        finally:
            resolve_commit.cache_clear()

        assert not sentinel.exists(), (
            f"git executed the injected command: {sentinel} was created"
        )
```

- [ ] **Step 2: Run it — it must fail because the file EXISTS**

Run: `uv run pytest tests/config/test_execute.py::TestRefResolution::test_a_clone_url_cannot_become_a_git_option -q`
Expected: `AssertionError: git executed the injected command: …/pwned.txt was created`

- [ ] **Step 3: Add the `--` separator**

`src/jormungandr/execute.py`, in `resolve_commit`:

```python
        proc = subprocess.run(
            # `--` is load-bearing: both operands come from prompts.jsonl, and
            # without it a clone_url of `--upload-pack=<cmd>` is parsed as an
            # option rather than a repository — which runs <cmd> on this host.
            # GitSource rejects such a url too; this is the half that does not
            # depend on the value having gone through the model.
            ["git", "ls-remote", "--", clone_url, target],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
```

- [ ] **Step 4: Verify the injection test passes**

Run: same command as Step 2. Expected: `1 passed`.

- [ ] **Step 5: Write the validation tests**

In `tests/config/test_config.py`, class `TestGitSource`, after `test_unknown_git_key_rejected` — both the positive and negative cases. Without the positive cases the regex could be `^$` and the negative test would still pass.

```python
    @pytest.mark.parametrize(
        "good",
        [
            "https://github.com/acme/app",
            "https://github.com/acme/app.git",
            "http://git.internal/acme/app.git",
            "git://git.kernel.org/pub/scm/git/git.git",
            "ssh://git@git.example.com:2222/team/mono.git",
            "git@git.example.com:team/mono.git",
            "https://user:token@git.example.com/team/mono.git",
        ],
    )
    def test_real_git_urls_are_accepted(self, good: str) -> None:
        record = PromptRecord(prompt="x", git={"clone_url": good})
        assert record.workspace.git.clone_url == good

    @pytest.mark.parametrize(
        "bad",
        [
            # An argument beginning with a dash is an *option* to every git
            # subcommand, and `--upload-pack=<cmd>` executes <cmd>.
            "--upload-pack=touch /tmp/pwned",
            "-u/bin/sh",
            # `ext::` is a transport that runs a command by design.
            "ext::sh -c 'touch /tmp/pwned'",
            # clone_url is interpolated unquoted into a `RUN git remote add
            # origin <url>` line in the workspace Dockerfile.
            "https://h/r; touch /tmp/pwned",
            "https://h/r && touch /tmp/pwned",
            "https://h/r$(touch /tmp/pwned)",
            "https://h/r`touch /tmp/pwned`",
            # No scheme at all: not a git url, and previously accepted.
            "u",
            "/etc/passwd",
            "file:///etc",
        ],
    )
    def test_a_clone_url_that_is_not_a_git_url_is_rejected(self, bad: str) -> None:
        # A prompts.jsonl is data. Rejecting here names the offending line at
        # load time instead of handing the string to `git` inside a worker.
        with pytest.raises(ValidationError, match="clone_url"):
            PromptRecord(prompt="x", git={"clone_url": bad})
```

- [ ] **Step 6: Run them — 10 must fail with DID NOT RAISE**

Run: `uv run pytest "tests/config/test_config.py::TestGitSource::test_a_clone_url_that_is_not_a_git_url_is_rejected" -q`
Expected: `Failed: DID NOT RAISE ValidationError`, 10 failed.

- [ ] **Step 7: Add the pattern and validator**

`src/jormungandr/config/prompts.py`, after the `RECORD_ID` constant:

```python
CLONE_URL = re.compile(
    r"^(?:(?:https?|ssh|git)://|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:)"
    r"[A-Za-z0-9._:/@%+=-]+$"
)
"""A clone url reaches two places that treat a bare string as code.

``resolve_commit`` passes it to ``git ls-remote`` as a positional argument, so
a value beginning with ``-`` is an *option* — and ``--upload-pack=<cmd>`` runs
``<cmd>`` on this host. ``compose_workspace`` interpolates it, unquoted, into a
``RUN git remote add origin <url>`` line, so ``;``, ``&&``, backticks and
``$( )`` run as root inside the build. Both call sites are hardened
independently, but a prompts.jsonl is data and the string should never have got
that far.

Hence an allowlist rather than a denylist: a scheme git can actually fetch
(``http``, ``https``, ``ssh``, ``git``) or scp-like ``user@host:path``,
followed only by characters that are inert to a shell. Deliberately excluded:
``ext::``, which is a transport whose entire purpose is running a command;
``file://`` and bare local paths, which have been meaningless since the clone
moved inside the image; and ``?``, ``~``, ``!`` and ``#``, which no real
remote needs and every shell treats specially."""
```

In `class GitSource`, immediately before `_validate_relative`:

```python
    @field_validator("clone_url")
    @classmethod
    def _validate_clone_url(cls, value: str) -> str:
        """Refuse anything that is not a fetchable, shell-inert git url.

        Checked here so a bad line is named at load time — ``load_prompts``
        reports ``line N`` — rather than reaching ``git ls-remote`` inside a
        worker thread, where the same string is an argument vector.
        """
        cleaned = value.strip()
        if not CLONE_URL.fullmatch(cleaned):
            raise ValueError(
                f"clone_url must be an http(s), ssh or git url, or "
                f"user@host:path; got {value!r}"
            )
        return cleaned
```

And correct the now-false field docstring:

```python
    clone_url: str = Field(min_length=1)
    """A git URL: ``http(s)://``, ``ssh://``, ``git://``, or ``user@host:path``.

    Not a host path. The clone happens inside the workspace image, so a host
    path would resolve against the build container's filesystem and find
    nothing."""
```

- [ ] **Step 8: Fix the 9 placeholder urls in existing tests**

Five existing tests break because they pass `clone_url: "u"`. This is expected fallout and belongs in this commit. Add at module level in `tests/config/test_config.py`, before `class TestGitSource`:

```python
# Real urls, not placeholders: clone_url is validated, because it reaches
# `git ls-remote` as an argument vector and a `RUN` line as shell text.
URL = "https://git.example.com/team/mono.git"
OTHER_URL = "https://git.example.com/team/other.git"
```

Then replace every `"clone_url": "u"` with `"clone_url": URL` and every `"clone_url": "other"` with `"clone_url": OTHER_URL` throughout `TestGitSource`. Note `test_parent_traversal_is_rejected` and `test_unknown_git_key_rejected` pass either way but must still be updated, or they silently become tests of the wrong thing.

- [ ] **Step 9: Update `docs/config-spec.md`** so the documented clone_url matches the validator.

- [ ] **Step 10: Run the full suite**

Run: `uv run pytest`
Expected: `437 passed, 38 deselected` (419 + 18 new).

- [ ] **Step 11: Commit**

```bash
git add src/jormungandr/execute.py src/jormungandr/config/prompts.py \
        tests/config/test_execute.py tests/config/test_config.py docs/config-spec.md
git commit
```
Failure mode for the message: a `clone_url` of `--upload-pack=…` in `prompts.jsonl` executes a command on the host while the run reports a failure.

---

### Task 3: Assign record ids before use

**Files:** Modify `src/jormungandr/execute.py`, `tests/config/test_execute.py`

- [ ] **Step 1: Write the tests** — new class `TestRecordsWithoutIds` at the end of `tests/config/test_execute.py`, five tests, reusing the module's existing `project`, `load`, `ENV`, `FakeBuilder`, `FakeRunner` fixtures. Full text is in the spec's companion notes; the five are:
  `test_a_record_without_an_id_still_runs`, `test_one_record_without_an_id_does_not_lose_the_other`, `test_ids_derived_here_match_the_ones_load_prompts_derives`, `test_a_supplied_id_is_never_overwritten`, `test_the_derived_id_reaches_the_per_record_summary`.

- [ ] **Step 2: Run — all five must fail**

Run: `uv run pytest tests/config/test_execute.py::TestRecordsWithoutIds -q`
Expected: `TypeError: unsupported operand type(s) for /: 'PosixPath' and 'NoneType'` at `src/jormungandr/execute.py:257`, 5 failed.

- [ ] **Step 3: Assign ids at the boundary**

In `execute()`, immediately after the `if not prompts: raise` guard:

```python
    # `id` is optional on a record, and load_prompts is the only thing that
    # ever fills it in — so `records=` supplied by a caller arrives with ids of
    # None. `output_dir / record.id` then raised TypeError inside a worker and
    # future.result() re-raised it: every record still ran and paid for its
    # container, but the run report was never written and the caller got a
    # TypeError instead of a result. Derived with load_prompts' own scheme, so
    # a record run this way lands where the file-driven run would have put it.
    prompts = tuple(
        record.with_id(f"prompt-{index:04d}") for index, record in enumerate(prompts)
    )
```

This must land **before** the reserved-name check and the `order` map, both of which read `record.id`. Do not edit line 257 — it becomes unreachable with a `None` id.

- [ ] **Step 4: Verify** — `uv run pytest tests/config/test_execute.py::TestRecordsWithoutIds -q` → `5 passed`; then `uv run pytest` → 442 passed.

- [ ] **Step 5: Commit.** Failure mode: `execute(config, records=[...])` without ids raises inside a worker; every container still runs and bills, but the run report is never written and the caller gets a `TypeError` instead of a result.

---

### Task 4: Restore signal handlers Python did not install

**Files:** Modify `src/jormungandr/runtime/container.py`, `tests/runtime/test_container.py`

- [ ] **Step 1: Write the tests** — a `restore_signal_handlers` fixture (listed **first** in each signature so it tears down after `monkeypatch`), plus class `TestSignalHandlerRestore` with four tests, one of which runs the repro out of process via `subprocess.run` because it rebuilds the signal module's handler table.

- [ ] **Step 2: Run — 3 of 4 must fail**

Run: `uv run pytest tests/runtime/test_container.py::TestSignalHandlerRestore`
Expected: `TypeError: signal handler must be signal.SIG_IGN, signal.SIG_DFL, or a callable object`, 3 failed 1 passed.

- [ ] **Step 3: Normalise at the source**

`src/jormungandr/runtime/container.py`, in `_install_handlers`:

```python
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                # Only the main thread may install handlers.
                #
                # `getsignal` reports None — not SIG_DFL — for a handler Python
                # did not install, which is what an embedder or a C extension
                # that called sigaction() before the signal module built its
                # table leaves behind. Storing that None would put a value
                # `signal.signal` rejects into `previous`, and `.get`'s default
                # cannot save us because the key is present: the restore inside
                # `handle` would raise TypeError, `os.kill` would never run, and
                # the process would die of an unhandled exception instead of the
                # signal. Normalise here so `previous` only ever holds something
                # that can be restored.
                current = signal.getsignal(signum)
                previous[signum] = signal.SIG_DFL if current is None else current
                signal.signal(signum, handle)
```

Leave the `.get(signum, signal.SIG_DFL)` read site alone — its default is correct for the genuinely-absent-key case, which happens when `signal.signal` raised `ValueError` off the main thread and `contextlib.suppress` swallowed it. Do **not** use `previous.get(signum) or signal.SIG_DFL`: that works only because `SIG_DFL == 0` is falsy, a numeric accident.

- [ ] **Step 4: Verify** — `4 passed`; then `uv run pytest` → 446 passed.

- [ ] **Step 5: Commit.** Failure mode: `getsignal` returns `None`, `signal.signal(signum, None)` raises inside the SIGINT handler, `os.kill` is never reached, and the shell sees exit 1 with a traceback instead of death by signal.

---

### Task 5: Import the names the annotations reference

**Files:** Create `tests/test_annotations.py`; modify `src/jormungandr/execute.py`, `tests/runtime/test_docker_integration.py`

- [ ] **Step 1: Write `tests/test_annotations.py`** — a guard that walks every `src` and `tests` module and calls `typing.get_type_hints` on every function the module itself defines. Two details are load-bearing: filter on `target.__module__ == module.__name__` (or re-exported `pydantic.Field` produces false positives via `JsonValue`), and use `inspect.unwrap` (to reach through `@pytest.fixture` over `@staticmethod`).

- [ ] **Step 2: Run — 2 of 39 must fail**

Run: `uv run pytest tests/test_annotations.py -q`
Expected: `2 failed, 37 passed`, naming `execute._image_for`, `execute._run_one` (`Any`) and `TestExecuteEndToEnd.project` (`Path`).

- [ ] **Step 3: Add the two imports** — `from typing import Any` in `src/jormungandr/execute.py`; `from pathlib import Path` in `tests/runtime/test_docker_integration.py`. Both are isort-correct in those positions.

- [ ] **Step 4: Verify** — `39 passed`, and `uvx ruff@latest check . --select F821` → `All checks passed!`

- [ ] **Step 5: Commit.** Failure mode: `NameError` the moment `get_type_hints()` runs; masked today by `from __future__ import annotations`, and the test-file one sits in the docker-deselected file so nobody has looked.

---

### Task 6: Widen tuple annotations to tuple[str, ...]

**Files:** Modify `src/jormungandr/runtime/invocation.py`, `src/jormungandr/runtime/modules/installers.py`, `tests/runtime/test_run.py`

**Note:** five sites, not the three the first draft of the spec claimed. There is no pytest test for this — runtime behaviour is byte-identical and the Protocols are `@runtime_checkable`, which checks attribute *presence*, never type. The mypy count is the proof. Do not write a test asserting `__annotations__`; it would be tautological.

- [ ] **Step 1: Record the baseline**

Run: `uvx --with pydantic --with cyclopts --with pyyaml --with pyarrow --with types-PyYAML mypy --strict src`
Expected: `Found 31 errors in 10 files` (33 minus the 2 from Task 5).

- [ ] **Step 2: Annotate all five sites**

```python
# src/jormungandr/runtime/invocation.py, class OpenCodeInvocation
    # Annotated, not inferred: a bare literal infers as tuple[str], a
    # fixed-length type that does not satisfy the protocol's tuple[str, ...].
    state_paths: tuple[str, ...] = (".local/share/opencode",)

# src/jormungandr/runtime/invocation.py, class DroidInvocation
    state_paths: tuple[str, ...] = (".factory/sessions", ".factory/logs")

# src/jormungandr/runtime/modules/installers.py, class NpmGlobal
    # Annotated, not inferred: a bare literal infers as tuple[str], a
    # fixed-length type that does not satisfy the protocol's tuple[str, ...].
    default_requires: tuple[str, ...] = ("node",)

# src/jormungandr/runtime/modules/installers.py, class GitPythonApp
    default_requires: tuple[str, ...] = ("python",)

# tests/runtime/test_run.py, class TestRunnerForwarding.Recording
        state_paths: tuple[str, ...] = ()
```

Leave the three Protocol declarations and `ShellInstall.default_requires` alone — already correct.

- [ ] **Step 3: Verify the delta** — `mypy --strict src` → `Found 27 errors in 9 files` (−4). With `--with pytest … mypy --strict src tests` → 264 (−22, i.e. 4 src + 18 tests).

- [ ] **Step 4: Run the suite** — `uv run pytest` → unchanged count, 0 failures.

- [ ] **Step 5: Commit.** Failure mode: a Protocol attribute is mutable and therefore invariant, so a bare literal inferring as fixed-length `tuple[str]` makes every implementation silently non-substitutable. Do **not** claim this removes `# type: ignore` comments — it removes zero; the 14 `[attr-defined]` ignores come from untyped provider mappings and die with a different change.

---

### Task 7: Format with ruff

**Files:** 25 of 39 `.py` files

- [ ] **Step 1: Fix import order first** — `uvx ruff@latest check . --config <scratch>/ruff.toml --select I --fix`. This order converges; the reverse needs a second format pass.
- [ ] **Step 2: Format** — `uvx ruff@latest format .` → `25 files reformatted, 14 files left unchanged`.
- [ ] **Step 3: Verify** — `uv run pytest` → same count, 0 failures.
- [ ] **Step 4: Commit** — nothing but formatting in this commit.

---

### Task 8: Clear the substantive ruff findings

**Files:** 20 files across `src` and `tests`. 74 findings: 29 auto-fixable, 45 hand edits.

- [ ] **Step 1: Three hand edits BEFORE `--fix`** — RUF100's fix deletes the whole trailing comment including prose. Repoint `cli_helpers.py`'s `# noqa: BLE001` to `S112`, and strip the `noqa:` prefix (keeping the prose) from the two in `execute.py`.
- [ ] **Step 2: Auto-fix** — `uvx ruff@latest check . --config <scratch>/ruff.toml --fix`. Expect the `builtin.py` isort fix to split the aliased import into a second statement; that is `combine-as-imports = false`, not a bug.
- [ ] **Step 3: The 45 hand edits**, by category:
  - **2 × N818 renames**: `MissingExtra` → `MissingExtraError` (3 sites), `DockerNotAvailable` → `DockerNotAvailableError` (6 sites). Both fully internal — no `except` clause names either, verified by grep.
  - **2 × S101**: replace `assert` with a raise in `config/providers.py:122` (do **not** interpolate `api_key` — that class exists to keep it out of error text) and `runtime/docker.py:190`.
  - **4 × S603 noqa** naming why the input is trusted. `execute.py`'s must name the `--` and the validator, not claim trust.
  - **3 × PLR0913 noqa** (the survivors of `max-args = 8`), **2 × PLR0912 noqa**, **1 × PLR0911** merge.
  - **9 × ARG002 noqa** — all structural protocol conformance; none removable. `invocation.py:93` needs the signature expanded, because the diagnostic is on the parameter line and a `noqa` on `def build(` yields RUF100 while ARG002 still fires.
  - **1 × ARG001**: rename `frame` → `_frame` in the `container.py` handler (coordinate with Task 4).
  - **1 × PLR2004**: named constant `_SHA256_HEX_LENGTH = 64`.
  - **2 × B017**: `pytest.raises(Exception)` → `TypeError, match="airgapp"` and `ValidationError, match="model"`.
  - **2 × RUF043** raw strings, **2 × S105** renames, **1 × E741**, **1 × SIM117**, **1 × F841**, **1 × S110 noqa**, **5 × PLW1510** explicit `check=False`.
- [ ] **Step 4: Verify** — ruff reports only `D` findings remaining; `uv run pytest` → same count, 0 failures.
- [ ] **Step 5: Commit.**

---

### Task 9: Document the public API

**Files:** `src/jormungandr/execute.py` (6), `config/models.py` (5), `cli_helpers.py` (4), `config/providers.py` (2), `config/prompts.py` (2), `__init__.py` (2), `cli.py` (1), `__main__.py` (1) — 23 `D1xx`; plus the 11 `D2xx`/`D3xx`/`D4xx` style fixes, 6 of which are in `runtime/`.

Held to the house style in the spec: imperative summary naming the job; a body only when it kills an assumption the reader would otherwise make; failure modes in units not adjectives; state the boundary; never restate the signature.

- [ ] **Step 1: Write the 23 docstrings.**
- [ ] **Step 2: Fix the 11 style findings** — 7 `D401` need imperative rewording, 1 `D205`+`D209` in `cli.py`, 1 `D400`, 1 `D301` (needs `r"""`).
- [ ] **Step 3: Verify** — ruff `--select D` → `All checks passed!`; `uv run pytest` (an unbalanced `"""` is a collection error).
- [ ] **Step 4: Commit.** Say in the message that `D401` commits the repo to imperative summaries, so a later reader does not "restore" the noun phrases.

---

### Task 10: Drop the Field() call that only sets a default

**Files:** `src/jormungandr/runtime/spec.py:117`

- [ ] **Step 1:** `tier_split: int = Field(default=20)` → `tier_split: int = 20`. **Only this line.** Lines 69/70/72 alternate with neighbours that must keep `Field()` for `gt=0`; changing them makes the block read worse for a rule nothing now enforces.
- [ ] **Step 2:** `uv run pytest` → unchanged. **Step 3:** Commit.

---

### Task 11: Require Python 3.14

**Files:** `pyproject.toml`, `.python-version`, `uv.lock`

- [ ] **Step 1:** `requires-python = ">=3.14"`; `.python-version` → `3.14`.
- [ ] **Step 2:** `uv lock` then `uv sync`.
- [ ] **Step 3:** `uv run python -V` → `3.14.x`; `uv run pytest` → same count, 0 failures.
- [ ] **Step 4: Commit** as `build!:` — breaking. Verified beforehand: every dependency has cp314 wheels, and both gates are invariant across the bump.

---

### Task 12: Add ruff and mypy configuration

**Files:** `pyproject.toml`

- [ ] **Step 1:** Add `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.lint.per-file-ignores]`, `[tool.ruff.lint.pydocstyle]`, `[tool.ruff.lint.pylint]`, `[tool.mypy]` exactly as in the spec, and add `ruff`, `mypy`, `types-PyYAML` to the `dev` group.
- [ ] **Step 2: Verify it lands green**

```bash
uv run ruff check          # All checks passed!
uv run ruff format --check # 39 files already formatted
uv run mypy                # Success: no issues found
uv run pytest
```

- [ ] **Step 3: Commit.**

---

### Task 13: Run the gates under tox

**Files:** `tox.ini`, `pyproject.toml` (dependency groups)

- [ ] **Step 1:** Split `dev` into `test` / `lint` / `type` groups composed via `include-group`; add `pytest-cov` to `test`.
- [ ] **Step 2:** Write `tox.ini` exactly as in the spec. `docker` stays outside `env_list`.
- [ ] **Step 3: Verify** — `tox l` lists the envs; `tox` runs 3.14 + lint + type green; `tox -e docker -- --collect-only` collects 38.
- [ ] **Step 4: Commit.** Failure mode: bare `uv run` silently rewrites `uv.lock` on drift; `uv-venv-lock-runner` passes `--locked` and refuses.

---

### Task 14: Run tox on push and pull request

**Files:** `.github/workflows/check.yml`

- [ ] **Step 1:** Write the workflow exactly as in the spec — `ubuntu-latest`, `setup-uv` with `enable-cache: true`, `uv tool install … --python 3.14 tox --with tox-uv --with tox-gh`, bare `tox`, then both Codecov actions with `slug: AletheiaResearch/jormungandr`.
- [ ] **Step 2:** Do **not** set `TOX_RUNNER: virtualenv` — that would bypass the lock runner.
- [ ] **Step 3:** Validate the YAML parses. **Step 4:** Commit.

---

### Task 15: Add the coverage badge and record the commands

**Files:** `README.md`, `CLAUDE.md`

- [ ] **Step 1:** Codecov badge at the top of `README.md`.
- [ ] **Step 2:** Record `tox`, `tox -e docker`, `tox -e lint`, `tox -e type` in `CLAUDE.md`, and note that `uv check` runs `ty`, not mypy — a contributor typing the obvious command gets a different type checker with different answers.
- [ ] **Step 3:** Commit.

---

## Final verification

```sh
tox                       # 3.14 + lint + type
tox -e docker             # opt-in; needs a daemon
docker images --filter 'label=dev.jormungandr.managed=true' -q | wc -l
docker ps -a --filter 'label=dev.jormungandr.session' -q | wc -l
```
