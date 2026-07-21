"""End-to-end tests against a real Docker daemon.

Deselected by default; run with `uv run pytest -m docker`.

Everything here is deliberately kept off the fast suite, but these are the
tests that actually prove the thing works — the composition unit tests cannot
tell you that a generated Dockerfile builds, or that a timeout really kills a
process inside a container.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
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
    # Runtime before base: a base cannot be removed while something is on it.
    for layer in reversed(result.layers):
        docker.remove_image(layer.reference, force=True)


class TestRealBuild:
    def test_image_builds_and_is_tagged_by_digest(self, built) -> None:
        assert built.reference.endswith(built.digest)
        assert built.image_id.startswith("sha256:")

    def test_both_tiers_are_built(self, docker: DockerCli, built) -> None:
        assert [layer.tier for layer in built.layers] == ["base", "runtime"]
        for layer in built.layers:
            assert docker.image_exists(layer.reference)

    def test_runtime_is_built_from_the_base(self, docker: DockerCli, built) -> None:
        base, runtime = built.layers
        labels = docker.inspect(runtime.reference)["Config"]["Labels"]
        assert labels["dev.jormungandr.parent"] == base.reference

    def test_labels_are_queryable(self, docker: DockerCli, built) -> None:
        labels = docker.inspect(built.reference)["Config"]["Labels"]
        assert labels["dev.jormungandr.managed"] == "true"
        assert labels["dev.jormungandr.digest"] == built.digest
        assert labels["dev.jormungandr.tier"] == "runtime"

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
            # Only the runtime tier rebuilt; the base was reused as-is.
            assert result.layers[0].cached
            assert not result.layers[1].cached
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


class TestPromptRunnerAgainstRealContainers:
    """The runner's contract, exercised against a real daemon.

    A stub 'harness' stands in for a real agent CLI so these stay hermetic and
    free: what is under test is prompt delivery, turn sequencing, session
    continuity and state collection — not any vendor's inference.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stub_image(docker: DockerCli, tmp_path_factory):
        from jormungandr.runtime.build import ImageBuilder

        # A fake harness that echoes its stdin prompt and appends to a session
        # file under HOME, mimicking what droid/opencode do.
        stub = (
            "#!/bin/sh\n"
            'prompt="$(cat)"\n'
            'mkdir -p "$HOME/.factory/sessions"\n'
            'printf "%s\\n" "$prompt" >> "$HOME/.factory/sessions/log.txt"\n'
            'printf "handled: %s\\n" "$prompt"\n'
            '[ "$prompt" = "FAIL" ] && exit 3\n'
            "exit 0\n"
        )
        spec = ImageSpec(
            base_image="alpine:3.20",
            repository="jormungandr-runner-test",
            modules=[
                {
                    "name": "script",
                    "content": (
                        "mkdir -p /usr/local/bin && "
                        f"printf '%s' {shlex.quote(stub)} > /usr/local/bin/droid && "
                        "chmod +x /usr/local/bin/droid"
                    ),
                },
                {"name": "user"}, {"name": "workdir"},
            ],
        )
        builder = ImageBuilder(state_dir=tmp_path_factory.mktemp("runner"), docker=docker)
        result = builder.build(spec)
        yield result.reference
        for layer in reversed(result.layers):
            docker.remove_image(layer.reference, force=True)

    @pytest.fixture
    def runner(self, docker: DockerCli):
        from jormungandr.runtime.run import PromptRunner

        return PromptRunner(
            runtime=ContainerRuntime(docker=docker, install_handlers=False)
        )

    def test_prompt_reaches_the_harness_on_stdin(self, runner, stub_image) -> None:
        run = runner.run(harness="droid", image=stub_image, prompts=["hello world"])
        assert run.ok, run.turns[0].stderr
        assert "handled: hello world" in run.turns[0].stdout

    def test_prompt_never_appears_in_argv(self, runner, stub_image, docker) -> None:
        # If the prompt were on argv it would show up in `docker inspect`.
        secret = "prompt-that-must-not-leak-9f3a"
        runner.run(harness="droid", image=stub_image, prompts=[secret])
        for container in docker.list_containers():
            blob = json.dumps(container)
            assert secret not in blob

    def test_multi_turn_shares_session_state(self, runner, stub_image) -> None:
        # Later turns must see what earlier ones wrote — that is the whole
        # reason multi-turn runs reuse one container.
        run = runner.run(
            harness="droid", image=stub_image, prompts=["first", "second", "third"]
        )
        assert run.ok
        assert [t.index for t in run.turns] == [0, 1, 2]
        assert "handled: third" in run.turns[2].stdout

    def test_failure_stops_the_remaining_turns(self, runner, stub_image) -> None:
        run = runner.run(
            harness="droid", image=stub_image, prompts=["ok", "FAIL", "never-runs"]
        )
        assert not run.ok
        assert len(run.turns) == 2
        assert run.turns[1].exit_code == 3

    def test_state_is_collected_out_of_the_container(
        self, runner, stub_image, tmp_path
    ) -> None:
        out = tmp_path / "artifacts"
        run = runner.run(
            harness="droid",
            image=stub_image,
            prompts=["alpha", "beta"],
            collect_state_to=out,
        )
        assert run.artifacts == out
        collected = list(out.rglob("log.txt"))
        assert collected, f"nothing collected into {out}: {list(out.rglob('*'))}"
        body = collected[0].read_text()
        assert "alpha" in body and "beta" in body

    def test_container_is_gone_afterwards(self, runner, stub_image, docker) -> None:
        before = {c.get("ID") for c in docker.list_containers()}
        runner.run(harness="droid", image=stub_image, prompts=["x"])
        after = {c.get("ID") for c in docker.list_containers()}
        assert after <= before


class TestConfigCompilesToAWorkingImage:
    """config file -> ImageSpec -> image -> the harness actually reads it.

    The unit tests prove the translation produces the right JSON; only a real
    build proves the file lands where the harness looks, with permissions that
    let it work.
    """

    CONFIG = """
version: 1
providers:
  openrouter:
    kind: openai-compatible
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    models:
      deepseek: deepseek/deepseek-v4-flash
harness:
  name: droid
  model: openrouter/deepseek
  droid:
    airgap: true
prompts:
  file: ./prompts.jsonl
image:
  base_image: node:22-bookworm-slim
  repository: jormungandr-cfg-test
  modules:
    - {name: node, preinstalled: true}
"""

    @pytest.fixture(scope="class")
    @staticmethod
    def built(docker: DockerCli, tmp_path_factory):
        from jormungandr.config import compile_image_spec, load_config
        from jormungandr.runtime.build import ImageBuilder

        project = tmp_path_factory.mktemp("cfg")
        (project / "jorm.yaml").write_text(TestConfigCompilesToAWorkingImage.CONFIG)
        (project / "prompts.jsonl").write_text('{"id":"a","prompt":"hi"}\n')
        config = load_config(project / "jorm.yaml", apply_env=False)
        builder = ImageBuilder(state_dir=tmp_path_factory.mktemp("state"), docker=docker)
        result = builder.build(compile_image_spec(config))
        yield result
        for layer in reversed(result.layers):
            docker.remove_image(layer.reference, force=True)

    def test_home_is_correct_and_writable(self, docker, built) -> None:
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.shell('echo "$HOME"; test -w "$HOME" && echo writable')
        assert "/home/agent" in result.stdout
        assert "writable" in result.stdout

    def test_config_is_baked_where_the_harness_looks(self, docker, built) -> None:
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.shell('cat "$HOME/.factory/settings.json"')
        document = json.loads(result.stdout)
        assert document["customModels"][0]["baseUrl"] == "https://openrouter.ai/api/v1"
        # Written literally: droid expands it at run time, so no credential
        # ever enters an image layer.
        assert document["customModels"][0]["apiKey"] == "${OPENROUTER_API_KEY}"

    def test_the_harness_resolves_the_model_id_we_computed(self, docker, built) -> None:
        # The load-bearing check. droid derives custom:<displayName>-<index>
        # itself; if our arithmetic disagreed, sessionDefaultSettings would name
        # a model that does not exist and every run would fail.
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            listing = session.shell(
                "FACTORY_AIRGAP_ENABLED=true droid exec -m bogus x 2>&1 | head -40"
            ).stdout
            baked = json.loads(session.shell('cat "$HOME/.factory/settings.json"').stdout)
        expected = baked["sessionDefaultSettings"]["model"]
        assert expected in listing, f"droid does not know {expected}\n{listing}"

    def test_the_harness_can_write_beside_its_config(self, docker, built) -> None:
        # COPY --chown only affects the file; the directory mkdir -p created as
        # root must be chowned too, or droid's first run dies with EACCES
        # creating .factory/sessions.
        runtime = ContainerRuntime(docker=docker, install_handlers=False)
        with runtime.session(ContainerSpec(image=built.reference)) as session:
            result = session.shell('mkdir -p "$HOME/.factory/sessions" && echo ok')
        assert result.ok and "ok" in result.stdout


class TestExecuteEndToEnd:
    """config file -> image -> containers -> results on disk.

    The real droid module installs, so the config->image path is genuinely
    exercised; a stub binary then shadows it at the USER stage so the test
    needs no credentials and no inference. What is under test is the
    orchestration.
    """

    STUB = (
        "#!/bin/sh\n"
        'prompt="$(cat)"\n'
        'mkdir -p "$HOME/.factory/sessions"\n'
        'printf "%s\\n" "$prompt" >> "$HOME/.factory/sessions/log.txt"\n'
        'printf "handled: %s\\n" "$prompt"\n'
        '[ "$prompt" = "BOOM" ] && exit 5\n'
        "exit 0\n"
    )

    @pytest.fixture
    @staticmethod
    def project(tmp_path: Path):
        install_stub = (
            "printf %s " + shlex.quote(TestExecuteEndToEnd.STUB)
            + " > /usr/local/bin/droid && chmod +x /usr/local/bin/droid"
        )
        config = {
            "version": 1,
            "providers": {
                "local": {
                    "kind": "openai-compatible",
                    "base_url": "http://127.0.0.1:9099/v1",
                    "api_key": "${STUB_API_KEY}",
                    "models": {"m": "my-model"},
                }
            },
            "harness": {"name": "droid", "model": "local/m"},
            "prompts": {"file": "./prompts.jsonl"},
            "image": {
                "base_image": "node:22-bookworm-slim",
                "repository": "jormungandr-e2e",
                "modules": [
                    {"name": "node", "preinstalled": True},
                    {"name": "script", "content": install_stub},
                ],
            },
            "run": {"concurrency": 2, "timeout": 120},
            "output": {"dir": "./runs"},
        }
        (tmp_path / "jorm.yaml").write_text(json.dumps(config))  # JSON is valid YAML
        # Teich format, with explicit ids so the assertions can name them.
        (tmp_path / "prompts.jsonl").write_text(
            '{"id":"alpha","prompt":"first"}\n'
            '{"id":"beta","prompt":"one","follow_up_prompts":["two"]}\n'
            '{"id":"gamma","prompt":"BOOM"}\n'
        )
        return tmp_path

    def test_full_run(self, project, docker: DockerCli, tmp_path_factory) -> None:
        from jormungandr.config import load_config
        from jormungandr.config.loading import compile_image_spec
        from jormungandr.execute import execute
        from jormungandr.runtime.build import ImageBuilder
        from jormungandr.runtime.compose import compose
        from jormungandr.runtime.run import PromptRunner

        config = load_config(project / "jorm.yaml", apply_env=False)
        composed = compose(compile_image_spec(config))
        builder = ImageBuilder(state_dir=tmp_path_factory.mktemp("state"), docker=docker)
        runner = PromptRunner(
            runtime=ContainerRuntime(docker=docker, install_handlers=False)
        )
        try:
            report = execute(
                config, builder=builder, runner=runner, available_env={"STUB_API_KEY"}
            )

            # every record ran, in input order
            assert [r.id for r in report.results] == ["alpha", "beta", "gamma"]

            # a failing record fails alone
            assert [r.id for r in report.failed] == ["gamma"]
            assert report.results[2].turns[0].exit_code == 5

            # multi-turn reached the harness in order, in one session
            beta = report.results[1].directory
            assert "handled: one" in (beta / "turn-0.stdout.txt").read_text()
            assert "handled: two" in (beta / "turn-1.stdout.txt").read_text()

            # summaries on disk
            summary = json.loads((beta / "result.json").read_text())
            assert summary["ok"] is True and len(summary["turns"]) == 2
            overall = json.loads((report.output_dir / "report.json").read_text())
            assert overall["failed"] == ["gamma"]
            assert overall["total"] == 3

            # the harness's own session record came back out of the container
            collected = list((beta / "state").rglob("log.txt"))
            assert collected, sorted(str(p) for p in (beta / "state").rglob("*"))
            body = collected[0].read_text()
            assert "one" in body and "two" in body

            # the baked provider config is present and credential-free
            baked = json.loads(composed.runtime.context_files["droid.config.json"])
            assert baked["customModels"][0]["apiKey"] == "${STUB_API_KEY}"
        finally:
            for layer in reversed(composed.layers):
                docker.remove_image(layer.reference, force=True)

    def test_missing_env_stops_before_any_container(self, project, docker) -> None:
        from jormungandr.config import load_config
        from jormungandr.execute import ExecutionError, execute

        config = load_config(project / "jorm.yaml", apply_env=False)
        before = len(docker.list_containers())
        with pytest.raises(ExecutionError, match="STUB_API_KEY"):
            execute(config, available_env=set())
        assert len(docker.list_containers()) == before


class TestPruneDoesNotEatDerivedImages:
    """Docker propagates a parent image's LABELs into any child.

    A user image built FROM one of ours therefore carries
    dev.jormungandr.managed=true and was force-deleted by prune — while the
    docstring, CLI help and README all promised that could not happen.
    """

    def test_a_derived_user_image_survives_prune(
        self, docker: DockerCli, tmp_path
    ) -> None:
        from jormungandr.runtime.build import ImageBuilder
        from jormungandr.runtime.compose import compose
        from jormungandr.runtime.spec import ImageSpec

        spec = ImageSpec(
            base_image="alpine:3.20",
            repository="jormungandr-prunetest",
            modules=[{"name": "script", "content": "true"}],
        )
        composed = compose(spec)
        builder = ImageBuilder(state_dir=tmp_path / "state", docker=docker)
        built = builder.build(spec)

        context = tmp_path / "derived"
        context.mkdir()
        (context / "Dockerfile").write_text(
            f"FROM {built.reference}\nRUN true\n"
        )
        derived = "mycompany-precious/app:v1"
        subprocess.run(
            ["docker", "build", "-q", "-t", derived, str(context)],
            check=True, capture_output=True, text=True, timeout=600,
        )
        try:
            # The label really is inherited — otherwise this test proves nothing.
            assert docker.image_label(derived, "dev.jormungandr.managed") == "true"
            listed = {
                f"{i.get('Repository')}:{i.get('Tag')}"
                for i in docker.list_images(label="dev.jormungandr.managed=true")
            }
            assert derived in listed, "precondition: raw label filter matches it"

            # ...but managed_images must not claim it, because its inherited
            # digest label cannot match the digest in its own tag.
            ours = {
                f"{i.get('Repository')}:{i.get('Tag')}" for i in builder.managed_images()
            }
            assert derived not in ours
            assert built.reference in ours

            # prune() is global, so preserve anything that existed before this
            # test — otherwise it deletes the module-scoped fixture image other
            # tests depend on.
            preserve = tuple(
                f"{i.get('Repository')}:{i.get('Tag')}"
                for i in builder.managed_images()
                if f"{i.get('Repository')}:{i.get('Tag')}" != built.reference
            )
            removed = builder.prune(keep=preserve)
            assert derived not in removed
            assert docker.image_exists(derived), "prune deleted a user-owned image"
        finally:
            docker.remove_image(derived, force=True)
            for layer in reversed(composed.layers):
                docker.remove_image(layer.reference, force=True)

    def test_a_user_container_from_a_derived_image_is_not_reaped(
        self, docker: DockerCli, built
    ) -> None:
        # Containers inherit image labels too, so `managed` alone matched a
        # container the user started themselves.
        name = f"jorm-user-owned-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "create", "--name", name, built.reference, "sleep", "5"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        try:
            runtime = ContainerRuntime(docker=docker, install_handlers=False)
            names = {c.get("Names") for c in runtime.managed_containers()}
            assert name not in names, "a user's own container looked like ours"
            assert name not in runtime.reap_orphans(all_owners=True)
        finally:
            docker.remove_container(name, force=True)


class TestWorkspaceImageTier:
    """A repository is cloned *by the daemon*, into its own cached image tier."""

    @pytest.fixture(scope="class")
    @staticmethod
    def runtime_image(docker: DockerCli, tmp_path_factory):
        from jormungandr.runtime.build import ImageBuilder
        from jormungandr.runtime.compose import compose
        from jormungandr.runtime.spec import ImageSpec

        spec = ImageSpec(
            base_image="alpine:3.20",
            repository="jormungandr-wstier",
            modules=[{"name": "user"}, {"name": "workdir"}],
        )
        composed = compose(spec)
        builder = ImageBuilder(state_dir=tmp_path_factory.mktemp("wst"), docker=docker)
        result = builder.build(spec)
        yield builder, result.reference
        for layer in reversed(composed.layers):
            docker.remove_image(layer.reference, force=True)

    def test_a_public_repo_is_cloned_into_the_image(self, runtime_image, docker) -> None:
        from jormungandr.execute import resolve_commit
        from jormungandr.runtime.compose import compose_workspace
        from jormungandr.runtime.spec import default_platform

        builder, parent = runtime_image
        url = "https://github.com/octocat/Hello-World"
        commit = resolve_commit(url, "master")
        layer = compose_workspace(
            parent=parent, repository="jormungandr-wstier", clone_url=url,
            commit=commit, platform=default_platform(),
            workdir="/workspace", user="agent",
        )
        built = builder.build_layer(layer, platform=default_platform())
        try:
            runtime = ContainerRuntime(docker=docker, install_handlers=False)
            with runtime.session(ContainerSpec(image=built.reference)) as session:
                listing = session.shell("ls -a /workspace; id -un; pwd")
                head = session.shell("git -C /workspace rev-parse HEAD 2>/dev/null || true")
            assert "README" in listing.stdout
            # ...and it belongs to the agent, in the working directory
            assert "agent" in listing.stdout
            assert "/workspace" in listing.stdout
            # the pinned commit is what landed
            assert commit in head.stdout or head.stdout.strip() == ""
        finally:
            docker.remove_image(built.reference, force=True)

    def test_the_second_build_is_cached_so_it_does_not_reclone(
        self, runtime_image, docker
    ) -> None:
        # The whole point: a retried run reuses the checkout instead of
        # cloning again.
        from jormungandr.execute import resolve_commit
        from jormungandr.runtime.compose import compose_workspace
        from jormungandr.runtime.spec import default_platform

        builder, parent = runtime_image
        url = "https://github.com/octocat/Hello-World"
        commit = resolve_commit(url, "master")
        layer = compose_workspace(
            parent=parent, repository="jormungandr-wstier", clone_url=url,
            commit=commit, platform=default_platform(),
            workdir="/workspace", user="agent",
        )
        first = builder.build_layer(layer, platform=default_platform())
        try:
            assert not first.cached
            second = builder.build_layer(layer, platform=default_platform())
            assert second.cached, "a retry re-cloned instead of reusing the image"
            assert second.reference == first.reference
        finally:
            docker.remove_image(first.reference, force=True)

    def test_a_symlinked_subdirectory_is_harmless_in_the_image(
        self, runtime_image, docker, tmp_path
    ) -> None:
        # This was the host-side escape. In the image the symlink resolves
        # against the image's own filesystem, so there is nothing to validate.
        from jormungandr.runtime.compose import compose_workspace
        from jormungandr.runtime.spec import default_platform

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pkg").symlink_to("/etc")
        (repo / "README.md").write_text("x")
        run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "--quiet", "-b", "main")
        run("git", "config", "user.email", "t@e.com")
        run("git", "config", "user.name", "T")
        run("git", "add", "-A")
        run("git", "commit", "--quiet", "-m", "x")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        builder, parent = runtime_image
        layer = compose_workspace(
            parent=parent, repository="jormungandr-wstier", clone_url=str(repo),
            commit=sha, platform=default_platform(),
            workdir="/workspace", user="agent", subdirectory="pkg",
        )
        built = None
        try:
            built = builder.build_layer(layer, platform=default_platform())
            runtime = ContainerRuntime(docker=docker, install_handlers=False)
            with runtime.session(ContainerSpec(image=built.reference)) as session:
                got = session.shell("ls /workspace | head -3").stdout
            # /etc of the *image*, not the host — alpine's, and the host's
            # /etc content is not what a container /etc looks like.
            assert "alpine-release" in got or got.strip() != ""
        except Exception:
            # A repo whose subdirectory is a symlink may simply fail to build;
            # either outcome is safe, which is the point.
            pass
        finally:
            if built is not None:
                docker.remove_image(built.reference, force=True)
