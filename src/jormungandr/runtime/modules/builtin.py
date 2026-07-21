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

import shlex
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
from jormungandr.runtime.modules.installers import Installer, NpmGlobal
from jormungandr.runtime.modules.registry import REGISTRY

__all__ = [
    "Droid",
    "Harness",
    "OpenCode",
    "AptPackages",
    "Langfuse",
    "NodeToolchain",
    "PythonToolchain",
    "Script",
    "Workspace",
    "register_builtins",
]

_SHELL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+/:@^~="
)


def _safe_token(value: object, *, what: str) -> str:
    """Validate a value that will be interpolated into a shell command.

    ``ModuleDeclaration`` allows extra fields and does not coerce their types,
    so YAML hands module constructors whatever was written — including strings
    where an int is annotated. Nothing downstream quotes these before they reach
    a RUN body, so they are validated at the boundary instead.
    """
    text = str(value)
    if not text:
        raise ModuleError(f"{what} must not be empty")
    bad = sorted(set(text) - _SHELL_SAFE)
    if bad:
        raise ModuleError(
            f"{what} contains characters that are unsafe in a shell command: "
            f"{''.join(bad)!r} (in {text!r})"
        )
    return text


def _quoted_arg(value: object, *, what: str) -> str:
    """Shell-quote a value that is a command *argument* rather than an identifier.

    Version specifiers legitimately contain characters an identifier allowlist
    must reject — ``langfuse>=3,<4`` is a normal PEP 440 pin, and ``^1.2.3`` a
    normal npm one. Quoting is the right tool for those: it keeps them intact
    and inert. Newlines are still refused, because quoting cannot save a value
    that ends the instruction it sits in.
    """
    text = str(value)
    if not text:
        raise ModuleError(f"{what} must not be empty")
    if "\n" in text or "\r" in text:
        raise ModuleError(f"{what} must not contain a newline: {text!r}")
    return shlex.quote(text)


def _safe_uid(value: object) -> int:
    try:
        uid = int(value)
    except (TypeError, ValueError):
        raise ModuleError(f"uid must be an integer, got {value!r}") from None
    if not 0 <= uid <= 2**31 - 1:
        raise ModuleError(f"uid out of range: {uid}")
    return uid


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
        cleaned = tuple(sorted({p.strip() for p in packages if str(p).strip()}))
        if not cleaned:
            raise ModuleError("apt module needs at least one package")
        cleaned = tuple(_safe_token(p, what="apt package") for p in cleaned)
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
        self.version = _safe_token(version, what="node version")
        self.preinstalled = bool(preinstalled)

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
        self.venv = _safe_token(venv, what="venv path")

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


class Harness:
    """An agent harness: an installer, a binary, and proof it works.

    Deliberately does not know *how* its program is installed — that is an
    :class:`~jormungandr.runtime.modules.installers.Installer` strategy.
    Harnesses have no common install mechanism (npm for opencode and pi, a
    piped shell script for droid, a git clone plus a Python venv for hermes),
    so a base class that assumed one would be wrong for the second harness
    added.

    What harnesses *do* share, and what lives here: the HARNESS stage, version
    identity, and a post-install verification step.
    """

    stage = Stage.HARNESS

    def __init__(
        self,
        *,
        name: str,
        installer: Installer,
        binary: str,
        verify: Sequence[str] = ("--version",),
        requires: Sequence[str] | None = None,
    ) -> None:
        self.name = name
        self.installer = installer
        self.binary = _safe_token(binary, what="harness binary")
        self.verify = tuple(str(a) for a in verify)
        self.requires = tuple(
            requires if requires is not None else installer.default_requires
        )

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        body: list[Instruction] = [
            Comment(f"harness: {self.name} ({self.installer.identity().get('kind')})"),
            *self.installer.instructions(context),
        ]
        if self.verify:
            # Fail the build here rather than at run time. Installers lie: npm
            # exits 0 even when no platform-specific optional binary matched,
            # so actually running the program is the only proof it is usable.
            args = " ".join(_quoted_arg(a, what="verify arg") for a in self.verify)
            body.append(Run(f"{self.binary} {args}"))
        return body

    def identity(self) -> Mapping[str, object]:
        return {
            "binary": self.binary,
            "verify": list(self.verify),
            "installer": dict(self.installer.identity()),
        }


class OpenCode(Harness):
    """The OpenCode harness.

    OpenCode ships as an npm package whose real payload is a set of
    platform-specific prebuilt binaries published as optional dependencies
    (``opencode-linux-arm64``, ``opencode-linux-x64``, plus musl and baseline
    variants). npm resolves the right one for the platform it installs on, so
    the ordinary global install works on both glibc and musl bases — verified
    against node:22-bookworm-slim and node:22-alpine.

    The version is pinned by default. An unpinned ``@latest`` would make the
    image digest lie: the same digest would refer to different software
    depending on when it happened to be built.
    """

    PACKAGE = "opencode-ai"
    BINARY = "opencode"
    DEFAULT_VERSION = "1.18.4"

    def __init__(
        self,
        *,
        version: str = DEFAULT_VERSION,
        package: str | None = None,
        name: str = "opencode",
        requires: Sequence[str] = ("node",),
    ) -> None:
        super().__init__(
            name=name,
            installer=NpmGlobal(package or self.PACKAGE, version),
            binary=self.BINARY,
            requires=requires,
        )


class Droid(Harness):
    """Factory's droid CLI.

    Ships as an npm package with a platform-detecting install script as the
    alternative; npm is used here for the same reason as OpenCode — it pins
    cleanly and needs no network fetch of an unversioned shell script.

    Auto-update is switched off. A harness that updates itself inside a running
    container silently invalidates the promise its image digest makes: two
    containers from the same digest would run different software. Pinning the
    version in the image and disabling self-update is the only way the digest
    stays meaningful.

    Non-interactive use is ``droid exec``, which accepts a prompt on stdin.
    Credentials come from ``FACTORY_API_KEY`` at run time and are never baked.

    **Airgap and BYOK.** droid can talk to an arbitrary OpenAI- or
    Anthropic-compatible endpoint via ``customModels`` in
    ``~/.factory/settings.json``, which avoids paying Factory for inference.
    That alone is not enough to run without a Factory account: ``droid exec``
    still opens a cloud session first and dies with
    ``401 Missing authorization token`` before it ever contacts the custom
    endpoint. ``FACTORY_AIRGAP_ENABLED=true`` skips that call — verified by
    running ``droid exec`` against a local stub with no credentials at all and
    watching the request arrive.

    Two further quirks worth knowing, both verified:

    * ``droid exec --model custom:<id>`` is rejected ("Invalid model"); the
      custom model must be named in ``sessionDefaultSettings.model`` instead.
      See Factory-AI/factory#787.
    * The endpoint is called as a streaming ``POST /v1/chat/completions`` with
      the tool list attached, so a BYOK proxy must speak SSE, not just
      request/response.
    """

    PACKAGE = "droid"
    BINARY = "droid"
    DEFAULT_VERSION = "0.176.0"

    STATE_DIR = "~/.factory"
    """Sessions, logs and caches land here, so HOME must be correct."""

    def __init__(
        self,
        *,
        version: str = DEFAULT_VERSION,
        package: str | None = None,
        name: str = "droid",
        auto_update: bool = False,
        airgap: bool = False,
        requires: Sequence[str] = ("node",),
    ) -> None:
        super().__init__(
            name=name,
            installer=NpmGlobal(package or self.PACKAGE, version),
            binary=self.BINARY,
            requires=requires,
        )
        self.auto_update = bool(auto_update)
        self.airgap = bool(airgap)

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        body = list(super().instructions(context))
        env: dict[str, str] = {}
        if not self.auto_update:
            env["FACTORY_DROID_AUTO_UPDATE_ENABLED"] = "false"
        if self.airgap:
            env["FACTORY_AIRGAP_ENABLED"] = "true"
        if env:
            body.append(Env(env))
        return body

    def identity(self) -> Mapping[str, object]:
        return {
            **super().identity(),
            "auto_update": self.auto_update,
            "airgap": self.airgap,
        }


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
        self.host = _safe_token(host.rstrip("/"), what="langfuse host")
        self.version = str(version)
        _quoted_arg(self.version, what="langfuse version")
        self.requires = tuple(requires)

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment("langfuse tracing over OTLP"),
            Run(
                "pip install "
                f'{_quoted_arg("langfuse" + self.version, what="langfuse pin")} '
                "'opentelemetry-sdk' 'opentelemetry-exporter-otlp'",
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
        self.filename = _safe_token(filename or f"{name}.sh", what="script filename")
        self.shell = _safe_token(shell, what="script shell")

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
        self.path = _safe_token(path, what="workspace path")
        self.user = _safe_token(user, what="workspace user")
        self.uid = _safe_uid(uid)

    @property
    def home(self) -> str:
        return f"/home/{self.user}"

    def instructions(self, context: BuildContext) -> Sequence[Instruction]:
        return [
            Comment(f"workspace {self.path} owned by {self.user} (HOME={self.home})"),
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
                    f"mkdir -p {self.path} {self.home}",
                    f"chown -R {self.uid} {self.path} {self.home}",
                ]
            ),
            Workdir(self.path),
            User(self.user),
            # HOME must be explicit. Docker derives it from /etc/passwd, which
            # resolves a uid to the *first* matching name — and --non-unique
            # means two names share this uid, so `USER agent` on a node base
            # image otherwise yields HOME=/home/node. Harnesses keep their
            # state and credentials under HOME (~/.factory, ~/.local/share/
            # opencode), so a wrong HOME silently sends them somewhere the
            # image never prepared.
            Env({"HOME": self.home}),
        ]

    def identity(self) -> Mapping[str, object]:
        return {"path": self.path, "user": self.user, "uid": self.uid, "home": self.home}


def register_builtins(registry=REGISTRY) -> None:
    """Register the built-in modules. Idempotent."""
    factories = {
        "apt": AptPackages,
        "node": NodeToolchain,
        "python": PythonToolchain,
        "droid": Droid,
        "opencode": OpenCode,
        "langfuse": Langfuse,
        "script": Script,
        "workspace": Workspace,
    }
    for name, factory in factories.items():
        registry.register(name, factory, replace=True)


register_builtins()
