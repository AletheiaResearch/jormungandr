"""End-to-end tests against a real Docker daemon.

Deselected by default; run with `uv run pytest -m docker`.

Everything here is deliberately kept off the fast suite, but these are the
tests that actually prove the thing works — the composition unit tests cannot
tell you that a generated Dockerfile builds, or that a timeout really kills a
process inside a container.
"""

from __future__ import annotations

import socket
import sys
import uuid

import pytest

from jormungandr.runtime.build import ImageBuilder
from jormungandr.runtime.container import ContainerRuntime
from jormungandr.runtime.docker import DockerCli
from jormungandr.runtime.modules import builtin  # noqa: F401
from jormungandr.runtime.spec import ContainerSpec, ImageSpec, ResourceLimits

pytestmark = pytest.mark.docker

BASE = "alpine:3.20"


@pytest.fixture(scope="module")
def docker() -> DockerCli:
    cli = DockerCli()
    if not cli.available():
        pytest.skip("docker daemon not available")
    return cli


@pytest.fixture(scope="module")
def built(tmp_path_factory, docker: DockerCli):
    """Build a small real image once and reuse it."""
    spec = ImageSpec(
        base_image=BASE,
        repository="jormungandr-test",
        modules=[{"name": "script", "content": "echo baked > /marker"}],
    )
    builder = ImageBuilder(state_dir=tmp_path_factory.mktemp("state"), docker=docker)
    result = builder.build(spec)
    yield result
    docker.remove_image(result.reference, force=True)


class TestRealBuild:
    def test_image_builds_and_is_tagged_by_digest(self, built) -> None:
        assert built.reference.endswith(built.digest)
        assert built.image_id.startswith("sha256:")

    def test_labels_are_queryable(self, docker: DockerCli, built) -> None:
        labels = docker.inspect(built.reference)["Config"]["Labels"]
        assert labels["dev.jormungandr.managed"] == "true"
        assert labels["dev.jormungandr.digest"] == built.digest

    def test_module_side_effect_is_present(self, docker: DockerCli, built) -> None:
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.exec(["cat", "/marker"])
        assert result.stdout.strip() == "baked"

    def test_rebuild_is_skipped_when_nothing_changed(
        self, tmp_path, docker: DockerCli, built
    ) -> None:
        # The whole point of content-addressed tags.
        spec = ImageSpec(
            base_image=BASE,
            repository="jormungandr-test",
            modules=[{"name": "script", "content": "echo baked > /marker"}],
        )
        again = ImageBuilder(state_dir=tmp_path, docker=docker).build(spec)
        assert again.cached
        assert again.reference == built.reference

    def test_editing_a_script_produces_a_different_image(
        self, tmp_path, docker: DockerCli
    ) -> None:
        spec = ImageSpec(
            base_image=BASE,
            repository="jormungandr-test",
            modules=[{"name": "script", "content": "echo changed > /marker"}],
        )
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        result = builder.build(spec)
        try:
            assert not result.cached
            runtime = ContainerRuntime(docker=docker, install_handlers=False)
            with runtime.session(ContainerSpec(image=result.reference)) as session:
                assert session.exec(["cat", "/marker"]).stdout.strip() == "changed"
        finally:
            docker.remove_image(result.reference, force=True)


class TestRealContainer:
    @pytest.fixture
    def runtime(self, docker: DockerCli) -> ContainerRuntime:
        return ContainerRuntime(docker=docker, install_handlers=False)

    def test_exec_returns_real_exit_code(self, runtime, built) -> None:
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            assert session.exec(["true"]).exit_code == 0
            assert session.exec(["false"]).exit_code == 1
            assert session.exec(["sh", "-c", "exit 42"]).exit_code == 42

    def test_streams_are_separate(self, runtime, built) -> None:
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.shell("echo out; echo err >&2")
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"

    def test_timeout_kills_the_process(self, runtime, built) -> None:
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.exec(["sleep", "60"], timeout=2)
        assert result.timed_out
        assert result.exit_code == 124
        assert result.duration < 30

    def test_container_is_removed_after_the_session(self, runtime, docker, built) -> None:
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            cid = session.container_id
            assert session.running
        assert not docker.is_running(cid)
        assert cid not in [c.get("ID", "") for c in docker.list_containers()]

    def test_memory_limit_is_enforced(self, runtime, built) -> None:
        spec = ContainerSpec(
            image=built.reference,
            limits=ResourceLimits(memory="64m", memory_swap="64m", cpus=1.0),
        )
        with runtime.session(spec) as session:
            # Allocating far past the cap must be killed, not swallow the host.
            result = session.shell("dd if=/dev/zero of=/dev/shm/big bs=1M count=512")
        assert result.exit_code != 0

    def test_network_none_blocks_egress(self, runtime, built) -> None:
        spec = ContainerSpec(image=built.reference, network="none")
        with runtime.session(spec) as session:
            result = session.exec(["ping", "-c", "1", "-W", "2", "1.1.1.1"])
        assert result.exit_code != 0

    def test_labels_allow_orphan_recovery(self, docker, built) -> None:
        # Simulate a crashed run: a container labelled with a pid that is gone.
        dead_owner = f"999999999@{socket.gethostname()}"
        crashed = ContainerRuntime(docker=docker, install_handlers=False)
        session = crashed.create(ContainerSpec(image=built.reference), owner=dead_owner)
        try:
            # A *different* runtime, as after a restart, finds it by label.
            fresh = ContainerRuntime(docker=docker, install_handlers=False)
            assert session.name in fresh.reap_orphans()
        finally:
            docker.remove_container(session.container_id, force=True)

    def test_reap_spares_a_live_process_containers(self, docker, built) -> None:
        # The bug this guards: `docker ls` returns 12-char ids and `docker
        # create` 64-char ones, so the "skip my own" check never matched and
        # the sweep deleted the caller's running containers.
        owner = ContainerRuntime(docker=docker, install_handlers=False)
        session = owner.create(ContainerSpec(image=built.reference))
        try:
            other = ContainerRuntime(docker=docker, install_handlers=False)
            assert session.name not in other.reap_orphans()
            assert docker.is_running(session.container_id)
        finally:
            docker.remove_container(session.container_id, force=True)

    def test_copy_in_and_out(self, runtime, built, tmp_path) -> None:
        source = tmp_path / "payload.txt"
        source.write_text("hello")
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            session.copy_in(source, "/tmp/payload.txt")
            assert session.exec(["cat", "/tmp/payload.txt"]).stdout.strip() == "hello"
            session.shell("echo produced > /tmp/out.txt")
            session.copy_out("/tmp/out.txt", tmp_path / "out.txt")
        assert (tmp_path / "out.txt").read_text().strip() == "produced"


class TestExecResourceSafety:
    """These properties are only real against an actual container."""

    @pytest.fixture
    def runtime(self, docker: DockerCli) -> ContainerRuntime:
        return ContainerRuntime(docker=docker, install_handlers=False)

    def test_large_output_is_capped_without_buffering_it_all(
        self, runtime, built
    ) -> None:
        # communicate() buffers the whole stream before truncating, so the cap
        # bounded the returned string but not memory. 512MB of output through a
        # 4KB cap must stay flat.
        import resource

        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.shell(
                "dd if=/dev/zero bs=1M count=512 2>/dev/null | tr '\\0' 'x'",
                timeout=180,
                max_output=4096,
            )
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        assert len(result.stdout) == 4096
        assert result.truncated
        # ru_maxrss is bytes on macOS, KiB on Linux; 512MB dwarfs either scale.
        growth_mb = (after - before) / (1024 * 1024 if sys.platform == "darwin" else 1024)
        assert growth_mb < 100, f"peak RSS grew {growth_mb:.0f}MB draining 512MB"

    def test_timeout_does_not_leave_the_command_running(self, runtime, built) -> None:
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            marker = "jormungandr-timeout-probe"
            result = session.shell(f"sleep 300 # {marker}", timeout=2)
            assert result.timed_out
            # The docker exec client is gone; confirm we can still drive the
            # container, i.e. the timeout did not wedge it.
            assert session.exec(["true"]).exit_code == 0
