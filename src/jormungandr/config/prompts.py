"""Prompt records and their JSONL file.

The one design decision the benchmark formats got unambiguously right is that
input records and output traces are distinct things joined by an id. The one
they got wrong is deriving that id from the prompt text: Teich hashes the
prompt, so two identical prompts collide and editing a prompt silently creates
a new task instead of showing a diff on an existing one.

So ``id`` is required and caller-supplied, and everything benchmark-specific
lives in ``metadata`` rather than in the schema every record must satisfy.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["PromptRecord", "Turn", "Workspace", "load_prompts"]


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"] = "user"
    content: str = Field(min_length=1)


class Workspace(BaseModel):
    """What the agent should find in its working directory.

    A tagged union rather than an overloaded string: it covers a local
    directory, a git checkout, and nothing, without inheriting Teich's
    ``github_repo`` assumption or SWE-bench's ``repo`` + ``base_commit``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["none", "local", "git"] = "none"
    path: str | None = None
    repo: str | None = None
    ref: str | None = None

    @model_validator(mode="after")
    def _check_fields_match_type(self) -> Workspace:
        if self.type == "local" and not self.path:
            raise ValueError("workspace type 'local' requires 'path'")
        if self.type == "git" and not self.repo:
            raise ValueError("workspace type 'git' requires 'repo'")
        if self.type == "git" and not self.ref:
            raise ValueError(
                "workspace type 'git' requires 'ref' — a branch name would make "
                "the run irreproducible; pin a tag or commit"
            )
        return self


class Overrides(BaseModel):
    """Per-record overrides. Run config is the base; these win per key.

    Deliberately no per-record ``model``. Model selection is baked into the
    image — droid resolves it from ``sessionDefaultSettings`` in a file that is
    part of the image digest — so varying the model per record would mean a
    separate image per record. Only knobs that cost nothing at run time live
    here; to compare models, run the config twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout: float | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)


class PromptRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    schema_version: str = "1"

    prompt: str | None = None
    follow_up_prompts: tuple[str, ...] = ()
    turns: tuple[Turn, ...] = ()

    workspace: Workspace = Field(default_factory=Workspace)
    overrides: Overrides = Field(default_factory=Overrides)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_turns(self) -> PromptRecord:
        """Accept Teich's spelling, store the canonical one.

        ``prompt`` + ``follow_up_prompts`` is sugar; ``turns`` is canonical
        because a role list can carry a leading system message or an assistant
        prefill, which a list of bare strings cannot.
        """
        if self.turns and (self.prompt or self.follow_up_prompts):
            raise ValueError(
                "give either 'turns' or 'prompt'/'follow_up_prompts', not both"
            )
        if self.turns:
            if not any(turn.role == "user" for turn in self.turns):
                raise ValueError("'turns' must contain at least one user turn")
            return self
        if not self.prompt:
            raise ValueError("a record needs 'prompt' or 'turns'")
        turns = [Turn(role="user", content=self.prompt)]
        turns += [Turn(role="user", content=f) for f in self.follow_up_prompts]
        object.__setattr__(self, "turns", tuple(turns))
        return self

    @property
    def user_turns(self) -> tuple[str, ...]:
        """The user turns, in order — what the runner actually sends."""
        return tuple(t.content for t in self.turns if t.role == "user")

    @property
    def system(self) -> str | None:
        for turn in self.turns:
            if turn.role == "system":
                return turn.content
        return None


def load_prompts(path: Path) -> tuple[PromptRecord, ...]:
    """Read a JSONL prompt file.

    Errors are reported as ``line N: …`` because a hand-authored data file
    with a bad line is the common case, and "validation error" without a line
    number is not actionable.
    """
    records: list[PromptRecord] = []
    seen: set[str] = set()
    # utf-8-sig: a BOM from a Windows editor would otherwise corrupt the first key.
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}: line {number}: invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}: line {number}: expected a JSON object")
            try:
                record = PromptRecord.model_validate(payload)
            except Exception as exc:
                raise ValueError(f"{path}: line {number}: {exc}") from exc
            if record.id in seen:
                raise ValueError(
                    f"{path}: line {number}: duplicate id {record.id!r}. "
                    "Ids name output directories, so they must be unique."
                )
            seen.add(record.id)
            records.append(record)
    if not records:
        raise ValueError(f"{path}: no prompt records found")
    return tuple(records)
