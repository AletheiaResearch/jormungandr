"""Loading a config file, and compiling it into runtime objects."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from jormungandr.config.models import JormConfig
from jormungandr.config.prompts import PromptRecord, load_prompts

__all__ = ["ConfigError", "compile_image_spec", "load_config", "resolve_prompts"]

log = logging.getLogger(__name__)

ENV_PREFIX = "JORM_"

# Scalars only. Anything structured belongs in the file, where it can be reviewed.
ENV_OVERRIDES = {
    f"{ENV_PREFIX}MODEL": ("harness", "model"),
    f"{ENV_PREFIX}HARNESS": ("harness", "name"),
    f"{ENV_PREFIX}CONCURRENCY": ("run", "concurrency"),
    f"{ENV_PREFIX}TIMEOUT": ("run", "timeout"),
    f"{ENV_PREFIX}NETWORK": ("run", "network"),
}


class ConfigError(ValueError):
    """The config file is unusable."""


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    """Apply ``JORM_*`` overrides, announcing each one.

    Teich applies its ``TEICH_*`` overrides to the raw dict before validation
    with no logging and no opt-out, so the file says one thing, the run does
    another, and nothing explains why. Every override here is reported with its
    source and the value it replaced.
    """
    for variable, (section, key) in ENV_OVERRIDES.items():
        if variable not in environ:
            continue
        value = environ[variable]
        target = data.setdefault(section, {})
        if not isinstance(target, dict):
            raise ConfigError(f"cannot apply {variable}: '{section}' is not a mapping")
        previous = target.get(key)
        target[key] = value
        log.info(
            "config: %s.%s = %s  (%s, overriding %r)",
            section,
            key,
            value,
            variable,
            previous,
        )


def _resolve_paths(data: dict[str, Any], base: Path) -> None:
    """Resolve every relative path against the config file's directory.

    One rule, applied everywhere. Teich resolves only ``prompts_file`` this way
    and leaves output directories relative to the process CWD, so running with
    a config from another directory writes output to the wrong place.
    """
    for section, key, default in (
        ("prompts", "file", None),
        ("output", "dir", "./runs"),
    ):
        block = data.get(section)
        if block is None and default is not None:
            # An omitted block still has a default path, and leaving it
            # unresolved makes it relative to the process CWD — so the same
            # config writes somewhere else depending on where it was invoked.
            block = data.setdefault(section, {})
        if isinstance(block, dict):
            value = block.get(key, default)
            if value is not None:
                block[key] = str((base / str(value)).resolve())

    run = data.get("run")
    if isinstance(run, dict) and run.get("env_files"):
        run["env_files"] = [str((base / str(p)).resolve()) for p in run["env_files"]]


def _reject_literal_credentials(data: dict[str, Any], path: Path) -> None:
    """Refuse a literal api_key before pydantic ever sees it.

    ProviderSpec already rejects one, but pydantic includes the offending
    ``input_value`` in its error text — so reporting a leaked key would print
    the key, to a terminal and very likely into a CI log where it outlives the
    run. Checking the raw mapping first means the value never reaches a
    formatter.
    """
    from jormungandr.config.providers import ENV_REFERENCE

    providers = data.get("providers")
    if not isinstance(providers, dict):
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        key = provider.get("api_key")
        if isinstance(key, str) and key and not ENV_REFERENCE.match(key):
            raise ConfigError(
                f"{path}: providers.{name}.api_key must be an environment "
                "reference like ${MY_API_KEY}, not a literal value. Baked config "
                "is readable by anyone who can pull the image; supply the value "
                "at run time via run.env_files. "
                "(The offending value is not shown here on purpose.)"
            )


def load_config(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
    apply_env: bool = True,
) -> JormConfig:
    """Load and validate a config file."""
    import yaml

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    _resolve_paths(raw, path.parent)
    if apply_env:
        _apply_env_overrides(raw, dict(environ if environ is not None else os.environ))

    _reject_literal_credentials(raw, path)

    try:
        return JormConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def resolve_prompts(config: JormConfig) -> tuple[PromptRecord, ...]:
    """Read the prompt file named by the config.

    Deliberately not done during config validation: a command that never reads
    prompts should not fail because the prompt file is missing. Teich validates
    ``prompts_file`` existence at load, so its own Studio has to special-case
    around it.
    """
    records = load_prompts(config.prompts.file)
    if config.prompts.limit is not None:
        records = records[: config.prompts.limit]
    return records


def compile_image_spec(config: JormConfig) -> Any:
    """Build the ImageSpec, including the harness with its translated config."""
    from jormungandr.runtime.modules import REGISTRY, builtin  # noqa: F401
    from jormungandr.runtime.spec import ImageSpec

    harness_cls = {"droid": builtin.Droid, "opencode": builtin.OpenCode}[
        config.harness.name
    ]
    translated = harness_cls.translate_providers(config.providers, config.harness.model)

    declaration: dict[str, Any] = {
        "name": config.harness.name,
        "config": translated,
        **config.harness.settings,
    }
    if config.harness.version:
        declaration["version"] = config.harness.version

    declared = {m.get("name") for m in config.image.modules}
    modules: list[dict[str, Any]] = list(config.image.modules)

    # The user account must exist before the harness writes config into $HOME,
    # and something has to own the final WORKDIR/USER. Added when absent rather
    # than required, so a minimal config still produces a working image.
    if "user" not in declared:
        modules.insert(0, {"name": "user"})

    # Likewise the harness's own prerequisites: droid and opencode both need a
    # node toolchain, and making every config spell that out would be a trap
    # whose only symptom is a resolver error about a module the user never
    # mentioned. Only requirements the registry actually knows are added; an
    # unknown one still surfaces as a clear error from resolve_order.
    probe = harness_cls()
    for requirement in probe.requires:
        if requirement not in declared and requirement in REGISTRY:
            modules.append({"name": requirement})
            declared.add(requirement)

    modules.append(declaration)
    if "workdir" not in declared:
        # Follow whatever user the `user` module declares. Defaulting to
        # "agent" while the user module created someone else produces a
        # Dockerfile whose chown and USER name an account that does not exist.
        user_module = next(
            (m for m in config.image.modules if m.get("name") == "user"), {}
        )
        workdir: dict[str, Any] = {"name": "workdir"}
        if user_module.get("user"):
            workdir["user"] = user_module["user"]
        modules.append(workdir)

    return ImageSpec(
        base_image=config.image.base_image,
        repository=config.image.repository,
        tier_split=config.image.tier_split,
        modules=modules,
        build_args=config.image.build_args,
        labels=config.image.labels,
    )
