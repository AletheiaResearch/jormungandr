"""Provider declarations, and their translation into harness-native config.

A provider is a base URL, a credential and some models. Every harness needs
exactly that and every harness wants it in a different shape, so it is declared
once here and translated per harness.

The translation is not cosmetic. Verified against real containers with stub
endpoints and no vendor accounts, the same declaration becomes:

===============  =====================================  ==========================
                 droid                                  opencode
===============  =====================================  ==========================
file             ``~/.factory/settings.json``           ``~/.config/opencode/opencode.json``
model name       ``custom:<displayName>-<index>``       ``<providerId>/<modelId>``
selected via     ``sessionDefaultSettings.model``       ``--model`` (works)
secret syntax    ``${VAR}``                             ``{env:VAR}``
also needs       ``FACTORY_AIRGAP_ENABLED``             ``limit.context``/``limit.output``
===============  =====================================  ==========================

Even the secret-interpolation syntax differs, which is why the config never
exposes a harness's native strings: a prompt file that named them would only
work for one harness.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ENV_REFERENCE",
    "ModelRef",
    "ProviderSpec",
    "env_names",
    "parse_model_ref",
]

ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
"""The one syntax the *user* writes. Harness-native forms are generated."""

# Values that look like real credentials rather than references. Baked config is
# readable by anyone who can pull the image, and `docker history` preserves it.
_CREDENTIAL_HINTS = ("sk-", "pk-", "ghp_", "gho_", "xoxb-", "fk-", "AKIA")


class ModelRef(BaseModel):
    """A resolved ``<provider>/<model>`` reference."""

    model_config = ConfigDict(frozen=True)

    provider: str
    alias: str
    model_id: str


def parse_model_ref(reference: str) -> tuple[str, str]:
    """Split ``provider/alias``.

    Splits on the *first* slash only: a model alias may itself contain slashes
    (``openrouter/deepseek/deepseek-v4`` is a provider plus a two-segment
    alias), which is also how OpenCode parses its own references.
    """
    provider, separator, alias = reference.partition("/")
    if not separator or not provider or not alias:
        raise ValueError(
            f"invalid model reference {reference!r}: expected '<provider>/<model>'"
        )
    return provider, alias


class ProviderSpec(BaseModel):
    """One model provider, declared once and referenced by name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["openai-compatible", "openai-responses", "anthropic"] = (
        "openai-compatible"
    )
    """Wire protocol.

    ``openai-compatible`` is ``/v1/chat/completions``; ``openai-responses`` is
    ``/v1/responses``. They are different endpoints and OpenCode needs a
    different SDK package for each, so guessing is not an option.
    """

    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    """An ``${ENV_VAR}`` reference. Literals are refused."""

    models: dict[str, str] = Field(min_length=1)
    """alias -> upstream model id, e.g. ``{"deepseek": "deepseek/deepseek-v4"}``."""

    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=16384, gt=0)

    @field_validator("api_key")
    @classmethod
    def _must_be_an_env_reference(cls, value: str) -> str:
        if ENV_REFERENCE.match(value):
            return value
        looks_secret = any(value.startswith(hint) for hint in _CREDENTIAL_HINTS)
        detail = (
            f" It looks like a literal credential ({value[:6]}…)."
            if looks_secret
            else ""
        )
        # The offending value is deliberately NOT interpolated. pydantic echoes
        # the input in its own error text, so this validator raises with the
        # value stripped out — otherwise reporting a leaked key would print the
        # key, into a terminal and very likely a CI log.
        raise ValueError(
            f"api_key must be an environment reference like ${{MY_API_KEY}}.{detail}"
            " Baked config is readable by anyone who can pull the image; supply"
            " the value at run time via run.env_files."
        )

    @property
    def env_var(self) -> str:
        match = ENV_REFERENCE.match(self.api_key)
        if match is None:
            # The validator guarantees this; an assert would vanish under -O.
            # The value is deliberately not interpolated — this class exists to
            # keep a leaked key out of error text.
            raise ValueError("api_key is not an environment reference")
        return match.group(1)

    def resolve(self, alias: str) -> str:
        try:
            return self.models[alias]
        except KeyError:
            known = ", ".join(sorted(self.models))
            raise ValueError(
                f"unknown model alias {alias!r}; declared: {known}"
            ) from None


def env_names(providers: dict[str, ProviderSpec]) -> set[str]:
    """Every environment variable the providers reference.

    Checked before a run starts: OpenCode substitutes an unset ``{env:VAR}``
    with the empty string rather than failing, which turns a config mistake into
    a confusing 401 from the provider.
    """
    return {provider.env_var for provider in providers.values()}
