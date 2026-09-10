"""Tests for getting the target repository onto disk.

The property worth pinning is that no credential is ever written into the
clone: the token is served by the helper, not embedded in the remote URL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_proc import FakeRunner

from pi_agent.container import repo

REPO = "example/agent-demo"


# ------------------------------------------------------------------ clone url


def test_the_clone_url_carries_no_credential() -> None:
    """A token in the URL would land in .git/config and outlive the run."""
    url = repo.clone_url(REPO)

    assert url == "https://github.com/example/agent-demo.git"
    assert "@" not in url
    assert "x-access-token" not in url


# ------------------------------------------------------------ clone / refresh


def test_a_missing_clone_is_cloned(tmp_path: Path) -> None:
    runner = FakeRunner()

    repo.clone_or_refresh(REPO, tmp_path / "absent", run=runner)

    assert runner.calls == [
        ("git", "clone", "--quiet", repo.clone_url(REPO), str(tmp_path / "absent"))
    ]


@pytest.mark.parametrize("branch", ["main", "master"])
def test_an_existing_clone_is_reset_to_the_repositorys_default_branch(
    tmp_path: Path, branch: str
) -> None:
    """Reused clones must start pristine, or a previous run's branch leaks in."""
    clone = tmp_path / "agent-demo"
    (clone / ".git").mkdir(parents=True)
    runner = FakeRunner([("defaultBranchRef", branch)])

    repo.clone_or_refresh(REPO, clone, run=runner)

    git_calls = runner.matching("git -C")
    assert [call[3] for call in git_calls] == ["remote", "fetch", "checkout", "reset"]
    assert branch in git_calls[-2]
    assert git_calls[-1][-1] == f"origin/{branch}"
    if branch == "master":
        assert not any("origin/main" in call for call in git_calls)


def test_refresh_repoints_the_remote_before_fetching() -> None:
    """Order matters: fetching first would use whatever the old remote was."""
    commands = repo.refresh_commands(REPO, Path("/work/r"), "trunk")

    assert commands[0][3:5] == ["remote", "set-url"]
    assert commands[1][3] == "fetch"


def test_a_fresh_clone_needs_no_default_branch_api_lookup(tmp_path: Path) -> None:
    runner = FakeRunner()

    repo.clone_or_refresh(REPO, tmp_path / "absent", run=runner)

    assert runner.matching("defaultBranchRef") == []


@pytest.mark.parametrize(
    "runner",
    [FakeRunner(failures=["defaultBranchRef"]), FakeRunner(), FakeRunner(default="null")],
)
def test_an_unresolvable_default_branch_is_a_named_failure(
    tmp_path: Path, runner: FakeRunner
) -> None:
    clone = tmp_path / "agent-demo"
    (clone / ".git").mkdir(parents=True)

    with pytest.raises(repo.UnknownDefaultBranch, match="default branch"):
        repo.clone_or_refresh(REPO, clone, run=runner)


def test_a_failed_refresh_is_a_named_repository_failure(tmp_path: Path) -> None:
    clone = tmp_path / "agent-demo"
    (clone / ".git").mkdir(parents=True)
    runner = FakeRunner([("defaultBranchRef", "main")], failures=["fetch --prune"])

    with pytest.raises(repo.RepositoryUnavailable, match="could not prepare"):
        repo.clone_or_refresh(REPO, clone, run=runner)


# ------------------------------------------------------------- verify access


def test_verify_access_accepts_a_readable_repository() -> None:
    repo.verify_access(REPO, run=FakeRunner([("repo view", "ADMIN")]))


def test_verify_access_stops_the_run_when_the_credential_cannot_read() -> None:
    """Better here than after a model has spent several minutes discovering it."""
    with pytest.raises(SystemExit):
        repo.verify_access(REPO, run=FakeRunner(failures=["repo view"]))
