"""Module registry, entry-point discovery, and deterministic ordering.

Resolution order is a topological sort over ``requires``, with ties broken by
``(stage, name)``. It never depends on registration order, dict iteration order,
or the order the caller happened to list modules in — because that order feeds
the image hash, and an unstable hash means either spurious rebuilds or, worse,
a stale image silently reused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

from jormungandr.runtime.modules.base import Module, ModuleError

__all__ = [
    "REGISTRY",
    "ModuleRegistry",
    "load_entry_point_modules",
    "resolve_order",
]

ModuleFactory = Callable[..., Module]

ENTRY_POINT_GROUP = "jormungandr.modules"


class ModuleRegistry:
    """Maps a name to a factory that builds a configured module."""

    def __init__(self) -> None:
        self._factories: dict[str, ModuleFactory] = {}

    def register(
        self, name: str, factory: ModuleFactory, *, replace: bool = False
    ) -> None:
        if not name:
            raise ModuleError("module name must be non-empty")
        if name in self._factories and not replace:
            raise ModuleError(
                f"module {name!r} is already registered; "
                "pass replace=True to override it deliberately"
            )
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def create(self, name: str, /, **config: object) -> Module:
        try:
            factory = self._factories[name]
        except KeyError:
            known = ", ".join(sorted(self._factories)) or "<none>"
            raise ModuleError(
                f"unknown module {name!r}; known modules: {known}"
            ) from None
        return factory(**config)

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._factories))

    def __len__(self) -> int:
        return len(self._factories)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


REGISTRY = ModuleRegistry()
"""The process-wide registry. Built-in modules register themselves on import."""


def load_entry_point_modules(registry: ModuleRegistry | None = None) -> tuple[str, ...]:
    """Discover third-party modules published under ``jormungandr.modules``.

    Lets a private repo or a downstream package add a module without forking.
    Returns the names that were loaded.
    """
    from importlib.metadata import entry_points

    target = REGISTRY if registry is None else registry
    loaded: list[str] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        factory = entry_point.load()
        target.register(entry_point.name, factory, replace=True)
        loaded.append(entry_point.name)
    return tuple(sorted(loaded))


def resolve_order(modules: Iterable[Module]) -> tuple[Module, ...]:
    """Order modules for rendering.

    Topological over ``requires``; ties broken by ``(stage, name)`` so the
    result is a pure function of the module set. Raises on duplicate names,
    unsatisfied requirements, and dependency cycles.
    """
    items = list(modules)
    by_name: dict[str, Module] = {}
    for module in items:
        if module.name in by_name:
            raise ModuleError(f"duplicate module name {module.name!r}")
        by_name[module.name] = module

    for module in items:
        for requirement in module.requires:
            if requirement not in by_name:
                raise ModuleError(
                    f"module {module.name!r} requires {requirement!r}, "
                    "which is not in the module set"
                )

    ordered: list[Module] = []
    placed: set[str] = set()
    # Deterministic candidate order; the topological constraint only ever
    # delays a module, so the result is fully determined by the set.
    remaining = sorted(items, key=lambda m: (m.stage, m.name))

    while remaining:
        ready = [m for m in remaining if all(r in placed for r in m.requires)]
        if not ready:
            stuck = ", ".join(sorted(m.name for m in remaining))
            raise ModuleError(f"dependency cycle among modules: {stuck}")
        chosen = ready[0]
        ordered.append(chosen)
        placed.add(chosen.name)
        remaining.remove(chosen)

    return tuple(ordered)


def build_modules(
    specs: Sequence[Mapping[str, object]],
    *,
    registry: ModuleRegistry | None = None,
) -> tuple[Module, ...]:
    """Instantiate modules from ``[{"name": ..., **config}]`` declarations."""
    target = REGISTRY if registry is None else registry
    built: list[Module] = []
    for entry in specs:
        config = dict(entry)
        name = config.pop("name", None)
        if not isinstance(name, str):
            raise ModuleError(f"module declaration missing a 'name': {entry!r}")
        built.append(target.create(name, **config))
    return resolve_order(built)
