"""Validate the one issue a run targets.

A run targets exactly one issue, and that issue is always named as an input.
Validation happens here rather than in the prompt because it is deterministic,
it is logged, and a bad issue number fails in a second rather than after a model
has spent several minutes discovering it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pi_agent import _log, _proc
from pi_agent.container import gh

LOG = _log.Logger("bootstrap")


class InvalidIssue(ValueError):
    """The issue is not a number, does not exist, or is not open."""


@dataclass(frozen=True, slots=True)
class Issue:
    """An open issue this run may work on."""

    number: str
    title: str


def normalise(raw: str) -> str:
    """Return a bare issue number from `12`, `#12` or a padded variant."""
    issue = raw.strip().lstrip("#")
    if not issue:
        raise InvalidIssue("no issue given -- pass an issue number as the first argument")
    if not issue.isdigit():
        raise InvalidIssue(f"issue must be a number, got: {raw.strip()}")
    return issue


def fetch(
    repository: str,
    raw: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> Issue:
    """Confirm the issue exists, is readable with this credential, and is open."""
    number = normalise(raw)

    def field(name: str) -> str:
        return gh.cli(
            ["issue", "view", number, "--repo", repository, "--json", name, "--jq", f".{name}"],
            env=env,
            run=run,
        )

    try:
        state = field("state")
    except _proc.CommandError as error:
        raise InvalidIssue(
            f"issue #{number} not found in {repository}, "
            f"or the credential cannot read issues: {error}"
        ) from error

    if state != "OPEN":
        raise InvalidIssue(f"issue #{number} is {state}, not OPEN")

    issue = Issue(number=number, title=field("title"))
    LOG.log(f"target: #{issue.number} — {issue.title}")
    return issue
