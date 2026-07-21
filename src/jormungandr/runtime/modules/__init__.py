"""Composable image modules.

Adding a capability to an image is: one module class, one registry entry, one
test. Nothing else moves.
"""

from __future__ import annotations

from jormungandr.runtime.modules.base import BuildContext, Module, ModuleError, Stage
from jormungandr.runtime.modules.registry import (
    REGISTRY,
    ModuleRegistry,
    build_modules,
    load_entry_point_modules,
    resolve_order,
)

__all__ = [
    "REGISTRY",
    "BuildContext",
    "Module",
    "ModuleError",
    "ModuleRegistry",
    "Stage",
    "build_modules",
    "load_entry_point_modules",
    "resolve_order",
]
