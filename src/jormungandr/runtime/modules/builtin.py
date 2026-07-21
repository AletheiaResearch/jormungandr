"""Built-in modules.

Each is a small, independent class. Adding another one — a different agent CLI,
a different tracing backend — means adding a class here (or in a third-party
package, via the ``jormungandr.modules`` entry point) and one registry line.

Every module that installs packages uses a BuildKit cache mount, so a rebuild
after a config change does not re-download the world. SWE-bench re-downloads
every conda and pip package on every environment rebuild because the legacy
builder cannot express this.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from jormungandr.runtime.layers import (
    CacheMount,
    Comment,
    Copy,
    Env,
    Instruction,
    Run,
    User,
    Workdir,
)
from jormungandr.runtime.modules.base import BuildContext, ModuleError, Stage
from jormungandr.runtime.modules.registry import REGISTRY

__all__ = [
    "AgentCli",
    "AptPackages",
    "Langfuse",
    "NodeToolchain",
    "PythonToolchain",
    "Script",
    "Workspace",
    "register_builtins",
]

_APT_CACHE = (
    CacheMount("/var/cache/apt", sharing="locked"),
    CacheMount("/var/lib/apt", sharing="locked"),
)
_NPM_CACHE = (CacheMount("/root/.npm"),)
_UV_CACHE = (CacheMount("/root/.cache/uv"),)
_PIP_CACHE = (CacheMount("/root/.cache/pip"),)


class AptPackages:
    """Install Debian/Ubuntu packages."""

    stage = Stage.SYSTEM
    requires: tuple[str, ...] = ()

    def __init__(self, packages: Sequence[str] = (), *, name: str = "apt") -> None:
        cleaned = tuple(sorted({p.strip() for p in packages if p.strip()}))
        if not cleaned:
            raise ModuleError("apt module needs at least one package")
        self.name = name
        self.packages = cleaned

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment(f"apt packages: {', '.join(self.packages)}"),
            Run(
                [
                    "apt-get update",
                    "apt-get install -y --no-install-recommends "
                    + " ".join(self.packages),
                    "rm -rf /var/lib/apt/lists/*",
                ],
                mounts=_APT_CACHE,
            ),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"packages": list(self.packages)}


class NodeToolchain:
    """Ensure a Node.js toolchain is present.

    Most agent CLIs ship as npm packages, so this is a prerequisite for
    :class:`AgentCli`. When the base image already carries Node (``node:*``),
    set ``preinstalled=True`` and this module only records the fact.
    """

    stage = Stage.TOOLCHAIN
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        version: str = "22",
        preinstalled: bool = False,
        name: str = "node",
    ) -> None:
        self.name = name
        self.version = version
        self.preinstalled = preinstalled

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        if self.preinstalled:
            return [Comment(f"node {self.version} provided by the base image")]
        return [
            Comment(f"node {self.version} via NodeSource"),
            Run(
                [
                    "curl -fsSL "
                    f"https://deb.nodesource.com/setup_{self.version}.x | bash -",
                    "apt-get install -y --no-install-recommends nodejs",
                    "rm -rf /var/lib/apt/lists/*",
                    "node --version",
                    "npm --version",
                ],
                mounts=_APT_CACHE,
            ),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"version": self.version, "preinstalled": self.preinstalled}


class PythonToolchain:
    """A Python venv plus the uv installer.

    The venv is placed on PATH so agent tooling that shells out to ``python``
    or ``pip`` gets an isolated, writable environment rather than the system
    interpreter.
    """

    stage = Stage.TOOLCHAIN
    requires: tuple[str, ...] = ()

    def __init__(self, *, venv: str = "/opt/venv", name: str = "python") -> None:
        self.name = name
        self.venv = venv

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment("python venv + uv"),
            # Self-contained: slim base images carry no python3, and a module
            # that silently depends on another module having listed the right
            # apt package is a module that breaks on someone else's base image.
            Run(
                [
                    "apt-get update",
                    "apt-get install -y --no-install-recommends "
                    "python3 python3-venv ca-certificates curl",
                    "rm -rf /var/lib/apt/lists/*",
                ],
                mounts=_APT_CACHE,
            ),
            Run(
                [
                    "curl -LsSf https://astral.sh/uv/install.sh | sh",
                    "mv /root/.local/bin/uv /usr/local/bin/uv",
                    "mv /root/.local/bin/uvx /usr/local/bin/uvx",
                    f"python3 -m venv {self.venv}",
                    f"{self.venv}/bin/python -m pip install --upgrade pip",
                ],
                mounts=_UV_CACHE,
            ),
            Env({"VIRTUAL_ENV": self.venv, "PATH": f"{self.venv}/bin:$PATH"}),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"venv": self.venv}


class AgentCli:
    """Install an agent harness CLI from npm.

    Versions are pinned by default. An unpinned ``@latest`` would make the image
    hash lie: the same digest would refer to different software depending on
    when it was built.
    """

    stage = Stage.HARNESS

    KNOWN: Mapping[str, str] = {
        "claude-code": "@anthropic-ai/claude-code",
        "codex": "@openai/codex",
        "gemini": "@google/gemini-cli",
        "opencode": "opencode-ai",
    }

    def __init__(
        self,
        harness: str,
        *,
        version: str = "latest",
        package: str | None = None,
        name: str | None = None,
        requires: Sequence[str] = ("node",),
    ) -> None:
        resolved = package or self.KNOWN.get(harness)
        if resolved is None:
            known = ", ".join(sorted(self.KNOWN))
            raise ModuleError(
                f"unknown harness {harness!r}; known: {known}. "
                "Pass package=... to install something else."
            )
        self.name = name or f"agent-{harness}"
        self.harness = harness
        self.package = resolved
        self.version = version
        self.requires = tuple(requires)

    @property
    def spec(self) -> str:
        return f"{self.package}@{self.version}"

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment(f"agent harness: {self.harness} ({self.spec})"),
            Run(f"npm install -g {self.spec}", mounts=_NPM_CACHE),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"harness": self.harness, "package": self.package, "version": self.version}


class Langfuse:
    """Route agent LLM traffic to Langfuse over OpenTelemetry.

    This is the module that motivates the whole design. In Teich the equivalent
    is an ``ARG TEICH_INSTALL_LANGFUSE=0`` threaded through ``if`` branches in
    two separate RUN layers, which is why a second integration would have to
    touch both of them. Here it is one class that nothing else knows about.

    Only the OTEL environment is baked in; credentials are injected at run time,
    never into a layer.
    """

    stage = Stage.INTEGRATION

    def __init__(
        self,
        *,
        host: str = "https://cloud.langfuse.com",
        version: str = ">=3,<4",
        name: str = "langfuse",
        requires: Sequence[str] = ("python",),
    ) -> None:
        self.name = name
        self.host = host.rstrip("/")
        self.version = version
        self.requires = tuple(requires)

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment("langfuse tracing over OTLP"),
            Run(
                f'pip install "langfuse{self.version}" '
                '"opentelemetry-sdk" "opentelemetry-exporter-otlp"',
                mounts=_PIP_CACHE,
            ),
            # Endpoint only. LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are
            # supplied per run so they never land in an image layer or in
            # `docker history`.
            Env(
                {
                    "LANGFUSE_HOST": self.host,
                    "OTEL_EXPORTER_OTLP_ENDPOINT": f"{self.host}/api/public/otel",
                    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                }
            ),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"host": self.host, "version": self.version}


class Script:
    """Bake a caller-supplied script into the image and run it.

    The escape hatch that keeps the module set from having to anticipate
    everything. The script text is part of the image hash, so editing it
    rebuilds.
    """

    stage = Stage.USER
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        content: str,
        *,
        name: str = "script",
        filename: str | None = None,
        # /bin/sh, not /bin/bash: alpine and other slim bases have no bash, and
        # a module that only works on Debian derivatives is not a general one.
        shell: str = "/bin/sh",
    ) -> None:
        if not content.strip():
            raise ModuleError("script module needs non-empty content")
        self.name = name
        self.content = content if content.endswith("\n") else content + "\n"
        self.filename = filename or f"{name}.sh"
        self.shell = shell

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        path = context.add_file(self.filename, self.content, mode=0o755)
        target = f"/opt/jormungandr/{self.filename}"
        return [
            Comment(f"user script: {self.name}"),
            Copy(path, target),
            Run(f"{self.shell} {target}"),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"filename": self.filename, "shell": self.shell, "content": self.content}


class Workspace:
    """Create the working directory and the unprivileged user agents run as.

    Runs last so it owns the final USER and WORKDIR regardless of what earlier
    modules set. Teich works around a fixed container uid by chmod-ing whole
    trees to 0777 and writing credential files 0666; the fix is to run as the
    caller's uid instead, which the builder wires up at run time.
    """

    stage = Stage.USER
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        path: str = "/workspace",
        user: str = "agent",
        uid: int = 1000,
        name: str = "workspace",
    ) -> None:
        self.name = name
        self.path = path
        self.user = user
        self.uid = uid

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment(f"workspace {self.path} owned by {self.user}"),
            # Node base images already ship a uid-1000 'node' user, so a plain
            # `useradd --uid 1000` fails there. `|| true` would swallow that and
            # leave a Dockerfile whose USER instruction then fails. Create the
            # user only if absent, and allow a shared uid so the container uid
            # can still match the host's.
            Run(
                [
                    f"if ! id -u {self.user} >/dev/null 2>&1; then "
                    f"useradd --create-home --non-unique --uid {self.uid} "
                    f"--shell /bin/bash {self.user}; fi",
                    f"mkdir -p {self.path}",
                    f"chown -R {self.uid} {self.path}",
                ]
            ),
            Workdir(self.path),
            User(self.user),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"path": self.path, "user": self.user, "uid": self.uid}


def register_builtins(registry=REGISTRY) -> None:
    """Register the built-in modules. Idempotent."""
    factories = {
        "apt": AptPackages,
        "node": NodeToolchain,
        "python": PythonToolchain,
        "agent": AgentCli,
        "langfuse": Langfuse,
        "script": Script,
        "workspace": Workspace,
    }
    for name, factory in factories.items():
        registry.register(name, factory, replace=True)


register_builtins()
