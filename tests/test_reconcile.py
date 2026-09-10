"""Tests for remote cleanup based on truth rather than model command outcomes.

The leased branch name and before-snapshot are the ownership proof. Every
decision is tested as a value; API tests assert only the exact ref that would be
deleted, without touching a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_proc import FakeRunner

from pi_agent.container import reconcile


def lease(*, before: frozenset[str] = frozenset()) -> reconcile.Lease:
    """A stable branch lease for pure reconciliation tests."""
    return reconcile.Lease(branch="issue-12-run-abcdef", before=before)


def test_a_run_branch_is_unique_and_absent_from_the_initial_snapshot(tmp_path: Path) -> None:
    runner = FakeRunner(default="a\trefs/heads/main\n")

    acquired = reconcile.acquire("12", tmp_path, entropy=lambda _size: "abcdef", run=runner)

    assert acquired == lease(before=frozenset({"main"}))


def test_an_orphaned_branch_leased_by_this_run_is_deletable() -> None:
    action = reconcile.plan(lease(), frozenset({"issue-12-run-abcdef"}), None)

    assert action.delete is True
    assert action.keep_reason is None


def test_a_branch_that_existed_before_the_run_is_never_deleted() -> None:
    existing = lease(before=frozenset({"issue-12-run-abcdef"}))

    action = reconcile.plan(existing, existing.before, None)

    assert action.delete is False
    assert action.keep_reason == "existed before this run"


def test_a_branch_with_a_pull_request_is_kept() -> None:
    pull_request = reconcile.PullRequest(
        number=7, url="https://github.com/o/r/pull/7", state="OPEN", is_draft=True
    )

    action = reconcile.plan(lease(), frozenset({"issue-12-run-abcdef"}), pull_request)

    assert action.delete is False
    assert action.keep_reason == "has PR https://github.com/o/r/pull/7"


@pytest.mark.parametrize(
    ("state", "is_draft"), [("OPEN", False), ("CLOSED", True), ("MERGED", False)]
)
def test_only_an_open_draft_pull_request_is_success(state: str, is_draft: bool) -> None:
    result = reconcile.Result(
        branch="issue-12-run-abcdef",
        pull_request=reconcile.PullRequest(7, "https://github.com/o/r/pull/7", state, is_draft),
        deleted=False,
        deletion_error=None,
        unexpected=(),
    )

    assert result.successful is False


def test_a_branch_created_by_someone_else_is_reported_not_deleted() -> None:
    action = reconcile.plan(lease(), frozenset({"someone-elses-branch"}), None)

    assert action.delete is False
    assert action.unexpected == ("someone-elses-branch",)


def test_the_delete_call_targets_exactly_one_leased_ref() -> None:
    runner = FakeRunner()

    assert reconcile.delete_branch("o/r", "issue-12-run-abcdef", run=runner) is None
    assert runner.calls == [
        (
            "gh",
            "api",
            "--method",
            "DELETE",
            "repos/o/r/git/refs/heads/issue-12-run-abcdef",
        )
    ]


def test_a_delete_failure_is_returned_for_the_final_report() -> None:
    runner = FakeRunner(failures=["DELETE"])

    error = reconcile.delete_branch("o/r", "issue-12-run-abcdef", run=runner)

    assert error is not None
    assert "fake failure" in error


def test_pull_request_lookup_uses_the_exact_leased_head() -> None:
    document = json.dumps(
        [
            {
                "number": 7,
                "url": "https://github.com/o/r/pull/7",
                "state": "OPEN",
                "isDraft": True,
            }
        ]
    )
    runner = FakeRunner([("pr list", document)])

    pull_request = reconcile.pull_request_for("o/r", "issue-12-run-abcdef", run=runner)

    assert pull_request == reconcile.PullRequest(7, "https://github.com/o/r/pull/7", "OPEN", True)
    assert "--head" in runner.calls[0]
    assert "issue-12-run-abcdef" in runner.calls[0]


def test_finish_deletes_a_pushed_branch_when_pr_creation_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "remote_heads",
        lambda *_args, **_kwargs: frozenset({"main", "issue-12-run-abcdef"}),
    )
    monkeypatch.setattr(reconcile, "pull_request_for", lambda *_args, **_kwargs: None)

    def delete(_repository: str, branch: str, **_kwargs: object) -> None:
        deleted.append(branch)

    monkeypatch.setattr(reconcile, "delete_branch", delete)

    result = reconcile.finish("o/r", tmp_path, lease(before=frozenset({"main"})), run=FakeRunner())

    assert result.deleted is True
    assert deleted == ["issue-12-run-abcdef"]


def test_finish_keeps_an_ambiguously_created_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pull_request = reconcile.PullRequest(7, "https://github.com/o/r/pull/7", "OPEN", True)
    monkeypatch.setattr(
        reconcile,
        "remote_heads",
        lambda *_args, **_kwargs: frozenset({"main", "issue-12-run-abcdef"}),
    )
    monkeypatch.setattr(reconcile, "pull_request_for", lambda *_args, **_kwargs: pull_request)

    def refuse_delete(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a branch with a PR must never be deleted")

    monkeypatch.setattr(reconcile, "delete_branch", refuse_delete)

    result = reconcile.finish("o/r", tmp_path, lease(before=frozenset({"main"})), run=FakeRunner())

    assert result.pull_request == pull_request
    assert result.deleted is False
