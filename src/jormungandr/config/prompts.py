"""Prompt records, in Teich's ``prompts.jsonl`` format.

The record schema is Teich's, so an existing prompt file loads unchanged::

    {"prompt": "Draft a plan"}
    {"prompt": "Build a page", "follow_up_prompts": ["Make it responsive"]}
    {"prompt": "Fix the bug", "system": "Be terse.", "github_repo": "acme/app"}

Fields: ``prompt`` (required), ``follow_up_prompts``, ``system``,
``github_repo``, ``image`` — matching ``teich/src/teich/config.py:244``.

Two additions, both optional, neither required by a Teich file:

* ``id`` — Teich identifies a run by hashing the prompt text, so two identical
  prompts collide and editing a prompt silently creates a new run rather than
  showing a diff on the existing one. Output directories need a name, so an id
  is derived from the record's *position* when absent: stable for a given file,
  and never colliding. Supply one explicitly if you expect to reorder the file.
* ``overrides`` — per-record ``timeout``/``max_turns``. Not ``model``: model
  selection is baked into the image, so varying it per record would mean an
  image per record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["Overrides", "PromptRecord", "Turn", "Workspace", "load_prompts"]

GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Turn(BaseModel):
    """The normalized internal form. Not part of the file format."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user"] = "user"
    content: str = Field(min_length=1)


class Workspace(BaseModel):
    """What the agent finds in its working directory.

    Derived from ``github_repo``; also constructible directly for a local
    directory, which Teich's format has no way to express.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["none", "local", "git"] = "none"
    path: str | None = None
    repo: str | None = None
    ref: str | None = None
    """Unset means the repository's default branch, as Teich does. That makes
    the run unreproducible — the same record clones different code tomorrow —
    so pin it when the result matters."""

    @model_validator(mode="after")
    def _check_fields_match_type(self) -> Workspace:
        if self.type == "local" and not self.path:
            raise ValueError("workspace type 'local' requires 'path'")
        if self.type == "git" and not self.repo:
            raise ValueError("workspace type 'git' requires 'repo'")
        return self


class Overrides(BaseModel):
    """Per-record overrides. Run config is the base; these win per key.

    Deliberately no ``model``. Model selection is baked into the image — droid
    resolves it from ``sessionDefaultSettings`` in a file that is part of the
    image digest — so varying it per record would mean a separate image per
    record. To compare models, run the config twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout: float | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)


class PromptRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Teich's format -----------------------------------------------------
    prompt: str = Field(min_length=1)
    follow_up_prompts: tuple[str, ...] = ()
    system: str | None = None
    github_repo: str | None = None
    image: str | None = None

    # --- additive, optional -------------------------------------------------
    id: str | None = None
    workspace: Workspace | None = None
    overrides: Overrides = Field(default_factory=Overrides)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt", "system", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> object:
        """Match Teich's normalization: CRLF to LF, strip, literal 'none'."""
        if value is None:
            return None
        text = value if isinstance(value, str) else str(value)
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or text.lower() == "none":
            return None
        return text

    @field_validator("follow_up_prompts", mode="before")
    @classmethod
    def _normalize_follow_ups(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("follow_up_prompts must be a list of strings")
        cleaned: list[str] = []
        for index, item in enumerate(value, start=1):
            text = str(item).replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                raise ValueError(f"follow_up_prompts entry {index} cannot be empty")
            cleaned.append(text)
        return tuple(cleaned)

    @field_validator("github_repo")
    @classmethod
    def _validate_github_repo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not GITHUB_REPO.fullmatch(value.strip()):
            raise ValueError(f"github_repo must be in owner/repo form, got {value!r}")
        return value.strip()

    @field_validator("image")
    @classmethod
    def _reject_per_record_image(cls, value: str | None) -> str | None:
        """Teich models this field and then refuses it at use time.

        Rejecting it here names the offending record instead of failing after
        the banner has printed and directories have been created. An image per
        record would also mean one run spanning several environments; if two
        records need different images, that is two runs.
        """
        if value is None:
            return None
        raise ValueError(
            "per-record 'image' is not supported: one run builds one image. "
            "Use a separate config for a different image."
        )

    @model_validator(mode="after")
    def _derive_workspace(self) -> PromptRecord:
        if self.workspace is None:
            derived = (
                Workspace(type="git", repo=f"https://github.com/{self.github_repo}")
                if self.github_repo
                else Workspace()
            )
            object.__setattr__(self, "workspace", derived)
        return self

    @property
    def turns(self) -> tuple[Turn, ...]:
        """The normalized turn list: the system turn, then each user turn."""
        turns: list[Turn] = []
        if self.system:
            turns.append(Turn(role="system", content=self.system))
        turns.append(Turn(role="user", content=self.prompt))
        turns += [Turn(role="user", content=f) for f in self.follow_up_prompts]
        return tuple(turns)

    @property
    def user_turns(self) -> tuple[str, ...]:
        """What the runner actually sends, in order."""
        return (self.prompt, *self.follow_up_prompts)

    def with_id(self, derived: str) -> PromptRecord:
        return self if self.id else self.model_copy(update={"id": derived})


def load_prompts(path: Path) -> tuple[PromptRecord, ...]:
    """Read a Teich-format ``prompts.jsonl``.

    Errors are reported as ``line N: …`` — a hand-authored data file with a bad
    line is the common case, and a validation error without a line number is
    not actionable.
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
            # A bare string is a prompt, matching Teich's plain-text loader.
            if isinstance(payload, str):
                payload = {"prompt": payload}
            if not isinstance(payload, dict):
                raise ValueError(
                    f"{path}: line {number}: expected a JSON object or a bare string"
                )
            try:
                record = PromptRecord.model_validate(payload)
            except Exception as exc:
                raise ValueError(f"{path}: line {number}: {exc}") from exc

            record = record.with_id(f"prompt-{len(records):04d}")
            if record.id in seen:
                raise ValueError(
                    f"{path}: line {number}: duplicate id {record.id!r}. "
                    "Ids name output directories, so they must be unique."
                )
            seen.add(str(record.id))
            records.append(record)
    if not records:
        raise ValueError(f"{path}: no prompt records found")
    return tuple(records)
