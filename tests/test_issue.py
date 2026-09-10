"""Tests for issue validation.

A run targets exactly one issue. Validating it here means a wrong number costs a
second rather than a model budget -- but the bash version conflated "no such
issue" with "cannot read issues" behind a `2>/dev/null ||`, so which failure
fired was unknowable. Now it is assertable.
"""

from __future__ import annotations

import pytest
from fake_proc import FakeRunner

from pi_agent.container import issue

REPO = "example/agent-demo"


# ------------------------------------------------------------------ normalise


@pytest.mark.parametrize("raw", ["12", "#12", " 12 ", "#12\n"])
def test_normalise_accepts_the_documented_forms(raw: str) -> None:
    assert issue.normalise(raw) == "12"


@pytest.mark.parametrize("raw", ["", "twelve", "12a", "#", "12 13"])
def test_normalise_rejects_a_non_number(raw: str) -> None:
    with pytest.raises(issue.InvalidIssue):
        issue.normalise(raw)


def test_normalise_reports_what_it_was_given() -> None:
    with pytest.raises(issue.InvalidIssue, match="twelve"):
        issue.normalise("twelve")


# ---------------------------------------------------------------------- fetch


def test_fetch_returns_an_open_issue() -> None:
    runner = FakeRunner([("--json state", "OPEN"), ("--json title", "Add a --dry-run flag")])

    target = issue.fetch(REPO, "#12", run=runner)

    assert target.number == "12"
    assert target.title == "Add a --dry-run flag"


def test_fetch_asks_github_the_way_the_bash_version_did() -> None:
    runner = FakeRunner([("--json state", "OPEN")])

    issue.fetch(REPO, "12", run=runner)

    assert runner.calls[0] == (
        "gh",
        "issue",
        "view",
        "12",
        "--repo",
        REPO,
        "--json",
        "state",
        "--jq",
        ".state",
    )


def test_a_closed_issue_is_refused_and_names_its_state() -> None:
    runner = FakeRunner([("--json state", "CLOSED")])

    with pytest.raises(issue.InvalidIssue, match="is CLOSED, not OPEN"):
        issue.fetch(REPO, "12", run=runner)


def test_a_closed_issue_is_refused_before_the_title_is_fetched() -> None:
    """Nothing further should be spent on an issue that will not be worked."""
    runner = FakeRunner([("--json state", "CLOSED")])

    with pytest.raises(issue.InvalidIssue):
        issue.fetch(REPO, "12", run=runner)

    assert runner.matching("--json title") == []


def test_an_unreadable_issue_is_distinguishable_from_a_closed_one() -> None:
    """The bash version hid this behind `2>/dev/null ||`; both looked the same."""
    runner = FakeRunner(failures=["gh issue view"])

    with pytest.raises(issue.InvalidIssue, match="not found .*or the credential cannot read"):
        issue.fetch(REPO, "12", run=runner)
