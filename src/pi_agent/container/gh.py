"""Calling GitHub through the `gh` CLI.

Two GitHub details are encoded here, both of which cost a debugging round.

App JWTs must be presented as ``Authorization: Bearer``, but `gh` sends
``Authorization: token`` for whatever is in ``GH_TOKEN`` -- and GitHub answers
that with a misleading *"A JSON web token could not be decoded"*. So a JWT call
overrides the header.

And `gh` refuses to run with no credential configured at all, so a JWT call
still has to set *some* ``GH_TOKEN``. It sets a placeholder, which the
overridden header then makes irrelevant.

After the bootstrap, ``gh`` on PATH is the image's wrapper (``image/bin/gh``),
which reads the installation token from the file ``GH_TOKEN_FILE`` names and
hands it to the real binary for that one call. So `cli` passes no token and
sets none: the wrapper does, for the bootstrap's calls and the model's alike,
and a ``GH_TOKEN`` that `api` sets explicitly takes precedence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from pi_agent import _proc

# `gh` will not start without a credential in the environment. For a JWT call
# the real authorization travels in an explicit header, so this value is only
# ever there to satisfy that check.
PLACEHOLDER_TOKEN = "unused-placeholder"


def api(
    path: str,
    token: str,
    *,
    method: str = "GET",
    jq: str | None = None,
    bearer: bool = False,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> str:
    """Call the GitHub API through `gh`, authenticated with `token`.

    `bearer` selects the App-JWT shape described in the module docstring;
    without it the token is treated as an ordinary installation or user token.
    """
    argv = ["gh", "api", "--method", method, path]
    if jq is not None:
        argv += ["--jq", jq]

    base = dict(os.environ if env is None else env)
    if bearer:
        argv += ["-H", f"Authorization: Bearer {token}"]
        base["GH_TOKEN"] = PLACEHOLDER_TOKEN
    else:
        base["GH_TOKEN"] = token

    return run(argv, env=base, check=True).stdout.strip()


def cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> str:
    """Run a `gh` subcommand and return its stdout, stripped."""
    return run(
        ["gh", *args], env=dict(os.environ if env is None else env), check=True
    ).stdout.strip()
