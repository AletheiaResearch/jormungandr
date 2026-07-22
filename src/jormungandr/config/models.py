"""The top-level jormungandr config.

One file in, the existing runtime objects out:

===================================  ==========================
config section                       compiles to
===================================  ==========================
``image`` + ``harness``              ``ImageSpec``
``providers`` + ``harness.<name>``   the harness's own baked config
``run``                              ``ContainerSpec``
``prompts`` + ``run``                ``PromptRunner.run(...)``
===================================  ==========================

The envelope is thin on purpose. It is not a parallel universe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jormungandr.config.providers import ProviderSpec, env_names, parse_model_ref

__all__ = ["HarnessSpec", "JormConfig", "OutputSpec", "PromptsSpec", "RunSpec"]

# Harnesses whose settings may appear as a block under `harness`.
KNOWN_HARNESSES = ("droid", "opencode")


class HarnessSpec(BaseModel):
    """Which agent to run, and its harness-specific settings.

    Harness-specific keys live *only* under a block named for that harness.
    Teich scatters Codex-only knobs (`approval_policy`, `sandbox`,
    `service_tier`) through a shared `model:` block alongside genuinely shared
    ones, so a reader cannot tell which keys apply to the harness they picked —
    and setting a Codex key while running Pi is silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["droid", "opencode"]
    version: str | None = None
    model: str = Field(min_length=1)
    """``<provider>/<model-alias>`` — our syntax, not the harness's."""

    droid: dict[str, Any] | None = None
    opencode: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _reject_foreign_harness_blocks(self) -> HarnessSpec:
        """Reject a settings block for a harness you are not running.

        Silently ignoring it means a stale `codex:` block left behind after
        switching to `droid` looks configured and does nothing.
        """
        for other in KNOWN_HARNESSES:
            if other != self.name and getattr(self, other) is not None:
                raise ValueError(
                    f"harness.{other} is set but harness.name is {self.name!r}. "
                    f"Remove the '{other}' block or switch harness.name."
                )
        return self

    @property
    def settings(self) -> dict[str, Any]:
        """Return the settings block belonging to the selected harness.

        Copied, not aliased: the model is frozen, and handing out the live dict
        would let a caller mutate configuration that is meant to be fixed once
        loaded.
        """
        return dict(getattr(self, self.name) or {})


class PromptsSpec(BaseModel):
    """Where the prompt records come from, and how many of them to run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: Path
    limit: int | None = Field(default=None, gt=0)


class RunSpec(BaseModel):
    """How the containers are run: concurrency, limits, network and env."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    concurrency: int = Field(default=1, ge=1)
    timeout: float = Field(default=900.0, gt=0)
    network: Literal["none", "bridge", "host"] = "bridge"
    cpus: float | None = Field(default=2.0, gt=0)
    memory: str | None = "4g"
    env: dict[str, str] = Field(default_factory=dict)
    env_files: tuple[Path, ...] = ()
    mounts: tuple[str, ...] = ()


class OutputSpec(BaseModel):
    """Where a run's artifacts land, and whether harness state is collected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dir: Path = Path("./runs")
    collect_state: bool = True


class ImageSection(BaseModel):
    """Image settings. Modules the harness implies are added automatically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_image: str = "node:22-bookworm-slim"
    repository: str = "jormungandr"
    tier_split: int = 20
    modules: tuple[dict[str, Any], ...] = ()
    build_args: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)


class JormConfig(BaseModel):
    """A whole run, described in one file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    providers: dict[str, ProviderSpec] = Field(min_length=1)
    harness: HarnessSpec
    prompts: PromptsSpec
    image: ImageSection = Field(default_factory=ImageSection)
    run: RunSpec = Field(default_factory=RunSpec)
    output: OutputSpec = Field(default_factory=OutputSpec)

    @model_validator(mode="after")
    def _model_reference_resolves(self) -> JormConfig:
        provider_name, alias = parse_model_ref(self.harness.model)
        provider = self.providers.get(provider_name)
        if provider is None:
            known = ", ".join(sorted(self.providers))
            raise ValueError(
                f"harness.model references provider {provider_name!r}, "
                f"which is not declared. Declared providers: {known}"
            )
        if alias not in provider.models:
            known = ", ".join(sorted(provider.models))
            raise ValueError(
                f"harness.model references model {alias!r} on provider "
                f"{provider_name!r}, which declares: {known}"
            )
        return self

    @property
    def required_env(self) -> set[str]:
        """Environment variables that must exist when the run starts.

        OpenCode substitutes an unset ``{env:VAR}`` with the empty string rather
        than failing, so an unchecked missing variable surfaces as a confusing
        401 from the provider instead of a config error.
        """
        return env_names(self.providers)

    def missing_env(self, available: set[str]) -> set[str]:
        """Name the required variables that ``available`` does not supply.

        Reported before any container starts. OpenCode substitutes an unset
        variable with the empty string rather than failing, so a missing key
        would otherwise surface as an authentication error from the provider,
        after the image was built and the container was running.
        """
        return self.required_env - available
