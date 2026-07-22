"""Every annotation in the tree must name something that exists.

Every module here opens with ``from __future__ import annotations``, so an
annotation is only a string until something calls ``typing.get_type_hints`` on
it. A name that was never imported therefore costs nothing at import time, and
raises ``NameError`` the moment anything introspects the signature — pydantic
building a ``TypeAdapter``, cyclopts deriving a CLI parameter,
``dataclasses.fields(eval_str=True)``, or any docs generator.

Three such names sat in the tree unnoticed: ``Any`` twice in ``execute.py`` and
``Path`` in the Docker-marked test module, which is deselected by default and so
had never been linted or introspected by anything.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pkgutil
import sys
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import jormungandr

TESTS_ROOT = Path(__file__).resolve().parent


def _functions(module: ModuleType) -> list[tuple[str, object]]:
    """Every function defined in ``module``, unwrapped past its decorators.

    ``inspect.unwrap`` follows ``__wrapped__``, which ``staticmethod``,
    ``functools.wraps`` and ``pytest.fixture`` all set, so a fixture declared as
    ``@pytest.fixture`` over ``@staticmethod`` still reaches the real function.

    Only functions this module actually defines count. A re-exported name such
    as ``pydantic.Field`` carries annotations written against *pydantic's*
    namespace, and resolving those here says nothing about this module.
    """
    found: list[tuple[str, object]] = []

    def visit(qualname: str, value: Any) -> None:
        target = inspect.unwrap(value)
        if isinstance(target, staticmethod | classmethod):
            target = target.__func__
        if isinstance(target, property):
            for part in (target.fget, target.fset, target.fdel):
                if part is not None:
                    visit(qualname, part)
            return
        if inspect.isfunction(target) and target.__module__ == module.__name__:
            found.append((qualname, target))

    for name, obj in vars(module).items():
        if inspect.isclass(obj) and obj.__module__ == module.__name__:
            for attr, value in vars(obj).items():
                visit(f"{name}.{attr}", value)
        else:
            visit(name, obj)
    return found


def _namespace(module: ModuleType) -> dict[str, Any]:
    """The module's globals, plus whatever it imports under ``TYPE_CHECKING``.

    A ``if TYPE_CHECKING:`` import is deliberate — it is how a module annotates
    against something it must not import at runtime, which is the whole reason
    ``commands/jobs.py`` can keep ``--help`` from paying for the docker stack.
    Those names are genuinely absent from the module at runtime, so resolving
    them has to be done explicitly.

    Executing the block rather than trusting it also makes this stricter: a
    ``TYPE_CHECKING`` import naming something that does not exist is invisible
    to the interpreter forever, and is exactly the class of mistake this file
    was written to catch.
    """
    namespace = dict(vars(module))
    source = inspect.getsource(module)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        guarded = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not guarded:
            continue
        for statement in node.body:
            if isinstance(statement, ast.Import | ast.ImportFrom):
                exec(  # noqa: S102 - the repo's own import statements, not input
                    compile(ast.Module([statement], []), "<type-checking>", "exec"),
                    namespace,
                )
    return namespace


def _unresolved(module: ModuleType) -> list[str]:
    """Names this module's annotations reference but its namespace does not hold."""
    namespace = _namespace(module)
    problems: list[str] = []
    for qualname, function in _functions(module):
        try:
            typing.get_type_hints(function, globalns=namespace)
        except NameError as exc:
            problems.append(f"{module.__name__}.{qualname}: {exc}")
    return problems


def _src_modules() -> list[str]:
    names = [jormungandr.__name__]
    names += [
        info.name
        for info in pkgutil.walk_packages(
            jormungandr.__path__, f"{jormungandr.__name__}."
        )
    ]
    return sorted(names)


def _test_modules() -> list[Path]:
    return sorted(p for p in TESTS_ROOT.rglob("test_*.py") if p != Path(__file__))


def _import_isolated(path: Path) -> ModuleType:
    """Import a test module under a private name, so pytest's copy is untouched."""
    name = f"_annotation_probe_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


@pytest.mark.parametrize("name", _src_modules())
def test_src_annotations_resolve(name: str) -> None:
    assert _unresolved(importlib.import_module(name)) == []


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_test_annotations_resolve(path: Path) -> None:
    assert _unresolved(_import_isolated(path)) == []
