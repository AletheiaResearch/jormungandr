"""Presentation and process concerns. Rich lives here, never in cli.py."""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from typing import Any


class MissingExtraError(RuntimeError):
    """An optional dependency is needed and is not installed."""

    def __init__(self, module: str, extra: str) -> None:
        """Build the message naming the extra that would supply ``module``.

        The install command goes in the message because a bare "no module named
        x" leaves the user to work out which extra provides it.
        """
        super().__init__(
            f"needs {module!r} — install with: pip install 'jormungandr[{extra}]'"
        )


def require(module: str, extra: str) -> Any:
    """Import ``module``, or say which extra would provide it.

    Turns an ImportError, which names a module the user never asked for, into
    one naming the extra they can actually install.
    """
    try:
        return import_module(module)
    except ImportError as exc:
        raise MissingExtraError(module, extra) from exc


def configure_logging(*, verbose: bool = False) -> None:
    """Quiet by default.

    Commands print their own progress in a form meant to be read; internal INFO
    logs carry a level and a logger name that only add noise beside it. At
    WARNING the default output is exactly what the command chose to say, and
    anything genuinely unexpected still surfaces.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def install_error_handler() -> None:
    """Report expected failures as one line instead of a traceback.

    A missing env var or an unreachable daemon is a condition the user can act
    on; printing forty frames of internal call stack for it buries the one line
    that matters. Genuine internal bugs still get the default hook.

    Set ``JORMUNGANDR_TRACEBACK`` to disable this entirely, which is what you
    want when the failure *is* the bug you are chasing.
    """
    if os.environ.get("JORMUNGANDR_TRACEBACK"):
        return

    always = (MissingExtraError, FileNotFoundError, PermissionError, ValueError)

    def _expected() -> tuple[type[BaseException], ...]:
        """Resolve the runtime error types only once something has failed.

        ExecutionError, BuildError and DockerError are RuntimeErrors, so they
        must be named explicitly rather than catching RuntimeError wholesale —
        a genuine internal bug should still produce a traceback. But importing
        them at install time would drag the whole docker stack into every
        invocation, including `--help`, which is exactly what keeping cli.py to
        declarations was meant to avoid. Nothing has failed yet at install
        time, so nothing needs importing yet.
        """
        extra: list[type[BaseException]] = []
        for module, name in (
            ("jormungandr.execute", "ExecutionError"),
            ("jormungandr.runtime.build", "BuildError"),
            ("jormungandr.runtime.docker", "DockerError"),
        ):
            try:
                extra.append(getattr(import_module(module), name))
            except Exception:  # noqa: S112 - reporting must not itself fail
                continue
        return always + tuple(extra)

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            raise SystemExit(130)
        if issubclass(exc_type, BrokenPipeError):
            raise SystemExit(0)
        if issubclass(exc_type, _expected()):
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
