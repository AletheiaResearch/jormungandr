from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jormungandr.commands.container import build_container_spec
from jormungandr.commands.image import load_spec, render_spec
from jormungandr.runtime.modules import builtin  # noqa: F401


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jormungandr", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestFlagRouting:
    """Regression tests for flags leaking between jormungandr and the container.

    `container exec c git --version` must print git's version, not ours. The
    meta app sees the whole command line, so it happily answered --version for
    a command that was meant for the container.
    """

    def test_root_version_still_works(self) -> None:
        result = run_cli("--version")
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_root_help_still_works(self) -> None:
        result = run_cli("--help")
        assert "Compose Docker images" in result.stdout

    def test_subcommand_help_still_works(self) -> None:
        assert "Build and manage harness images" in run_cli("image", "--help").stdout

    def test_meta_does_not_claim_version(self) -> None:
        from jormungandr.cli import app

        assert app.meta.version_flags == ()
        assert app.meta.help_flags == ()

    def test_exec_command_does_not_claim_version(self) -> None:
        from jormungandr.cli import container_app

        exec_app = container_app["exec"]
        assert exec_app.version_flags == ()
        assert exec_app.help_flags == ()


class TestLoadSpec:
    def test_yaml_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text(
            "base_image: alpine:3.20\n"
            "repository: demo\n"
            "modules:\n"
            "  - name: apt\n"
            "    packages: [git]\n"
        )
        spec = load_spec(path)
        assert spec.base_image == "alpine:3.20"
        assert spec.modules[0].name == "apt"

    def test_json_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.json"
        path.write_text('{"base_image": "alpine:3.20"}')
        assert load_spec(path).base_image == "alpine:3.20"

    def test_override_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("base_image: alpine:3.20\n")
        assert load_spec(path, base_image="debian:trixie-slim").base_image == (
            "debian:trixie-slim"
        )

    def test_none_override_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("base_image: alpine:3.20\n")
        assert load_spec(path, base_image=None).base_image == "alpine:3.20"

    def test_no_path_uses_defaults(self) -> None:
        assert load_spec(None).base_image

    def test_non_mapping_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(ValueError, match="expected a mapping"):
            load_spec(path)

    def test_render_produces_a_dockerfile(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("base_image: alpine:3.20\n")
        assert "FROM alpine:3.20" in render_spec(load_spec(path))


class TestBuildContainerSpec:
    def test_env_parsing(self) -> None:
        spec = build_container_spec("img", env=("A=1", "B=2"))
        assert spec.env == {"A": "1", "B": "2"}

    def test_env_with_equals_in_value(self) -> None:
        assert build_container_spec("img", env=("A=b=c",)).env == {"A": "b=c"}

    def test_malformed_env_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected KEY=VALUE"):
            build_container_spec("img", env=("JUSTAKEY",))

    def test_default_command_keeps_container_alive(self) -> None:
        assert build_container_spec("img").command == ("sleep", "infinity")

    def test_limits_override(self) -> None:
        spec = build_container_spec("img", cpus=8.0, memory="16g")
        assert spec.limits.cpus == 8.0
        assert spec.limits.memory == "16g"
