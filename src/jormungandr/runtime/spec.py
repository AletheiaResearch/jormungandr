"""The validated contract between configuration and the runtime.

Two models, deliberately separate:

* :class:`ImageSpec` — everything that determines the *built image*. Hashed,
  cached, shared across many runs.
* :class:`ContainerSpec` — everything that varies *per run*. Never baked into a
  layer, because anything that varies per attempt destroys the cache if it is.

SWE-bench conflates both into one ``TestSpec`` dataclass and relies on
convention to keep the two halves apart. Making the split a type means a
per-run value cannot accidentally end up in the image hash.
"""

from __future__ import annotations

import platform as _platform
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ContainerSpec",
    "ImageSpec",
    "ModuleDeclaration",
    "ResourceLimits",
    "default_platform",
]


def default_platform() -> str:
    """Target the host architecture.

    SWE-bench hardcodes ``x86_64``, leaving its arm64 branch dead code and
    running everything under emulation on Apple Silicon — a large, invisible
    performance tax. Correct for a reproducible benchmark, wrong for a
    developer-facing runtime.
    """
    machine = _platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "linux/arm64"
    return "linux/amd64"


class ModuleDeclaration(BaseModel):
    """A module to apply, by registry name, plus its configuration."""

    model_config = ConfigDict(extra="allow", frozen=True)

    name: str = Field(min_length=1)

    def config(self) -> dict[str, Any]:
        return self.model_dump(exclude={"name"})


class ResourceLimits(BaseModel):
    """Caps applied to every container.

    Neither reference implementation sets any of these. SWE-bench passes only
    image/user/command/platform, and its one ``nano_cpus`` value is vestigial —
    nothing reads it. Teich sets none at all while running agents with
    ``--dangerously-skip-permissions`` semantics. An agent that forks a fork
    bomb or allocates all available memory takes the host down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: float | None = Field(default=2.0, gt=0)
    memory: str | None = Field(default="4g")
    memory_swap: str | None = Field(default=None)
    pids: int | None = Field(default=512, gt=0)
    shm_size: str | None = Field(default=None)
    nofile: int | None = Field(default=4096, gt=0)

    @field_validator("memory", "memory_swap", "shm_size")
    @classmethod
    def _validate_size(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if not text or not text[0].isdigit() or not text.rstrip("bkmg").isdigit():
            raise ValueError(
                f"invalid size {value!r}: expected a number optionally "
                "suffixed with b, k, m or g (e.g. '4g')"
            )
        return text

    def docker_args(self) -> list[str]:
        args: list[str] = []
        if self.cpus is not None:
            args += ["--cpus", str(self.cpus)]
        if self.memory is not None:
            args += ["--memory", self.memory]
        if self.memory_swap is not None:
            args += ["--memory-swap", self.memory_swap]
        if self.pids is not None:
            args += ["--pids-limit", str(self.pids)]
        if self.shm_size is not None:
            args += ["--shm-size", self.shm_size]
        if self.nofile is not None:
            args += ["--ulimit", f"nofile={self.nofile}:{self.nofile}"]
        return args


class ImageSpec(BaseModel):
    """A recipe for one image. Pure data — building it is a separate step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_image: str = Field(default="debian:trixie-slim", min_length=1)
    repository: str = Field(default="jormungandr", min_length=1)
    modules: tuple[ModuleDeclaration, ...] = ()
    target_platform: str = Field(default_factory=default_platform)
    build_args: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("target_platform")
    @classmethod
    def _validate_platform(cls, value: str) -> str:
        if "/" not in value:
            raise ValueError(
                f"invalid platform {value!r}: expected os/arch, e.g. 'linux/arm64'"
            )
        return value

    @model_validator(mode="after")
    def _validate_unique_modules(self) -> ImageSpec:
        seen: set[str] = set()
        for declaration in self.modules:
            if declaration.name in seen:
                raise ValueError(f"module {declaration.name!r} declared twice")
            seen.add(declaration.name)
        return self


class ContainerSpec(BaseModel):
    """Everything that varies per run. Never enters the image hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(min_length=1)
    command: tuple[str, ...] = ("sleep", "infinity")
    entrypoint: tuple[str, ...] | None = None
    workdir: str | None = None
    user: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    env_files: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    network: Literal["none", "bridge", "host"] = "bridge"
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    labels: dict[str, str] = Field(default_factory=dict)
    read_only: bool = False
    no_new_privileges: bool = True
    cap_drop_all: bool = True
    cap_add: tuple[str, ...] = ()
    init: bool = True
    auto_remove: bool = False
    tmpfs: tuple[str, ...] = ()

    @field_validator("env")
    @classmethod
    def _reject_secretish_env(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse to put obvious credentials on the command line.

        ``docker run -e KEY=value`` puts the value in the host process table and
        records it permanently in ``docker inspect``. Teich passes API keys,
        Langfuse secret keys and OAuth tokens exactly this way. Secrets belong
        in ``env_files``, which the CLI reads with mode 0600.
        """
        suspicious = {
            key
            for key in value
            if any(marker in key.upper() for marker in ("SECRET", "TOKEN", "PASSWORD"))
            or key.upper().endswith("_API_KEY")
        }
        if suspicious:
            listed = ", ".join(sorted(suspicious))
            raise ValueError(
                f"refusing to pass {listed} via -e: visible in `ps` and recorded "
                "in `docker inspect`. Use env_files instead."
            )
        return value
