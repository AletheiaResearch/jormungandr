"""Implementations for the `jormungandr image` commands.

Heavy imports live here rather than in cli.py, so `--help` stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jormungandr.runtime.build import ImageBuilder
from jormungandr.runtime.compose import compose
from jormungandr.runtime.spec import ImageSpec

__all__ = ["build_image", "load_spec", "prune_images", "render_spec", "show_modules"]


def load_spec(path: Path | None, **overrides: Any) -> ImageSpec:
    """Load an ImageSpec from YAML/JSON, applying CLI overrides."""
    data: dict[str, Any] = {}
    if path is not None:
        text = path.read_text(encoding="utf-8")
        if path.suffix in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
    data.update({k: v for k, v in overrides.items() if v is not None})
    return ImageSpec.model_validate(data)


def render_spec(spec: ImageSpec) -> str:
    """Render the Dockerfile without building anything."""
    from jormungandr.runtime.modules import builtin  # noqa: F401

    return compose(spec).dockerfile


def build_image(
    spec: ImageSpec,
    *,
    state_dir: Path | None = None,
    force: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    from jormungandr.runtime.modules import builtin  # noqa: F401

    builder = ImageBuilder(state_dir=state_dir)
    result = builder.build(
        spec,
        force=force,
        on_output=None if quiet else _echo,
    )
    return {
        "reference": result.reference,
        "digest": result.digest,
        "image_id": result.image_id,
        "cached": result.cached,
        "log": str(result.log_path),
    }


def prune_images(*, state_dir: Path | None = None, keep: tuple[str, ...] = ()) -> list[str]:
    return ImageBuilder(state_dir=state_dir).prune(keep=keep)


def show_modules() -> list[dict[str, str]]:
    from jormungandr.runtime.modules import REGISTRY, builtin  # noqa: F401
    from jormungandr.runtime.modules.registry import load_entry_point_modules

    load_entry_point_modules()
    return [{"name": name} for name in REGISTRY.names]


def _echo(line: str) -> None:
    print(line, flush=True)
