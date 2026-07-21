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
import shutil
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from jormungandr.config.models import JormConfig
from jormungandr.config.prompts import PromptRecord
from jormungandr.runtime.invocation import invocation_for
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


def _write_agents_md(workspace: Path, system: str) -> None:
    """Deliver a system prompt through AGENTS.md.

    For harnesses with no system-prompt flag — opencode's ``run`` exposes none.
    Appended rather than overwritten: a cloned repository may ship its own
    AGENTS.md, and silently discarding the project's instructions to inject
    ours would change the agent's behaviour in a way nobody asked for.
    """
    target = workspace / "AGENTS.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    target.write_text(existing + separator + system.rstrip() + "\n", encoding="utf-8")


def _prepare_workspace(record: PromptRecord, directory: Path) -> Path | None:
    """Materialize the record's workspace under its output directory.

    A local directory is *copied* rather than mounted in place: an agent given
    a bind mount edits the caller's source tree, and a run that mutates its own
    inputs cannot be repeated. Copying also leaves the post-run state on disk to
    inspect.
    """
    spec = record.workspace
    assert spec is not None  # set by the model validator
    if spec.type == "none":
        return None

    target = directory / "workspace"
    if target.exists():
        shutil.rmtree(target)

    if spec.type == "local":
        source = Path(spec.path or "").expanduser()
        if not source.is_dir():
            raise ExecutionError(f"{record.id}: workspace path is not a directory: {source}")
        shutil.copytree(source, target, symlinks=True)
        return target

    source = spec.git
    assert source is not None  # guaranteed by Workspace validation

    # Clone into a staging directory so `subdirectory` and `clone_as` can be
    # applied before anything is mounted. Cloning straight into the final
    # location would make "one directory out of a monorepo" impossible.
    staging = directory / ".clone"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        subprocess.run(
            ["git", "clone", "--quiet", source.clone_url, str(staging)],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if source.ref:
            subprocess.run(
                ["git", "-C", str(staging), "checkout", "--quiet", source.ref],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            # github_repo has no ref, so this is the Teich-compatible default —
            # but the same record clones different code tomorrow.
            log.warning(
                "%s: cloning %s at its default branch; the run is not "
                "reproducible. Pin git.ref.",
                record.id,
                source.clone_url,
            )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ExecutionError(
            f"{record.id}: could not clone {source.clone_url}"
            f"@{source.ref or 'default branch'}: {exc.stderr.strip()}"
        ) from exc

    content = staging
    if source.subdirectory:
        content = staging / source.subdirectory
        if not content.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            raise ExecutionError(
                f"{record.id}: subdirectory {source.subdirectory!r} does not exist "
                f"in {source.clone_url}"
                + (f" at {source.ref}" if source.ref else "")
            )

    destination = target / source.clone_as if source.clone_as else target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(content), str(destination))
    # Taking a subtree leaves the rest of the clone behind; a whole-repo move
    # already took .git with it and leaves only an empty shell.
    shutil.rmtree(staging, ignore_errors=True)
    return target


def _container_spec(
    config: JormConfig, image: str, workspace: Path | None
) -> ContainerSpec:
    mounts = list(config.run.mounts)
    if workspace is not None:
        # :Z is deliberately omitted — it is SELinux-specific and breaks on
        # Docker Desktop. The container user owns the copy via its uid.
        mounts.append(f"{workspace}:/workspace")
    return ContainerSpec(
        image=image,
        env=dict(config.run.env),
        env_files=tuple(str(p) for p in config.run.env_files),
        mounts=tuple(mounts),
        network=config.run.network,  # type: ignore[arg-type]
        limits=ResourceLimits(cpus=config.run.cpus, memory=config.run.memory),
    )


def _write_result(directory: Path, record: PromptRecord, run: HarnessRun | None,
                  error: str | None) -> None:
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


def _run_one(
    config: JormConfig,
    image: str,
    record: PromptRecord,
    runner: PromptRunner,
    output_dir: Path,
) -> RecordResult:
    directory = output_dir / record.id
    directory.mkdir(parents=True, exist_ok=True)

    try:
        workspace = _prepare_workspace(record, directory)
    except ExecutionError as exc:
        _write_result(directory, record, None, str(exc))
        return RecordResult(record.id, False, None, directory, str(exc))

    prompts = list(record.user_turns)
    if record.overrides.max_turns is not None:
        prompts = prompts[: record.overrides.max_turns]

    timeout = record.overrides.timeout or config.run.timeout

    # System prompt delivery is per-harness: droid takes a flag, opencode has
    # none and reads AGENTS.md from the working directory.
    system = record.system
    invocation = invocation_for(config.harness.name)
    system_argv: str | None = None
    if system:
        if getattr(invocation, "system_via", "argv") == "agents_md":
            if workspace is None:
                workspace = directory / "workspace"
                workspace.mkdir(parents=True, exist_ok=True)
            _write_agents_md(workspace, system)
        else:
            system_argv = system

    try:
        run = runner.run(
            harness=config.harness.name,
            image=image,
            prompts=prompts,
            system=system_argv,
            timeout=timeout,
            container_spec=_container_spec(config, image, workspace),
            collect_state_to=directory / "state" if config.output.collect_state else None,
        )
    except Exception as exc:  # noqa: BLE001 - one record failing must not end the run
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
    environ = available_env if available_env is not None else set(os.environ)
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

    image_builder = builder if builder is not None else ImageBuilder()
    build = image_builder.build(compile_image_spec(config))  # type: ignore[attr-defined]
    log.info("image %s (%s)", build.reference, "cached" if build.cached else "built")

    output_dir = Path(config.output.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_runner = runner if runner is not None else PromptRunner()

    results: list[RecordResult] = []
    workers = max(1, config.run.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_one, config, build.reference, record, prompt_runner, output_dir
            ): record
            for record in prompts
        }
        for future in futures:
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
