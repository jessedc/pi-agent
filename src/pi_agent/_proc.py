"""The subprocess seam.

Almost everything this package does is a call to `git`, `gh`, `docker`,
`tailscale` or `pi`. Routing all of them through one injectable `Runner` is what
makes the logic testable: a test asserts on the argv that *would* have run,
without a daemon, a credential, or a network.

`run` is the real implementation and the default everywhere. Tests pass their
own callable with the same signature.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

# The status a child gets when it is stopped for exceeding its deadline: the
# one coreutils' `timeout` uses, so it reads the same way in a transcript.
TIMED_OUT = 124

# How long a child that has been told to stop gets to flush before it is
# killed. pi writes its session file on SIGTERM; this is generous for that.
STOP_GRACE_SECONDS = 30.0


class CommandError(RuntimeError):
    """A command exited non-zero and the caller asked to be told."""

    def __init__(self, result: Result) -> None:
        self.result = result
        detail = result.stderr.strip() or f"exited {result.returncode}"
        super().__init__(f"{result.command}: {detail}")


@dataclass(frozen=True, slots=True)
class Result:
    """What one command did."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        """The command name, for error messages."""
        return self.argv[0] if self.argv else "<empty>"

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    """The call signature every module that shells out depends on."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> Result: ...


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> Result:
    """Run a command and return what it did.

    `capture=False` lets the child write straight to this process's streams,
    which is what the interesting long-running children (`pi`, `git clone`)
    want -- their output is the user's view of the run.
    """
    completed = subprocess.run(  # noqa: S603
        list(argv),
        env=dict(env) if env is not None else None,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=False,
    )
    result = Result(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "" if capture else "",
        stderr=completed.stderr or "" if capture else "",
    )
    if check and not result.ok:
        raise CommandError(result)
    return result


class Process(Protocol):
    """The call signature for running the one long-lived child, `pi`."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> int: ...


def run_forwarding_signals(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    grace: float = STOP_GRACE_SECONDS,
) -> int:
    """Run a child, forwarding SIGTERM/SIGINT to it, and return its status.

    The container entrypoint is PID 1, and PID 1 does not get the default signal
    dispositions: without an explicit handler, SIGTERM is ignored. `docker stop`
    would then wait out its grace period and SIGKILL the whole tree, killing
    `pi` mid-write and losing the session transcript -- the one artefact a
    finished run is supposed to leave behind.

    Forwarding instead lets `pi` flush and exit, and lets the post-run reporting
    still name the transcript it wrote.

    `timeout` is the child's deadline in seconds. A model that loops, or hangs
    on a call the egress rules reject slowly, would otherwise run until a human
    noticed. On expiry the child is told to stop, given `grace` to flush, then
    killed, and the status is `TIMED_OUT` -- so the caller still reconciles the
    remote and reports the transcript, which a kill from outside would skip.
    """
    process = subprocess.Popen(list(argv), cwd=cwd)  # noqa: S603

    def forward(signum: int, _frame: FrameType | None) -> None:
        process.send_signal(signum)

    previous = [(sig, signal.signal(sig, forward)) for sig in (signal.SIGTERM, signal.SIGINT)]
    try:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return TIMED_OUT
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)
