from __future__ import annotations

import pytest

from jormungandr.runtime.layers import (
    Arg,
    CacheMount,
    Cmd,
    Comment,
    Copy,
    DockerfileError,
    Env,
    From,
    Label,
    Run,
    SecretMount,
    User,
    Workdir,
    render_dockerfile,
)


class TestFrom:
    def test_plain(self) -> None:
        assert From("node:26-slim").render() == "FROM node:26-slim"

    def test_with_platform_and_alias(self) -> None:
        rendered = From("node:26-slim", platform="linux/arm64", alias="build").render()
        assert rendered == "FROM --platform=linux/arm64 node:26-slim AS build"

    def test_empty_image_rejected(self) -> None:
        with pytest.raises(DockerfileError, match="empty image"):
            From("").render()


class TestRun:
    def test_single_command_is_a_one_liner(self) -> None:
        assert Run("apt-get update").render() == "RUN apt-get update"

    def test_multiple_commands_chain_with_and(self) -> None:
        rendered = Run(["apt-get update", "apt-get install -y git"]).render()
        assert rendered == "RUN apt-get update \\\n    && apt-get install -y git"

    def test_cache_mount(self) -> None:
        rendered = Run(
            "npm install -g codex", mounts=[CacheMount("/root/.npm")]
        ).render()
        assert rendered.startswith(
            "RUN --mount=type=cache,target=/root/.npm,sharing=locked "
        )

    def test_secret_mount(self) -> None:
        rendered = Run("use-token", mounts=[SecretMount(id="npm_token")]).render()
        assert "--mount=type=secret,id=npm_token,required=true" in rendered

    def test_empty_rejected(self) -> None:
        with pytest.raises(DockerfileError, match="no commands"):
            Run([])

    def test_blank_command_rejected(self) -> None:
        with pytest.raises(DockerfileError, match="blank command"):
            Run(["apt-get update", "   "])


class TestQuoting:
    def test_bare_value_unquoted(self) -> None:
        assert Env({"TZ": "Etc/UTC"}).render() == "ENV TZ=Etc/UTC"

    def test_value_with_space_is_quoted(self) -> None:
        assert Env({"MSG": "hello world"}).render() == 'ENV MSG="hello world"'

    def test_dollar_expansion_is_preserved(self) -> None:
        # The whole point of not using str.format(): $VAR must survive intact.
        rendered = Env({"PATH": "/opt/venv/bin:$PATH"}).render()
        assert rendered == "ENV PATH=/opt/venv/bin:$PATH"

    def test_braces_survive_untouched(self) -> None:
        # str.format()-based renderers corrupt this; we must not.
        rendered = Run("awk '{print $1}' /etc/hostname").render()
        assert "{print $1}" in rendered

    def test_brace_default_expansion_survives(self) -> None:
        rendered = Env({"HOME": "${HOME:-/root}"}).render()
        assert "${HOME:-/root}" in rendered

    def test_multiple_pairs_wrap(self) -> None:
        rendered = Env({"A": "1", "B": "2"}).render()
        assert rendered == "ENV A=1 \\\n    B=2"

    def test_empty_pairs_rejected(self) -> None:
        with pytest.raises(DockerfileError, match="no key/value"):
            Env({}).render()


class TestCopy:
    def test_simple(self) -> None:
        assert Copy("setup.sh", "/root/").render() == "COPY setup.sh /root/"

    def test_chown_and_multiple_sources(self) -> None:
        rendered = Copy(["a.sh", "b.sh"], "/root/", chown="agent:agent").render()
        assert rendered == "COPY --chown=agent:agent a.sh b.sh /root/"

    def test_whitespace_path_rejected_loudly(self) -> None:
        with pytest.raises(DockerfileError, match="whitespace"):
            Copy("my script.sh", "/root/")


class TestMisc:
    def test_arg_with_default(self) -> None:
        assert Arg("VERSION", "1.2.3").render() == "ARG VERSION=1.2.3"

    def test_arg_without_default(self) -> None:
        assert Arg("VERSION").render() == "ARG VERSION"

    def test_label(self) -> None:
        assert Label({"org.x.tier": "base"}).render() == "LABEL org.x.tier=base"

    def test_workdir_user_cmd(self) -> None:
        assert Workdir("/workspace").render() == "WORKDIR /workspace"
        assert User("agent").render() == "USER agent"
        assert Cmd(["sleep", "infinity"]).render() == 'CMD ["sleep", "infinity"]'

    def test_multiline_comment(self) -> None:
        assert Comment("one\ntwo").render() == "# one\n# two"


class TestRenderDockerfile:
    def test_syntax_directive_comes_first(self) -> None:
        out = render_dockerfile([From("alpine:3.20")])
        assert out.splitlines()[0] == "# syntax=docker/dockerfile:1"

    def test_syntax_can_be_disabled(self) -> None:
        out = render_dockerfile([From("alpine:3.20")], syntax=None)
        assert out == "FROM alpine:3.20\n"

    def test_requires_a_from(self) -> None:
        with pytest.raises(DockerfileError, match="no FROM"):
            render_dockerfile([Run("echo hi")])

    def test_empty_rejected(self) -> None:
        with pytest.raises(DockerfileError, match="no instructions"):
            render_dockerfile([])

    def test_deterministic(self) -> None:
        # Image identity is a hash of this text, so it must be stable.
        def build() -> str:
            return render_dockerfile(
                [From("node:26-slim"), Env({"A": "1"}), Run(["x", "y"]), Cmd(["bash"])]
            )

        assert build() == build()

    def test_trailing_newline(self) -> None:
        assert render_dockerfile([From("alpine:3.20")]).endswith("\n")
