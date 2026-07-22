"""Compose Docker images and run containers for agent harnesses.

The runtime layer only: it builds images that can run agent CLIs and manages
the containers they run in. Reading, parsing or converting what a harness
wrote is a separate contract, and is deliberately not crossed here.

Start at :mod:`jormungandr.execute`, which joins the two halves — a config
compiles to an image spec, and a runner drives prompts in a container.
"""


def main() -> None:
    """Print a greeting.

    Vestigial: left by ``uv init`` and referenced by nothing. The console
    script declared in ``pyproject.toml`` is ``jormungandr.cli:main``, which
    is what running ``jormungandr`` actually invokes.
    """
    print("Hello from jormungandr!")
