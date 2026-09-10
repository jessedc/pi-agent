"""Tests for resolving the model host on the tailnet.

"tailscale is not installed", "tailscale is down" and "no such machine" are
distinct failures, and each is reachable from a test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from fake_proc import FakeRunner

from pi_agent import _proc
from pi_agent.host import tailnet

HOST = "model-box.example.ts.net"


def test_the_short_machine_name_is_used() -> None:
    """tailscale knows `model-box`, not the full MagicDNS name."""
    runner = FakeRunner(default="100.64.0.1")

    tailnet.resolve(HOST, run=runner)

    assert runner.calls == [("tailscale", "ip", "-4", "model-box")]


def test_the_address_is_returned() -> None:
    assert tailnet.resolve(HOST, run=FakeRunner(default="100.64.0.1\n")) == "100.64.0.1"


def test_only_the_first_address_is_used() -> None:
    """tailscale can print more than one line."""
    runner = FakeRunner(default="100.64.0.1\nfd7a::1\n")

    assert tailnet.resolve(HOST, run=runner) == "100.64.0.1"


def test_an_override_skips_tailscale_entirely() -> None:
    """MODEL_IP=... is how you run this with no tailnet at all."""
    runner = FakeRunner()

    assert tailnet.resolve(HOST, override="10.0.0.1", run=runner) == "10.0.0.1"
    assert runner.calls == []


def test_empty_output_is_reported_as_unresolvable() -> None:
    with pytest.raises(tailnet.Unresolvable, match="is tailscale up"):
        tailnet.resolve(HOST, run=FakeRunner(default=""))


def test_a_failing_tailscale_is_reported_as_unresolvable() -> None:
    with pytest.raises(tailnet.Unresolvable, match="is tailscale up"):
        tailnet.resolve(HOST, run=FakeRunner(failures=["tailscale"]))


def test_a_missing_tailscale_says_so_rather_than_traversing_a_traceback() -> None:
    def absent(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> _proc.Result:
        raise FileNotFoundError(argv[0])

    with pytest.raises(tailnet.Unresolvable, match="not installed"):
        tailnet.resolve(HOST, run=absent)
