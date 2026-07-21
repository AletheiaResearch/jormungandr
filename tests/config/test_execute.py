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
            collect_state_to=None, **kwargs):
        self.calls.append(
            {
                "harness": harness,
                "image": image,
                "prompts": list(prompts),
                "timeout": timeout,
                "spec": container_spec,
                "collect": collect_state_to,
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


class TestWorkspaces:
    def test_local_workspace_is_copied_not_mounted_in_place(
        self, project: Path, tmp_path: Path
    ) -> None:
        # An agent handed a bind mount edits the caller's source tree, and a run
        # that mutates its own inputs cannot be repeated.
        source = tmp_path / "src"
        source.mkdir()
        (source / "file.txt").write_text("original")
        record = PromptRecord(
            id="w", prompt="x", workspace={"type": "local", "path": str(source)}
        )
        runner = FakeRunner()
        report = execute(
            load(project),
            records=[record],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        copied = report.results[0].directory / "workspace" / "file.txt"
        assert copied.read_text() == "original"
        assert copied.resolve() != (source / "file.txt").resolve()
        assert any("/workspace" in m for m in runner.calls[0]["spec"].mounts)

    def test_missing_local_workspace_fails_that_record_only(self, project: Path) -> None:
        record = PromptRecord(
            id="w", prompt="x", workspace={"type": "local", "path": "/nope/missing"}
        )
        report = execute(
            load(project),
            records=[record],
            builder=FakeBuilder(),
            runner=FakeRunner(),
            available_env=ENV,
        )
        assert not report.ok
        assert "not a directory" in (report.results[0].error or "")

    def test_no_workspace_means_no_mount(self, project: Path) -> None:
        runner = FakeRunner()
        self.__class__  # noqa: B018
        execute(
            load(project),
            records=[PromptRecord(id="w", prompt="x")],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert runner.calls[0]["spec"].mounts == ()


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


class TestGitWorkspaces:
    """Exercised against real local git repositories — no network needed."""

    @pytest.fixture
    @staticmethod
    def repo(tmp_path_factory) -> Path:
        import subprocess

        root = tmp_path_factory.mktemp("repo")
        (root / "README.md").write_text("root readme\n")
        pkg = root / "services" / "api"
        pkg.mkdir(parents=True)
        (pkg / "main.py").write_text("print('api')\n")
        run = lambda *a: subprocess.run(  # noqa: E731
            a, cwd=root, check=True, capture_output=True
        )
        run("git", "init", "--quiet", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "T")
        run("git", "add", "-A")
        run("git", "commit", "--quiet", "-m", "first")
        run("git", "tag", "v1.0.0")
        (root / "README.md").write_text("changed after the tag\n")
        run("git", "add", "-A")
        run("git", "commit", "--quiet", "-m", "second")
        return root

    def execute_with(self, project: Path, git: dict):
        return execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w", git=git)],
            builder=FakeBuilder(),
            runner=FakeRunner(),
            available_env=ENV,
        )

    def test_whole_repo_lands_at_the_workspace_root(self, project, repo) -> None:
        report = self.execute_with(project, {"clone_url": str(repo)})
        workspace = report.results[0].directory / "workspace"
        assert (workspace / "README.md").exists()
        # A full clone keeps its history, so the agent can diff and commit.
        assert (workspace / ".git").is_dir()

    def test_ref_is_checked_out(self, project, repo) -> None:
        report = self.execute_with(project, {"clone_url": str(repo), "ref": "v1.0.0"})
        readme = report.results[0].directory / "workspace" / "README.md"
        assert readme.read_text() == "root readme\n"

    def test_default_branch_without_a_ref(self, project, repo) -> None:
        report = self.execute_with(project, {"clone_url": str(repo)})
        readme = report.results[0].directory / "workspace" / "README.md"
        assert readme.read_text() == "changed after the tag\n"

    def test_subdirectory_becomes_the_workspace(self, project, repo) -> None:
        report = self.execute_with(
            project, {"clone_url": str(repo), "subdirectory": "services/api"}
        )
        workspace = report.results[0].directory / "workspace"
        assert (workspace / "main.py").exists()
        assert not (workspace / "README.md").exists()
        # A subtree is not a repository; no history comes with it.
        assert not (workspace / ".git").exists()

    def test_clone_as_nests_the_content(self, project, repo) -> None:
        report = self.execute_with(project, {"clone_url": str(repo), "clone_as": "app"})
        workspace = report.results[0].directory / "workspace"
        assert (workspace / "app" / "README.md").exists()
        assert not (workspace / "README.md").exists()

    def test_subdirectory_and_clone_as_together(self, project, repo) -> None:
        report = self.execute_with(
            project,
            {"clone_url": str(repo), "ref": "v1.0.0",
             "subdirectory": "services/api", "clone_as": "api"},
        )
        workspace = report.results[0].directory / "workspace"
        assert (workspace / "api" / "main.py").exists()

    def test_missing_subdirectory_fails_that_record_clearly(self, project, repo) -> None:
        report = self.execute_with(
            project, {"clone_url": str(repo), "subdirectory": "does/not/exist"}
        )
        assert not report.ok
        assert "does/not/exist" in (report.results[0].error or "")

    def test_bad_ref_fails_that_record(self, project, repo) -> None:
        report = self.execute_with(project, {"clone_url": str(repo), "ref": "nope"})
        assert not report.ok
        assert "could not clone" in (report.results[0].error or "")

    def test_staging_directory_is_cleaned_up(self, project, repo) -> None:
        report = self.execute_with(
            project, {"clone_url": str(repo), "subdirectory": "services/api"}
        )
        assert not (report.results[0].directory / ".clone").exists()

    def test_github_repo_shorthand_reaches_the_same_path(self, project, repo) -> None:
        # github_repo desugars to a GitSource, so it walks the same code.
        record = PromptRecord(prompt="x", id="gh", github_repo="acme/app")
        assert record.workspace.git.clone_url == "https://github.com/acme/app"

    def test_workspace_is_mounted(self, project, repo) -> None:
        runner = FakeRunner()
        execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w", git={"clone_url": str(repo)})],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert any("/workspace" in m for m in runner.calls[0]["spec"].mounts)


class TestMountCollisions:
    """The workspace mount must go where the agent actually works."""

    def test_workspace_mounts_at_the_configured_workdir(self, project: Path, tmp_path) -> None:
        # Hardcoding /workspace while the workdir module says otherwise hands
        # the agent an empty directory and no error.
        import json as _json

        config = _json.loads(_json.dumps(
            {"version": 1,
             "providers": {"p": {"base_url": "https://h/v1", "api_key": "${K}",
                                 "models": {"m": "up"}}},
             "harness": {"name": "droid", "model": "p/m"},
             "prompts": {"file": "./prompts.jsonl"},
             "image": {"modules": [{"name": "workdir", "path": "/srv/app"}]}}))
        (project / "jorm.yaml").write_text(_json.dumps(config))
        source = tmp_path / "src"
        source.mkdir()
        runner = FakeRunner()
        execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w",
                                  workspace={"type": "local", "path": str(source)})],
            builder=FakeBuilder(),
            runner=runner,
            available_env={"K"},
        )
        assert any(m.endswith(":/srv/app") for m in runner.calls[0]["spec"].mounts)

    def test_a_colliding_run_mount_is_an_error(self, project: Path, tmp_path) -> None:
        # Docker takes the last of two mounts on one path and discards the
        # other silently, so the agent gets one with no indication which.
        (project / "jorm.yaml").write_text(
            CONFIG.replace("output:", "run:\n  mounts: ['/tmp/other:/workspace']\noutput:")
        )
        source = tmp_path / "src"
        source.mkdir()
        report = execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w",
                                  workspace={"type": "local", "path": str(source)})],
            builder=FakeBuilder(),
            runner=FakeRunner(),
            available_env=ENV,
        )
        assert not report.ok
        assert "already mounts" in (report.results[0].error or "")

    def test_a_run_mount_elsewhere_is_fine(self, project: Path, tmp_path) -> None:
        (project / "jorm.yaml").write_text(
            CONFIG.replace("output:", "run:\n  mounts: ['/tmp/other:/data']\noutput:")
        )
        source = tmp_path / "src"
        source.mkdir()
        runner = FakeRunner()
        report = execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w",
                                  workspace={"type": "local", "path": str(source)})],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        assert report.ok
        assert len(runner.calls[0]["spec"].mounts) == 2

    def test_run_mounts_alone_do_not_collide(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(
            CONFIG.replace("output:", "run:\n  mounts: ['/tmp/other:/workspace']\noutput:")
        )
        runner = FakeRunner()
        report = execute(
            load(project),
            records=[PromptRecord(prompt="x", id="w")],
            builder=FakeBuilder(),
            runner=runner,
            available_env=ENV,
        )
        # No record workspace, so the user's mount is the workspace. Fine.
        assert report.ok


class TestReviewRegressions:
    """Each of these reproduces a defect found by adversarial review."""

    def test_a_symlinked_subdirectory_is_refused(self, project: Path, tmp_path) -> None:
        # A repo can store `pkg` as a symlink to a host path. is_dir() follows
        # it, shutil.move relocates the link, and Docker resolves a bind-mount
        # source HOST-side — handing the agent that directory, writable, past
        # every capability and network restriction.
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "secret.txt").write_text("do not touch")
        (repo / "pkg").symlink_to(victim)
        (repo / "README.md").write_text("x")
        run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "--quiet", "-b", "main")
        run("git", "config", "user.email", "t@e.com")
        run("git", "config", "user.name", "T")
        run("git", "add", "-A")
        run("git", "commit", "--quiet", "-m", "x")

        report = execute(
            load(project),
            records=[
                PromptRecord(
                    prompt="x",
                    id="evil",
                    git={"clone_url": str(repo), "subdirectory": "pkg"},
                )
            ],
            builder=FakeBuilder(),
            runner=FakeRunner(),
            available_env=ENV,
        )
        assert not report.ok
        assert "symlink" in (report.results[0].error or "")
        assert (victim / "secret.txt").read_text() == "do not touch"

    def test_a_non_execution_error_fails_only_that_record(self, project: Path) -> None:
        # A clone can raise OSError (disk full) or PermissionError; losing 199
        # completed records because record 200 hit a full disk is the wrong
        # trade.
        import jormungandr.execute as execute_module

        original = execute_module._prepare_workspace

        def explode(record, directory):
            if record.id == "b":
                raise OSError("no space left on device")
            return original(record, directory)

        execute_module._prepare_workspace = explode
        try:
            report = execute(
                load(project), builder=FakeBuilder(), runner=FakeRunner(), available_env=ENV
            )
        finally:
            execute_module._prepare_workspace = original

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
