"""CLI declarations. Stdlib + cyclopts imports only — command bodies
import their implementations lazily so --help stays fast."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

app = App(
    name="jormungandr",
    help="Compose Docker images and run containers for agent harnesses.",
)

image_app = App(name="image", help="Build and manage harness images.")
container_app = App(name="container", help="Run and manage harness containers.")
app.command(image_app)
app.command(container_app)


@image_app.command(name="render")
def image_render(
    spec: Annotated[Path | None, Parameter(help="Path to a YAML/JSON ImageSpec.")] = None,
    *,
    base_image: str | None = None,
) -> None:
    """Render the Dockerfile for a spec without building it."""
    from jormungandr.commands.image import load_spec, render_spec

    print(render_spec(load_spec(spec, base_image=base_image)))


@image_app.command(name="build")
def image_build(
    spec: Annotated[Path | None, Parameter(help="Path to a YAML/JSON ImageSpec.")] = None,
    *,
    base_image: str | None = None,
    state_dir: Path | None = None,
    force: Annotated[bool, Parameter(help="Rebuild even if the image exists.")] = False,
    quiet: bool = False,
) -> None:
    """Build an image. Skipped when an image with the same content digest exists."""
    from jormungandr.commands.image import build_image, load_spec

    result = build_image(
        load_spec(spec, base_image=base_image),
        state_dir=state_dir,
        force=force,
        quiet=quiet,
    )
    status = "cached" if result["cached"] else "built"
    print(f"{status}: {result['reference']}")


@image_app.command(name="modules")
def image_modules() -> None:
    """List available modules, including any registered by third parties."""
    from jormungandr.commands.image import show_modules

    for module in show_modules():
        print(module["name"])


@image_app.command(name="prune")
def image_prune(*, state_dir: Path | None = None) -> None:
    """Remove images this tool created. Selected by label, never by name prefix."""
    from jormungandr.commands.image import prune_images

    removed = prune_images(state_dir=state_dir)
    print(f"removed {len(removed)} image(s)")
    for reference in removed:
        print(f"  {reference}")


@container_app.command(name="run")
def container_run(
    image: str,
    *command: Annotated[str, Parameter(allow_leading_hyphen=True)],
    name: str | None = None,
    network: str = "bridge",
    cpus: float | None = None,
    memory: str | None = None,
    env: Annotated[tuple[str, ...], Parameter(help="KEY=VALUE (non-secret only).")] = (),
    env_file: Annotated[tuple[str, ...], Parameter(help="File of KEY=VALUE secrets.")] = (),
    volume: Annotated[tuple[str, ...], Parameter(help="Bind mount, host:container.")] = (),
    workdir: str | None = None,
    user: str | None = None,
) -> None:
    """Start a labelled, resource-capped container."""
    from jormungandr.commands.container import build_container_spec, run_container

    spec = build_container_spec(
        image,
        command=command,
        network=network,
        cpus=cpus,
        memory=memory,
        env=env,
        env_file=env_file,
        volume=volume,
        workdir=workdir,
        user=user,
    )
    print(json.dumps(run_container(spec, name=name), indent=2))


# help_flags/version_flags are cleared: the command after the container name
# belongs to the container, not to us. Otherwise cyclopts answers
# `... exec c claude --version` with jormungandr's own version, and
# `... exec c claude --help` with jormungandr's help. Same reasoning as
# `docker exec` / `kubectl exec --`.
@container_app.command(name="exec", help_flags=(), version_flags=())
def container_exec(
    container: str,
    *command: Annotated[str, Parameter(allow_leading_hyphen=True)],
    timeout: float | None = None,
) -> None:
    """Run a command in a container, with a real timeout and exit code."""
    import sys

    from jormungandr.commands.container import exec_in_container

    result = exec_in_container(container, command, timeout=timeout)
    if result["stdout"]:
        print(result["stdout"], end="")
    if result["stderr"]:
        print(result["stderr"], end="", file=sys.stderr)
    raise SystemExit(result["exit_code"])


@container_app.command(name="list")
def container_list() -> None:
    """List containers this tool created."""
    from jormungandr.commands.container import list_containers

    for container in list_containers():
        print(f"{container.get('Names', '?')}\t{container.get('Status', '?')}")


@container_app.command(name="reap")
def container_reap() -> None:
    """Remove containers left behind by crashed runs."""
    from jormungandr.commands.container import reap_containers

    removed = reap_containers()
    print(f"reaped {len(removed)} container(s)")
    for name in removed:
        print(f"  {name}")


# The meta app must not claim --version/--help itself: it sees the whole
# command line, so it would answer them even when they were meant for a command
# running inside a container. Cleared here, they fall through to `tokens` and
# are resolved by the root app (for `jormungandr --version`) or forwarded by a
# passthrough command like `container exec`.
app.meta.version_flags = ()
app.meta.help_flags = ()


@app.meta.default
def _launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: bool = False,
) -> None:
    from jormungandr.cli_helpers import configure_logging

    configure_logging(verbose=verbose)
    app(tokens)


def main() -> None:
    from jormungandr.cli_helpers import install_error_handler

    install_error_handler()
    app.meta()
