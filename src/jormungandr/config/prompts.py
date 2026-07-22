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
from pydantic_core.core_schema import ValidationInfo

__all__ = [
    "GitSource",
    "Overrides",
    "PromptRecord",
    "Turn",
    "Workspace",
    "load_prompts",
]

GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
"""Ids become a directory name under the output directory, so they must be a
single safe path component. Without this an id of ``../../x`` escapes that
directory — and since the workspace is cleared with ``rmtree`` before each run,
that is arbitrary deletion driven by a data file."""

CLONE_URL = re.compile(
    r"^(?:(?:https?|ssh|git)://|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:)"
    r"[A-Za-z0-9._:/@%+=-]+$"
)
"""A clone url reaches two places that treat a bare string as code.

``resolve_commit`` passes it to ``git ls-remote`` as a positional argument, so
a value beginning with ``-`` is an *option* — and ``--upload-pack=<cmd>`` runs
``<cmd>`` on this host. ``compose_workspace`` interpolates it, unquoted, into a
``RUN git remote add origin <url>`` line, so ``;``, ``&&``, backticks and
``$( )`` run as root inside the build. Both call sites are hardened
independently, but a prompts.jsonl is data and the string should never have got
that far.

Hence an allowlist rather than a denylist: a scheme git can actually fetch
(``http``, ``https``, ``ssh``, ``git``) or scp-like ``user@host:path``,
followed only by characters that are inert to a shell. Deliberately excluded:
``ext::``, which is a transport whose entire purpose is running a command;
``file://`` and bare local paths, which have been meaningless since the clone
moved inside the image; and ``?``, ``~``, ``!`` and ``#``, which no real remote
needs and every shell treats specially."""


def _safe_relative(value: str | None, *, field: str) -> str | None:
    """Normalize a path that will be joined below the workspace directory.

    Only ``..`` can actually escape: these values are always joined onto the
    clone or workspace root, so a leading slash is sloppiness rather than an
    absolute path — ``/pkg/api`` inside a repository plainly means ``pkg/api``,
    and trimming it is friendlier than refusing it. ``..`` is rejected, because
    a prompt file is data and must not be able to write outside the run's
    output directory.
    """
    if value is None:
        return None
    cleaned = value.strip().strip("/")
    if not cleaned:
        return None
    if ".." in Path(cleaned).parts:
        raise ValueError(
            f"{field} must stay inside the workspace; '..' is not allowed, "
            f"got {value!r}"
        )
    return cleaned


class Turn(BaseModel):
    """The normalized internal form. Not part of the file format."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user"] = "user"
    content: str = Field(min_length=1)


class GitSource(BaseModel):
    """A repository to place in the agent's working directory.

    Everything ``github_repo`` cannot express: a non-GitHub host, a pinned
    revision, one directory out of a monorepo, and a chosen destination name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    clone_url: str = Field(min_length=1)
    """A git URL: ``http(s)://``, ``ssh://``, ``git://``, or ``user@host:path``.

    Not a host path. The clone happens inside the workspace image, so a host
    path would resolve against the build container's filesystem and find
    nothing."""

    ref: str | None = None
    """Branch, tag or commit. Unset means the default branch, which makes the
    run unreproducible: the same record clones different code tomorrow."""

    subdirectory: str | None = None
    """Use only this directory of the repository as the workspace content.

    For monorepos. The extracted directory is not itself a git repository, so
    the agent will not have history to inspect — that is inherent to taking a
    subtree, not a limitation of the implementation.
    """

    clone_as: str | None = None
    """Directory name the content lands in, below the working directory.

    Unset puts the repository *at* the working directory root. Setting it gives
    the agent ``/workspace/<clone_as>``, which is what you want when the repo
    should sit alongside other material, or when a stable name matters more
    than the repository's own.
    """

    @field_validator("clone_url")
    @classmethod
    def _validate_clone_url(cls, value: str) -> str:
        """Refuse anything that is not a fetchable, shell-inert git url.

        Checked here so a bad line is named at load time — ``load_prompts``
        reports ``line N`` — rather than reaching ``git ls-remote`` inside a
        worker thread, where the same string is an argument vector.
        """
        cleaned = value.strip()
        if not CLONE_URL.fullmatch(cleaned):
            raise ValueError(
                f"clone_url must be an http(s), ssh or git url, or "
                f"user@host:path; got {value!r}"
            )
        return cleaned

    @field_validator("subdirectory", "clone_as")
    @classmethod
    def _validate_relative(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _safe_relative(value, field=str(info.field_name))

    @property
    def has_history(self) -> bool:
        """Whether the materialized workspace keeps its .git directory."""
        return self.subdirectory is None


class Workspace(BaseModel):
    """What the agent finds in its working directory — the resolved form.

    Produced from ``github_repo`` or ``git``. A repository is materialized as
    an image tier built by the daemon, so nothing here refers to the host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["none", "git"] = "none"
    git: GitSource | None = None

    @model_validator(mode="after")
    def _check_fields_match_type(self) -> Workspace:
        if self.type == "git" and self.git is None:
            raise ValueError("workspace type 'git' requires 'git'")
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
    """One unit of work: a prompt, its follow-ups, and where it runs.

    A superset of Teich's record format, so an existing prompts file loads
    unchanged. Everything beyond ``prompt``/``follow_up_prompts``/``system``
    is this project's own and is optional.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Teich's format -----------------------------------------------------
    prompt: str = Field(min_length=1)
    follow_up_prompts: tuple[str, ...] = ()
    system: str | None = None
    github_repo: str | None = None
    image: str | None = None

    # --- additive, optional -------------------------------------------------
    id: str | None = None
    git: GitSource | None = None
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

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str | None) -> str | None:
        """Ids name a directory, so they must be one safe path component.

        `output_dir / record.id` with an absolute id discards output_dir
        entirely, and with `..` it escapes upward — into an rmtree.
        """
        if value is None:
            return None
        cleaned = value.strip()
        if not RECORD_ID.fullmatch(cleaned) or cleaned in {".", ".."}:
            raise ValueError(
                f"id must be a single path component of letters, digits, dot, "
                f"dash or underscore, starting alphanumeric; got {value!r}"
            )
        return cleaned

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
    def _resolve_workspace(self) -> PromptRecord:
        """Exactly one workspace source, normalized into ``workspace``.

        ``github_repo`` and ``git`` describe the same thing at different levels
        of detail, so accepting both would mean silently picking a winner. A
        record that sets more than one is a mistake worth naming.
        """
        explicit = self.workspace is not None and self.workspace.type != "none"
        given = [
            name
            for name, present in (
                ("github_repo", self.github_repo is not None),
                ("git", self.git is not None),
                ("workspace", explicit),
            )
            if present
        ]
        if len(given) > 1:
            raise ValueError(
                f"a record may set only one workspace source, got: {', '.join(given)}. "
                "github_repo is shorthand for git; use whichever fits, not both."
            )

        if self.git is not None:
            resolved = Workspace(type="git", git=self.git)
        elif self.github_repo is not None:
            resolved = Workspace(
                type="git",
                git=GitSource(clone_url=f"https://github.com/{self.github_repo}"),
            )
        elif explicit:
            resolved = self.workspace  # type: ignore[assignment]
        else:
            resolved = Workspace()
        object.__setattr__(self, "workspace", resolved)
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
        """Attach ``derived`` as the id, unless one was supplied.

        An id is optional in the file format — Teich records have none — but
        becomes a directory name once the record runs. Deriving it from
        position keeps a hand-written id authoritative while giving every
        record somewhere to write.
        """
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
