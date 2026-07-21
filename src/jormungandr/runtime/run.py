"""Run prompts through a harness in a container.

The execution model follows Teich's, which gets the shape right:

* **one prompt** — a container for the lifetime of the run, then torn down.
* **several prompts in one session** — one long-lived container, one ``exec``
  per turn, so the harness's own session state carries across turns and the
  filesystem the agent worked on persists between them.

The branch is a single ``len(prompts) > 1``.

What this deliberately does not do: read, parse, or interpret anything the
harness wrote. A :class:`TurnResult` carries raw stdout/stderr and the paths
where the harness left its session record. Turning those into training data is
a separate concern with a separate contract.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jormungandr.runtime.container import ContainerRuntime, ContainerSession
from jormungandr.runtime.docker import CommandResult
from jormungandr.runtime.invocation import HarnessInvocation, invocation_for
from jormungandr.runtime.spec import ContainerSpec

__all__ = ["HarnessRun", "PromptRunner", "TurnResult"]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The outcome of one prompt."""

    index: int
    prompt: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool
    truncated: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @classmethod
    def from_command(cls, index: int, prompt: str, result: CommandResult) -> TurnResult:
        return cls(
            index=index,
            prompt=prompt,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration=result.duration,
            timed_out=result.timed_out,
            truncated=result.truncated,
        )


@dataclass(frozen=True, slots=True)
class HarnessRun:
    """Everything one run produced."""

    harness: str
    image: str
    turns: tuple[TurnResult, ...]
    state_paths: tuple[str, ...]
    artifacts: Path | None = None

    @property
    def ok(self) -> bool:
        return all(turn.ok for turn in self.turns)

    @property
    def failed(self) -> tuple[TurnResult, ...]:
        return tuple(turn for turn in self.turns if not turn.ok)


class PromptRunner:
    """Runs prompts against a harness image."""

    def __init__(
        self,
        *,
        runtime: ContainerRuntime | None = None,
        default_timeout: float | None = 900.0,
    ) -> None:
        self.runtime = runtime or ContainerRuntime()
        self.default_timeout = default_timeout

    def run(
        self,
        *,
        harness: str,
        image: str,
        prompts: Sequence[str],
        model: str | None = None,
        env: Mapping[str, str] | None = None,
        env_files: Sequence[str] = (),
        mounts: Sequence[str] = (),
        network: str = "bridge",
        timeout: float | None = None,
        collect_state_to: Path | None = None,
        invocation: HarnessInvocation | None = None,
        container_spec: ContainerSpec | None = None,
    ) -> HarnessRun:
        """Run ``prompts`` in order against ``image``.

        Prompts share one container and therefore one session: later turns see
        what earlier ones did to the filesystem, which is the point of a
        multi-turn run.
        """
        if not prompts:
            raise ValueError("no prompts given")
        if any(not p.strip() for p in prompts):
            raise ValueError("prompts must be non-empty")

        how = invocation or invocation_for(harness)
        spec = container_spec or ContainerSpec(
            image=image,
            env=dict(env or {}),
            env_files=tuple(env_files),
            mounts=tuple(mounts),
            network=network,  # type: ignore[arg-type]
        )

        turns: list[TurnResult] = []
        with self.runtime.session(spec) as session:
            for index, prompt in enumerate(prompts):
                call = how.build(prompt, model=model)
                result = self._exec(session, call, timeout=timeout)
                turns.append(TurnResult.from_command(index, prompt, result))
                # A failed turn poisons the ones after it — the session state
                # is now whatever the failure left behind — so stop rather than
                # collect misleading results.
                if not turns[-1].ok:
                    break

            artifacts = None
            if collect_state_to is not None:
                artifacts = self._collect(session, how.state_paths, collect_state_to)

        return HarnessRun(
            harness=harness,
            image=image,
            turns=tuple(turns),
            state_paths=tuple(how.state_paths),
            artifacts=artifacts,
        )

    def _exec(
        self, session: ContainerSession, call, *, timeout: float | None
    ) -> CommandResult:
        """Run one invocation, delivering the prompt on stdin.

        `docker exec -i` is required for stdin to reach the process, and the
        prompt is written to the container's stdin rather than argv so it never
        appears in the host process table or in `docker inspect`.
        """
        return session.exec_with_stdin(
            call.argv,
            stdin=call.stdin,
            timeout=timeout if timeout is not None else self.default_timeout,
            env=call.env or None,
        )

    @staticmethod
    def _collect(
        session: ContainerSession, state_paths: Sequence[str], destination: Path
    ) -> Path:
        """Copy the harness's session record out of the container.

        Paths are relative to HOME, which is why the workspace module sets HOME
        explicitly — resolving it from /etc/passwd picks the wrong user when a
        uid is shared.
        """
        destination.mkdir(parents=True, exist_ok=True)
        home = session.exec(["sh", "-c", "printf %s \"$HOME\""]).stdout.strip() or "/root"
        for relative in state_paths:
            source = f"{home}/{relative}"
            target = destination / relative.replace("/", "_")
            # Absent state is normal: a harness that failed early may not have
            # written anything, and that is not itself an error.
            with contextlib.suppress(Exception):
                session.copy_out(source, target)
        return destination
