"""A thin, typed wrapper over the ``docker`` CLI.

Why the CLI rather than docker-py:

* docker-py drives the legacy ``/build`` endpoint, which Docker has formally
  deprecated ("the legacy builder is deprecated and will be removed in a future
  release"). It cannot reach BuildKit, so ``RUN --mount=type=cache`` — the
  single largest build-speed win — is unavailable. SWE-bench re-downloads every
  package on every environment rebuild for exactly this reason.
* ``docker exec`` returns the process exit status as the subprocess return code,
  and gives separate stdout/stderr pipes. Through the API those require a
  follow-up ``exec_inspect`` and manual stream demultiplexing, which SWE-bench
  gets wrong (it discards the exit code and merges the streams).
* No API-version negotiation, and no third-party runtime dependency: the docker
  CLI is already required.

The cost is parsing ``docker inspect`` JSON instead of getting typed objects,
which is contained here and unit-tested.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CommandResult",
    "DockerError",
    "DockerNotAvailable",
    "DockerCli",
]


class DockerError(RuntimeError):
    """A docker command failed."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr.strip()
        rendered = " ".join(argv[:4])
        super().__init__(f"`{rendered} ...` failed ({returncode}): {self.stderr}")


class DockerNotAvailable(DockerError):
    """The docker CLI or daemon is unreachable."""

    def __init__(self, detail: str) -> None:
        RuntimeError.__init__(self, f"docker is not available: {detail}")
        self.argv = ()
        self.returncode = -1
        self.stderr = detail


class _CappedSink:
    """Consumes a pipe, keeping at most ``cap`` characters.

    Reading continues past the cap so the writer never blocks on a full pipe;
    the excess is counted and discarded rather than stored.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._chunks: list[str] = []
        self._kept = 0
        self.truncated = False

    def drain(self, pipe) -> None:
        if pipe is None:
            return
        with contextlib.suppress(Exception):
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                room = self.cap - self._kept
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._kept += min(room, len(chunk))
                if len(chunk) > room:
                    self.truncated = True

    @property
    def text(self) -> str:
        return "".join(self._chunks)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command run inside a container."""

    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class DockerCli:
    """Runs ``docker`` subcommands.

    Every method takes explicit arguments and returns parsed results; nothing
    here knows about images, specs, or modules.
    """

    def __init__(
        self,
        *,
        executable: str = "docker",
        default_timeout: float | None = 600.0,
    ) -> None:
        self.executable = executable
        self.default_timeout = default_timeout

    # -- plumbing ---------------------------------------------------------

    def _argv(self, args: Sequence[str]) -> list[str]:
        return [self.executable, *args]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = self._argv(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.default_timeout,
                input=stdin,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerNotAvailable(f"{self.executable!r} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(argv, -1, f"timed out after {exc.timeout}s") from exc
        if check and proc.returncode != 0:
            raise DockerError(argv, proc.returncode, proc.stderr)
        return proc

    def stream(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Iterator[str]:
        """Run a command, yielding merged output lines as they arrive.

        Used for builds, where progress must be visible rather than buffered
        until the end. Raises :class:`DockerError` if the command fails, with
        the tail of the output as context.
        """
        argv = self._argv(args)
        merged_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
            )
        except FileNotFoundError as exc:
            raise DockerNotAvailable(f"{self.executable!r} not found on PATH") from exc

        tail: list[str] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\n")
                tail.append(stripped)
                del tail[:-40]
                yield stripped
        finally:
            with contextlib.suppress(Exception):
                proc.stdout.close()
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Reached when the caller abandons the generator (a break, or an
                # on_output callback raising) and the child neither notices the
                # closed pipe nor exits. A bare wait() would block here for as
                # long as the build runs.
                self._terminate_group(proc)
                returncode = proc.poll() if proc.poll() is not None else -1
        if returncode != 0:
            raise DockerError(argv, returncode, "\n".join(tail))

    # -- daemon -----------------------------------------------------------

    def available(self) -> bool:
        if shutil.which(self.executable) is None:
            return False
        try:
            self.run(["version", "--format", "{{.Server.Version}}"], timeout=20)
        except DockerError:
            return False
        return True

    def require(self) -> None:
        if not self.available():
            raise DockerNotAvailable(
                "could not reach the Docker daemon; is Docker running?"
            )

    # -- images -----------------------------------------------------------

    def image_exists(self, reference: str) -> bool:
        proc = self.run(["image", "inspect", reference], check=False, timeout=60)
        return proc.returncode == 0

    def inspect(self, reference: str) -> dict[str, Any]:
        proc = self.run(["inspect", reference], timeout=60)
        payload = json.loads(proc.stdout)
        if not payload:
            raise DockerError(("inspect", reference), 1, "empty inspect response")
        return payload[0]

    def image_digest(self, reference: str) -> str:
        """The image's content-addressable ID.

        Downstream operations reference this rather than the tag: a mutable tag
        can be reassigned by a concurrent build or an external ``docker tag``,
        silently changing what actually runs. SWE-bench never captures an image
        ID and re-resolves the tag every time.
        """
        return str(self.inspect(reference).get("Id", ""))

    def image_label(self, reference: str, label: str) -> str:
        """One label off an image, or "" if absent or the image is gone."""
        # json.dumps, not repr: Go templates need double quotes, and %r
        # produces single ones — which silently yields an empty result rather
        # than an error.
        quoted = json.dumps(label)
        proc = self.run(
            [
                "image",
                "inspect",
                "--format",
                f"{{{{index .Config.Labels {quoted}}}}}",
                reference,
            ],
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return ""
        value = proc.stdout.strip()
        return "" if value in {"", "<no value>"} else value

    def list_images(self, *, label: str | None = None) -> list[dict[str, str]]:
        args = ["image", "ls", "--format", "{{json .}}"]
        if label:
            args += ["--filter", f"label={label}"]
        proc = self.run(args, timeout=60)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def remove_image(self, reference: str, *, force: bool = False) -> bool:
        args = ["image", "rm", reference]
        if force:
            args.append("--force")
        return self.run(args, check=False, timeout=120).returncode == 0

    # -- containers -------------------------------------------------------

    def list_containers(
        self, *, label: str | None = None, all_states: bool = True
    ) -> list[dict[str, str]]:
        args = ["container", "ls", "--format", "{{json .}}"]
        if all_states:
            args.append("--all")
        if label:
            args += ["--filter", f"label={label}"]
        proc = self.run(args, timeout=60)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def create(self, args: Sequence[str]) -> str:
        return self.run(["create", *args], timeout=120).stdout.strip()

    def start(self, container: str) -> None:
        self.run(["start", container], timeout=120)

    def stop(self, container: str, *, timeout: int = 10) -> bool:
        return (
            self.run(
                ["stop", "--time", str(timeout), container],
                check=False,
                timeout=timeout + 30,
            ).returncode
            == 0
        )

    def remove_container(self, container: str, *, force: bool = True) -> bool:
        args = ["rm", container]
        if force:
            args.append("--force")
        # Reap anonymous volumes too; SWE-bench never does, so they accumulate.
        args.append("--volumes")
        return self.run(args, check=False, timeout=120).returncode == 0

    def is_running(self, container: str) -> bool:
        proc = self.run(
            ["inspect", "--format", "{{.State.Running}}", container],
            check=False,
            timeout=60,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def logs(self, container: str, *, tail: int | None = None) -> str:
        args = ["logs", container]
        if tail is not None:
            args += ["--tail", str(tail)]
        return self.run(args, check=False, timeout=120).stdout

    def copy_in(self, source: Path | str, container: str, destination: str) -> None:
        # `source` is passed through verbatim: a trailing "/." is meaningful to
        # `docker cp` and is destroyed by Path normalization.
        self.run(["cp", str(source), f"{container}:{destination}"], timeout=300)

    def copy_out(self, container: str, source: str, destination: Path | str) -> None:
        self.run(["cp", f"{container}:{source}", str(destination)], timeout=300)

    def exec(
        self,
        container: str,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        user: str | None = None,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
        max_output: int = 10 * 1024 * 1024,
        stdin: str | None = None,
    ) -> CommandResult:
        """Run a command in a container, with a real timeout and a real exit code.

        Improves on the prior art in four ways:

        * Captures the exit code. SWE-bench's helper discards it entirely.
        * Keeps stdout and stderr separate rather than merging them.
        * On timeout, kills the whole process group, then escalates to SIGKILL.
          SWE-bench sends TERM to the top-level pid only, so children survive.
        * Caps captured output. SWE-bench accumulates into an unbounded bytes
          object, so a chatty process can exhaust host memory.
        """
        args = ["exec"]
        if stdin is not None:
            # Without -i the container's stdin is closed immediately, so a
            # harness reading its prompt from stdin sees EOF and does nothing.
            args.append("-i")
        if user:
            args += ["--user", user]
        if workdir:
            args += ["--workdir", workdir]
        for key, value in sorted((env or {}).items()):
            args += ["--env", f"{key}={value}"]
        args.append(container)
        args.extend(command)

        argv = self._argv(args)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise DockerNotAvailable(f"{self.executable!r} not found on PATH") from exc

        if stdin is not None and proc.stdin is not None:
            # Write and close before waiting: the harness blocks until it sees
            # EOF, and we block until it exits, so leaving the pipe open
            # deadlocks both sides.
            with contextlib.suppress(BrokenPipeError, OSError):
                proc.stdin.write(stdin)
                proc.stdin.close()

        # Drain both pipes in threads, discarding past the cap as we go.
        # proc.communicate() would buffer the entire stream before any
        # truncation, so a process that prints a gigabyte takes the host with
        # it — the exact failure the cap is supposed to prevent.
        out_sink = _CappedSink(max_output)
        err_sink = _CappedSink(max_output)
        readers = [
            threading.Thread(target=out_sink.drain, args=(proc.stdout,), daemon=True),
            threading.Thread(target=err_sink.drain, args=(proc.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_group(proc)
                proc.wait(timeout=10)
        except BaseException:
            # Any other exit from this block — KeyboardInterrupt above all —
            # must not leave `docker exec` running, because the command it is
            # driving keeps running inside the container.
            self._terminate_group(proc)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(Exception):
                        pipe.close()

        duration = time.monotonic() - started
        return CommandResult(
            exit_code=124 if timed_out else (proc.returncode or 0),
            stdout=out_sink.text,
            stderr=err_sink.text,
            duration=duration,
            timed_out=timed_out,
            truncated=out_sink.truncated or err_sink.truncated,
        )

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[str], *, grace: float = 5.0) -> None:
        """TERM the process group, then KILL what survives.

        ``start_new_session=True`` puts the child in its own process group, so
        this reaches grandchildren too — the case a bare ``proc.terminate()``
        misses.
        """
        if proc.returncode is not None:
            return  # already reaped; its pid may since have been recycled
        try:
            group = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(Exception):
                proc.kill()
            return
        if group == os.getpgid(0):
            # The child was not started in its own session, so its group is
            # ours: killing it would take down this process and its whole job.
            with contextlib.suppress(Exception):
                proc.kill()
            return
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
