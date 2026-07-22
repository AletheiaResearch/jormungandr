"""Run a config file end to end.

Joins the two halves that already exist: a :class:`~jormungandr.config.JormConfig`
compiles to an ``ImageSpec``, and :class:`~jormungandr.runtime.run.PromptRunner`
runs prompts in a container. This builds the image once, then runs every prompt
record against it.

Each record gets its own container and its own directory under ``output.dir``,
holding the workspace the agent worked in, its raw stdout/stderr, and whatever
session record the harness left behind. Nothing here reads or interprets those
files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jormungandr.config.models import JormConfig
from jormungandr.config.prompts import PromptRecord
from jormungandr.runtime.run import HarnessRun, PromptRunner
from jormungandr.runtime.spec import ContainerSpec, ResourceLimits

__all__ = [
    "ExecutionError",
    "ExecutionReport",
    "RecordResult",
    "env_file_names",
    "execute",
]

log = logging.getLogger(__name__)

RESERVED_NAMES = frozenset({"report.json"})
"""Names the run writes into output.dir itself."""


class ExecutionError(RuntimeError):
    """The run could not start."""


@dataclass(frozen=True, slots=True)
class RecordResult:
    id: str
    ok: bool
    run: HarnessRun | None
    directory: Path
    error: str | None = None

    @property
    def turns(self) -> tuple:
        return self.run.turns if self.run else ()


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    image: str
    results: tuple[RecordResult, ...]
    output_dir: Path

    @property
    def succeeded(self) -> tuple[RecordResult, ...]:
        return tuple(r for r in self.results if r.ok)

    @property
    def failed(self) -> tuple[RecordResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    @property
    def ok(self) -> bool:
        return not self.failed


def user_of(config: JormConfig) -> str:
    """The account the agent runs as, per the ``user`` module."""
    for declaration in config.image.modules:
        if declaration.get("name") == "user":
            return str(declaration.get("user") or DEFAULT_USER)
    return DEFAULT_USER


@lru_cache(maxsize=512)
def resolve_commit(clone_url: str, ref: str | None) -> str:
    """Turn a ref into a concrete revision.

    The workspace image is content-addressed, so it can only be honest about a
    fixed revision: caching a branch name would serve yesterday's code from
    today's tag. An already-resolved 40-character sha is taken as-is; anything
    else — including an omitted ref, which means the default branch — is
    resolved with ``git ls-remote`` so the digest names real content.

    Memoized per ``(url, ref)``: a run of two hundred records against one
    repository otherwise makes two hundred identical network calls, and worse,
    could resolve to different commits mid-run if the branch moved — records in
    the same run would then be testing different code.

    Public repositories only. No credentials are read or forwarded.
    """
    if ref and _FULL_SHA.fullmatch(ref):
        return ref
    target = ref or "HEAD"
    try:
        proc = subprocess.run(  # noqa: S603 - argv is fixed; `--` and GitSource.clone_url's validator stop clone_url being read as an option
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
    except subprocess.CalledProcessError as exc:
        raise ExecutionError(
            f"could not resolve {target!r} in {clone_url}: {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError(f"timed out resolving {target!r} in {clone_url}") from exc

    for line in proc.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if sha and (not ref or name.endswith(target) or name == target):
            return sha.strip()
    raise ExecutionError(f"{clone_url} has no ref matching {target!r}")


DEFAULT_WORKDIR = "/workspace"
DEFAULT_USER = "agent"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def workdir_of(config: JormConfig) -> str:
    """Where the agent actually works inside the container.

    Read from the ``workdir`` module rather than assumed: the module's path is
    configurable, and mounting the prepared workspace at a hardcoded
    ``/workspace`` while the agent runs somewhere else would hand it an empty
    directory and no error.
    """
    for declaration in config.image.modules:
        if declaration.get("name") == "workdir":
            return str(declaration.get("path") or DEFAULT_WORKDIR)
    return DEFAULT_WORKDIR


def _container_spec(config: JormConfig, image: str) -> ContainerSpec:
    return ContainerSpec(
        image=image,
        env=dict(config.run.env),
        env_files=tuple(str(p) for p in config.run.env_files),
        mounts=tuple(config.run.mounts),
        network=config.run.network,  # type: ignore[arg-type]
        limits=ResourceLimits(cpus=config.run.cpus, memory=config.run.memory),
    )


def _write_result(
    directory: Path, record: PromptRecord, run: HarnessRun | None, error: str | None
) -> None:
    """Persist the record's outcome as JSON plus raw per-turn output.

    stdout and stderr go to their own files rather than into the JSON: harness
    output is frequently large and occasionally not valid UTF-8-safe JSON
    content, and a summary you cannot open in a pager is not a summary.
    """
    summary = {
        "id": record.id,
        "ok": bool(run and run.ok) and error is None,
        "error": error,
        "turns": [
            {
                "index": turn.index,
                "exit_code": turn.exit_code,
                "duration": round(turn.duration, 3),
                "timed_out": turn.timed_out,
                "truncated": turn.truncated,
            }
            for turn in (run.turns if run else ())
        ],
        "state_paths": list(run.state_paths) if run else [],
    }
    (directory / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for turn in run.turns if run else ():
        if turn.stdout:
            (directory / f"turn-{turn.index}.stdout.txt").write_text(
                turn.stdout, encoding="utf-8"
            )
        if turn.stderr:
            (directory / f"turn-{turn.index}.stderr.txt").write_text(
                turn.stderr, encoding="utf-8"
            )


def _image_for(
    config: JormConfig,
    runtime_image: str,
    record: PromptRecord,
    builder: Any,
    directory: Path,
    platform: str,
) -> str:
    """The image this record runs from.

    A record with a repository gets its own workspace tier built on the runtime
    image, so the checkout is cached: a retried run reuses it instead of
    cloning again, and two records on the same commit share one image. A record
    without one runs the runtime image directly.
    """
    from jormungandr.runtime.compose import compose_workspace

    spec = record.workspace
    if spec is None or spec.type != "git" or spec.git is None:
        return runtime_image

    source = spec.git
    commit = resolve_commit(source.clone_url, source.ref)
    if not source.ref:
        log.info(
            "%s: %s default branch resolved to %s",
            record.id,
            source.clone_url,
            commit[:12],
        )
    layer = compose_workspace(
        parent=runtime_image,
        repository=config.image.repository,
        clone_url=source.clone_url,
        commit=commit,
        platform=platform,
        workdir=workdir_of(config),
        user=user_of(config),
        subdirectory=source.subdirectory,
        clone_as=source.clone_as,
    )
    result = builder.build_layer(layer, platform=platform)
    (directory / "workspace-image.txt").write_text(
        f"{result.reference}\n{source.clone_url}@{commit}\n", encoding="utf-8"
    )
    return result.reference


def _run_one(
    config: JormConfig,
    image: str,
    record: PromptRecord,
    runner: PromptRunner,
    output_dir: Path,
    builder: Any = None,
    platform: str = "",
) -> RecordResult:
    directory = output_dir / record.id
    if directory.exists():
        # Otherwise a re-run leaves last run's turn-*.txt and state/ beside the
        # new ones, and the directory describes two runs at once.
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)

    try:
        # A record with a repository runs from its own workspace image, built
        # on the runtime one, so the checkout is cached across retries.
        image = _image_for(config, image, record, builder, directory, platform)
    except Exception as exc:  # one record must not end the run
        # Not just ExecutionError: resolving a ref or building the workspace
        # image can raise OSError, a subprocess timeout, or a BuildError, and
        # losing 199 completed records because record 200 hit a full disk is
        # the wrong trade.
        log.exception("record %s: workspace preparation failed", record.id)
        _write_result(directory, record, None, str(exc))
        return RecordResult(record.id, False, None, directory, str(exc))

    prompts = list(record.user_turns)
    if record.overrides.max_turns is not None:
        prompts = prompts[: record.overrides.max_turns]

    timeout = record.overrides.timeout or config.run.timeout

    try:
        run = runner.run(
            harness=config.harness.name,
            image=image,
            prompts=prompts,
            # Delivered by the runner, which knows whether this harness takes a
            # flag or needs a file written inside the container.
            system=record.system,
            timeout=timeout,
            container_spec=_container_spec(config, image),
            collect_state_to=directory / "state"
            if config.output.collect_state
            else None,
            workdir=workdir_of(config),
        )
    except Exception as exc:  # one record failing must not end the run
        log.exception("record %s failed to run", record.id)
        _write_result(directory, record, None, str(exc))
        return RecordResult(record.id, False, None, directory, str(exc))

    _write_result(directory, record, run, None)
    return RecordResult(record.id, run.ok, run, directory)


def execute(
    config: JormConfig,
    *,
    records: Sequence[PromptRecord] | None = None,
    builder: object | None = None,
    runner: PromptRunner | None = None,
    available_env: set[str] | None = None,
    on_progress: Callable[[RecordResult], None] | None = None,
) -> ExecutionReport:
    """Build the image and run every prompt record against it."""
    import os

    from jormungandr.config.loading import compile_image_spec, resolve_prompts
    from jormungandr.runtime.build import ImageBuilder

    # Checked before anything expensive happens. OpenCode substitutes an unset
    # {env:VAR} with the empty string rather than failing, so a missing key
    # would otherwise surface as a 401 from the provider after a full image
    # build and N container starts.
    environ = set(available_env) if available_env is not None else set(os.environ)
    for path in config.run.env_files:
        environ |= env_file_names(Path(path))
    missing = config.missing_env(environ)
    if missing:
        raise ExecutionError(
            "missing environment variable(s) referenced by providers: "
            + ", ".join(sorted(missing))
            + ". Supply them in the environment or in run.env_files."
        )

    prompts = tuple(records) if records is not None else resolve_prompts(config)
    if not prompts:
        raise ExecutionError("no prompt records to run")

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

    # Checked before any container starts: a record named "report.json" would
    # otherwise collide with the run report and fail after every record had
    # already run, discarding the whole run's work.
    clashing = sorted(r.id for r in prompts if r.id in RESERVED_NAMES)
    if clashing:
        raise ExecutionError(
            f"record id(s) {', '.join(clashing)} are reserved names used by the "
            "run's own output; rename them"
        )

    image_builder = builder if builder is not None else ImageBuilder()
    spec = compile_image_spec(config)
    build = image_builder.build(spec)  # type: ignore[attr-defined]
    log.info("image %s (%s)", build.reference, "cached" if build.cached else "built")

    output_dir = Path(config.output.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_runner = runner if runner is not None else PromptRunner()

    results: list[RecordResult] = []
    workers = max(1, config.run.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_one,
                config,
                build.reference,
                record,
                prompt_runner,
                output_dir,
                image_builder,
                spec.target_platform,
            ): record
            for record in prompts
        }
        # as_completed, not submission order: reporting in submission order
        # means a slow first record withholds every later record's line, so the
        # run looks stalled while it is in fact progressing.
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_progress is not None:
                on_progress(result)

    # Restore input order: ThreadPoolExecutor completion order is arbitrary and
    # a report that reorders itself between runs is hard to diff.
    order = {record.id: index for index, record in enumerate(prompts)}
    results.sort(key=lambda r: order[r.id])

    report = ExecutionReport(
        image=build.reference, results=tuple(results), output_dir=output_dir
    )
    (output_dir / "report.json").write_text(
        json.dumps(
            {
                "image": report.image,
                "total": len(report.results),
                "succeeded": [r.id for r in report.succeeded],
                "failed": [r.id for r in report.failed],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


def env_file_names(path: Path) -> set[str]:
    """Variable names declared in an env file, without reading their values."""
    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names
