"""Tests for the validation gate.

The gate is the agent's one success condition, so what it consists of and the
order it runs in are worth pinning -- a silently dropped check would let a red
branch look green.
"""

from __future__ import annotations

import pytest
from fake_proc import FakeRunner

from pi_agent.host import checks


def test_all_five_gates_run_in_order() -> None:
    """Cheapest first: a formatting slip should not wait on pyright."""
    assert [gate.name for gate in checks.GATES] == [
        "ruff format --check",
        "ruff check",
        "pytest",
        "mypy",
        "pyright",
    ]


def test_the_gate_is_read_only_by_default() -> None:
    """`check` must never change the tree it is judging."""
    assert not any("--fix" in gate.argv for gate in checks.GATES)
    assert ("uv", "run", "ruff", "format", ".") not in [gate.argv for gate in checks.GATES]


def test_fix_applies_format_before_lint() -> None:
    """Lint fixes can be undone by a later reformat, not the other way round."""
    assert [gate.name for gate in checks.FIXES] == ["ruff format", "ruff check --fix"]


def test_check_runs_every_gate() -> None:
    runner = FakeRunner()

    checks.check([], run=runner)

    assert len(runner.calls) == len(checks.GATES)


def test_fix_first_applies_then_verifies() -> None:
    runner = FakeRunner()

    checks.check(["--fix"], run=runner)

    assert len(runner.calls) == len(checks.FIXES) + len(checks.GATES)
    assert "--fix" in runner.calls[1]


def test_the_docker_gate_is_opt_in() -> None:
    """It needs a daemon and takes minutes, so it is not part of the default gate."""
    runner = FakeRunner()

    checks.check(["--docker"], run=runner)

    assert runner.calls[-1][0] == "docker"
    assert "--pull" in runner.calls[-1]
    assert checks.DOCKER_GATE not in checks.GATES


def test_a_failing_gate_stops_the_run() -> None:
    """Later checks would only report noise caused by the first failure."""
    runner = FakeRunner(failures=["pytest"])

    with pytest.raises(SystemExit):
        checks.check([], run=runner)

    assert not runner.matching("mypy")
    assert not runner.matching("pyright")


def test_the_project_root_is_where_the_gate_runs() -> None:
    """`cd "$(dirname "$0")/.."` in bash; here it is derived from the package."""
    assert (checks.project_root() / "pyproject.toml").is_file()
