from __future__ import annotations

from pathlib import Path

import pytest

from jormungandr.runtime.docker import CommandResult
from jormungandr.runtime.invocation import (
    DroidInvocation,
    OpenCodeInvocation,
    invocation_for,
)
from jormungandr.runtime.run import PromptRunner, TurnResult
from jormungandr.runtime.spec import ContainerSpec


class FakeSession:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = results or []
        self.copied: list[tuple[str, Path]] = []
        self.container_id = "c" * 64
        self.name = "fake"

    def exec(self, command, *, timeout=None, stdin=None, **kwargs):
        self.calls.append({"argv": list(command), "stdin": stdin, "timeout": timeout})
        if command[:2] == ["sh", "-c"]:
            return CommandResult(0, "/home/agent", "", 0.01)
        if self.results:
            return self.results.pop(0)
        return CommandResult(0, "ok", "", 0.1)

    def exec_with_stdin(self, command, *, stdin, timeout=None, **kwargs):
        return self.exec(command, timeout=timeout, stdin=stdin, **kwargs)

    def copy_out(self, source, destination):
        self.copied.append((source, Path(destination)))


class FakeRuntime:
    def __init__(self, session: FakeSession) -> None:
        self.session_obj = session
        self.specs: list[ContainerSpec] = []

    def session(self, spec, **kwargs):
        from contextlib import contextmanager

        self.specs.append(spec)

        @contextmanager
        def cm():
            yield self.session_obj

        return cm()


def runner(session: FakeSession) -> PromptRunner:
    return PromptRunner(runtime=FakeRuntime(session))


class TestOpenCodeInvocation:
    def test_uses_the_non_interactive_subcommand(self) -> None:
        call = OpenCodeInvocation().build("fix the bug")
        assert call.argv[:2] == ("opencode", "run")

    def test_prompt_goes_on_stdin_not_argv(self) -> None:
        # argv would put the prompt in the host process table and in
        # `docker inspect`.
        call = OpenCodeInvocation().build("secret prompt")
        assert call.stdin == "secret prompt"
        assert "secret prompt" not in call.argv

    def test_model_is_forwarded(self) -> None:
        call = OpenCodeInvocation().build("x", model="anthropic/claude-sonnet-4-5")
        assert "--model" in call.argv
        assert "anthropic/claude-sonnet-4-5" in call.argv

    def test_state_paths_point_at_the_session_store(self) -> None:
        assert OpenCodeInvocation().state_paths == (".local/share/opencode",)


class TestDroidInvocation:
    def test_uses_exec_subcommand(self) -> None:
        call = DroidInvocation().build("fix the bug")
        assert call.argv[:2] == ("droid", "exec")

    def test_prompt_goes_on_stdin(self) -> None:
        call = DroidInvocation().build("secret prompt")
        assert call.stdin == "secret prompt"
        assert "secret prompt" not in call.argv

    def test_autonomy_and_output_format(self) -> None:
        call = DroidInvocation(autonomy="medium", output_format="stream-json").build("x")
        assert "--auto" in call.argv and "medium" in call.argv
        assert "--output-format" in call.argv and "stream-json" in call.argv

    def test_invalid_autonomy_rejected(self) -> None:
        with pytest.raises(ValueError, match="autonomy must be"):
            DroidInvocation(autonomy="max")

    def test_invalid_output_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported droid output format"):
            DroidInvocation(output_format="yaml")

    def test_state_paths_include_sessions(self) -> None:
        assert ".factory/sessions" in DroidInvocation().state_paths


class TestInvocationRegistry:
    def test_lookup(self) -> None:
        assert invocation_for("droid").harness == "droid"
        assert invocation_for("opencode").harness == "opencode"

    def test_unknown_harness_lists_known(self) -> None:
        with pytest.raises(KeyError, match="droid, opencode"):
            invocation_for("ghost")


class TestPromptRunner:
    def test_single_prompt_runs_once(self) -> None:
        session = FakeSession()
        run = runner(session).run(harness="droid", image="img", prompts=["hello"])
        harness_calls = [c for c in session.calls if c["argv"][0] == "droid"]
        assert len(harness_calls) == 1
        assert harness_calls[0]["stdin"] == "hello"
        assert run.ok

    def test_multiple_prompts_share_one_container(self) -> None:
        # The point of a multi-turn run: later turns see what earlier ones did.
        session = FakeSession()
        rt = FakeRuntime(session)
        PromptRunner(runtime=rt).run(
            harness="droid", image="img", prompts=["one", "two", "three"]
        )
        assert len(rt.specs) == 1
        assert [c["stdin"] for c in session.calls if c["argv"][0] == "droid"] == [
            "one",
            "two",
            "three",
        ]

    def test_turns_are_indexed_in_order(self) -> None:
        session = FakeSession()
        run = runner(session).run(harness="droid", image="img", prompts=["a", "b"])
        assert [t.index for t in run.turns] == [0, 1]
        assert [t.prompt for t in run.turns] == ["a", "b"]

    def test_stops_after_a_failing_turn(self) -> None:
        # A failed turn leaves the session in an unknown state, so later turns
        # would produce misleading results rather than useful ones.
        session = FakeSession(
            results=[
                CommandResult(0, "ok", "", 0.1),
                CommandResult(1, "", "boom", 0.1),
                CommandResult(0, "never", "", 0.1),
            ]
        )
        run = runner(session).run(harness="droid", image="img", prompts=["a", "b", "c"])
        assert len(run.turns) == 2
        assert not run.ok
        assert run.failed[0].index == 1

    def test_timeout_is_reported_not_swallowed(self) -> None:
        session = FakeSession(results=[CommandResult(124, "", "", 5.0, timed_out=True)])
        run = runner(session).run(harness="droid", image="img", prompts=["a"])
        assert run.turns[0].timed_out
        assert not run.ok

    def test_empty_prompts_rejected(self) -> None:
        with pytest.raises(ValueError, match="no prompts"):
            runner(FakeSession()).run(harness="droid", image="img", prompts=[])

    def test_blank_prompt_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            runner(FakeSession()).run(harness="droid", image="img", prompts=["a", "  "])

    def test_env_files_are_used_for_secrets(self) -> None:
        session = FakeSession()
        rt = FakeRuntime(session)
        PromptRunner(runtime=rt).run(
            harness="droid",
            image="img",
            prompts=["a"],
            env_files=["/run/secrets.env"],
        )
        assert rt.specs[0].env_files == ("/run/secrets.env",)

    def test_state_collection_resolves_home(self) -> None:
        session = FakeSession()
        run = runner(session).run(
            harness="droid",
            image="img",
            prompts=["a"],
            collect_state_to=Path("/tmp/does-not-need-to-exist-for-fake"),
        )
        assert run.artifacts is not None
        assert any(src.startswith("/home/agent/.factory") for src, _ in session.copied)

    def test_missing_state_is_not_an_error(self) -> None:
        # A harness that failed early may have written nothing.
        session = FakeSession()

        def explode(source, destination):
            raise RuntimeError("no such path in container")

        session.copy_out = explode  # type: ignore[assignment]
        run = runner(session).run(
            harness="droid", image="img", prompts=["a"], collect_state_to=Path("/tmp/x")
        )
        assert run.ok

    def test_state_paths_recorded_on_the_run(self) -> None:
        run = runner(FakeSession()).run(harness="opencode", image="img", prompts=["a"])
        assert run.state_paths == (".local/share/opencode",)

    def test_harness_and_image_recorded(self) -> None:
        run = runner(FakeSession()).run(harness="opencode", image="img:tag", prompts=["a"])
        assert run.harness == "opencode"
        assert run.image == "img:tag"


class TestTurnResult:
    def test_ok_requires_zero_and_no_timeout(self) -> None:
        assert TurnResult.from_command(0, "p", CommandResult(0, "", "", 1.0)).ok
        assert not TurnResult.from_command(0, "p", CommandResult(1, "", "", 1.0)).ok
        assert not TurnResult.from_command(
            0, "p", CommandResult(0, "", "", 1.0, timed_out=True)
        ).ok

    def test_carries_raw_output_only(self) -> None:
        # No parsing here: turning harness output into training data is a
        # separate concern with a separate contract.
        turn = TurnResult.from_command(0, "p", CommandResult(0, '{"a":1}', "warn", 1.0))
        assert turn.stdout == '{"a":1}'
        assert turn.stderr == "warn"


class TestRunnerForwarding:
    """What PromptRunner passes to the invocation was never asserted.

    model, system and timeout could each have been silently dropped, and every
    existing test would still have passed.
    """

    class Recording:
        harness = "droid"
        state_paths: tuple[str, ...] = ()
        system_via = "argv"

        def __init__(self) -> None:
            self.built: list[dict] = []

        def build(self, prompt, *, model=None, system=None):
            from jormungandr.runtime.invocation import Invocation

            self.built.append({"prompt": prompt, "model": model, "system": system})
            return Invocation(argv=("true",), stdin=prompt)

    def run_with(self, session: FakeSession, **kwargs):
        invocation = self.Recording()
        PromptRunner(runtime=FakeRuntime(session)).run(
            harness="droid",
            image="img",
            prompts=kwargs.pop("prompts", ["one"]),
            invocation=invocation,
            **kwargs,
        )
        return invocation

    def test_model_is_forwarded(self) -> None:
        built = self.run_with(FakeSession(), model="p/m").built
        assert built[0]["model"] == "p/m"

    def test_system_is_forwarded_to_every_turn(self) -> None:
        built = self.run_with(
            FakeSession(), prompts=["a", "b"], system="be terse"
        ).built
        assert [b["system"] for b in built] == ["be terse", "be terse"]

    def test_no_system_means_none(self) -> None:
        assert self.run_with(FakeSession()).built[0]["system"] is None

    def test_timeout_reaches_the_exec(self) -> None:
        session = FakeSession()
        self.run_with(session, timeout=42)
        harness_calls = [c for c in session.calls if c["argv"] == ["true"]]
        assert harness_calls[0]["timeout"] == 42

    def test_default_timeout_is_used_when_unset(self) -> None:
        session = FakeSession()
        invocation = self.Recording()
        PromptRunner(runtime=FakeRuntime(session), default_timeout=77).run(
            harness="droid", image="img", prompts=["a"], invocation=invocation
        )
        harness_calls = [c for c in session.calls if c["argv"] == ["true"]]
        assert harness_calls[0]["timeout"] == 77

    def test_the_prompt_goes_on_stdin_for_every_turn(self) -> None:
        session = FakeSession()
        self.run_with(session, prompts=["one", "two"])
        stdins = [c["stdin"] for c in session.calls if c["argv"] == ["true"]]
        assert stdins == ["one", "two"]
