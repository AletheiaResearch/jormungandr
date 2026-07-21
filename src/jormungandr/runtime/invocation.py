"""How to invoke a harness non-interactively.

Every harness has a different non-interactive entrypoint, a different way of
being handed a prompt, and a different place it leaves its session record. That
knowledge lives here, one class per harness, so the runner stays harness-blind.

Prompt delivery is the part worth being careful about. Three options exist and
they are not equivalent:

* **argv** — ``droid exec "the prompt"``. The prompt becomes visible in the
  host's process table and in ``docker inspect``, and it has to survive shell
  quoting. Avoided.
* **a file in the workspace** — what Teich does, writing ``.teich-prompt.txt``
  into the agent's working directory. It pollutes the repo the agent sees, and
  Teich needs a dedicated unwrap step later to scrub it back out of traces.
* **stdin** — the prompt never touches argv, the filesystem, or the agent's
  view of its workspace. Both supported harnesses accept it. This is the
  default here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "DroidInvocation",
    "HarnessInvocation",
    "Invocation",
    "OpenCodeInvocation",
    "INVOCATIONS",
    "invocation_for",
]


@dataclass(frozen=True, slots=True)
class Invocation:
    """A concrete command to run inside a container."""

    argv: tuple[str, ...]
    stdin: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class HarnessInvocation(Protocol):
    """Knows how to ask one harness to answer a prompt, non-interactively."""

    harness: str
    """Module name this corresponds to."""

    state_paths: tuple[str, ...]
    """Container paths holding session records, relative to HOME.

    Where a run's artifacts land. Reading or interpreting them is out of scope
    here — this only says where to look.
    """

    system_via: str
    """How this harness accepts a per-record system prompt.

    ``"argv"``  — a command-line flag.
    ``"agents_md"`` — an ``AGENTS.md`` in the working directory, for harnesses
    with no flag. opencode has none: its ``run`` subcommand exposes no
    system-prompt option at all, so the file convention is the only route.
    """

    def build(
        self, prompt: str, *, model: str | None = None, system: str | None = None
    ) -> Invocation:
        """Return the command that runs ``prompt`` to completion."""
        ...


class OpenCodeInvocation:
    """``opencode run`` — OpenCode's non-interactive mode.

    OpenCode also has a headless server (``opencode serve``), which would be a
    better surface for many turns against one process. It is deliberately not
    used: every turn here is a fresh exec, which is uniform across harnesses,
    and adopting a server for one of them would put two execution models in one
    runner. See docs/notes.md.
    """

    harness = "opencode"
    state_paths = (".local/share/opencode",)
    # `opencode run --help` lists no system-prompt flag, so AGENTS.md in the
    # working directory is the only route.
    system_via = "agents_md"

    def build(
        self, prompt: str, *, model: str | None = None, system: str | None = None
    ) -> Invocation:
        argv: list[str] = ["opencode", "run"]
        if model:
            argv += ["--model", model]
        # opencode reads the message from argv or stdin; stdin keeps the prompt
        # out of the process table.
        return Invocation(argv=tuple(argv), stdin=prompt)


class DroidInvocation:
    """``droid exec`` — Factory droid's scripting mode.

    ``--model`` deliberately unused for custom models: droid rejects
    ``custom:<id>`` there (Factory-AI/factory#787) and only honours the model
    named in ``sessionDefaultSettings.model``. Passing a built-in model id is
    still fine, so ``model`` is forwarded when given and callers using BYOK are
    expected to pin theirs in settings instead.
    """

    harness = "droid"
    state_paths = (".factory/sessions", ".factory/logs")
    system_via = "argv"

    def __init__(self, *, autonomy: str = "low", output_format: str = "json") -> None:
        if autonomy not in {"low", "medium", "high"}:
            raise ValueError(f"autonomy must be low, medium or high; got {autonomy!r}")
        if output_format not in {"text", "json", "stream-json", "stream-jsonrpc"}:
            raise ValueError(f"unsupported droid output format: {output_format!r}")
        self.autonomy = autonomy
        self.output_format = output_format

    def build(
        self, prompt: str, *, model: str | None = None, system: str | None = None
    ) -> Invocation:
        argv: list[str] = ["droid", "exec", "--auto", self.autonomy]
        argv += ["--output-format", self.output_format]
        if model:
            argv += ["--model", model]
        if system:
            argv += ["--append-system-prompt", system]
        return Invocation(argv=tuple(argv), stdin=prompt)


INVOCATIONS: dict[str, HarnessInvocation] = {
    "opencode": OpenCodeInvocation(),
    "droid": DroidInvocation(),
}


def invocation_for(harness: str) -> HarnessInvocation:
    try:
        return INVOCATIONS[harness]
    except KeyError:
        known = ", ".join(sorted(INVOCATIONS)) or "<none>"
        raise KeyError(
            f"no invocation registered for harness {harness!r}; known: {known}"
        ) from None
