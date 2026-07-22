"""The install-strategy abstraction must fit harnesses that are not npm packages.

Only OpenCode ships as a registered module — the others below are constructed
directly, to prove the mechanism accommodates their shapes without committing
to package names and versions that have not been verified by an actual build.
"""

from __future__ import annotations

import pytest

from jormungandr.runtime.compose import compose
from jormungandr.runtime.layers import render_dockerfile
from jormungandr.runtime.modules.base import BuildContext, ModuleError, Stage
from jormungandr.runtime.modules.builtin import Harness, OpenCode
from jormungandr.runtime.modules.installers import GitPythonApp, NpmGlobal, ShellInstall
from jormungandr.runtime.modules.registry import ModuleRegistry, resolve_order
from jormungandr.runtime.spec import ImageSpec


def render(module) -> str:
    """Render a module's instructions in isolation, with a FROM so it is valid."""
    from jormungandr.runtime.layers import From

    context = BuildContext()
    body = [From("scratch"), *module.instructions(context)]
    return render_dockerfile(body)


class TestNpmGlobal:
    def test_renders_a_pinned_global_install(self) -> None:
        installer = NpmGlobal("opencode-ai", "1.18.4")
        assert "npm install -g opencode-ai@1.18.4" in render(
            Harness(name="x", installer=installer, binary="opencode")
        )

    def test_scoped_packages_work(self) -> None:
        # pi ships as @mariozechner/pi-coding-agent.
        installer = NpmGlobal("@mariozechner/pi-coding-agent", "0.73.1")
        out = render(Harness(name="pi", installer=installer, binary="pi"))
        assert "npm install -g @mariozechner/pi-coding-agent@0.73.1" in out

    def test_requires_node_by_default(self) -> None:
        assert NpmGlobal("x", "1").default_requires == ("node",)

    def test_uses_the_npm_cache_mount(self) -> None:
        out = render(Harness(name="x", installer=NpmGlobal("p", "1"), binary="p"))
        assert "--mount=type=cache,target=/root/.npm" in out

    def test_identity_covers_package_and_version(self) -> None:
        identity = NpmGlobal("p", "1.2.3").identity()
        assert identity == {"kind": "npm", "package": "p", "version": "1.2.3"}


class TestShellInstall:
    """droid: curl -fsSL https://app.factory.ai/cli | sh"""

    def test_downloads_then_runs_rather_than_piping_blind(self) -> None:
        out = render(
            Harness(
                name="droid",
                installer=ShellInstall("https://app.factory.ai/cli"),
                binary="droid",
                requires=(),
            )
        )
        assert "curl -fsSL https://app.factory.ai/cli -o /tmp/install.sh" in out
        assert "sh /tmp/install.sh" in out

    def test_unpinned_installer_is_flagged_in_the_output(self) -> None:
        # Without a checksum the image contents depend on what the URL served
        # at build time, which breaks the digest's promise.
        out = render(
            Harness(
                name="droid",
                installer=ShellInstall("https://app.factory.ai/cli"),
                binary="droid",
                requires=(),
            )
        )
        assert "WARNING: unpinned remote installer" in out

    def test_checksum_is_verified_when_given(self) -> None:
        digest = "a" * 64
        out = render(
            Harness(
                name="droid",
                installer=ShellInstall("https://app.factory.ai/cli", sha256=digest),
                binary="droid",
                requires=(),
            )
        )
        assert f'echo "{digest}  /tmp/install.sh" | sha256sum -c -' in out
        assert "WARNING" not in out

    def test_malformed_checksum_rejected(self) -> None:
        with pytest.raises(ModuleError, match="64 hex characters"):
            ShellInstall("https://x/y", sha256="abc")

    def test_checksum_changes_identity(self) -> None:
        a = ShellInstall("https://x/y", sha256="a" * 64).identity()
        b = ShellInstall("https://x/y", sha256="b" * 64).identity()
        assert a != b

    def test_env_is_passed_to_the_installer(self) -> None:
        out = render(
            Harness(
                name="d",
                installer=ShellInstall("https://x/y", env={"VERSION": "1.2"}),
                binary="d",
                requires=(),
            )
        )
        assert "VERSION=1.2 sh /tmp/install.sh" in out


class TestGitPythonApp:
    """hermes: git clone -> uv venv -> editable install -> PATH shim."""

    @staticmethod
    def hermes() -> Harness:
        return Harness(
            name="hermes",
            installer=GitPythonApp(
                "https://github.com/NousResearch/hermes-agent.git",
                ref="v1.0.0",
                binary="hermes",
            ),
            binary="hermes",
        )

    def test_clones_at_a_pinned_ref(self) -> None:
        out = render(self.hermes())
        assert (
            "git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git"
            in out
        )
        assert "checkout v1.0.0" in out

    def test_builds_an_isolated_venv(self) -> None:
        out = render(self.hermes())
        assert "uv venv /usr/local/lib/hermes/venv --python python3" in out
        assert "uv pip install --python /usr/local/lib/hermes/venv/bin/python" in out

    def test_shim_is_a_real_context_file_not_a_printf(self) -> None:
        # Teich printf-s this shim inline inside a RUN. Making it a context
        # file keeps it readable, reviewable, and part of the digest.
        context = BuildContext()
        self.hermes().instructions(context)
        shim = context.files["hermes.shim.sh"]
        assert shim.startswith("#!/usr/bin/env bash")
        assert "exec /usr/local/lib/hermes/venv/bin/hermes" in shim
        assert context.modes["hermes.shim.sh"] == 0o755

    def test_shim_clears_inherited_python_environment(self) -> None:
        context = BuildContext()
        self.hermes().instructions(context)
        shim = context.files["hermes.shim.sh"]
        assert "unset PYTHONPATH" in shim
        assert "unset PYTHONHOME" in shim

    def test_shim_is_copied_onto_path(self) -> None:
        assert "COPY hermes.shim.sh /usr/local/bin/hermes" in render(self.hermes())

    def test_requires_python_by_default(self) -> None:
        assert GitPythonApp("r", ref="v1", binary="b").default_requires == ("python",)

    def test_ref_change_changes_identity(self) -> None:
        a = GitPythonApp("r", ref="v1", binary="b").identity()
        b = GitPythonApp("r", ref="v2", binary="b").identity()
        assert a != b


class TestHarness:
    def test_verifies_the_binary_after_install(self) -> None:
        # Installers lie: npm exits 0 even when no platform binary matched.
        out = render(Harness(name="x", installer=NpmGlobal("p", "1"), binary="prog"))
        assert "RUN prog --version" in out

    def test_verification_is_customisable(self) -> None:
        out = render(
            Harness(
                name="x",
                installer=NpmGlobal("p", "1"),
                binary="prog",
                verify=("--help",),
            )
        )
        assert "RUN prog --help" in out

    def test_verification_can_be_skipped(self) -> None:
        out = render(
            Harness(name="x", installer=NpmGlobal("p", "1"), binary="prog", verify=())
        )
        assert "RUN prog" not in out

    def test_requires_defaults_to_the_installer(self) -> None:
        assert Harness(
            name="x", installer=NpmGlobal("p", "1"), binary="b"
        ).requires == ("node",)
        assert Harness(
            name="x", installer=GitPythonApp("r", ref="v1", binary="b"), binary="b"
        ).requires == ("python",)

    def test_requires_can_be_overridden(self) -> None:
        harness = Harness(
            name="x",
            installer=NpmGlobal("p", "1"),
            binary="b",
            requires=("node", "apt"),
        )
        assert harness.requires == ("node", "apt")

    def test_all_harnesses_share_the_harness_stage(self) -> None:
        for installer in (
            NpmGlobal("p", "1"),
            ShellInstall("https://x/y"),
            GitPythonApp("r", ref="v1", binary="b"),
        ):
            assert (
                Harness(name="x", installer=installer, binary="b").stage
                == Stage.HARNESS
            )

    def test_installer_kind_is_in_the_identity(self) -> None:
        harness = Harness(name="x", installer=NpmGlobal("p", "1"), binary="b")
        assert harness.identity()["installer"]["kind"] == "npm"

    def test_swapping_installers_changes_the_digest(self) -> None:
        # An image built by curl-installer is not the image built by npm, even
        # for the same nominal harness at the same version.
        npm = Harness(name="h", installer=NpmGlobal("droid", "1.0"), binary="droid")
        shell = Harness(
            name="h",
            installer=ShellInstall("https://app.factory.ai/cli", sha256="a" * 64),
            binary="droid",
            requires=(),
        )
        assert npm.identity() != shell.identity()

    def test_newline_injection_via_verify_args_rejected(self) -> None:
        harness = Harness(
            name="x",
            installer=NpmGlobal("p", "1"),
            binary="b",
            verify=("--version\nUSER root",),
        )
        with pytest.raises(ModuleError, match="newline"):
            render(harness)


class TestNonNpmHarnessComposesEndToEnd:
    """A non-npm harness must work through the real compose path, not just render."""

    def test_git_python_harness_lands_in_the_runtime_tier(self) -> None:
        registry = ModuleRegistry()
        from jormungandr.runtime.modules.builtin import (
            AptPackages,
            NodeToolchain,
            PythonToolchain,
        )

        registry.register("apt", AptPackages)
        registry.register("python", PythonToolchain)
        registry.register(
            "hermes",
            lambda: Harness(
                name="hermes",
                installer=GitPythonApp(
                    "https://github.com/NousResearch/hermes-agent.git",
                    ref="v1.0.0",
                    binary="hermes",
                ),
                binary="hermes",
            ),
        )

        result = compose(
            ImageSpec(
                base_image="debian:trixie-slim",
                modules=[
                    {"name": "apt", "packages": ["git"]},
                    {"name": "python"},
                    {"name": "hermes"},
                ],
            ),
            registry=registry,
        )
        # Toolchain in base, harness in runtime — the split holds for a harness
        # that has nothing to do with npm.
        assert result.base.module_names == ("apt", "python")
        assert result.runtime.module_names == ("hermes",)
        assert "hermes.shim.sh" in result.runtime.context_files
        assert "COPY hermes.shim.sh" in result.runtime.dockerfile

    def test_harness_requiring_a_missing_toolchain_is_rejected(self) -> None:
        registry = ModuleRegistry()
        registry.register(
            "hermes",
            lambda: Harness(
                name="hermes",
                installer=GitPythonApp("r", ref="v1", binary="hermes"),
                binary="hermes",
            ),
        )
        with pytest.raises(ModuleError, match="requires 'python'"):
            compose(ImageSpec(modules=[{"name": "hermes"}]), registry=registry)


class TestOpenCodeStillWorks:
    def test_opencode_is_a_thin_harness(self) -> None:
        harness = OpenCode()
        assert harness.stage == Stage.HARNESS
        assert harness.requires == ("node",)
        assert harness.identity()["installer"] == {
            "kind": "npm",
            "package": "opencode-ai",
            "version": "1.18.4",
        }

    def test_ordering_places_the_toolchain_first(self) -> None:
        from jormungandr.runtime.modules.builtin import NodeToolchain

        ordered = resolve_order([OpenCode(), NodeToolchain()])
        assert [m.name for m in ordered] == ["node", "opencode"]
