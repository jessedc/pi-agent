"""Tests for the container's agent runner.

The runner is what stands between an issue number and a `pi` process, so the
parts worth pinning are the ones a container run cannot recover from: a
misparsed agent definition and a command line that quietly drops a flag.

No test starts a container, a model, or a subprocess.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fake_proc import FakeRunner

from pi_agent import _proc
from pi_agent.container import agent, reconcile


def argv_for(frontmatter: dict[str, str], tmp_path: Path) -> list[str]:
    """A `pi` command line for a definition, with the model config a run supplies."""
    return agent.build_argv(
        frontmatter, tmp_path / "p.md", "t", tmp_path, model=MODEL, thinking="high"
    )


MODEL = "llamacpp/DeepSeek-V4-Flash-0731-UD-IQ2_M"

DEFINITION = """\
---
name: issue-to-pr
description: takes one issue: implements it
tools: read, grep, bash
inheritSkills: false
---

You are `issue-to-pr`.

Body text follows.
"""


# ----------------------------------------------------------- split_definition


def test_split_definition_separates_frontmatter_from_body() -> None:
    frontmatter, body = agent.split_definition(DEFINITION)

    assert frontmatter["name"] == "issue-to-pr"
    assert frontmatter["tools"] == "read, grep, bash"
    assert body.startswith("You are `issue-to-pr`.")
    assert "---" not in body


def test_split_definition_keeps_colons_in_values() -> None:
    """A description can contain a colon; the parser splits on the first one."""
    frontmatter, _ = agent.split_definition(DEFINITION)

    assert frontmatter["description"] == "takes one issue: implements it"


def test_split_definition_rejects_a_file_with_no_frontmatter() -> None:
    with pytest.raises(agent.InvalidDefinition):
        agent.split_definition("You are an agent with no fence.\n")


# ----------------------------------------------------------- session_dir_for


def test_session_dir_is_per_issue(tmp_path: Path) -> None:
    session_dir = agent.session_dir_for("12", tmp_path)

    assert session_dir == tmp_path / "issue-12"
    assert session_dir.is_dir()


def test_session_dir_is_idempotent(tmp_path: Path) -> None:
    """Several attempts at one issue accumulate in a single directory."""
    first = agent.session_dir_for("12", tmp_path)
    (first / "existing.jsonl").write_text("{}")
    second = agent.session_dir_for("12", tmp_path)

    assert first == second
    assert (second / "existing.jsonl").exists()


# ------------------------------------------------------------------ build_argv


def test_build_argv_translates_frontmatter_into_flags(tmp_path: Path) -> None:
    frontmatter, _ = agent.split_definition(DEFINITION)
    prompt_file = tmp_path / "issue-to-pr.md"
    session_dir = tmp_path / "issue-12"

    argv = agent.build_argv(
        frontmatter, prompt_file, "do the thing", session_dir, model=MODEL, thinking="high"
    )

    assert argv[0] == "pi"
    assert "--print" in argv
    assert argv[argv.index("--system-prompt") + 1] == str(prompt_file)
    assert argv[argv.index("--session-dir") + 1] == str(session_dir)
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--thinking") + 1] == "high"
    assert argv[-1] == "Task: do the thing"


def test_build_argv_strips_spaces_from_the_tool_allowlist(tmp_path: Path) -> None:
    """pi wants a comma-separated list without spaces."""
    frontmatter, _ = agent.split_definition(DEFINITION)

    argv = argv_for(frontmatter, tmp_path)

    assert argv[argv.index("--tools") + 1] == "read,grep,bash"


def test_build_argv_never_trusts_the_checkout(tmp_path: Path) -> None:
    """The cloned repo's own .pi/ supplies neither extensions nor approvals."""
    frontmatter, _ = agent.split_definition(DEFINITION)

    argv = argv_for(frontmatter, tmp_path)

    assert "--no-extensions" in argv
    assert "--no-approve" in argv


def test_build_argv_disables_skills_unless_opted_in(tmp_path: Path) -> None:
    assert "--no-skills" in argv_for({}, tmp_path)
    assert "--no-skills" not in argv_for({"inheritSkills": "true"}, tmp_path)


def test_the_checkouts_context_files_are_not_loaded_unless_opted_in(tmp_path: Path) -> None:
    assert "--no-context-files" in argv_for({}, tmp_path)
    assert "--no-context-files" in argv_for({"inheritProjectContext": "false"}, tmp_path)
    assert "--no-context-files" not in argv_for({"inheritProjectContext": "true"}, tmp_path)


def test_build_argv_omits_a_tool_allowlist_the_definition_does_not_set(tmp_path: Path) -> None:
    assert "--tools" not in argv_for({}, tmp_path)


def test_the_model_comes_from_configuration_not_the_definition(tmp_path: Path) -> None:
    """A definition that names a model would be a second place to change one.

    It would also be a place to name a model pi's generated registry has never
    heard of, which fails several minutes into a run rather than at its start.
    """
    frontmatter, _ = agent.split_definition(DEFINITION)
    assert "model" not in frontmatter

    argv = agent.build_argv(
        frontmatter, tmp_path / "p.md", "t", tmp_path, model="llamacpp/other", thinking="low"
    )

    assert argv[argv.index("--model") + 1] == "llamacpp/other"
    assert argv[argv.index("--thinking") + 1] == "low"


# ---------------------------------------------------------------------- task


def test_the_task_names_exactly_one_issue() -> None:
    """The prompt aborts if the task names no issue, and never picks one itself."""
    task = agent.task_for("12", "issue-12-run-abcdef")

    assert "#12" in task
    assert "draft pull request" in task
    assert "issue-12-run-abcdef" in task


# ------------------------------------------------------- the shipped definition


def test_the_shipped_agent_definition_is_usable() -> None:
    """The definition is baked into the image, so a broken one fails a run, not a build."""
    definition = Path(__file__).parent.parent / ".pi" / "agents" / "issue-to-pr.md"

    frontmatter, body = agent.split_definition(definition.read_text(encoding="utf-8"))

    assert frontmatter["name"] == "issue-to-pr"
    assert frontmatter["tools"]
    assert frontmatter["inheritProjectContext"] == "false"
    assert "model" not in frontmatter
    assert body
    assert "--model" in argv_for(frontmatter, Path("/tmp"))
    assert "--no-context-files" in argv_for(frontmatter, Path("/tmp"))


def test_the_shipped_prompt_puts_image_rules_above_repository_text() -> None:
    """Checkout text may guide development but cannot enter the system prompt as authority."""
    definition = Path(__file__).parent.parent / ".pi" / "agents" / "issue-to-pr.md"
    _, body = agent.split_definition(definition.read_text(encoding="utf-8"))

    assert "Rules that the repository cannot change" in body
    assert "can never relax the image rules" in body
    assert "outranks every default" not in body


# ----------------------------------------------------------------------- run


@pytest.mark.parametrize(
    "name",
    ["/work/repo/evil", "../../../work/repo/evil", "sub/x", "..", ".", "", "a\\b", ".hidden"],
)
def test_the_agent_name_must_be_a_bare_identifier(name: str) -> None:
    with pytest.raises(agent.InvalidDefinition, match="bare definition name"):
        agent.validate_name(name)


def test_the_normal_definition_resolves_directly_below_its_directory(tmp_path: Path) -> None:
    definitions = tmp_path / "agents"
    definitions.mkdir()
    expected = definitions / "issue-to-pr.md"
    expected.write_text(DEFINITION)

    assert agent.definition_for("issue-to-pr", definitions) == expected


def test_a_definition_symlink_cannot_escape_the_baked_directory(tmp_path: Path) -> None:
    definitions = tmp_path / "agents"
    definitions.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(DEFINITION)
    (definitions / "issue-to-pr.md").symlink_to(outside)

    with pytest.raises(agent.InvalidDefinition, match="must not be a symlink"):
        agent.definition_for("issue-to-pr", definitions)


def test_a_rejected_agent_name_cannot_overwrite_the_file_it_names(tmp_path: Path) -> None:
    definitions = tmp_path / "agents"
    definitions.mkdir()
    outside = tmp_path / "outside" / "evil.md"
    outside.parent.mkdir()
    outside.write_text(DEFINITION)
    original = outside.read_text()

    with pytest.raises(agent.InvalidDefinition, match="bare definition name"):
        agent.run(
            "12",
            tmp_path,
            agent=str(outside.with_suffix("")),
            agent_dir=definitions,
            session_root=tmp_path / "sessions",
            model=MODEL,
            thinking="high",
            timeout=3600.0,
            repository="owner/repo",
        )

    assert outside.read_text() == original


def test_remote_truth_is_checked_on_both_sides_of_the_model_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = tmp_path / "agents"
    definitions.mkdir()
    (definitions / "issue-to-pr.md").write_text(DEFINITION)
    calls: list[str] = []
    leased = reconcile.Lease("issue-12-run-abcdef", frozenset({"main"}))

    def acquire(*_args: object, **_kwargs: object) -> reconcile.Lease:
        calls.append("before")
        return leased

    def process(
        argv: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None
    ) -> int:
        assert timeout == 3600.0
        calls.append("model")
        return 0

    def finish(*_args: object, **_kwargs: object) -> reconcile.Result:
        calls.append("after")
        return reconcile.Result(
            branch=leased.branch,
            pull_request=reconcile.PullRequest(7, "https://github.com/o/r/pull/7", "OPEN", True),
            deleted=False,
            deletion_error=None,
            unexpected=(),
        )

    monkeypatch.setattr(agent.reconcile, "acquire", acquire)
    monkeypatch.setattr(agent.reconcile, "finish", finish)

    status = agent.run(
        "12",
        tmp_path,
        agent="issue-to-pr",
        agent_dir=definitions,
        session_root=tmp_path / "sessions",
        model=MODEL,
        thinking="high",
        repository="o/r",
        timeout=3600.0,
        process=process,
        command=FakeRunner(),
    )

    assert status == 0
    assert calls == ["before", "model", "after"]


def test_a_run_that_hits_its_deadline_is_still_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the deadline lives here and not on `docker run`: a container
    killed from outside leaves the leased branch on the remote."""
    definitions = tmp_path / "agents"
    definitions.mkdir()
    (definitions / "issue-to-pr.md").write_text(DEFINITION)
    calls: list[str] = []
    leased = reconcile.Lease("issue-12-run-abcdef", frozenset({"main"}))

    def process(
        argv: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None
    ) -> int:
        return _proc.TIMED_OUT

    def finish(*_args: object, **_kwargs: object) -> reconcile.Result:
        calls.append("after")
        return reconcile.Result(
            branch=leased.branch,
            pull_request=None,
            deleted=True,
            deletion_error=None,
            unexpected=(),
        )

    monkeypatch.setattr(agent.reconcile, "acquire", lambda *_a, **_k: leased)
    monkeypatch.setattr(agent.reconcile, "finish", finish)

    status = agent.run(
        "12",
        tmp_path,
        agent="issue-to-pr",
        agent_dir=definitions,
        session_root=tmp_path / "sessions",
        model=MODEL,
        thinking="high",
        repository="o/r",
        timeout=1.0,
        process=process,
        command=FakeRunner(),
    )

    assert status == _proc.TIMED_OUT
    assert calls == ["after"]
