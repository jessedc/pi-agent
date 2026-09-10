"""Mint a GitHub App installation token, and resolve the identity behind it.

The bot authenticates only as its GitHub App. There is deliberately no path that
accepts a pre-existing token from the environment: a stray ``GH_TOKEN`` would
silently run the whole pipeline as a human, attributing the bot's commits and
pull requests to them.

Identity is resolved here rather than by the caller, because an installation
token cannot call ``/app`` -- the App slug is only knowable at the moment the
token is minted.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pi_agent import _log, _proc
from pi_agent.container import gh

LOG = _log.Logger("token")

# GitHub caps App JWTs at ten minutes. Nine leaves room for clock skew on both
# sides without brushing the limit.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Credential:
    """One run's credential, and the GitHub identity it acts as."""

    token: str = field(repr=False)
    actor: str
    actor_id: int

    def __repr__(self) -> str:
        """Redact the token.

        The bash version passed this around as a shell word, where `set -x` or
        an unlucky trace printed it. Here it structurally cannot appear in a
        repr, a log line, or a traceback frame summary.
        """
        return f"Credential(actor={self.actor!r}, actor_id={self.actor_id}, token=<redacted>)"

    @property
    def git_email(self) -> str:
        """The noreply address GitHub associates with this bot user."""
        return f"{self.actor_id}+{self.actor}@users.noreply.github.com"


def b64url(data: bytes) -> str:
    """Base64url-encode without padding, as JWT requires."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_jwt(app_id: str, private_key_file: Path) -> str:
    """Sign a short-lived RS256 JWT identifying the App itself."""
    key = serialization.load_pem_private_key(private_key_file.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        LOG.die(f"{private_key_file} is not an RSA private key")

    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {
        "iat": now - JWT_BACKDATE_SECONDS,
        "exp": now + JWT_LIFETIME_SECONDS,
        "iss": app_id,
    }
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64url(signature)}"


def require_env(env: Mapping[str, str], name: str, hint: str) -> str:
    """Read a required environment variable or explain what is missing."""
    value = env.get(name)
    if not value:
        LOG.die(f"{name} is required -- {hint}")
    return value


def mint(
    env: Mapping[str, str] | None = None,
    *,
    run: _proc.Runner = _proc.run,
) -> Credential:
    """Exchange an App JWT for a one-hour installation token and its identity."""
    env = dict(os.environ if env is None else env)

    app_id = require_env(env, "GITHUB_APP_ID", "the numeric id from the App's settings page")
    repository = require_env(env, "GITHUB_REPOSITORY", "owner/repo the App is installed on")
    key_path = require_env(
        env, "GITHUB_APP_PRIVATE_KEY_FILE", "path to the App .pem, mounted read-only"
    )

    key_file = Path(key_path)
    if not key_file.is_file():
        LOG.die(f"private key not readable at {key_file}")

    jwt = build_jwt(app_id, key_file)

    # Validate the JWT on its own first. Without this, a bad App id, a key from
    # a different App, and a genuinely-missing installation all surface as the
    # same unhelpful "not installed" message.
    try:
        slug = gh.api("/app", jwt, jq=".slug", bearer=True, env=env, run=run)
    except _proc.CommandError as error:
        LOG.die(
            f"the App itself did not authenticate (app_id={app_id}, key={key_file}).\n"
            f"  Check that GITHUB_APP_ID matches the App the key was generated for.\n"
            f"  GitHub said: {error}"
        )
    LOG.log(f"authenticated as App '{slug}' (id={app_id})")

    installation_id = env.get("GITHUB_APP_INSTALLATION_ID")
    if not installation_id:
        LOG.log(f"resolving installation for {repository}")
        try:
            installation_id = gh.api(
                f"/repos/{repository}/installation", jwt, jq=".id", bearer=True, env=env, run=run
            )
        except _proc.CommandError as error:
            LOG.die(
                f"App '{slug}' is not installed on {repository}.\n"
                f"  Install it at: https://github.com/apps/{slug}/installations/new\n"
                f"  Then grant it access to that repository specifically.\n"
                f"  GitHub said: {error}"
            )

    LOG.log(f"minting installation token (id={installation_id}, valid 1h)")
    try:
        token = gh.api(
            f"/app/installations/{installation_id}/access_tokens",
            jwt,
            method="POST",
            jq=".token",
            bearer=True,
            env=env,
            run=run,
        )
    except _proc.CommandError as error:
        LOG.die(f"token mint failed: {error}")

    if not token or token == "null":
        LOG.die("token mint returned nothing")

    actor = f"{slug}[bot]"
    try:
        actor_id = int(gh.api(f"/users/{slug}%5Bbot%5D", token, jq=".id", env=env, run=run))
    except (_proc.CommandError, ValueError) as error:
        LOG.die(f"could not resolve the bot user behind App '{slug}': {error}")

    credential = Credential(token=token, actor=actor, actor_id=actor_id)
    LOG.log(f"identity: {credential.actor} (id={credential.actor_id})")
    return credential
