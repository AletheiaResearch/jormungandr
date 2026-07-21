"""Container lifecycle.

The model is a long-lived container plus repeated ``exec``: ``create`` → ``start``
→ many ``exec`` → ``remove``. That is the right shape for an agent loop, which
runs many commands against shared filesystem state and wants host-side
inspection between steps.

Crash-safety is the part that cannot be bolted on later, so it is here from the
start:

* every container is labelled at creation time;
* orphans from previously crashed runs are swept by label;
* ``atexit`` and SIGINT/SIGTERM handlers reap the live set;
* per-session ``try/finally`` remains the fast path.

SIGKILL still leaks — nothing in userspace can prevent that — which is exactly
why the label sweep, not the signal handler, is the real guarantee. Neither
reference implementation has any of this: SWE-bench leaves every container
alive on SIGKILL and its cleanup script looks up a container name that its
runner never actually creates, while Teich tracks containers only in an
in-process registry with no labels, so a `--filter label=` sweep is not even
possible.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType

from jormungandr.runtime.docker import CommandResult, DockerCli
from jormungandr.runtime.identity import LABEL_NAMESPACE
from jormungandr.runtime.spec import ContainerSpec

__all__ = ["ContainerSession", "ContainerRuntime", "SESSION_LABEL", "MANAGED_LABEL"]

SESSION_LABEL = f"{LABEL_NAMESPACE}.session"
MANAGED_LABEL = f"{LABEL_NAMESPACE}.managed"
OWNER_LABEL = f"{LABEL_NAMESPACE}.owner"


class ContainerSession:
    """A running container you can exec into.

    Prefer the :meth:`ContainerRuntime.session` context manager; construct this
    directly only when the lifetime must outlive a single ``with`` block.
    """

    def __init__(
        self,
        *,
        container_id: str,
        name: str,
        session_id: str,
        spec: ContainerSpec,
        docker: DockerCli,
    ) -> None:
        self.container_id = container_id
        self.name = name
        self.session_id = session_id
        self.spec = spec
        self._docker = docker
        self._removed = False

    def __repr__(self) -> str:
        return f"<ContainerSession {self.name} ({self.container_id[:12]})>"

    @property
    def running(self) -> bool:
        return self._docker.is_running(self.container_id)

    def exec(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        user: str | None = None,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a command inside the container."""
        return self._docker.exec(
            self.container_id,
            command,
            timeout=timeout,
            user=user,
            workdir=workdir,
            env=env,
        )

    def shell(
        self,
        script: str,
        *,
        timeout: float | None = None,
        shell: str = "sh",
        **kwargs,
    ) -> CommandResult:
        """Run a shell snippet.

        Defaults to ``sh`` rather than ``bash`` so this works on slim and
        alpine-based images; pass ``shell="bash"`` when the image has it and the
        snippet needs bashisms.
        """
        return self.exec([shell, "-c", script], timeout=timeout, **kwargs)

    def copy_in(self, source: Path | str, destination: str) -> None:
        self._docker.copy_in(Path(source), self.container_id, destination)

    def copy_out(self, source: str, destination: Path | str) -> None:
        self._docker.copy_out(self.container_id, source, Path(destination))

    def logs(self, *, tail: int | None = None) -> str:
        return self._docker.logs(self.container_id, tail=tail)

    def stop(self, *, timeout: int = 10) -> None:
        self._docker.stop(self.container_id, timeout=timeout)

    def remove(self) -> None:
        """Tear down. Idempotent, and never raises.

        Cleanup that can itself fail is cleanup you cannot put in a ``finally``.
        """
        if self._removed:
            return
        self._removed = True
        with contextlib.suppress(Exception):
            self._docker.stop(self.container_id, timeout=5)
        with contextlib.suppress(Exception):
            self._docker.remove_container(self.container_id, force=True)


class ContainerRuntime:
    """Creates, tracks, and reaps containers."""

    def __init__(
        self,
        *,
        docker: DockerCli | None = None,
        owner: str | None = None,
        install_handlers: bool = True,
    ) -> None:
        self.docker = docker or DockerCli()
        self.owner = owner or f"{os.getpid()}@{_hostname()}"
        self._live: dict[str, ContainerSession] = {}
        self._lock = threading.Lock()
        self._handlers_installed = False
        if install_handlers:
            self._install_handlers()

    # -- creation ---------------------------------------------------------

    def create(
        self,
        spec: ContainerSpec,
        *,
        session_id: str | None = None,
        name: str | None = None,
        start: bool = True,
        track: bool = True,
    ) -> ContainerSession:
        """Create a container.

        ``track=True`` ties the container's lifetime to this process: it is
        reaped by :meth:`shutdown` via the atexit and signal handlers. Pass
        ``track=False`` for a container that must outlive the process — a
        detached ``container run`` would otherwise be deleted the instant the
        CLI exits. Untracked containers still carry the labels, so ``reap``
        can find them later.
        """
        self.docker.require()
        sid = session_id or uuid.uuid4().hex[:12]
        container_name = name or f"jormungandr-{sid}"

        args = self._create_args(spec, session_id=sid, name=container_name)
        container_id = self.docker.create(args)
        session = ContainerSession(
            container_id=container_id,
            name=container_name,
            session_id=sid,
            spec=spec,
            docker=self.docker,
        )
        if track:
            with self._lock:
                self._live[container_id] = session
        if start:
            self.docker.start(container_id)
        return session

    def release(self, session: ContainerSession) -> None:
        """Stop tracking a container so it survives process exit."""
        with self._lock:
            self._live.pop(session.container_id, None)

    def _create_args(
        self, spec: ContainerSpec, *, session_id: str, name: str
    ) -> list[str]:
        args = ["--name", name, "--label", f"{MANAGED_LABEL}=true"]
        args += ["--label", f"{SESSION_LABEL}={session_id}"]
        args += ["--label", f"{OWNER_LABEL}={self.owner}"]
        for key, value in sorted(spec.labels.items()):
            args += ["--label", f"{key}={value}"]

        args += ["--network", spec.network]
        args += spec.limits.docker_args()

        if spec.init:
            # Reap zombies: agent CLIs spawn subprocesses freely, and PID 1
            # without an init leaves them accumulating.
            args.append("--init")
        if spec.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        if spec.cap_drop_all:
            args += ["--cap-drop", "ALL"]
        for capability in spec.cap_add:
            args += ["--cap-add", capability]
        if spec.read_only:
            args.append("--read-only")
        if spec.auto_remove:
            args.append("--rm")
        for mount in spec.tmpfs:
            args += ["--tmpfs", mount]
        for mount in spec.mounts:
            args += ["--volume", mount]
        # Secrets travel by file, never as -e on the command line, where they
        # would be visible in `ps` and preserved in `docker inspect`.
        for env_file in spec.env_files:
            args += ["--env-file", env_file]
        for key, value in sorted(spec.env.items()):
            args += ["--env", f"{key}={value}"]
        if spec.user:
            args += ["--user", spec.user]
        if spec.workdir:
            args += ["--workdir", spec.workdir]
        if spec.entrypoint:
            args += ["--entrypoint", spec.entrypoint[0]]

        args.append(spec.image)
        if spec.entrypoint and len(spec.entrypoint) > 1:
            args.extend(spec.entrypoint[1:])
        args.extend(spec.command)
        return args

    @contextlib.contextmanager
    def session(
        self,
        spec: ContainerSpec,
        *,
        session_id: str | None = None,
        name: str | None = None,
        remove: bool = True,
    ) -> Iterator[ContainerSession]:
        """Create a container, yield it, and guarantee teardown."""
        session = self.create(spec, session_id=session_id, name=name)
        try:
            yield session
        finally:
            if remove:
                session.remove()
            with self._lock:
                self._live.pop(session.container_id, None)

    # -- reaping ----------------------------------------------------------

    def managed_containers(self) -> list[dict[str, str]]:
        """Containers this tool created, found by label rather than by name."""
        return self.docker.list_containers(label=f"{MANAGED_LABEL}=true")

    def reap_orphans(self, *, owner: str | None = None) -> list[str]:
        """Remove managed containers left behind by earlier runs.

        This — not the signal handler — is the real guarantee, because a
        SIGKILLed process runs no handlers at all.
        """
        removed: list[str] = []
        with self._lock:
            live = set(self._live)
        for container in self.managed_containers():
            container_id = container.get("ID", "")
            if not container_id or container_id in live:
                continue
            if owner is not None:
                labels = container.get("Labels", "")
                if f"{OWNER_LABEL}={owner}" not in labels:
                    continue
            if self.docker.remove_container(container_id, force=True):
                removed.append(container.get("Names", container_id))
        return removed

    def shutdown(self) -> None:
        """Best-effort teardown of everything this process still owns."""
        with self._lock:
            sessions = list(self._live.values())
            self._live.clear()
        for session in sessions:
            session.remove()

    def _install_handlers(self) -> None:
        if self._handlers_installed:
            return
        self._handlers_installed = True
        atexit.register(self.shutdown)

        def handle(signum: int, frame: FrameType | None) -> None:
            self.shutdown()
            # Restore and re-raise so the caller's own handling, and the shell's
            # view of why we died, both stay correct.
            signal.signal(signum, previous.get(signum, signal.SIG_DFL))
            os.kill(os.getpid(), signum)

        previous: dict[int, object] = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                # Only the main thread may install handlers.
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handle)


def _hostname() -> str:
    import socket

    with contextlib.suppress(Exception):
        return socket.gethostname()
    return "unknown"
