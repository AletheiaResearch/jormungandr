from __future__ import annotations

import pytest

from jormungandr.runtime.container import (
    MANAGED_LABEL,
    SESSION_LABEL,
    ContainerRuntime,
)
from jormungandr.runtime.spec import ContainerSpec, ResourceLimits


class FakeDocker:
    def __init__(self, containers: list[dict[str, str]] | None = None) -> None:
        self.created: list[list[str]] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.stopped: list[str] = []
        self.containers = containers or []

    def require(self) -> None:
        pass

    def create(self, args):
        self.created.append(list(args))
        return f"cid{len(self.created)}"

    def start(self, container):
        self.started.append(container)

    def stop(self, container, *, timeout=10):
        self.stopped.append(container)
        return True

    def remove_container(self, container, *, force=True):
        self.removed.append(container)
        return True

    def list_containers(self, *, label=None, all_states=True):
        return self.containers

    def is_running(self, container):
        return container in self.started


@pytest.fixture
def runtime() -> ContainerRuntime:
    return ContainerRuntime(docker=FakeDocker(), install_handlers=False, owner="test")


def args_of(runtime: ContainerRuntime) -> list[str]:
    return runtime.docker.created[0]  # type: ignore[attr-defined]


class TestCreateArgs:
    def test_labels_are_always_applied(self, runtime: ContainerRuntime) -> None:
        # Labels are what make crash recovery possible at all.
        runtime.create(ContainerSpec(image="img"))
        args = args_of(runtime)
        assert f"{MANAGED_LABEL}=true" in args
        assert any(a.startswith(f"{SESSION_LABEL}=") for a in args)

    def test_resource_limits_are_applied_by_default(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img"))
        args = args_of(runtime)
        assert "--cpus" in args and "--memory" in args and "--pids-limit" in args

    def test_security_defaults(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img"))
        args = args_of(runtime)
        assert "no-new-privileges" in args
        assert "--cap-drop" in args and "ALL" in args
        assert "--init" in args

    def test_network_is_explicit(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img", network="none"))
        args = args_of(runtime)
        assert args[args.index("--network") + 1] == "none"

    def test_env_files_used_for_secrets(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img", env_files=("/tmp/secrets.env",)))
        args = args_of(runtime)
        assert args[args.index("--env-file") + 1] == "/tmp/secrets.env"

    def test_image_precedes_command(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img", command=("echo", "hi")))
        args = args_of(runtime)
        assert args[args.index("img") + 1 :] == ["echo", "hi"]

    def test_cap_add_is_possible(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img", cap_add=("SYS_PTRACE",)))
        args = args_of(runtime)
        assert args[args.index("--cap-add") + 1] == "SYS_PTRACE"

    def test_limits_can_be_relaxed(self, runtime: ContainerRuntime) -> None:
        spec = ContainerSpec(
            image="img", limits=ResourceLimits(cpus=None, memory=None, pids=None, nofile=None)
        )
        runtime.create(spec)
        assert "--cpus" not in args_of(runtime)

    def test_container_is_started(self, runtime: ContainerRuntime) -> None:
        session = runtime.create(ContainerSpec(image="img"))
        assert session.container_id in runtime.docker.started  # type: ignore[attr-defined]


class TestSessionLifecycle:
    def test_session_removes_on_exit(self, runtime: ContainerRuntime) -> None:
        with runtime.session(ContainerSpec(image="img")) as session:
            cid = session.container_id
        assert cid in runtime.docker.removed  # type: ignore[attr-defined]

    def test_session_removes_on_exception(self, runtime: ContainerRuntime) -> None:
        with pytest.raises(RuntimeError):
            with runtime.session(ContainerSpec(image="img")) as session:
                cid = session.container_id
                raise RuntimeError("boom")
        assert cid in runtime.docker.removed  # type: ignore[attr-defined]

    def test_remove_is_idempotent(self, runtime: ContainerRuntime) -> None:
        session = runtime.create(ContainerSpec(image="img"))
        session.remove()
        session.remove()
        assert runtime.docker.removed.count(session.container_id) == 1  # type: ignore[attr-defined]

    def test_remove_never_raises(self, runtime: ContainerRuntime) -> None:
        # Cleanup that can fail is cleanup you cannot put in a finally.
        session = runtime.create(ContainerSpec(image="img"))

        def explode(*a, **k):
            raise OSError("daemon gone")

        runtime.docker.remove_container = explode  # type: ignore[attr-defined]
        runtime.docker.stop = explode  # type: ignore[attr-defined]
        session.remove()

    def test_shutdown_clears_live_set(self, runtime: ContainerRuntime) -> None:
        runtime.create(ContainerSpec(image="img"))
        runtime.create(ContainerSpec(image="img"))
        runtime.shutdown()
        assert len(runtime.docker.removed) == 2  # type: ignore[attr-defined]


class TestReaping:
    def test_reaps_orphans_by_label(self) -> None:
        docker = FakeDocker(
            containers=[
                {"ID": "old1", "Names": "jormungandr-dead", "Labels": f"{MANAGED_LABEL}=true"}
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans() == ["jormungandr-dead"]
        assert docker.removed == ["old1"]

    def test_does_not_reap_its_own_live_containers(self) -> None:
        docker = FakeDocker()
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        session = runtime.create(ContainerSpec(image="img"))
        docker.containers = [
            {"ID": session.container_id, "Names": session.name, "Labels": ""}
        ]
        assert runtime.reap_orphans() == []

    def test_owner_filter(self) -> None:
        docker = FakeDocker(
            containers=[
                {"ID": "a", "Names": "mine", "Labels": "dev.jormungandr.owner=me"},
                {"ID": "b", "Names": "theirs", "Labels": "dev.jormungandr.owner=you"},
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans(owner="me") == ["mine"]
