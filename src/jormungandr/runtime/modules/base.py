"""The composable-module contract.

A module is one self-contained addition to an image: a language toolchain, an
agent CLI, a tracing integration, a set of apt packages, a user script. It
contributes Dockerfile instructions and, optionally, files to bake into the
build context.

Teich gates its one optional feature with ``ARG TEICH_INSTALL_LANGFUSE=0`` and
``if [ "$TEICH_INSTALL_LANGFUSE" = "1" ]`` branches inside RUN layers. That
works for exactly one feature and collapses at two, because every combination
has to be spelled out inside every affected layer. SWE-bench has no composition
primitive at all — adding an optional layer means editing all eight base
templates by hand.

Here, adding a capability is: one module class, one registry entry, one test.
Nothing else moves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from jormungandr.runtime.layers import Instruction

__all__ = ["BuildContext", "Module", "ModuleError", "Stage"]


class ModuleError(ValueError):
    """Raised when a module is misdeclared or cannot be resolved."""


class Stage:
    """Where a module's instructions land in the rendered Dockerfile.

    Ordering within the image follows rate-of-change, so that the layers that
    change most often sit on top and invalidate the least cache below them.
    """

    SYSTEM = 10
    """OS-level packages and users. Changes rarely, shared by everything."""

    TOOLCHAIN = 20
    """Language runtimes and package managers (node, python, uv)."""

    HARNESS = 30
    """Agent CLIs and the tooling they need."""

    INTEGRATION = 40
    """Observability, proxies, plugins — things layered onto a harness."""

    USER = 50
    """Caller-supplied scripts and overrides. Changes most often."""


class BuildContext:
    """Files a module wants baked into the build context.

    Modules never touch the filesystem themselves; they hand content to the
    builder, which materializes it. That keeps module rendering pure, so the
    same module always produces the same bytes and can be snapshot-tested with
    no Docker daemon and no temp directories.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._modes: dict[str, int] = {}

    def add_file(self, path: str, content: str, *, mode: int = 0o644) -> str:
        """Register a file at ``path`` relative to the context root.

        Returns the path, so a module can inline the call into a COPY.
        Re-adding identical content is a no-op; conflicting content is an error,
        because a silent overwrite between two modules would be near-impossible
        to debug.
        """
        if path.startswith("/") or ".." in path.split("/"):
            raise ModuleError(f"context path must be relative and contained: {path!r}")
        existing = self._files.get(path)
        if existing is not None and (existing != content or self._modes[path] != mode):
            raise ModuleError(
                f"two modules disagree about the contents of {path!r}; "
                "rename one of them"
            )
        self._files[path] = content
        self._modes[path] = mode
        return path

    @property
    def files(self) -> Mapping[str, str]:
        return dict(self._files)

    @property
    def modes(self) -> Mapping[str, int]:
        return dict(self._modes)


@runtime_checkable
class Module(Protocol):
    """A composable unit of image construction.

    Implementations are ordinary objects; construct them with whatever
    configuration they need, then hand them to the resolver.
    """

    name: str
    """Stable identifier. Appears in the image hash, so renaming forces a rebuild."""

    stage: int
    """One of the :class:`Stage` constants. Determines layer ordering."""

    requires: tuple[str, ...]
    """Names of modules that must be applied before this one."""

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        """Return this module's Dockerfile instructions.

        Must be pure: no network, no filesystem, no clock. Files go through
        ``context.add_file``. Called once per build, and its output is hashed.
        """
        ...

    def identity(self) -> Mapping[str, object]:
        """Configuration that affects the built image.

        Folded into the image hash. Anything that changes the resulting image
        must appear here, or a stale image will be silently reused.
        """
        ...
