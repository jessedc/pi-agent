"""Bootstrap with the App key, discard privilege, then run the agent.

Everything identity-related is settled here rather than in the agent prompt, so
a forgetful model cannot commit under the wrong name or push with the wrong
credential.

This process begins privileged only so it can traverse the root-only directory
holding the App key. Immediately after minting the short-lived credential it
irreversibly becomes ``pi`` and stays alive for the post-run report.

Nothing here defaults. Every path and every model value is named in `.env` and
forwarded by the host CLI, so a container that was started without one stops on
the variable's name rather than running somewhere nobody meant it to.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path

from pi_agent import _log, _piconfig
from pi_agent.container import (
    agent,
    egress,
    identity,
    issue,
    privilege,
    reconcile,
    repo,
    token,
)

LOG = _log.Logger("bootstrap")


def require(environ: MutableMapping[str, str], name: str) -> str:
    """Read a variable the host was supposed to forward, or stop."""
    value = environ.get(name)
    if not value:
        LOG.die(f"{name} is not set — the host CLI forwards it from .env")
    return value


def main(argv: list[str] | None = None, *, environ: MutableMapping[str, str] | None = None) -> int:
    """Run one issue end to end, or drop to an unprivileged credential shell."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ

    repository = require(environ, "GITHUB_REPOSITORY")

    workdir = Path(require(environ, "WORKDIR"))
    repo_name = repository.rpartition("/")[2]
    clone_dir = workdir / repo_name
    session_root = Path(require(environ, "SESSION_DIR"))

    # ------------------------------------------------------- pi configuration
    # pi's registry and defaults are generated here rather than baked into the
    # image, so the model a run uses is configuration and not a rebuild. Done
    # before anything slow, because a bad model config should cost a second.
    try:
        pi = _piconfig.Config.from_environ(environ)
    except _piconfig.MissingConfig as error:
        LOG.die(str(error))

    pi_dir = Path(require(environ, "PI_CODING_AGENT_DIR"))

    # The model's deadline, checked before anything slow for the same reason
    # as the model config: a typo should cost a second, not a token mint.
    raw_timeout = require(environ, "RUN_TIMEOUT")
    if not raw_timeout.isdigit() or int(raw_timeout) <= 0:
        LOG.die(f"RUN_TIMEOUT must be a positive number of seconds, got: {raw_timeout}")
    timeout = float(raw_timeout)

    # ------------------------------------------------------------ credentials
    # token.mint resolves both the credential and the identity behind it: an
    # installation token cannot call /app, so the App slug is only knowable at
    # the moment the token is minted.
    credential = token.mint(environ)

    # ---------------------------------------------------------------- egress
    # Closed while still root, with the capability the drop below discards.
    # The token was minted on an open network; everything after this line --
    # the clone, the model, the push -- goes through the allowlist, and the
    # credential check that follows the drop is what proves GitHub is on it.
    try:
        policy = egress.policy_for(
            credential.token,
            model_ip=require(environ, "MODEL_IP"),
            model_port=pi.port,
            allow=environ.get("EGRESS_ALLOW"),
            env=environ,
        )
        egress.apply(egress.ruleset(policy))
    except egress.EgressError as error:
        LOG.die(str(error))
    LOG.log(f"egress closed: {policy.summary()}")

    # Bootstrap is done with the key path, and the process that runs model
    # tools must be unable to traverse its root-only parent directory.
    environ.pop("GITHUB_APP_PRIVATE_KEY_FILE", None)
    try:
        privilege.drop_to(privilege.PI, environ)
    except privilege.PrivilegeDropError as error:
        LOG.die(str(error))

    models_path, settings_path = _piconfig.write(pi, pi_dir)
    LOG.log(f"model: {pi.model_ref} at {pi.base_url}")
    LOG.log(f"pi config: {models_path}, {settings_path}")

    # -------------------------------------------------------------- identity
    identity.apply(credential, environ, token_file=identity.token_file(environ))

    # ----------------------------------------------------------------- clone
    repo.verify_access(repository)
    try:
        repo.clone_or_refresh(repository, clone_dir)
    except repo.RepositoryUnavailable as error:
        LOG.die(str(error))
    os.chdir(clone_dir)

    # ------------------------------------------------------------ issue input
    mode = args[0] if args else "run"
    if mode == "shell":
        # A credential-verification shell has no issue to work on.
        LOG.log("dropping to an unprivileged shell (credential is live; App key is unreachable)")
        LOG.log(f"session transcripts: {session_root}")
        os.execvpe("bash", ["bash"], environ)  # noqa: S606 -- replaces PID 1 by design

    raw = args[0] if args else environ.get("ISSUE", "")
    try:
        target = issue.fetch(repository, raw)
    except issue.InvalidIssue as error:
        LOG.die(str(error))

    agent_dir = Path(require(environ, "AGENT_DIR"))
    agent_name = require(environ, "AGENT")
    LOG.log(f"agent definition: {agent_dir / f'{agent_name}.md'} (image artifact)")
    environ["ISSUE"] = target.number

    # -------------------------------------------------------------- handoff
    try:
        return agent.run(
            target.number,
            clone_dir,
            agent=agent_name,
            agent_dir=agent_dir,
            session_root=session_root,
            model=pi.model_ref,
            thinking=pi.thinking,
            repository=repository,
            timeout=timeout,
        )
    except (agent.InvalidDefinition, reconcile.RemoteStateError) as error:
        LOG.die(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
