"""CLI declarations.

Stdlib and cyclopts imports only. Command bodies import their implementations
lazily, so ``--help`` does not pay for the compose and container machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

app = App(
    name="jormungandr",
    help="Run agent harnesses in containers, from a config file.",
)


@app.command
def check(
    config: Annotated[
        Path, Parameter(help="Path to a jormungandr config file.")
    ] = Path("jorm.yaml"),
) -> None:
    """Validate the config and prompts without building or running anything.

    Reports the image that would be built, how many prompt records were found,
    and whether every referenced environment variable resolves.
    """
    from jormungandr.commands.jobs import check as run_check

    raise SystemExit(run_check(config))


@app.command
def render(
    config: Path = Path("jorm.yaml"),
    *,
    tier: Annotated[str, Parameter(help="base, runtime, or all.")] = "all",
) -> None:
    """Print the generated Dockerfiles without building them."""
    from jormungandr.commands.jobs import render as run_render

    raise SystemExit(run_render(config, tier=tier))


@app.command
def build(
    config: Path = Path("jorm.yaml"),
    *,
    force: Annotated[bool, Parameter(help="Rebuild even if the image exists.")] = False,
    quiet: Annotated[bool, Parameter(help="Suppress build output.")] = False,
) -> None:
    """Build the image described by the config.

    Skipped when an image with the same content digest already exists.
    """
    from jormungandr.commands.jobs import build as run_build

    raise SystemExit(run_build(config, force=force, quiet=quiet))


@app.command
def run(
    config: Path = Path("jorm.yaml"),
    *,
    limit: Annotated[
        int | None, Parameter(help="Run only the first N records.")
    ] = None,
    concurrency: Annotated[
        int | None, Parameter(help="Override run.concurrency.")
    ] = None,
    output: Annotated[Path | None, Parameter(help="Override output.dir.")] = None,
) -> None:
    """Build the image and run every prompt record against it.

    Exits non-zero if any record failed, so CI notices without parsing output.
    """
    from jormungandr.commands.jobs import run as run_job

    raise SystemExit(
        run_job(config, limit=limit, concurrency=concurrency, output=output)
    )


@app.command
def prune(
    *,
    keep: Annotated[
        tuple[str, ...], Parameter(help="Image references to preserve.")
    ] = (),
) -> None:
    """Remove images this tool built.

    Identified by a label *and* a digest that matches the one embedded in the
    tag. The label alone is not enough: Docker propagates a parent image's
    labels into any child, so an image you built FROM one of ours carries it
    too — and would otherwise be deleted.
    """
    from jormungandr.commands.jobs import prune as run_prune

    raise SystemExit(run_prune(keep=keep))


@app.command
def reap(
    *,
    all_owners: Annotated[
        bool,
        Parameter(
            name=["--all"],
            help="Also remove containers owned by live processes and detached runs.",
        ),
    ] = False,
) -> None:
    """Remove containers left behind by crashed runs.

    By default only containers whose owning process is gone, so a concurrent
    run's live containers are left alone.
    """
    from jormungandr.commands.jobs import reap as run_reap

    raise SystemExit(run_reap(all_owners=all_owners))


@app.meta.default
def _launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: Annotated[
        bool, Parameter(help="Log config overrides and internal progress.")
    ] = False,
) -> None:
    from jormungandr.cli_helpers import configure_logging

    configure_logging(verbose=verbose)
    app(tokens)


def main() -> None:
    """Run the CLI.

    Installs the error handler before dispatching, so a failure surfaces as one
    line naming the cause rather than a traceback the user cannot act on.
    """
    from jormungandr.cli_helpers import install_error_handler

    install_error_handler()
    app.meta()
