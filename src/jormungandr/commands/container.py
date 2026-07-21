"""Implementations for the `jormungandr container` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jormungandr.runtime.container import ContainerRuntime
from jormungandr.runtime.spec import ContainerSpec, ResourceLimits

__all__ = ["exec_in_container", "list_containers", "reap_containers", "run_container"]


def build_container_spec(
    image: str,
    *,
    command: tuple[str, ...] = (),
    network: str = "bridge",
    cpus: float | None = None,
    memory: str | None = None,
    env: tuple[str, ...] = (),
    env_file: tuple[str, ...] = (),
    volume: tuple[str, ...] = (),
    workdir: str | None = None,
    user: str | None = None,
) -> ContainerSpec:
    limits = ResourceLimits(
        **{k: v for k, v in {"cpus": cpus, "memory": memory}.items() if v is not None}
    )
    parsed_env: dict[str, str] = {}
    for item in env:
        key, _, value = item.partition("=")
        if not key or not _:
            raise ValueError(f"invalid --env {item!r}: expected KEY=VALUE")
        parsed_env[key] = value
    return ContainerSpec(
        image=image,
        command=command or ("sleep", "infinity"),
        network=network,  # type: ignore[arg-type]
        limits=limits,
        env=parsed_env,
        env_files=env_file,
        mounts=volume,
        workdir=workdir,
        user=user,
    )


def run_container(spec: ContainerSpec, *, name: str | None = None) -> dict[str, Any]:
    # Untracked and owner-marked "detached": this container must outlive the CLI
    # process, so it is neither torn down at exit nor treated as an orphan by a
    # later reap. `reap --all` still collects it.
    from jormungandr.runtime.container import DETACHED_OWNER

    runtime = ContainerRuntime(install_handlers=False)
    session = runtime.create(spec, name=name, track=False, owner=DETACHED_OWNER)
    return {
        "id": session.container_id,
        "name": session.name,
        "session": session.session_id,
    }


def exec_in_container(
    container: str, command: tuple[str, ...], *, timeout: float | None = None
) -> dict[str, Any]:
    from jormungandr.runtime.docker import DockerCli

    result = DockerCli().exec(container, command, timeout=timeout)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration": round(result.duration, 3),
        "timed_out": result.timed_out,
    }


def list_containers() -> list[dict[str, str]]:
    return ContainerRuntime(install_handlers=False).managed_containers()


def reap_containers(*, all_owners: bool = False) -> list[str]:
    return ContainerRuntime(install_handlers=False).reap_orphans(all_owners=all_owners)
