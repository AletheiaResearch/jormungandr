from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from jormungandr.runtime.layers import Instruction, Run
from jormungandr.runtime.modules.base import BuildContext, Module, ModuleError, Stage
from jormungandr.runtime.modules.registry import (
    ModuleRegistry,
    build_modules,
    resolve_order,
)


class FakeModule:
    """Minimal Module implementation for ordering tests."""

    def __init__(
        self,
        name: str,
        *,
        stage: int = Stage.SYSTEM,
        requires: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.stage = stage
        self.requires = requires

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [Run(f"echo {self.name}")]

    def identity(self) -> Mapping[str, object]:
        return {"name": self.name}


def names(modules: Sequence[Module]) -> list[str]:
    return [m.name for m in modules]


class TestResolveOrder:
    def test_orders_by_stage_then_name(self) -> None:
        result = resolve_order(
            [
                FakeModule("zulu", stage=Stage.USER),
                FakeModule("alpha", stage=Stage.HARNESS),
                FakeModule("beta", stage=Stage.SYSTEM),
            ]
        )
        assert names(result) == ["beta", "alpha", "zulu"]

    def test_ties_broken_by_name(self) -> None:
        result = resolve_order(
            [FakeModule("b", stage=Stage.SYSTEM), FakeModule("a", stage=Stage.SYSTEM)]
        )
        assert names(result) == ["a", "b"]

    def test_independent_of_input_order(self) -> None:
        # The order feeds the image hash, so it must not depend on how the
        # caller happened to list the modules.
        mods = [
            FakeModule("node", stage=Stage.TOOLCHAIN),
            FakeModule("apt", stage=Stage.SYSTEM),
            FakeModule("langfuse", stage=Stage.INTEGRATION),
        ]
        forward = names(resolve_order(mods))
        backward = names(resolve_order(list(reversed(mods))))
        assert forward == backward

    def test_requires_is_honoured_against_stage_order(self) -> None:
        # 'early' sorts first by stage, but must wait for its requirement.
        result = resolve_order(
            [
                FakeModule("early", stage=Stage.SYSTEM, requires=("late",)),
                FakeModule("late", stage=Stage.USER),
            ]
        )
        assert names(result) == ["late", "early"]

    def test_transitive_requires(self) -> None:
        result = resolve_order(
            [
                FakeModule("c", requires=("b",)),
                FakeModule("b", requires=("a",)),
                FakeModule("a"),
            ]
        )
        assert names(result) == ["a", "b", "c"]

    def test_missing_requirement_is_an_error(self) -> None:
        with pytest.raises(ModuleError, match="requires 'ghost'"):
            resolve_order([FakeModule("x", requires=("ghost",))])

    def test_cycle_is_an_error(self) -> None:
        with pytest.raises(ModuleError, match="cycle"):
            resolve_order(
                [FakeModule("a", requires=("b",)), FakeModule("b", requires=("a",))]
            )

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ModuleError, match="duplicate"):
            resolve_order([FakeModule("dup"), FakeModule("dup")])

    def test_empty_is_fine(self) -> None:
        assert resolve_order([]) == ()


class TestModuleRegistry:
    def test_register_and_create(self) -> None:
        registry = ModuleRegistry()
        registry.register("fake", FakeModule)
        module = registry.create("fake", name="fake")
        assert module.name == "fake"

    def test_duplicate_registration_rejected(self) -> None:
        registry = ModuleRegistry()
        registry.register("fake", FakeModule)
        with pytest.raises(ModuleError, match="already registered"):
            registry.register("fake", FakeModule)

    def test_replace_is_explicit(self) -> None:
        registry = ModuleRegistry()
        registry.register("fake", FakeModule)
        registry.register("fake", FakeModule, replace=True)
        assert "fake" in registry

    def test_unknown_module_lists_known_ones(self) -> None:
        registry = ModuleRegistry()
        registry.register("apt", FakeModule)
        with pytest.raises(ModuleError, match="known modules: apt"):
            registry.create("nope")

    def test_names_are_sorted(self) -> None:
        registry = ModuleRegistry()
        registry.register("z", FakeModule)
        registry.register("a", FakeModule)
        assert registry.names == ("a", "z")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ModuleError, match="non-empty"):
            ModuleRegistry().register("", FakeModule)


class TestBuildModules:
    @staticmethod
    def _registry() -> ModuleRegistry:
        registry = ModuleRegistry()
        registry.register("late", lambda: FakeModule("late", stage=Stage.USER))
        registry.register("early", lambda: FakeModule("early", stage=Stage.SYSTEM))
        return registry

    def test_builds_and_orders(self) -> None:
        result = build_modules(
            [{"name": "late"}, {"name": "early"}], registry=self._registry()
        )
        assert names(result) == ["early", "late"]

    def test_config_is_passed_to_the_factory(self) -> None:
        # The registry key selects the factory; the factory owns the module's
        # .name, so "name" is consumed by the lookup and not forwarded.
        registry = ModuleRegistry()
        registry.register("cfg", lambda **kw: FakeModule("cfg", **kw))
        (module,) = build_modules(
            [{"name": "cfg", "stage": Stage.HARNESS}], registry=registry
        )
        assert module.stage == Stage.HARNESS

    def test_duplicate_declarations_rejected(self) -> None:
        with pytest.raises(ModuleError, match="duplicate"):
            build_modules(
                [{"name": "early"}, {"name": "early"}], registry=self._registry()
            )

    def test_unknown_module_rejected(self) -> None:
        with pytest.raises(ModuleError, match="unknown module"):
            build_modules([{"name": "ghost"}], registry=self._registry())

    def test_missing_name_rejected(self) -> None:
        with pytest.raises(ModuleError, match="missing a 'name'"):
            build_modules([{"stage": 1}], registry=ModuleRegistry())


class TestBuildContext:
    def test_add_and_read(self) -> None:
        ctx = BuildContext()
        path = ctx.add_file("setup.sh", "echo hi")
        assert path == "setup.sh"
        assert ctx.files == {"setup.sh": "echo hi"}
        assert ctx.modes == {"setup.sh": 0o644}

    def test_identical_readd_is_fine(self) -> None:
        ctx = BuildContext()
        ctx.add_file("a.sh", "x")
        ctx.add_file("a.sh", "x")
        assert ctx.files == {"a.sh": "x"}

    def test_conflicting_content_rejected(self) -> None:
        ctx = BuildContext()
        ctx.add_file("a.sh", "x")
        with pytest.raises(ModuleError, match="disagree"):
            ctx.add_file("a.sh", "y")

    def test_conflicting_mode_rejected(self) -> None:
        ctx = BuildContext()
        ctx.add_file("a.sh", "x", mode=0o644)
        with pytest.raises(ModuleError, match="disagree"):
            ctx.add_file("a.sh", "x", mode=0o755)

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ModuleError, match="relative"):
            BuildContext().add_file("/etc/passwd", "x")

    def test_parent_traversal_rejected(self) -> None:
        with pytest.raises(ModuleError, match="relative"):
            BuildContext().add_file("../escape.sh", "x")

    def test_files_property_is_a_copy(self) -> None:
        ctx = BuildContext()
        ctx.add_file("a", "1")
        ctx.files["b"] = "2"  # type: ignore[index]
        assert "b" not in ctx.files
