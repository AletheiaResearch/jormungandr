from __future__ import annotations

import os
import socket
import weakref

import pytest

from jormungandr.runtime.container import (
    DETACHED_OWNER,
    MANAGED_LABEL,
    OWNER_LABEL,
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
        self.label_filters: list[str | None] = []

    def require(self) -> None:
        pass

    def create(self, args):
        self.created.append(list(args))
        # A full 64-hex id, exactly as `docker create` returns. The fake used to
        # return "cid1" and echo it back from list_containers, which hid a real
        # bug: `docker container ls` reports 12-char ids, so the "don't reap my
        # own live containers" guard never matched and the sweep deleted them.
        return f"{len(self.created):064x}"

    def start(self, container):
        self.started.append(container)

    def stop(self, container, *, timeout=10):
        self.stopped.append(container)
        return True

    def remove_container(self, container, *, force=True):
        self.removed.append(container)
        return True

    def list_containers(self, *, label=None, all_states=True):
        self.label_filters.append(label)
        # Honour the filter, so a test asserting "selected by label" is testing
        # the code rather than the mock.
        if label is None:
            return self.containers
        return [c for c in self.containers if label in c.get("Labels", "")]

    def image_label(self, reference, label):
        return ""

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


DEAD_PID = 999_999_999  # far above any real pid; ProcessLookupError on kill(0)


def container_row(
    *, cid: str, name: str, owner: str, managed: bool = True
) -> dict[str, str]:
    # The session label is what identifies a container we created: a container
    # inherits its image's labels, so `managed` alone also matches anything a
    # user ran from a jormungandr-built image.
    labels = [f"{OWNER_LABEL}={owner}", f"{SESSION_LABEL}=sess-{cid[:6]}"]
    if managed:
        labels.append(f"{MANAGED_LABEL}=true")
    return {"ID": cid[:12], "Names": name, "Labels": ",".join(labels)}


class TestReaping:
    def test_reaps_a_container_whose_owner_process_is_gone(self) -> None:
        host = socket.gethostname()
        docker = FakeDocker(
            containers=[
                container_row(cid="a" * 64, name="jormungandr-dead", owner=f"{DEAD_PID}@{host}")
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans() == ["jormungandr-dead"]

    def test_selects_by_the_session_label(self) -> None:
        # Not `managed`: a container inherits its image's labels, so a user
        # container started from a jormungandr image would be swept. The
        # session label is written at create time and cannot come from an image.
        docker = FakeDocker(containers=[])
        ContainerRuntime(docker=docker, install_handlers=False).reap_orphans()
        assert docker.label_filters == [SESSION_LABEL]

    def test_a_container_without_a_session_label_is_not_ours(self) -> None:
        docker = FakeDocker(
            containers=[
                {
                    "ID": "f" * 12,
                    "Names": "user-container",
                    # inherited from the image, no session label
                    "Labels": f"{MANAGED_LABEL}=true",
                }
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans(all_owners=True) == []

    def test_does_not_reap_its_own_live_containers(self) -> None:
        # `docker ls` reports 12-char ids while `docker create` returns 64; if
        # these are compared at different widths the guard never fires and the
        # sweep deletes the containers it is running against.
        docker = FakeDocker()
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        session = runtime.create(ContainerSpec(image="img"))
        docker.containers = [
            container_row(
                cid=session.container_id, name=session.name, owner=runtime.owner
            )
        ]
        assert len(session.container_id) == 64
        assert runtime.reap_orphans() == []
        assert docker.removed == []

    def test_does_not_reap_a_live_other_process(self) -> None:
        # The container of a concurrently running session must survive.
        host = socket.gethostname()
        docker = FakeDocker(
            containers=[
                container_row(cid="b" * 64, name="other-live", owner=f"{os.getpid()}@{host}")
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans() == []

    def test_does_not_reap_detached_runs(self) -> None:
        docker = FakeDocker(
            containers=[container_row(cid="c" * 64, name="detached", owner=DETACHED_OWNER)]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans() == []

    def test_all_owners_sweeps_everything(self) -> None:
        host = socket.gethostname()
        docker = FakeDocker(
            containers=[
                container_row(cid="c" * 64, name="detached", owner=DETACHED_OWNER),
                container_row(cid="d" * 64, name="live", owner=f"{os.getpid()}@{host}"),
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert sorted(runtime.reap_orphans(all_owners=True)) == ["detached", "live"]

    def test_foreign_host_owner_is_left_alone(self) -> None:
        # A pid from another machine says nothing about a pid here.
        docker = FakeDocker(
            containers=[container_row(cid="e" * 64, name="remote", owner=f"{DEAD_PID}@elsewhere")]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans() == []

    def test_owner_filter_is_exact_not_substring(self) -> None:
        docker = FakeDocker(
            containers=[
                container_row(cid="f" * 64, name="mine", owner="123@hostA"),
                container_row(cid="0" * 64, name="lookalike", owner="123@hostA2"),
            ]
        )
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        assert runtime.reap_orphans(owner="123@hostA", all_owners=True) == ["mine"]


class TestShutdownRobustness:
    def test_interrupted_shutdown_leaves_the_rest_tracked(self) -> None:
        # Clearing the live set up front loses every remaining container when a
        # second Ctrl-C unwinds the loop; the atexit retry then finds nothing.
        docker = FakeDocker()
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        sessions = [runtime.create(ContainerSpec(image="img")) for _ in range(3)]

        calls = {"n": 0}
        real_remove = docker.remove_container

        def flaky(container, *, force=True):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real_remove(container, force=force)

        docker.remove_container = flaky
        with pytest.raises(KeyboardInterrupt):
            runtime.shutdown()

        # The interrupted one and the untouched one are both still tracked.
        assert len(runtime._live) == 2
        docker.remove_container = real_remove
        runtime.shutdown()
        assert len(runtime._live) == 0
        assert len(docker.removed) == 3

    def test_lock_is_reentrant(self) -> None:
        # The signal handler calls shutdown() on a thread that may already hold
        # the lock; a plain Lock deadlocks unrecoverably there.
        runtime = ContainerRuntime(docker=FakeDocker(), install_handlers=False)
        with runtime._lock:
            runtime.shutdown()  # must not hang


class TestHandlerLifetime:
    def test_runtimes_are_not_pinned_by_atexit(self) -> None:
        import gc

        runtime = ContainerRuntime(docker=FakeDocker(), install_handlers=True)
        ref = weakref.ref(runtime)
        del runtime
        gc.collect()
        assert ref() is None, "atexit/handler closure kept the runtime alive"
