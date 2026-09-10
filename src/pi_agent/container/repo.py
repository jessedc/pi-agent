"""Get the target repository onto disk, as the bot.

The clone is made over HTTPS with the credential served by the helper that
`identity` installed, so no token is ever written into `.git/config`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pi_agent import _log, _proc
from pi_agent.container import gh

LOG = _log.Logger("bootstrap")


class RepositoryUnavailable(RuntimeError):
    """The repository could not be prepared for a model run."""


class UnknownDefaultBranch(RepositoryUnavailable):
    """The remote's default branch could not be resolved."""


def clone_url(repository: str) -> str:
    """The HTTPS remote for `owner/repo`, carrying no credential."""
    return f"https://github.com/{repository}.git"


def clone_commands(repository: str, clone_dir: Path) -> list[list[str]]:
    """Commands that create a fresh clone."""
    return [["git", "clone", "--quiet", clone_url(repository), str(clone_dir)]]


def refresh_commands(repository: str, clone_dir: Path, branch: str) -> list[list[str]]:
    """Commands that reset an existing clone to the named remote branch."""
    at = ["git", "-C", str(clone_dir)]
    return [
        [*at, "remote", "set-url", "origin", clone_url(repository)],
        [*at, "fetch", "--prune", "origin"],
        [*at, "checkout", "--force", "-B", branch, f"origin/{branch}"],
        [*at, "reset", "--hard", f"origin/{branch}"],
    ]


def default_branch(
    repository: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> str:
    """Return GitHub's current default branch, without inventing a fallback."""
    try:
        branch = gh.cli(
            [
                "repo",
                "view",
                repository,
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ],
            env=env,
            run=run,
        )
    except _proc.CommandError as error:
        raise UnknownDefaultBranch(
            f"could not resolve the default branch for {repository}: {error}"
        ) from error
    if not branch or branch == "null":
        raise UnknownDefaultBranch(f"GitHub returned no default branch for {repository}")
    return branch


def clone_or_refresh(
    repository: str,
    clone_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> None:
    """Clone the repository, or reset an existing clone to its actual default."""
    if (clone_dir / ".git").is_dir():
        LOG.log(f"reusing existing clone at {clone_dir}")
        branch = default_branch(repository, env=env, run=run)
        LOG.log(f"default branch: {branch}")
        commands = refresh_commands(repository, clone_dir, branch)
    else:
        LOG.log(f"cloning {repository} into {clone_dir}")
        commands = clone_commands(repository, clone_dir)

    try:
        for argv in commands:
            run(argv, env=env, check=True, capture=False)
    except _proc.CommandError as error:
        raise RepositoryUnavailable(f"could not prepare {repository}: {error}") from error


def verify_access(
    repository: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> None:
    """Prove the credential can read the repository, before spending a budget."""
    try:
        gh.cli(
            ["repo", "view", repository, "--json", "viewerPermission", "--jq", ".viewerPermission"],
            env=env,
            run=run,
        )
    except _proc.CommandError:
        LOG.die(f"cannot read {repository} with this credential")
    LOG.log(f"credential verified against {repository}")
