"""Docker image composition and container lifecycle for agent harnesses.

Public API is re-exported lazily so that importing :mod:`jormungandr.runtime`
never pulls in the build or container machinery unless it is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jormungandr.runtime.spec import ContainerSpec, ImageSpec, ResourceLimits

__all__ = ["ContainerSpec", "ImageSpec", "ResourceLimits"]

_LAZY = {
    "ContainerSpec": "jormungandr.runtime.spec",
    "ImageSpec": "jormungandr.runtime.spec",
    "ResourceLimits": "jormungandr.runtime.spec",
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target), name)


def __dir__() -> list[str]:
    return sorted(__all__)
