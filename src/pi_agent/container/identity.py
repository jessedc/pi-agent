"""Settle who the bot is, before a model is ever started.

Commit authorship comes from git config, PR authorship comes from whichever
token calls the API, and pushes come from whichever credential git resolves.
Those are three separate mechanisms, and getting any one wrong silently
misattributes the work -- a PR that says "bot" containing commits signed by a
human. So none of it is left to the prompt: the model cannot get this wrong
because it is never asked to.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from pi_agent import _log, _proc
from pi_agent.container.token import Credential

LOG = _log.Logger("bootstrap")

# The token is served from a file, not the environment. Every process the
# model's bash tool spawns inherits the environment, so a token there is one
# `env` away from a transcript, a crash reporter, or a test suite that dumps
# its surroundings. The file is readable by the run's uid alone, and the two
# consumers -- git's credential helper below and the `gh` wrapper the image
# puts ahead of /usr/bin/gh -- read it per call. The same uid can still `cat`
# it: this narrows the exposure, it does not remove the trust in the uid.
TOKEN_FILE_NAME = ".gh-token"

# Serve the token through a credential helper rather than baking it into the
# remote URL, so it never lands in .git/config -- and so the plain
# `git push -u origin <branch>` in the agent prompt just works.
#
# This is a shell snippet on purpose. It is git's own credential-helper
# protocol: git documents a command string it runs through `sh`, and answering
# a two-line stdout protocol from a separate Python program would be the same
# mechanism reading the same file, with an extra process on every fetch and
# push. What matters is the property this string has, which is tested: it
# names the file through ${GH_TOKEN_FILE}, it does not contain a token.
CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$(cat "${GH_TOKEN_FILE}")"; }; f'
)


def token_file(environ: Mapping[str, str]) -> Path:
    """Where this run keeps its token: in the dropped account's home, a tmpfs."""
    return Path(environ["HOME"]) / TOKEN_FILE_NAME


def write_token(path: Path, token: str) -> None:
    """Write the token readable by its owner alone, from the first byte."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    path.chmod(0o600)


def author_env(credential: Credential) -> dict[str, str]:
    """The GIT_AUTHOR_*/GIT_COMMITTER_* pairs this credential commits under."""
    name = credential.actor
    email = credential.git_email
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def git_config_commands() -> list[list[str]]:
    """The global git configuration a run needs, in order.

    Nothing here marks a directory safe. git's dubious-ownership check fires
    only on a uid mismatch, and there is none: the Dockerfile chowns /work to
    this user, and the clone and every worktree beneath it are created at run
    time by this user. A `safe.directory` entry for the clone would be inert,
    and one for the directory the worktrees go in could not work even where it
    mattered -- git tests the repository it opens by exact path, and for a
    linked worktree that is the worktree itself (`<repo>-wt/issue-<n>`), not
    its parent, so no prefix form matches it. `tests/test_identity.py` proves
    the assumption against real git.
    """
    return [
        # No signing key in the container, and signing with anyone else's key
        # would misattribute the work.
        ["git", "config", "--global", "commit.gpgsign", "false"],
        ["git", "config", "--global", "init.defaultBranch", "main"],
        ["git", "config", "--global", "advice.detachedHead", "false"],
        ["git", "config", "--global", "credential.helper", CREDENTIAL_HELPER],
    ]


def apply(
    credential: Credential,
    environ: MutableMapping[str, str],
    *,
    token_file: Path,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> None:
    """Apply this credential's identity to git config and to `environ`.

    The token is written to `token_file` here, deliberately and in one place,
    and `environ` is told the path rather than the value. `git push` and `gh`
    are issued by the model inside a bash tool, in a descendant of this
    process, so the path can only reach them through inherited environment --
    but the token itself need not, and does not.
    """
    write_token(token_file, credential.token)
    environ["GH_TOKEN_FILE"] = str(token_file)
    # Nothing forwards a token in, and this module used to be the one place
    # that set one; keeping it out is now the property.
    environ.pop("GH_TOKEN", None)
    environ.update(author_env(credential))
    LOG.log(f"commits will be authored as {credential.actor} <{credential.git_email}>")

    for argv in git_config_commands():
        run(argv, env=env, check=True)
