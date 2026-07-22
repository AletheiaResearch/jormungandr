"""Entry point for ``python -m jormungandr``.

Separate from the console script so the package runs the same way whether or
not it was installed with its entry point on PATH — which is how the CLI tests
invoke it.
"""

from __future__ import annotations

from jormungandr.cli import main

if __name__ == "__main__":
    main()
