"""CLI declarations. Stdlib + cyclopts imports only — command bodies
import their implementations lazily so --help stays fast.

No commands are wired up yet: the runtime is still internals-only. Commands
land here once there is a user-facing workflow to expose.
"""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter

app = App(name="jormungandr", help="...")


@app.meta.default
def _launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: bool = False,
) -> None:
    from jormungandr.cli_helpers import configure_logging

    configure_logging(verbose=verbose)
    app(tokens)


def main() -> None:
    from jormungandr.cli_helpers import install_error_handler

    install_error_handler()
    app.meta()
