from __future__ import annotations

import json
from pathlib import Path

import pytest

from jormungandr.config import load_config
from jormungandr.config.prompts import PromptRecord
from jormungandr.execute import ExecutionError, execute
from jormungandr.runtime.docker import CommandResult
from jormungandr.runtime.run import HarnessRun, TurnResult

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
prompts:
  file: ./prompts.jsonl
output:
  dir: ./runs
"""


class FakeBuild:
    reference = "img:runtime-abc"
    cached = True


class FakeBuilder:
    def __init__(self) -> None:
        self.built: list = []

    def build(self, spec):
        self.built.append(spec)
        return FakeBuild()


class FakeRunner:
    """Records what it was asked to run; fabricates plausible results."""

    def __init__(self, *, fail_ids: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_ids = fail_ids or set()

    def run(self, *, harness, image, prompts, timeout=None, container_spec=None,
            collect_state_to=None, workdir=None, **kwargs):
        self.calls.append(
            {
                "harness": harness,
                "image": image,
                "prompts": list(prompts),
                "timeout": timeout,
                "spec": container_spec,
                "collect": collect_state_to,
                "workdir": workdir,
            }
        )
        failing = any(p in self.fail_ids for p in prompts)
        turns = tuple(
            TurnResult.from_command(
                i, p, CommandResult(3 if failing else 0, f"out:{p}", "", 0.1)
            )
            for i, p in enumerate(prompts)
        )
        return HarnessRun(
            harness=harness,
            image=image,
            turns=turns,
            state_paths=(".factory/sessions",),
            artifacts=collect_state_to,
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "jorm.yaml").write_text(CONFIG)
    (tmp_path / "prompts.jsonl").write_text(
        '{"id":"a","prompt":"first"}\n'
        '{"id":"b","prompt":"one","follow_up_prompts":["two"]}\n'
    )
    return tmp_path


def load(project: Path):
    return load_config(project / "jorm.yaml", apply_env=False)


ENV = {"OPENROUTER_API_KEY"}


class TestPreflight:
    def test_missing_env_stops_before_building(self, project: Path) -> None:
        # A full image build and N container starts, only to hit a 401, is a
        # bad way to learn a variable is unset.
        builder = FakeBuilder()
        with pytest.raises(ExecutionError, match="OPENROUTER_API_KEY"):
            execute(load(project), builder=builder, runner=FakeRunner(), available_env=set())
        assert builder.built == []

    def test_env_file_satisfies_the_requirement(self, project: Path) -> None:
        secrets = project / "secrets.env"
        secrets.write_text("# comment\nOPENROUTER_API_KEY=sk-whatever\n")
        (project / "jorm.yaml").write_text(
            CONFIG.replace("output:", f"run:\n  env_files: [{secrets}]\noutput:")
        )
        report = execute(
            load(project), builder=FakeBuilder(), runner=FakeRunner(), available_env=set()
        )
        assert report.ok

    def test_empty_prompt_set_is_an_error(self, project: Path) -> None:
        with pytest.raises(ExecutionError, match="no prompt records"):
            execute(
                load(project),
                records=[],
                builder=FakeBuilder(),
                runner=FakeRunner(),
                available_env=ENV,
            )


class TestExecute:
    def run_it(self, project: Path, **kwargs):
        return execute(
            load(project),
            builder=kwargs.pop("builder", FakeBuilder()),
            runner=kwargs.pop("runner", FakeRunner()),
            available_env=ENV,
            **kwargs,
        )

    def test_builds_once_and_runs_every_record(self, project: Path) -> None:
        builder, runner = FakeBuilder(), FakeRunner()
        report = self.run_it(project, builder=builder, runner=runner)
        assert len(builder.built) == 1
        assert len(runner.calls) == 2
        assert report.ok

    def test_multi_turn_records_pass_every_turn(self, project: Path) -> None:
        runner = FakeRunner()
        self.run_it(project, runner=runner)
        by_first = {c["prompts"][0]: c["prompts"] for c in runner.calls}
        assert by_first["one"] == ["one", "two"]

    def test_results_keep_input_order(self, project: Path) -> None:
        # Completion order is arbitrary; a report that reorders itself between
        # runs cannot be diffed.
        report = self.run_it(project)
        assert [r.id for r in report.results] == ["a", "b"]

    def test_per_record_directories_and_summaries(self, project: Path) -> None:
        report = self.run_it(project)
        for result in report.results:
            assert result.directory.name == result.id
            summary = json.loads((result.directory / "result.json").read_text())
            assert summary["id"] == result.id
            assert summary["ok"] is True

    def test_raw_output_is_written_beside_the_summary(self, project: Path) -> None:
        report = self.run_it(project)
        first = report.results[0].directory
        assert (first / "turn-0.stdout.txt").read_text() == "out:first"

    def test_report_json_is_written(self, project: Path) -> None:
        report = self.run_it(project)
        payload = json.loads((report.output_dir / "report.json").read_text())
        assert payload["total"] == 2
        assert sorted(payload["succeeded"]) == ["a", "b"]

    def test_a_failing_record_does_not_stop_the_others(self, project: Path) -> None:
        report = self.run_it(project, runner=FakeRunner(fail_ids={"first"}))
        assert not report.ok
        assert [r.id for r in report.failed] == ["a"]
        assert [r.id for r in report.succeeded] == ["b"]

    def test_a_crashing_runner_is_recorded_not_raised(self, project: Path) -> None:
        class Exploding(FakeRunner):
            def run(self, **kwargs):
                raise RuntimeError("daemon went away")

        report = self.run_it(project, runner=Exploding())
        assert not report.ok
        assert all("daemon went away" in (r.error or "") for r in report.results)
        summary = json.loads((report.results[0].directory / "result.json").read_text())
        assert summary["ok"] is False

    def test_state_collection_is_requested(self, project: Path) -> None:
        runner = FakeRunner()
        self.run_it(project, runner=runner)
        assert all(c["collect"] is not None for c in runner.calls)

    def test_state_collection_can_be_disabled(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(CONFIG + "  collect_state: false\n")
        runner = FakeRunner()
        self.run_it(project, runner=runner)
        assert all(c["collect"] is None for c in runner.calls)

    def test_progress_callback_fires_per_record(self, project: Path) -> None:
        seen: list[str] = []
        self.run_it(project, on_progress=lambda r: seen.append(r.id))
        assert sorted(seen) == ["a", "b"]

    def test_run_settings_reach_the_container_spec(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(
            CONFIG.replace("output:", "run:\n  network: none\n  cpus: 8\noutput:")
        )
        runner = FakeRunner()
        self.run_it(project, runner=runner)
        spec = runner.calls[0]["spec"]
        assert spec.network == "none"
        assert spec.limits.cpus == 8


class TestOverrides:
    def test_timeout_override_wins(self, project: Path) -> None:
        runner = FakeRunner()
        execute(
            load(project),
            records=[PromptRecord(id="w", prompt="x", overrides={"timeout": 42})],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert runner.calls[0]["timeout"] == 42

    def test_run_timeout_is_the_default(self, project: Path) -> None:
        runner = FakeRunner()
        execute(
            load(project),
            records=[PromptRecord(id="w", prompt="x")],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert runner.calls[0]["timeout"] == 900.0

    def test_max_turns_truncates(self, project: Path) -> None:
        runner = FakeRunner()
        execute(
            load(project),
            records=[
                PromptRecord(
                    id="w",
                    prompt="one",
                    follow_up_prompts=["two", "three"],
                    overrides={"max_turns": 2},
                )
            ],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert runner.calls[0]["prompts"] == ["one", "two"]

    def test_no_per_record_model_override_exists(self) -> None:
        # Model selection is baked into the image, so varying it per record
        # would mean an image per record. Compare models by running twice.
        with pytest.raises(Exception):
            PromptRecord(id="w", prompt="x", overrides={"model": "other/model"})


class TestGitWorkspacesBecomeAnImage:
    """A repository is a third image tier, not a host-side clone.

    This is SWE-bench's *instance* tier and exists for the same reason: the
    checkout is the most expensive per-record step and the one most worth
    caching. A retried run reuses the image; two records on the same commit
    share one.
    """

    def compose_for(self, **git):
        from jormungandr.execute import resolve_commit  # noqa: F401
        from jormungandr.runtime.compose import compose_workspace

        return compose_workspace(
            parent="demo:runtime-abc",
            repository="demo",
            clone_url=git.get("clone_url", "https://github.com/a/b"),
            commit="d" * 40,
            platform="linux/arm64",
            workdir=git.get("workdir", "/workspace"),
            user=git.get("user", "agent"),
            subdirectory=git.get("subdirectory"),
            clone_as=git.get("clone_as"),
        )

    def test_it_builds_on_the_runtime_image(self) -> None:
        layer = self.compose_for()
        assert layer.tier == "workspace"
        assert layer.parent == "demo:runtime-abc"
        assert "FROM demo:runtime-abc" in layer.dockerfile

    def test_the_commit_is_fetched_not_the_branch(self) -> None:
        # A digest is only honest about fixed content; caching a branch name
        # would serve yesterday's code from today's tag.
        layer = self.compose_for()
        assert f"fetch --quiet --depth 1 origin {'d' * 40}" in layer.dockerfile

    def test_the_same_commit_yields_the_same_image(self) -> None:
        assert self.compose_for().digest == self.compose_for().digest

    def test_a_different_commit_yields_a_different_image(self) -> None:
        from jormungandr.runtime.compose import compose_workspace

        other = compose_workspace(
            parent="demo:runtime-abc", repository="demo",
            clone_url="https://github.com/a/b", commit="e" * 40,
            platform="linux/arm64", workdir="/workspace", user="agent",
        )
        assert other.digest != self.compose_for().digest

    def test_a_different_runtime_parent_yields_a_different_image(self) -> None:
        from jormungandr.runtime.compose import compose_workspace

        other = compose_workspace(
            parent="demo:runtime-CHANGED", repository="demo",
            clone_url="https://github.com/a/b", commit="d" * 40,
            platform="linux/arm64", workdir="/workspace", user="agent",
        )
        assert other.digest != self.compose_for().digest

    def test_subdirectory_selects_the_content(self) -> None:
        layer = self.compose_for(subdirectory="services/api")
        assert "/tmp/jormungandr-clone/services/api/. /workspace/" in layer.dockerfile

    def test_clone_as_nests_the_content(self) -> None:
        layer = self.compose_for(clone_as="app")
        assert "/workspace/app/" in layer.dockerfile

    def test_it_ends_as_the_unprivileged_user(self) -> None:
        layer = self.compose_for(user="runner")
        lines = [l for l in layer.dockerfile.splitlines() if l.startswith("USER")]
        assert lines[0] == "USER root"   # cloning and chown need it
        assert lines[-1] == "USER runner"

    def test_git_is_installed_if_absent(self) -> None:
        # The package manager differs by distribution, same as useradd.
        layer = self.compose_for()
        assert "apt-get install" in layer.dockerfile
        assert "apk add" in layer.dockerfile

    def test_no_host_clone_happens(self, project: Path) -> None:
        # The record's directory must gain no workspace copy: the checkout
        # lives in the image.
        runner = FakeRunner()

        class RecordingBuilder(FakeBuilder):
            def __init__(self):
                super().__init__()
                self.layers = []

            def build_layer(self, layer, *, platform, **kwargs):
                self.layers.append(layer)
                return type(
                    "R", (), {"reference": f"demo:{layer.tier}-{layer.digest}"}
                )()

        builder = RecordingBuilder()
        import jormungandr.execute as ex

        original = ex.resolve_commit
        ex.resolve_commit = lambda url, ref: "d" * 40
        try:
            report = execute(
                load(project),
                records=[
                    PromptRecord(prompt="x", id="w", git={"clone_url": "https://h/r"})
                ],
                builder=builder,
                runner=runner,
                available_env=ENV,
            )
        finally:
            ex.resolve_commit = original

        assert report.ok
        assert builder.layers and builder.layers[0].tier == "workspace"
        # the container runs from the workspace image, not the runtime one
        assert runner.calls[0]["image"].startswith("demo:workspace-")

    def test_a_record_without_a_repo_uses_the_runtime_image(self, project: Path) -> None:
        runner = FakeRunner()
        execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w")],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert runner.calls[0]["image"] == FakeBuild.reference


class TestRefResolution:
    def test_a_full_sha_passes_through_without_network(self) -> None:
        from jormungandr.execute import resolve_commit

        sha = "a" * 40
        assert resolve_commit("https://unreachable.invalid/r", sha) == sha

    def test_an_unresolvable_repo_is_a_clear_error(self) -> None:
        from jormungandr.execute import ExecutionError, resolve_commit

        with pytest.raises(ExecutionError, match="could not resolve|timed out"):
            resolve_commit("https://github.invalid/nope/nope", "main")


class TestReviewRegressions:
    """Each of these reproduces a defect found by adversarial review."""

    def test_no_git_subprocess_runs_on_the_host_for_a_repo_record(
        self, project: Path, monkeypatch
    ) -> None:
        # The symlink escape that made a host-side clone dangerous cannot exist
        # once the clone happens in the image, where a symlink resolves against
        # the image's own filesystem. The guarantee is that nothing shells out
        # to git for the checkout at all — only ls-remote, to pin the commit.
        import subprocess as sp

        calls: list[list[str]] = []
        real = sp.run

        def record_run(args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                calls.append(list(args))
            return real(args, *a, **kw)

        monkeypatch.setattr(sp, "run", record_run)

        class Builder(FakeBuilder):
            def build_layer(self, layer, *, platform, **kwargs):
                return type("R", (), {"reference": "demo:workspace-x"})()

        import jormungandr.execute as ex

        original = ex.resolve_commit
        ex.resolve_commit = lambda url, ref: "d" * 40
        try:
            execute(
                load(project),
                records=[
                    PromptRecord(prompt="x", id="w", git={"clone_url": "https://h/r"})
                ],
                builder=Builder(),
                runner=FakeRunner(),
                available_env=ENV,
            )
        finally:
            ex.resolve_commit = original

        assert not [c for c in calls if "clone" in c], f"cloned on the host: {calls}"

    def test_a_non_execution_error_fails_only_that_record(self, project: Path) -> None:
        # A clone can raise OSError (disk full) or PermissionError; losing 199
        # completed records because record 200 hit a full disk is the wrong
        # trade.
        import jormungandr.execute as execute_module

        original = execute_module._image_for

        def explode(config, image, record, builder, directory, platform):
            if record.id == "b":
                raise OSError("no space left on device")
            return original(config, image, record, builder, directory, platform)

        execute_module._image_for = explode
        try:
            report = execute(
                load(project), builder=FakeBuilder(), runner=FakeRunner(), available_env=ENV
            )
        finally:
            execute_module._image_for = original

        assert [r.id for r in report.failed] == ["b"]
        assert [r.id for r in report.succeeded] == ["a"]
        # the report survived
        assert (report.output_dir / "report.json").exists()

    def test_a_rerun_does_not_merge_old_artifacts(self, project: Path) -> None:
        first = execute(
            load(project), builder=FakeBuilder(), runner=FakeRunner(), available_env=ENV
        )
        stale = first.results[0].directory / "turn-9.stdout.txt"
        stale.write_text("from a previous run")
        second = execute(
            load(project), builder=FakeBuilder(), runner=FakeRunner(), available_env=ENV
        )
        assert not (second.results[0].directory / "turn-9.stdout.txt").exists()

    def test_a_reserved_record_id_is_refused_before_running(self, project: Path) -> None:
        # Otherwise it collides with the run report and fails after every
        # container has already run.
        runner = FakeRunner()
        with pytest.raises(ExecutionError, match="reserved"):
            execute(
                load(project),
                records=[PromptRecord(prompt="x", id="report.json")],
                builder=FakeBuilder(),
                runner=runner,
                available_env=ENV,
            )
        assert runner.calls == []

    def test_available_env_is_not_mutated(self, project: Path) -> None:
        caller_set = set(ENV)
        execute(
            load(project),
            builder=FakeBuilder(),
            runner=FakeRunner(),
            available_env=caller_set,
        )
        assert caller_set == ENV

    def test_run_settings_actually_reach_the_container(self, project: Path) -> None:
        # Previously asserted nowhere: env, env_files and memory could have
        # been dropped silently.
        (project / "secrets.env").write_text("OPENROUTER_API_KEY=x\n")
        (project / "jorm.yaml").write_text(
            CONFIG.replace(
                "output:",
                "run:\n"
                "  env: {LANGFUSE_HOST: 'https://h'}\n"
                f"  env_files: ['{project / 'secrets.env'}']\n"
                "  memory: 2g\n"
                "  network: none\n"
                "output:",
            )
        )
        runner = FakeRunner()
        execute(load(project), builder=FakeBuilder(), runner=runner, available_env=set())
        spec = runner.calls[0]["spec"]
        assert spec.env["LANGFUSE_HOST"] == "https://h"
        assert spec.env_files and spec.env_files[0].endswith("secrets.env")
        assert spec.limits.memory == "2g"
        assert spec.network == "none"
