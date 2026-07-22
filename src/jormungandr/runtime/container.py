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
import weakref
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType

from jormungandr.runtime.docker import CommandResult, DockerCli
from jormungandr.runtime.identity import LABEL_NAMESPACE
from jormungandr.runtime.spec import ContainerSpec

__all__ = ["MANAGED_LABEL", "SESSION_LABEL", "ContainerRuntime", "ContainerSession"]

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
        max_output: int = 10 * 1024 * 1024,
        stdin: str | None = None,
    ) -> CommandResult:
        """Run a command inside the container."""
        return self._docker.exec(
            self.container_id,
            command,
            timeout=timeout,
            user=user,
            workdir=workdir,
            env=env,
            max_output=max_output,
            stdin=stdin,
        )

    def exec_with_stdin(
        self,
        command: Sequence[str],
        *,
        stdin: str | None,
        timeout: float | None = None,
        **kwargs,
    ) -> CommandResult:
        """Run a command, feeding ``stdin`` to it.

        Prompts travel this way rather than on argv, where they would be
        visible in the host process table and recorded in `docker inspect`.
        """
        return self.exec(command, timeout=timeout, stdin=stdin, **kwargs)

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
        # Not coerced through Path: `docker cp src/. dest` means "the contents
        # of src", and Path("/a/b/.") normalizes to "/a/b", which silently
        # turns that into "the directory b, placed inside dest".
        self._docker.copy_in(str(source), self.container_id, destination)

    def copy_out(self, source: str, destination: Path | str) -> None:
        self._docker.copy_out(self.container_id, source, str(destination))

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
        with contextlib.suppress(Exception):
            self._docker.stop(self.container_id, timeout=5)
        with contextlib.suppress(Exception):
            self._docker.remove_container(self.container_id, force=True)
        # Marked done only after the work actually happened. Setting this first
        # means an interrupted removal (KeyboardInterrupt is not an Exception,
        # so it escapes the suppressions above) marks itself complete, and the
        # retry from atexit silently skips a container that is still running.
        self._removed = True


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
        # Reentrant: the signal handler calls shutdown() on the main thread, and
        # a plain Lock would deadlock against it if the signal arrived while
        # that same thread already held the lock inside create()/session()/reap.
        # The window is a few bytecodes wide, but the failure is an unkillable
        # hang whose only escape is SIGKILL — the exact case this class exists
        # to avoid.
        self._lock = threading.RLock()
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
        owner: str | None = None,
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

        args = self._create_args(
            spec, session_id=sid, name=container_name, owner=owner or self.owner
        )
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
            try:
                self.docker.start(container_id)
            except Exception:
                # The container exists but never ran. An untracked one is
                # invisible to shutdown(), and a tracked one is only reaped at
                # exit — either way it lingers, so remove it now and let the
                # failure propagate.
                session.remove()
                with self._lock:
                    self._live.pop(container_id, None)
                raise
        return session

    def release(self, session: ContainerSession) -> None:
        """Stop tracking a container so it survives process exit."""
        with self._lock:
            self._live.pop(session.container_id, None)

    def _create_args(  # noqa: PLR0912 - one branch per optional `docker create` flag; splitting it would only scatter the flag list
        self, spec: ContainerSpec, *, session_id: str, name: str, owner: str
    ) -> list[str]:
        args = ["--name", name, "--label", f"{MANAGED_LABEL}=true"]
        args += ["--label", f"{SESSION_LABEL}={session_id}"]
        args += ["--label", f"{OWNER_LABEL}={owner}"]
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
        """Containers this tool created.

        Filtered on the *session* label, not ``managed``. A container inherits
        its image's labels, so anything a user runs from a jormungandr-built
        image carries ``managed=true`` too and would be swept. The session
        label is written at container-create time and cannot come from an
        image, so it identifies containers we actually started.
        """
        return self.docker.list_containers(label=f"{SESSION_LABEL}")

    def reap_orphans(
        self, *, owner: str | None = None, all_owners: bool = False
    ) -> list[str]:
        """Remove managed containers left behind by dead processes.

        This — not the signal handler — is the real guarantee, because a
        SIGKILLed process runs no handlers at all.

        By default only containers whose owning process is gone are removed.
        Reaping every managed container instead would destroy the live sessions
        of *other* concurrently running processes, which is a far worse outcome
        than leaving a stale container around. ``all_owners=True`` opts into the
        indiscriminate sweep.
        """
        removed: list[str] = []
        with self._lock:
            # `docker container ls` reports 12-char IDs while `docker create`
            # returns the full 64-char one, so these must be compared at a
            # common width. Comparing them directly means the guard never
            # matches and the sweep deletes this process's own live containers.
            live_short = {cid[:12] for cid in self._live}

        for container in self.managed_containers():
            container_id = container.get("ID", "")
            if not container_id or container_id[:12] in live_short:
                continue
            labels = _parse_labels(container.get("Labels", ""))
            container_owner = labels.get(OWNER_LABEL, "")
            if owner is not None and container_owner != owner:
                continue
            if not all_owners and not _owner_is_dead(container_owner):
                continue
            if self.docker.remove_container(container_id, force=True):
                removed.append(container.get("Names", container_id))
        return removed

    def shutdown(self) -> None:
        """Best-effort teardown of everything this process still owns.

        Each session is discarded only after it has actually been removed. The
        obvious alternative — clear the whole set up front, then remove — loses
        every remaining container if the loop is interrupted, and a second
        Ctrl-C does exactly that: the handler re-raises, unwinds out of this
        loop, and the atexit-registered retry then finds an empty set and does
        nothing.
        """
        while True:
            with self._lock:
                if not self._live:
                    return
                container_id, session = next(iter(self._live.items()))
            session.remove()
            with self._lock:
                self._live.pop(container_id, None)

    def _install_handlers(self) -> None:
        if self._handlers_installed:
            return
        self._handlers_installed = True

        # Weak reference: a bound method handed to atexit (or captured in a
        # handler closure) keeps the runtime alive for the life of the process.
        # A library creating runtimes in a loop would otherwise leak every one
        # of them, along with a signal handler each.
        ref = weakref.ref(self)

        def shutdown_if_alive() -> None:
            runtime = ref()
            if runtime is not None:
                runtime.shutdown()

        atexit.register(shutdown_if_alive)

        previous: dict[int, object] = {}

        def handle(signum: int, _frame: FrameType | None) -> None:
            shutdown_if_alive()
            # Restore and re-raise so the caller's own handling, and the shell's
            # view of why we died, both stay correct.
            signal.signal(signum, previous.get(signum, signal.SIG_DFL))
            os.kill(os.getpid(), signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                # Only the main thread may install handlers.
                #
                # `getsignal` reports None — not SIG_DFL — for a handler Python
                # did not install, which is what an embedder or a C extension
                # that called sigaction() before the signal module built its
                # table leaves behind. Storing that None would put a value
                # `signal.signal` rejects into `previous`, and `.get`'s default
                # cannot save us because the key is present: the restore inside
                # `handle` would raise TypeError, `os.kill` would never run, and
                # the process would die of an unhandled exception instead of
                # the signal. Normalise here so `previous` only ever holds
                # something that can be restored.
                current = signal.getsignal(signum)
                previous[signum] = signal.SIG_DFL if current is None else current
                signal.signal(signum, handle)


DETACHED_OWNER = "detached"
"""Owner marker for containers meant to outlive the process that made them.

A detached `container run` is not an orphan, so the default sweep leaves it
alone; `--all` still collects it.
"""


def _hostname() -> str:
    import socket

    with contextlib.suppress(Exception):
        return socket.gethostname()
    return "unknown"


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse the comma-separated ``k=v`` list `docker ls` emits for Labels."""
    labels: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep:
            labels[key.strip()] = value.strip()
    return labels


def _owner_is_dead(owner: str) -> bool:
    """Report whether the process that created a container is gone.

    Only decidable for containers created on this host: a pid from another
    machine says nothing about a pid here, so those are treated as alive
    (i.e. left alone) rather than guessed at.
    """
    if not owner or owner == DETACHED_OWNER:
        return False
    pid_text, _, host = owner.partition("@")
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if host != _hostname() or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False
