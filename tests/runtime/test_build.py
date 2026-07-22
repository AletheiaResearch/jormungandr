from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jormungandr.runtime.build import BuildError, ImageBuilder
from jormungandr.runtime.compose import compose
from jormungandr.runtime.docker import CommandResult, DockerCli, DockerError
from jormungandr.runtime.layers import Run
from jormungandr.runtime.modules import builtin  # noqa: F401
from jormungandr.runtime.spec import ImageSpec


class FakeDocker:
    """Stand-in for DockerCli; records calls, fakes daemon state."""

    def __init__(self, *, existing: set[str] | None = None, fail: bool = False) -> None:
        self.existing = existing or set()
        self.fail = fail
        self.builds: list[list[str]] = []
        self.required = False

    def require(self) -> None:
        self.required = True

    def image_exists(self, reference: str) -> bool:
        return reference in self.existing

    def image_digest(self, reference: str) -> str:
        return f"sha256:{reference}"

    def stream(self, args, **kwargs):
        self.builds.append(list(args))
        if self.fail:
            raise DockerError(args, 1, "step 3/5 failed")
        yield "#1 [internal] load build definition"
        yield "#5 DONE 1.2s"

    def list_images(self, *, label=None):
        return []

    def remove_image(self, reference, *, force=False):
        return True


@pytest.fixture
def builder(tmp_path: Path) -> ImageBuilder:
    return ImageBuilder(state_dir=tmp_path, docker=FakeDocker())


def simple_spec(**kwargs) -> ImageSpec:
    return ImageSpec(base_image="debian:trixie-slim", **kwargs)


class TestWriteContext:
    def test_writes_dockerfile_and_dockerignore(self, builder: ImageBuilder) -> None:
        layer = compose(simple_spec()).base
        context = builder.write_context(layer)
        assert (context / "Dockerfile").read_text() == layer.dockerfile
        assert "!Dockerfile" in (context / ".dockerignore").read_text()

    def test_writes_module_files_with_modes(self, builder: ImageBuilder) -> None:
        layer = compose(
            simple_spec(modules=[{"name": "script", "content": "echo x"}])
        ).runtime
        context = builder.write_context(layer)
        script = context / "script.sh"
        assert script.read_text() == "echo x\n"
        assert script.stat().st_mode & 0o777 == 0o755

    def test_dockerignore_allows_declared_module_files(
        self, builder: ImageBuilder
    ) -> None:
        # A static `*` + `!Dockerfile` silently drops every baked-in script and
        # only fails later, as a COPY that cannot find its source.
        layer = compose(
            simple_spec(modules=[{"name": "script", "content": "echo x"}])
        ).runtime
        context = builder.write_context(layer)
        assert "!script.sh" in (context / ".dockerignore").read_text()

    def test_context_is_rebuilt_from_scratch(self, builder: ImageBuilder) -> None:
        layer = compose(simple_spec()).base
        context = builder.write_context(layer)
        stale = context / "stale.txt"
        stale.write_text("left over")
        builder.write_context(layer)
        assert not stale.exists()

    def test_log_is_not_inside_the_context(self, builder: ImageBuilder) -> None:
        # SWE-bench writes its build log into the context and ships it to the
        # daemon on every rebuild.
        layer = compose(simple_spec()).base
        context = builder.write_context(layer)
        assert builder.log_path(layer.digest).parent != context
        assert context not in builder.log_path(layer.digest).parents

    def test_context_validation_happens_before_any_build_log_is_promised(
        self, builder: ImageBuilder
    ) -> None:
        # This check now lives in compose(), where it is a pure property of the
        # spec. Raising it from write_context meant raising a BuildError that
        # advertised a build log which had not been created yet.
        from jormungandr.runtime.compose import ComposeError
        from jormungandr.runtime.modules.base import Stage

        class OrphanFile:
            name = "orphan"
            stage = Stage.USER
            requires = ()

            def instructions(self, context):
                context.add_file("never-copied.sh", "echo hi")
                return [Run("true")]

            def identity(self):
                return {}

        from jormungandr.runtime.modules.registry import ModuleRegistry

        registry = ModuleRegistry()
        registry.register("orphan", OrphanFile)
        with pytest.raises(ComposeError, match="no COPY"):
            compose(simple_spec(modules=[{"name": "orphan"}]), registry=registry)


class TestBuild:
    def test_builds_both_tiers_when_absent(self, builder: ImageBuilder) -> None:
        result = builder.build(simple_spec())
        assert result.built
        assert not result.cached
        assert [layer.tier for layer in result.layers] == ["base", "runtime"]
        assert all(layer.log_path.exists() for layer in result.layers)

    def test_skips_when_both_tiers_exist(self, tmp_path: Path) -> None:
        composed = compose(simple_spec())
        docker = FakeDocker(
            existing={composed.base.reference, composed.runtime.reference}
        )
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        result = builder.build(simple_spec())
        assert result.cached
        assert docker.builds == []

    def test_cached_base_is_reused_while_the_runtime_rebuilds(
        self, tmp_path: Path
    ) -> None:
        # The payoff of tiering: an existing base is not rebuilt.
        composed = compose(simple_spec())
        docker = FakeDocker(existing={composed.base.reference})
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        result = builder.build(simple_spec())
        assert result.layers[0].cached
        assert not result.layers[1].cached
        assert len(docker.builds) == 1
        assert composed.runtime.reference in docker.builds[0]

    def test_force_rebuilds_and_disables_cache(self, tmp_path: Path) -> None:
        composed = compose(simple_spec())
        docker = FakeDocker(
            existing={composed.base.reference, composed.runtime.reference}
        )
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        result = builder.build(simple_spec(), force=True)
        assert result.built
        assert all("--no-cache" in build for build in docker.builds)

    def test_platform_passed_as_build_flag(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        builder.build(ImageSpec(target_platform="linux/amd64"))
        args = docker.builds[0]
        assert args[args.index("--platform") + 1] == "linux/amd64"

    def test_build_args_forwarded(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        builder = ImageBuilder(state_dir=tmp_path, docker=docker)
        builder.build(simple_spec(build_args={"FOO": "bar"}))
        assert "FOO=bar" in docker.builds[0]

    def test_tag_is_the_content_digest(self, builder: ImageBuilder) -> None:
        composed = compose(simple_spec())
        result = builder.build(simple_spec())
        assert result.reference.endswith(composed.digest)
        assert result.reference == composed.runtime.reference

    def test_log_records_the_dockerfile(self, builder: ImageBuilder) -> None:
        result = builder.build(simple_spec())
        log = result.layers[0].log_path.read_text()
        assert "FROM debian:trixie-slim" in log
        assert "#5 DONE" in log

    def test_failure_carries_the_log_path(self, tmp_path: Path) -> None:
        builder = ImageBuilder(state_dir=tmp_path, docker=FakeDocker(fail=True))
        with pytest.raises(BuildError) as excinfo:
            builder.build(simple_spec())
        assert excinfo.value.log_path.exists()
        assert "build log:" in str(excinfo.value)
        assert "BUILD FAILED" in excinfo.value.log_path.read_text()

    def test_daemon_is_checked_first(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        ImageBuilder(state_dir=tmp_path, docker=docker).build(simple_spec())
        assert docker.required

    def test_output_callback_receives_lines(self, builder: ImageBuilder) -> None:
        seen: list[str] = []
        builder.build(simple_spec(), on_output=seen.append)
        assert any("DONE" in line for line in seen)


class TestTerminateGroup:
    """Process-group termination is plain OS behaviour, testable without Docker."""

    def test_kills_grandchildren_not_just_the_direct_child(self) -> None:
        # The failure mode this exists to prevent: SWE-bench signals only the
        # top-level pid, so a shell's children outlive the "timeout".
        import os
        import time

        proc = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 60 & echo $!; wait"],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline().strip())
        assert _pid_alive(grandchild)

        DockerCli._terminate_group(proc, grace=1.0)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild):
            time.sleep(0.05)
        assert not _pid_alive(grandchild), "grandchild survived the group kill"

    def test_survives_an_already_dead_process(self) -> None:
        proc = subprocess.Popen(["/bin/sh", "-c", "true"], start_new_session=True)
        proc.wait()
        DockerCli._terminate_group(proc, grace=0.1)  # must not raise


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestDockerCliPlumbing:
    def test_missing_executable_is_reported_clearly(self) -> None:
        cli = DockerCli(executable="definitely-not-a-real-binary")
        with pytest.raises(Exception, match="not found on PATH"):
            cli.exec("container", ["true"], timeout=5)

    def test_available_is_false_without_the_binary(self) -> None:
        assert not DockerCli(executable="definitely-not-a-real-binary").available()

    def test_error_message_includes_the_command(self) -> None:
        err = DockerError(["docker", "build", "-t", "x", "ctx"], 2, "boom")
        assert "docker build -t x" in str(err)
        assert "boom" in str(err)


class TestCommandResult:
    def test_ok_requires_zero_and_no_timeout(self) -> None:
        assert CommandResult(0, "", "", 1.0).ok
        assert not CommandResult(1, "", "", 1.0).ok
        assert not CommandResult(0, "", "", 1.0, timed_out=True).ok
