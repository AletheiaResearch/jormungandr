"""Implementations for the config-driven commands.

Output is plain text on purpose: this runs in CI as often as in a terminal, and
progress written with cursor control is unreadable in a log file. One line per
record, appended as it finishes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only, so the lazy imports in the command bodies stay lazy — this
    # module exists to keep `--help` from paying for the runtime stack.
    from collections.abc import Sequence

    from jormungandr.config.models import JormConfig
    from jormungandr.execute import RecordResult
    from jormungandr.runtime.run import TurnResult

__all__ = ["build", "check", "prune", "reap", "render", "run"]


def _echo(message: str = "", *, err: bool = False) -> None:
    print(message, file=sys.stderr if err else sys.stdout, flush=True)


def _load(config_path: Path, **overrides: Any) -> JormConfig:
    from jormungandr.config import load_config

    config = load_config(config_path)
    if not overrides:
        return config
    changes = {k: v for k, v in overrides.items() if v is not None}
    return config.model_copy(update=changes) if changes else config


def _with_run_overrides(
    config: JormConfig, *, concurrency: int | None, output: Path | None
) -> JormConfig:
    """Apply CLI overrides onto the nested models.

    Kept explicit rather than generic: two knobs are worth two lines, and a
    generic ``--set a.b.c=v`` mechanism is a config language nobody asked for.
    """
    run = config.run
    if concurrency is not None:
        run = run.model_copy(update={"concurrency": concurrency})
    out = config.output
    if output is not None:
        out = out.model_copy(update={"dir": output})
    return config.model_copy(update={"run": run, "output": out})


def check(config_path: Path) -> int:
    """Validate the config and prompts without building or running anything."""
    import os

    from jormungandr.config import resolve_prompts
    from jormungandr.config.loading import compile_image_spec
    from jormungandr.runtime.compose import compose

    config = _load(config_path)
    composed = compose(compile_image_spec(config))

    _echo(f"config    {config_path}")
    _echo(f"harness   {config.harness.name}  model {config.harness.model}")
    _echo(f"image     {composed.reference}")
    _echo(f"          base {composed.base.reference}")
    _echo(f"modules   {', '.join(composed.module_names)}")

    records = resolve_prompts(config)
    multi = sum(1 for r in records if r.follow_up_prompts)
    repos = sum(1 for r in records if r.workspace and r.workspace.type == "git")
    _echo(f"prompts   {len(records)} records ({multi} multi-turn, {repos} with a repo)")

    available = set(os.environ)
    for path in config.run.env_files:
        from jormungandr.execute import env_file_names

        available |= env_file_names(Path(path))
    missing = config.missing_env(available)
    if missing:
        # Worth failing on: an unset key surfaces as a 401 from the provider
        # after a full build and N container starts, not as a config error.
        _echo("")
        _echo(
            f"error: missing environment variable(s): {', '.join(sorted(missing))}",
            err=True,
        )
        _echo(
            "       set them in the environment or list a file in run.env_files",
            err=True,
        )
        return 1
    _echo(f"env       {', '.join(sorted(config.required_env))} resolved")
    _echo("")
    _echo("ok")
    return 0


def render(config_path: Path, *, tier: str = "all") -> int:
    """Print the generated Dockerfiles without building."""
    from jormungandr.config.loading import compile_image_spec
    from jormungandr.runtime.compose import compose

    composed = compose(compile_image_spec(_load(config_path)))
    layers = [layer for layer in composed.layers if tier in ("all", layer.tier)]
    if not layers:
        _echo(f"error: unknown tier {tier!r}; expected base, runtime or all", err=True)
        return 1
    for index, layer in enumerate(layers):
        if index:
            _echo()
        if len(layers) > 1:
            _echo(f"# ===== {layer.tier}: {layer.reference} =====")
        _echo(layer.dockerfile.rstrip())
    return 0


def build(config_path: Path, *, force: bool = False, quiet: bool = False) -> int:
    """Build the image described by the config."""
    from jormungandr.config.loading import compile_image_spec
    from jormungandr.runtime.build import ImageBuilder

    config = _load(config_path)
    result = ImageBuilder().build(
        compile_image_spec(config),
        force=force,
        on_output=None if quiet else _echo,
    )
    if not quiet:
        _echo()
    for layer in result.layers:
        _echo(
            f"{layer.tier:8} {'cached' if layer.cached else 'built ':6} {layer.reference}"
        )
    return 0


def run(
    config_path: Path,
    *,
    limit: int | None = None,
    concurrency: int | None = None,
    output: Path | None = None,
) -> int:
    """Build the image and run every prompt record against it."""
    from jormungandr.config import resolve_prompts
    from jormungandr.execute import execute

    config = _with_run_overrides(
        _load(config_path), concurrency=concurrency, output=output
    )
    if limit is not None:
        config = config.model_copy(
            update={"prompts": config.prompts.model_copy(update={"limit": limit})}
        )

    records = resolve_prompts(config)
    total = len(records)

    # Before building: a missing key would otherwise be discovered after a full
    # image build, which is the slow part.
    import os

    from jormungandr.execute import ExecutionError, env_file_names

    available = set(os.environ)
    for path in config.run.env_files:
        available |= env_file_names(Path(path))
    missing = config.missing_env(available)
    if missing:
        raise ExecutionError(
            "missing environment variable(s) referenced by providers: "
            + ", ".join(sorted(missing))
            + ". Supply them in the environment or in run.env_files."
        )

    from jormungandr.config.loading import compile_image_spec
    from jormungandr.runtime.build import ImageBuilder

    # Built here rather than inside execute() so the reference can be reported
    # before the first record starts — a build is the slow part, and a silent
    # minute is indistinguishable from a hang.
    builder = ImageBuilder()
    build_result = builder.build(compile_image_spec(config))
    state = "cached" if build_result.cached else "built"
    _echo(f"image     {build_result.reference} ({state})")
    _echo(f"{total} record(s), concurrency {config.run.concurrency}")

    done = 0

    def progress(result: RecordResult) -> None:
        nonlocal done
        done += 1
        turns = result.turns
        elapsed = sum(t.duration for t in turns)
        if result.ok:
            detail = f"{len(turns)} turn(s)  {elapsed:.1f}s"
            _echo(f"[{done}/{total}] {result.id:<24} ok    {detail}")
        else:
            reason = result.error or _first_failure(turns)
            _echo(f"[{done}/{total}] {result.id:<24} FAIL  {reason}")

    report = execute(config, records=records, builder=builder, on_progress=progress)

    _echo()
    _echo(
        f"{len(report.results)} record(s): {len(report.succeeded)} ok, "
        f"{len(report.failed)} failed  ->  {report.output_dir}"
    )
    # Non-zero when anything failed, so CI notices without parsing output.
    return 0 if report.ok else 1


def _first_failure(turns: Sequence[TurnResult]) -> str:
    for turn in turns:
        if turn.timed_out:
            return f"turn {turn.index} timed out"
        if turn.exit_code != 0:
            return f"turn {turn.index} exited {turn.exit_code}"
    return "unknown failure"


def prune(*, keep: tuple[str, ...] = ()) -> int:
    """Remove images this tool created, selected by label."""
    from jormungandr.runtime.build import ImageBuilder

    removed = ImageBuilder().prune(keep=keep)
    _echo(f"removed {len(removed)} image(s)")
    for reference in removed:
        _echo(f"  {reference}")
    return 0


def reap(*, all_owners: bool = False) -> int:
    """Remove containers left behind by crashed runs."""
    from jormungandr.runtime.container import ContainerRuntime

    removed = ContainerRuntime(install_handlers=False).reap_orphans(
        all_owners=all_owners
    )
    _echo(f"reaped {len(removed)} container(s)")
    for name in removed:
        _echo(f"  {name}")
    return 0
