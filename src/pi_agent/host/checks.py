"""The validation gate, as data.

Five checks, in order, each of which must pass. Defining them as a list rather
than a run of shell lines means the set is assertable, and that `check` and
`fix` cannot drift apart about what "format" means.

The gate deliberately does not build the image by default -- that takes minutes
and needs a daemon. `--docker` adds it, and is the thing to run before touching
the Dockerfile or the entrypoint, because that is the one change that can leave
the gate green and the image broken.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pi_agent import _log, _proc

LOG = _log.Logger("check")


@dataclass(frozen=True, slots=True)
class Gate:
    """One check, and the command that performs it."""

    name: str
    argv: tuple[str, ...]


GATES: tuple[Gate, ...] = (
    Gate("ruff format --check", ("uv", "run", "ruff", "format", "--check", ".")),
    Gate("ruff check", ("uv", "run", "ruff", "check", ".")),
    Gate("pytest", ("uv", "run", "pytest")),
    Gate("mypy", ("uv", "run", "mypy")),
    Gate("pyright", ("uv", "run", "pyright")),
)

FIXES: tuple[Gate, ...] = (
    Gate("ruff format", ("uv", "run", "ruff", "format", ".")),
    Gate("ruff check --fix", ("uv", "run", "ruff", "check", "--fix", ".")),
)

# --pull for the same reason as docker.build_image: the base image is pinned
# by digest, and a stale local tag must not stand in for it.
DOCKER_GATE = Gate("docker build", ("docker", "build", "--pull", "-t", "pi-issue-to-pr:local", "."))


def project_root() -> Path:
    """The repository root, from this file's location in an editable install."""
    return Path(__file__).resolve().parents[3]


def run_gates(
    gates: Sequence[Gate],
    *,
    cwd: Path | None = None,
    run: _proc.Runner = _proc.run,
) -> int:
    """Run each gate in order, stopping at the first failure."""
    for gate in gates:
        LOG.log(f"→ {gate.name}")
        result = run(list(gate.argv), cwd=cwd, check=False, capture=False)
        if not result.ok:
            LOG.die(f"{gate.name} failed")
    return 0


def check(argv: Sequence[str] | None = None, *, run: _proc.Runner = _proc.run) -> int:
    """Validate: format check + lint + tests + static type analysis."""
    parser = argparse.ArgumentParser(prog="check", description=check.__doc__)
    parser.add_argument("--fix", action="store_true", help="apply safe auto-fixes first")
    parser.add_argument(
        "--docker", action="store_true", help="also build the image (slow; needs a daemon)"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = project_root()
    gates = (*FIXES, *GATES) if args.fix else GATES
    if args.docker:
        gates = (*gates, DOCKER_GATE)

    run_gates(gates, cwd=root, run=run)
    names = ", ".join(gate.name for gate in GATES)
    LOG.log(f"✓ all checks passed ({names})")
    return 0


def fix(argv: Sequence[str] | None = None, *, run: _proc.Runner = _proc.run) -> int:
    """Apply safe auto-fixes: ruff format + ruff check --fix."""
    parser = argparse.ArgumentParser(prog="fix", description=fix.__doc__)
    parser.parse_args(list(argv) if argv is not None else None)

    run_gates(FIXES, cwd=project_root(), run=run)
    LOG.log("Done. Run ./scripts/check.py to verify the full gate is green.")
    return 0
