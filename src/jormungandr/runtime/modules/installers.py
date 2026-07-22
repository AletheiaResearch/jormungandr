"""Installation strategies for harness modules.

Harnesses do not share an install mechanism, and an abstraction that assumes
one is wrong on contact with the second harness. The three shapes that exist in
the wild today:

* **npm global** — opencode (``opencode-ai``), pi
  (``@mariozechner/pi-coding-agent``), droid (as an alternative).
* **piped shell installer** — droid (``curl -fsSL https://app.factory.ai/cli | sh``).
* **git clone + Python venv + wrapper shim** — hermes, which clones a repo,
  builds a venv with uv, installs editable, and writes a launcher onto PATH.

The third is the one that breaks a package-manager-shaped design: it needs git
and Python as prerequisites, installs into a library directory rather than a
bin, and has to emit a shell script. So the install method is a strategy the
harness composes, not a fixed part of what a harness *is*.

Adding a fourth shape (a release tarball, a distro package, a prebuilt binary)
means adding one class here — no harness module changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from jormungandr.runtime.layers import CacheMount, Comment, Copy, Instruction, Run
from jormungandr.runtime.modules.base import BuildContext, ModuleError

__all__ = [
    "GitPythonApp",
    "Installer",
    "NpmGlobal",
    "ShellInstall",
]

_NPM_CACHE = (CacheMount("/root/.npm"),)
_UV_CACHE = (CacheMount("/root/.cache/uv"),)


@runtime_checkable
class Installer(Protocol):
    """Knows how to put one program into an image."""

    default_requires: tuple[str, ...]
    """Modules this strategy needs present (e.g. ``("node",)``)."""

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        """Dockerfile instructions that perform the install. Must be pure."""
        ...

    def identity(self) -> Mapping[str, object]:
        """Everything about this install that affects the resulting image."""
        ...


def _validate(value: object, *, what: str) -> str:
    from jormungandr.runtime.modules.builtin import _quoted_arg

    _quoted_arg(value, what=what)  # rejects empty and newline-bearing values
    return str(value)


class NpmGlobal:
    """``npm install -g <package>@<version>``.

    Used by opencode, pi, droid, and most JS-distributed CLIs. Note that npm
    exits 0 even when a package's platform-specific optional dependency did not
    match, so the caller is expected to verify the binary afterwards.
    """

    # Annotated, not inferred: a bare literal infers as tuple[str], a
    # fixed-length type that does not satisfy the protocol's tuple[str, ...].
    default_requires: tuple[str, ...] = ("node",)

    def __init__(self, package: str, version: str) -> None:
        from jormungandr.runtime.modules.builtin import _safe_token

        self.package = _safe_token(package, what="npm package")
        self.version = _validate(version, what="npm version")

    @property
    def spec(self) -> str:
        return f"{self.package}@{self.version}"

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        from jormungandr.runtime.modules.builtin import _quoted_arg

        return [
            Run(
                f"npm install -g {_quoted_arg(self.spec, what='npm install spec')}",
                mounts=_NPM_CACHE,
            )
        ]

    def identity(self) -> Mapping[str, object]:
        return {"kind": "npm", "package": self.package, "version": self.version}


class ShellInstall:
    """A vendor's ``curl … | sh`` installer, as used by droid.

    Piping a network response into a shell is a poor way to install software,
    but it is what several vendors document and sometimes the only supported
    path. The damage is bounded a little here: the script is downloaded to disk
    first so it can be checksummed, and ``sha256`` is strongly recommended —
    without it the image contents depend on whatever the URL served at build
    time, which quietly breaks the promise that a digest identifies a fixed set
    of software.
    """

    default_requires: tuple[str, ...] = ()

    def __init__(
        self,
        url: str,
        *,
        sha256: str | None = None,
        shell: str = "sh",
        env: Mapping[str, str] | None = None,
    ) -> None:
        from jormungandr.runtime.modules.builtin import _safe_token

        self.url = _safe_token(url, what="installer url")
        self.shell = _safe_token(shell, what="installer shell")
        self.sha256 = _safe_token(sha256, what="installer sha256") if sha256 else None
        if self.sha256 is not None and len(self.sha256) != 64:
            raise ModuleError(
                f"sha256 must be 64 hex characters, got {len(self.sha256)}"
            )
        self.env = {str(k): str(v) for k, v in (env or {}).items()}

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        prefix = "".join(f"{k}={v} " for k, v in sorted(self.env.items()))
        commands = [f"curl -fsSL {self.url} -o /tmp/install.sh"]
        if self.sha256:
            commands.append(f'echo "{self.sha256}  /tmp/install.sh" | sha256sum -c -')
        commands += [f"{prefix}{self.shell} /tmp/install.sh", "rm -f /tmp/install.sh"]
        head: list[Instruction] = []
        if not self.sha256:
            head.append(
                Comment(
                    "WARNING: unpinned remote installer — the image contents depend "
                    "on what this URL serves at build time. Pass sha256= to pin it."
                )
            )
        return [*head, Run(commands)]

    def identity(self) -> Mapping[str, object]:
        return {
            "kind": "shell",
            "url": self.url,
            "sha256": self.sha256,
            "shell": self.shell,
            "env": dict(self.env),
        }


class GitPythonApp:
    """Clone a Python project, install it into its own venv, and shim it onto PATH.

    The shape hermes needs. The venv is deliberately separate from any other
    Python environment in the image so the harness's dependencies cannot
    conflict with a module's, and the shim clears ``PYTHONPATH``/``PYTHONHOME``
    so an inherited environment cannot break it.

    ``ref`` should be a tag or commit rather than a branch: a branch name makes
    the image digest a lie, since the same digest would refer to whatever the
    branch pointed at when it was built.
    """

    default_requires: tuple[str, ...] = ("python",)

    def __init__(
        self,
        repo: str,
        *,
        ref: str,
        binary: str,
        dest: str | None = None,
        entrypoint: str | None = None,
    ) -> None:
        from jormungandr.runtime.modules.builtin import _safe_token

        self.repo = _safe_token(repo, what="git repo url")
        self.ref = _safe_token(ref, what="git ref")
        self.binary = _safe_token(binary, what="binary name")
        self.dest = _safe_token(dest or f"/usr/local/lib/{binary}", what="install dir")
        self.entrypoint = _safe_token(entrypoint or binary, what="venv entrypoint")

    @property
    def _venv(self) -> str:
        return f"{self.dest}/venv"

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        shim_name = f"{self.binary}.shim.sh"
        shim = context.add_file(
            shim_name,
            "#!/usr/bin/env bash\n"
            "# Generated by jormungandr.\n"
            "# PYTHONPATH/PYTHONHOME are cleared so an inherited environment\n"
            "# cannot shadow this venv's packages.\n"
            "unset PYTHONPATH\n"
            "unset PYTHONHOME\n"
            f'exec {self._venv}/bin/{self.entrypoint} "$@"\n',
            mode=0o755,
        )
        return [
            Run(
                [
                    f"git clone --filter=blob:none {self.repo} {self.dest}",
                    f"git -C {self.dest} checkout {self.ref}",
                    f"rm -rf {self.dest}/.git",
                    f"uv venv {self._venv} --python python3",
                    f"uv pip install --python {self._venv}/bin/python {self.dest}",
                ],
                mounts=_UV_CACHE,
            ),
            Copy(shim, f"/usr/local/bin/{self.binary}"),
        ]

    def identity(self) -> Mapping[str, object]:
        return {
            "kind": "git-python",
            "repo": self.repo,
            "ref": self.ref,
            "dest": self.dest,
            "entrypoint": self.entrypoint,
        }
