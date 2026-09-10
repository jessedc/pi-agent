"""Reconcile the remote truth left by model-issued push and PR commands.

Individual command outcomes are hidden inside ``pi`` and can be ambiguous over
the network. Each run therefore leases a cryptographically unique branch name
that was absent before the model started, then decides cleanup from GitHub's
final branch and pull-request state rather than from the model's report.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pi_agent import _proc
from pi_agent.container import gh


class RemoteStateError(RuntimeError):
    """The run's remote branch or pull-request state could not be verified."""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The remote pull request attached to the run's leased branch."""

    number: int
    url: str
    state: str
    is_draft: bool


@dataclass(frozen=True, slots=True)
class Lease:
    """A unique branch assigned to this run and the heads preceding it."""

    branch: str
    before: frozenset[str]


@dataclass(frozen=True, slots=True)
class Plan:
    """The safe action implied by before/after remote truth."""

    delete: bool
    keep_reason: str | None
    unexpected: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Result:
    """What reconciliation observed and whether cleanup completed."""

    branch: str
    pull_request: PullRequest | None
    deleted: bool
    deletion_error: str | None
    unexpected: tuple[str, ...]

    @property
    def successful(self) -> bool:
        """Whether final truth is the one permitted success state."""
        return (
            self.pull_request is not None
            and self.pull_request.state == "OPEN"
            and self.pull_request.is_draft
        )


def remote_heads(repo: Path, *, run: _proc.Runner = _proc.run) -> frozenset[str]:
    """Return every branch currently advertised by origin."""
    try:
        output = run(["git", "-C", str(repo), "ls-remote", "--heads", "origin"], check=True).stdout
    except _proc.CommandError as error:
        raise RemoteStateError(f"could not inspect remote branches: {error}") from error

    prefix = "refs/heads/"
    heads: set[str] = set()
    for line in output.splitlines():
        _sha, separator, ref = line.partition("\t")
        if separator and ref.startswith(prefix):
            heads.add(ref.removeprefix(prefix))
    return frozenset(heads)


def acquire(
    issue: str,
    repo: Path,
    *,
    entropy: Callable[[int], str] = secrets.token_hex,
    run: _proc.Runner = _proc.run,
) -> Lease:
    """Lease a unique issue branch that did not exist before this run."""
    before = remote_heads(repo, run=run)
    for _attempt in range(16):
        branch = f"issue-{issue}-run-{entropy(6)}"
        if branch not in before:
            return Lease(branch=branch, before=before)
    raise RemoteStateError("could not allocate a unique remote branch name")


def pull_request_for(
    repository: str,
    branch: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> PullRequest | None:
    """Return the PR attached to an exact head branch, if one exists."""
    try:
        raw = gh.cli(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--head",
                branch,
                "--json",
                "number,url,state,isDraft",
            ],
            env=env,
            run=run,
        )
        document = json.loads(raw)
    except (_proc.CommandError, json.JSONDecodeError) as error:
        raise RemoteStateError(f"could not inspect pull requests for {branch}: {error}") from error
    if not document:
        return None
    if not isinstance(document, list) or not isinstance(document[0], dict):
        raise RemoteStateError(f"GitHub returned an invalid pull-request result for {branch}")
    try:
        return PullRequest(
            number=int(document[0]["number"]),
            url=str(document[0]["url"]),
            state=str(document[0]["state"]),
            is_draft=bool(document[0]["isDraft"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RemoteStateError(
            f"GitHub returned an invalid pull-request result for {branch}"
        ) from error


def plan(lease: Lease, after: frozenset[str], pull_request: PullRequest | None) -> Plan:
    """Choose cleanup without ever treating an unleased branch as owned."""
    unexpected = tuple(sorted(after - lease.before - {lease.branch}))
    if pull_request is not None:
        return Plan(delete=False, keep_reason=f"has PR {pull_request.url}", unexpected=unexpected)
    if lease.branch in lease.before:
        return Plan(delete=False, keep_reason="existed before this run", unexpected=unexpected)
    if lease.branch in after:
        return Plan(delete=True, keep_reason=None, unexpected=unexpected)
    return Plan(delete=False, keep_reason=None, unexpected=unexpected)


def delete_branch(
    repository: str,
    branch: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> str | None:
    """Delete exactly one leased ref, returning an error instead of hiding it."""
    try:
        gh.cli(
            ["api", "--method", "DELETE", f"repos/{repository}/git/refs/heads/{branch}"],
            env=env,
            run=run,
        )
    except _proc.CommandError as error:
        return str(error)
    return None


def finish(
    repository: str,
    repo: Path,
    lease: Lease,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> Result:
    """Inspect final truth and remove only an orphaned branch leased by this run."""
    after = remote_heads(repo, run=run)
    pull_request = pull_request_for(repository, lease.branch, env=env, run=run)
    action = plan(lease, after, pull_request)
    deleted = False
    deletion_error: str | None = None
    if action.delete:
        deletion_error = delete_branch(repository, lease.branch, env=env, run=run)
        deleted = deletion_error is None

    return Result(
        branch=lease.branch,
        pull_request=pull_request,
        deleted=deleted,
        deletion_error=deletion_error,
        unexpected=action.unexpected,
    )
