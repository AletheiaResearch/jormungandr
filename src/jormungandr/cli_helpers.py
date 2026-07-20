"""Presentation and process concerns. Rich lives here, never in cli.py."""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from typing import Any


class MissingExtra(RuntimeError):
    def __init__(self, module: str, extra: str) -> None:
        super().__init__(
            f"needs {module!r} — install with: pip install 'jormungandr[{extra}]'"
        )


def require(module: str, extra: str) -> Any:
    try:
        return import_module(module)
    except ImportError as exc:
        raise MissingExtra(module, extra) from exc


def configure_logging(*, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def install_error_handler() -> None:
    if os.environ.get("JORMUNGANDR_TRACEBACK"):
        return

    expected = (MissingExtra, FileNotFoundError, PermissionError, ValueError)

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            raise SystemExit(130)
        if issubclass(exc_type, BrokenPipeError):
            raise SystemExit(0)
        if issubclass(exc_type, expected):
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook