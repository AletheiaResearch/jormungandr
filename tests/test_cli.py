from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jormungandr.commands import jobs

CONFIG = {
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
    "image": {"base_image": "alpine:3.20", "repository": "cli-test"},
    "output": {"dir": "./runs"},
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "jorm.yaml").write_text(json.dumps(CONFIG))
    (tmp_path / "prompts.jsonl").write_text(
        '{"id":"a","prompt":"x"}\n'
        '{"id":"b","prompt":"y","follow_up_prompts":["z"]}\n'
    )
    return tmp_path


def cli(*args: str, cwd: Path | None = None, env: dict | None = None):
    import os

    return subprocess.run(
        [sys.executable, "-m", "jormungandr", *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
    )


class TestHelp:
    def test_lists_every_command(self) -> None:
        out = cli("--help").stdout
        for command in ("build", "check", "prune", "reap", "render", "run"):
            assert command in out

    def test_version(self) -> None:
        assert cli("--version").returncode == 0

    def test_help_does_not_import_docker_machinery(self) -> None:
        # cli.py is declarations only; command bodies import lazily so --help
        # stays fast and works without a daemon.
        probe = (
            "import sys; import jormungandr.cli; "
            "print('jormungandr.execute' in sys.modules, "
            "'jormungandr.runtime.build' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
        ).stdout
        assert out.strip() == "False False"


class TestCheck:
    def test_reports_image_prompts_and_env(self, project: Path) -> None:
        result = cli("check", "jorm.yaml", cwd=project, env={"STUB_API_KEY": "x"})
        assert result.returncode == 0, result.stderr
        assert "cli-test:runtime-" in result.stdout
        assert "2 records" in result.stdout
        assert "1 multi-turn" in result.stdout

    def test_missing_env_fails_with_a_named_variable(self, project: Path) -> None:
        import os

        env = {k: v for k, v in os.environ.items() if k != "STUB_API_KEY"}
        result = subprocess.run(
            [sys.executable, "-m", "jormungandr", "check", "jorm.yaml"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(project),
            env=env,
        )
        assert result.returncode == 1
        assert "STUB_API_KEY" in result.stderr

    def test_env_file_satisfies_the_check(self, project: Path) -> None:
        (project / "secrets.env").write_text("STUB_API_KEY=whatever\n")
        config = {**CONFIG, "run": {"env_files": ["./secrets.env"]}}
        (project / "jorm.yaml").write_text(json.dumps(config))
        import os

        env = {k: v for k, v in os.environ.items() if k != "STUB_API_KEY"}
        result = subprocess.run(
            [sys.executable, "-m", "jormungandr", "check", "jorm.yaml"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(project),
            env=env,
        )
        assert result.returncode == 0, result.stderr

    def test_missing_config_is_a_clean_error(self, project: Path) -> None:
        result = cli("check", "nope.yaml", cwd=project)
        assert result.returncode != 0
        assert "not found" in result.stderr
        assert "Traceback" not in result.stderr

    def test_invalid_config_is_a_clean_error(self, project: Path) -> None:
        (project / "bad.yaml").write_text("version: 1\n")
        result = cli("check", "bad.yaml", cwd=project)
        assert result.returncode != 0
        assert "Traceback" not in result.stderr


class TestRender:
    def test_prints_both_tiers(self, project: Path) -> None:
        result = cli("render", "jorm.yaml", cwd=project)
        assert result.returncode == 0
        assert "base tier" in result.stdout and "runtime tier" in result.stdout

    def test_single_tier(self, project: Path) -> None:
        result = cli("render", "jorm.yaml", "--tier", "base", cwd=project)
        assert "base tier" in result.stdout
        assert "runtime tier" not in result.stdout

    def test_unknown_tier_is_rejected(self, project: Path) -> None:
        result = cli("render", "jorm.yaml", "--tier", "middle", cwd=project)
        assert result.returncode == 1
        assert "unknown tier" in result.stderr

    def test_renders_without_a_daemon(self, project: Path) -> None:
        # Composition is pure; rendering must not need Docker.
        result = cli(
            "render", "jorm.yaml", cwd=project, env={"DOCKER_HOST": "unix:///nope.sock"}
        )
        assert result.returncode == 0


class TestOverrides:
    def test_limit_shortens_the_record_set(self, project: Path) -> None:
        from jormungandr.config import load_config, resolve_prompts

        config = load_config(project / "jorm.yaml", apply_env=False)
        assert len(resolve_prompts(config)) == 2
        limited = config.model_copy(
            update={"prompts": config.prompts.model_copy(update={"limit": 1})}
        )
        assert len(resolve_prompts(limited)) == 1

    def test_concurrency_and_output_overrides_apply(self, project: Path) -> None:
        from jormungandr.config import load_config

        config = load_config(project / "jorm.yaml", apply_env=False)
        updated = jobs._with_run_overrides(
            config, concurrency=7, output=Path("/tmp/elsewhere")
        )
        assert updated.run.concurrency == 7
        assert updated.output.dir == Path("/tmp/elsewhere")

    def test_no_overrides_leaves_config_alone(self, project: Path) -> None:
        from jormungandr.config import load_config

        config = load_config(project / "jorm.yaml", apply_env=False)
        updated = jobs._with_run_overrides(config, concurrency=None, output=None)
        assert updated.run.concurrency == config.run.concurrency
        assert updated.output.dir == config.output.dir


class TestFailureSummary:
    def test_names_the_failing_turn(self) -> None:
        from jormungandr.runtime.docker import CommandResult
        from jormungandr.runtime.run import TurnResult

        turns = (
            TurnResult.from_command(0, "a", CommandResult(0, "", "", 0.1)),
            TurnResult.from_command(1, "b", CommandResult(7, "", "", 0.1)),
        )
        assert jobs._first_failure(turns) == "turn 1 exited 7"

    def test_timeout_is_distinguished_from_an_exit_code(self) -> None:
        from jormungandr.runtime.docker import CommandResult
        from jormungandr.runtime.run import TurnResult

        turns = (
            TurnResult.from_command(
                0, "a", CommandResult(124, "", "", 9.0, timed_out=True)
            ),
        )
        assert "timed out" in jobs._first_failure(turns)


@pytest.mark.docker
class TestRunAgainstDocker:
    """`jormungandr run` end to end, as a user would invoke it."""

    @pytest.fixture
    @staticmethod
    def project(tmp_path: Path) -> Path:
        import shlex

        stub = (
            "#!/bin/sh\n"
            'prompt="$(cat)"\n'
            'printf "handled: %s\\n" "$prompt"\n'
            '[ "$prompt" = "BOOM" ] && exit 5\n'
            "exit 0\n"
        )
        config = {
            **CONFIG,
            "image": {
                "base_image": "node:22-bookworm-slim",
                "repository": "jormungandr-cli-e2e",
                "modules": [
                    {"name": "node", "preinstalled": True},
                    {
                        "name": "script",
                        "content": "printf %s " + shlex.quote(stub)
                        + " > /usr/local/bin/droid && chmod +x /usr/local/bin/droid",
                    },
                ],
            },
            "run": {"concurrency": 2, "timeout": 120},
        }
        (tmp_path / "jorm.yaml").write_text(json.dumps(config))
        (tmp_path / "prompts.jsonl").write_text(
            '{"id":"alpha","prompt":"first"}\n'
            '{"id":"beta","prompt":"one","follow_up_prompts":["two"]}\n'
            '{"id":"gamma","prompt":"BOOM"}\n'
        )
        return tmp_path

    @staticmethod
    def _cleanup(project: Path) -> None:
        from jormungandr.config import load_config
        from jormungandr.config.loading import compile_image_spec
        from jormungandr.runtime.compose import compose
        from jormungandr.runtime.docker import DockerCli

        composed = compose(compile_image_spec(load_config(project / "jorm.yaml", apply_env=False)))
        docker = DockerCli()
        for layer in reversed(composed.layers):
            docker.remove_image(layer.reference, force=True)

    def test_run_reports_progress_and_exits_non_zero(self, project: Path) -> None:
        try:
            result = cli("run", "jorm.yaml", cwd=project, env={"STUB_API_KEY": "x"})
            assert result.returncode == 1, result.stdout + result.stderr

            out = result.stdout
            assert "jormungandr-cli-e2e:runtime-" in out
            assert "[1/3]" in out and "[3/3]" in out
            assert "2 ok, 1 failed" in out
            # the failing record says why, not just that
            assert "exited 5" in out

            # results are on disk
            report = json.loads((project / "runs" / "report.json").read_text())
            assert report["failed"] == ["gamma"]
            beta = json.loads((project / "runs" / "beta" / "result.json").read_text())
            assert len(beta["turns"]) == 2
        finally:
            self._cleanup(project)

    def test_limit_runs_only_the_first_records(self, project: Path) -> None:
        try:
            result = cli(
                "run", "jorm.yaml", "--limit", "2", cwd=project, env={"STUB_API_KEY": "x"}
            )
            # alpha and beta both pass, so a limited run succeeds
            assert result.returncode == 0, result.stdout + result.stderr
            assert "2 record(s): 2 ok" in result.stdout
            assert not (project / "runs" / "gamma").exists()
        finally:
            self._cleanup(project)

    def test_default_output_is_quiet(self, project: Path) -> None:
        # Internal INFO logs carry a level and logger name that only add noise
        # beside the progress the command prints itself.
        try:
            result = cli("run", "jorm.yaml", "--limit", "1", cwd=project, env={"STUB_API_KEY": "x"})
            assert "INFO" not in result.stderr
        finally:
            self._cleanup(project)
