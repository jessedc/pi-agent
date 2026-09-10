"""Tests for the subprocess seam.

This is the module every other one depends on to be testable, so its own
contract is worth pinning: what a failure raises, what it says, and that the
PID-1 signal forwarding actually reaches the child.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pi_agent import _proc

ECHO_STDERR = [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]


# ------------------------------------------------------------------- run


def test_run_captures_stdout() -> None:
    result = _proc.run([sys.executable, "-c", "print('hello')"])

    assert result.stdout.strip() == "hello"
    assert result.ok


def test_run_records_the_argv_it_ran() -> None:
    """Callers build error messages from this, and tests assert on it."""
    argv = [sys.executable, "-c", "pass"]

    assert _proc.run(argv).argv == tuple(argv)


def test_run_raises_on_failure_by_default() -> None:
    with pytest.raises(_proc.CommandError):
        _proc.run(ECHO_STDERR)


def test_the_error_carries_the_child_stderr() -> None:
    """A `gh` failure is only actionable if GitHub's own message survives."""
    with pytest.raises(_proc.CommandError) as error_info:
        _proc.run(ECHO_STDERR)

    assert "boom" in str(error_info.value)
    assert error_info.value.result.returncode == 3


def test_check_false_returns_the_failure_instead_of_raising() -> None:
    result = _proc.run(ECHO_STDERR, check=False)

    assert not result.ok
    assert result.returncode == 3


def test_run_passes_the_environment_through() -> None:
    result = _proc.run(
        [sys.executable, "-c", "import os; print(os.environ['MARKER'])"],
        env={"MARKER": "seen", "PATH": "/usr/bin:/bin"},
    )

    assert result.stdout.strip() == "seen"


def test_run_honours_cwd(tmp_path: Path) -> None:
    result = _proc.run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


# ------------------------------------------------------ run_forwarding_signals


def test_forwarding_returns_the_child_status() -> None:
    assert _proc.run_forwarding_signals([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_sigterm_reaches_the_child(tmp_path: Path) -> None:
    """The property `docker stop` depends on: pi gets a chance to flush.

    Without forwarding, PID 1 ignores SIGTERM, the daemon waits out its grace
    period and SIGKILLs -- and the session transcript is lost.
    """
    marker = tmp_path / "flushed"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(f"""
            import signal, sys, time
            from pathlib import Path

            def handle(signum, frame):
                Path({str(marker)!r}).write_text("flushed")
                sys.exit(0)

            signal.signal(signal.SIGTERM, handle)
            print("ready", flush=True)
            time.sleep(30)
        """)
    )

    def stop_it_shortly() -> None:
        # The child installs its handler before sleeping; give it a moment,
        # then deliver a SIGTERM here for run_forwarding_signals to pass on.
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stop_it_shortly, daemon=True).start()
    status = _proc.run_forwarding_signals([sys.executable, str(child)])

    assert marker.read_text() == "flushed"
    assert status == 0


def test_a_child_past_its_deadline_is_told_to_stop_and_may_flush(tmp_path: Path) -> None:
    """The deadline path is the `docker stop` path: SIGTERM first, so pi writes
    its transcript, and a status that says why it ended."""
    marker = tmp_path / "flushed"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(f"""
            import signal, sys, time
            from pathlib import Path

            def handle(signum, frame):
                Path({str(marker)!r}).write_text("flushed")
                sys.exit(0)

            signal.signal(signal.SIGTERM, handle)
            time.sleep(30)
        """)
    )

    started = time.monotonic()
    status = _proc.run_forwarding_signals([sys.executable, str(child)], timeout=0.5)

    assert status == _proc.TIMED_OUT
    assert marker.read_text() == "flushed"
    assert time.monotonic() - started < 10


def test_a_child_that_ignores_the_stop_is_killed_after_the_grace() -> None:
    child = [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
    ]

    started = time.monotonic()
    status = _proc.run_forwarding_signals(child, timeout=0.3, grace=0.3)

    assert status == _proc.TIMED_OUT
    assert time.monotonic() - started < 10


def test_no_deadline_means_none() -> None:
    assert _proc.run_forwarding_signals([sys.executable, "-c", "pass"], timeout=None) == 0


def test_forwarding_restores_the_previous_handlers() -> None:
    """A run must not leave the process with handlers pointing at a dead child."""
    before = signal.getsignal(signal.SIGTERM)

    _proc.run_forwarding_signals([sys.executable, "-c", "pass"])

    assert signal.getsignal(signal.SIGTERM) is before
