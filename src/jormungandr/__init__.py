"""Compose Docker images and run containers for agent harnesses.

The runtime layer only: it builds images that can run agent CLIs and manages
the containers they run in. Reading, parsing or converting what a harness
wrote is a separate contract, and is deliberately not crossed here.

Start at :mod:`jormungandr.execute`, which joins the two halves — a config
compiles to an image spec, and a runner drives prompts in a container.

Deliberately empty otherwise: importing anything here would make ``import
jormungandr`` pay for the runtime stack, which is the cost ``cli.py`` is
arranged to avoid.
"""
