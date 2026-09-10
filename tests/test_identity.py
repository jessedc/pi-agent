"""Tests for the identity the bot commits and pushes as.

This is the logic behind the "Identity is settled in code, before a model
starts" invariant in AGENTS.md. Getting any of it wrong misattributes the work
silently: a PR that says "bot" containing commits signed by a human.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fake_proc import FakeRunner

from pi_agent.container import identity
from pi_agent.container.token import Credential

CREDENTIAL = Credential(token="ghs_secret_value", actor="agent-demo-bot[bot]", actor_id=4707632)


# ------------------------------------------------------------------ authorship


def test_author_email_is_githubs_noreply_form() -> None:
    """GitHub only links a commit to the bot user if the address matches exactly."""
    assert CREDENTIAL.git_email == "4707632+agent-demo-bot[bot]@users.noreply.github.com"


def test_committer_matches_author() -> None:
    """A mismatch shows up in the GitHub UI as "committed by" someone else."""
    env = identity.author_env(CREDENTIAL)

    assert env["GIT_COMMITTER_NAME"] == env["GIT_AUTHOR_NAME"] == "agent-demo-bot[bot]"
    assert env["GIT_COMMITTER_EMAIL"] == env["GIT_AUTHOR_EMAIL"]


# --------------------------------------------------------------- git config


def test_signing_is_disabled() -> None:
    """There is no signing key in the container, and the prompt commits with -m.

    If this regressed, every commit would block waiting for a key that is not
    there, and the run would hang until it was killed.
    """
    commands = identity.git_config_commands()

    assert ["git", "config", "--global", "commit.gpgsign", "false"] in commands


def test_no_directory_is_marked_safe() -> None:
    """No `safe.directory` entry, and this is what keeps it that way.

    One would be inert -- see `git_config_commands` -- and an entry for the
    worktrees' parent could never match the path git actually tests. Adding one
    is a change that should have to argue for itself, not one that slips in.
    """
    flattened = [word for command in identity.git_config_commands() for word in command]

    assert "safe.directory" not in flattened


# ------------------------------------------------------- the credential helper


def test_the_helper_carries_no_secret() -> None:
    """It names the file; it must never contain a value, nor read a variable
    that would have to hold one."""
    assert "${GH_TOKEN_FILE}" in identity.CREDENTIAL_HELPER
    assert "${GH_TOKEN}" not in identity.CREDENTIAL_HELPER
    assert "ghs_" not in identity.CREDENTIAL_HELPER


def test_the_helper_string_is_exactly_what_git_expects() -> None:
    """Pinned byte-for-byte: a quoting slip here breaks every push."""
    assert identity.CREDENTIAL_HELPER == (
        '!f() { echo username=x-access-token; echo "password=$(cat "${GH_TOKEN_FILE}")"; }; f'
    )


# ------------------------------------------------------------------- apply


def test_apply_writes_the_token_to_a_file_the_owner_alone_can_read(tmp_path: Path) -> None:
    """The exposure finding: a token in the environment is inherited by every
    process the model spawns and is one `env` away from a transcript."""
    token_file = tmp_path / ".gh-token"

    identity.apply(CREDENTIAL, {}, token_file=token_file, run=FakeRunner())

    assert token_file.read_text() == "ghs_secret_value"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_apply_tells_the_environment_the_path_and_never_the_token(tmp_path: Path) -> None:
    """`git push` and `gh` run in descendants of the model's bash tool, so the
    path reaches them through inherited environment. The value need not."""
    environ: dict[str, str] = {"GH_TOKEN": "left over from somewhere"}
    token_file = tmp_path / ".gh-token"

    identity.apply(CREDENTIAL, environ, token_file=token_file, run=FakeRunner())

    assert environ["GH_TOKEN_FILE"] == str(token_file)
    assert "GH_TOKEN" not in environ
    assert "ghs_secret_value" not in " ".join(environ.values())
    assert environ["GIT_AUTHOR_NAME"] == "agent-demo-bot[bot]"


def test_the_token_file_lives_in_the_dropped_accounts_home() -> None:
    """The home is a tmpfs owned by that account and mode 0700, so the file dies
    with the container and no other uid can reach the directory holding it."""
    assert identity.token_file({"HOME": "/home/pi"}) == Path("/home/pi/.gh-token")


def test_apply_runs_every_config_command(tmp_path: Path) -> None:
    runner = FakeRunner()

    identity.apply(CREDENTIAL, {}, token_file=tmp_path / ".gh-token", run=runner)

    assert len(runner.calls) == 4
    assert all(call[:2] == ("git", "config") for call in runner.calls)


# --------------------------------------------------- end to end, against git


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_actually_serves_the_token_through_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real proof: ask git for the credential and see the token come back.

    This exercises the helper string through git's own parser and `sh`, which
    is the only way to catch a quoting regression in it.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    token_file = tmp_path / ".gh-token"
    identity.write_token(token_file, "ghs_a_very_secret_value")
    monkeypatch.setenv("GH_TOKEN_FILE", str(token_file))

    for argv in identity.git_config_commands():
        subprocess.run(argv, check=True)

    filled = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "username=x-access-token" in filled
    assert "password=ghs_a_very_secret_value" in filled


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_works_in_a_worktree_beside_the_clone_with_nothing_marked_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assumption that lets `git_config_commands` mark nothing safe, proved.

    The container is one user: /work is chowned to it in the Dockerfile, and the
    clone and every worktree under it are created at run time by it. So git's
    dubious-ownership check never fires and nothing needs marking safe.

    That is an assumption about git, not about us, so it is held against real
    git rather than asserted against our own strings: comparing this module's
    idea of the worktree path to the prompt's idea of it proves nothing, because
    neither is the path git opens.

    Built to the prompt's Phase 3 layout exactly: a clone, and a worktree at
    `../<repo>-wt/issue-<n>` beside it.

    What it does not prove, so that nobody reads it as proving it: the failure
    side. Fabricating a uid mismatch needs root, which a test run does not have.
    That half was verified by hand against the image's git -- a foreign-owned
    worktree is refused, an entry for its parent does not help, and no prefix
    form matches -- and is why there is no entry at all rather than a corrected
    one.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    for argv in identity.git_config_commands():
        subprocess.run(argv, check=True)

    # The premise: an empty allowlist. `--get-all` exits 1 when nothing is set.
    assert (
        subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"], capture_output=True
        ).returncode
        == 1
    )

    work = tmp_path / "work"
    clone = work / "agent-demo"
    worktree = work / "agent-demo-wt" / "issue-12"
    committing = {**os.environ, **identity.author_env(CREDENTIAL)}

    def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=committing, capture_output=True, text=True, check=True
        )

    clone.mkdir(parents=True)
    git("init", "--quiet", ".", cwd=clone)
    (clone / "f.txt").write_text("x")
    git("add", "f.txt", cwd=clone)
    git("commit", "--quiet", "-m", "init", cwd=clone)

    # Phase 3, as the prompt writes it.
    git("worktree", "add", "--quiet", str(worktree), "-b", "issue-12-slug", cwd=clone)

    # Phase 4 onward happens in there: git must open it, and commit in it.
    assert git("status", "--porcelain", "--branch", cwd=worktree).stdout.startswith(
        "## issue-12-slug"
    )
    (worktree / "g.txt").write_text("y")
    git("add", "g.txt", cwd=worktree)
    git("commit", "--quiet", "-m", "work", cwd=worktree)

    assert git("log", "--oneline", "-1", cwd=worktree).stdout.strip().endswith("work")
