"""Tests for the credential minting.

The parts pinned here are the ones GitHub rejects silently or with a misleading
error: JWT encoding, the claim window, and the rule that no credential may be
taken from the environment. Nothing here talks to GitHub -- `gh` is never run.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fake_proc import FakeRunner

from pi_agent.container import token


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def key_file(rsa_key: rsa.RSAPrivateKey, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("secrets") / "app.pem"
    path.write_bytes(
        rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


def b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# ----------------------------------------------------------------------- b64url


def test_b64url_drops_padding() -> None:
    assert not token.b64url(b"any carnal pleasure").endswith("=")


def test_b64url_is_url_safe() -> None:
    """Standard base64 would emit + and /, which are not legal in a JWT segment."""
    encoded = token.b64url(bytes(range(256)))

    assert "+" not in encoded
    assert "/" not in encoded


def test_b64url_round_trips() -> None:
    payload = b'{"alg":"RS256"}'

    assert b64url_decode(token.b64url(payload)) == payload


# --------------------------------------------------------------------- build_jwt


def test_build_jwt_has_three_segments(key_file: Path) -> None:
    assert len(token.build_jwt("123456", key_file).split(".")) == 3


def test_build_jwt_header_declares_rs256(key_file: Path) -> None:
    header_segment = token.build_jwt("123456", key_file).split(".")[0]

    assert json.loads(b64url_decode(header_segment)) == {"alg": "RS256", "typ": "JWT"}


def test_build_jwt_issuer_is_the_app_id(key_file: Path) -> None:
    payload_segment = token.build_jwt("123456", key_file).split(".")[1]

    assert json.loads(b64url_decode(payload_segment))["iss"] == "123456"


def test_build_jwt_claim_window_stays_inside_githubs_ten_minute_cap(key_file: Path) -> None:
    """Backdated against clock skew, and short enough that GitHub accepts it."""
    payload_segment = token.build_jwt("123456", key_file).split(".")[1]
    claims = json.loads(b64url_decode(payload_segment))

    assert claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] < claims["exp"]


def test_build_jwt_signature_verifies_against_the_public_key(
    key_file: Path, rsa_key: rsa.RSAPrivateKey
) -> None:
    header, payload, signature = token.build_jwt("123456", key_file).split(".")

    rsa_key.public_key().verify(
        b64url_decode(signature),
        f"{header}.{payload}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_build_jwt_rejects_a_non_rsa_key(tmp_path: Path) -> None:
    """GitHub Apps sign RS256; an EC key would fail later with a worse message."""
    path = tmp_path / "ec.pem"
    path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises((SystemExit, TypeError, ValueError)):
        token.build_jwt("123456", path)


# ------------------------------------------------------------------ require_env


def test_require_env_returns_a_set_value() -> None:
    assert token.require_env({"GITHUB_APP_ID": "123456"}, "GITHUB_APP_ID", "hint") == "123456"


@pytest.mark.parametrize("env", [{}, {"GITHUB_APP_ID": ""}])
def test_require_env_stops_when_missing_or_empty(env: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        token.require_env(env, "GITHUB_APP_ID", "hint")


# -------------------------------------------------------------- no human tokens


def test_a_preexisting_gh_token_is_never_accepted_as_the_credential(key_file: Path) -> None:
    """A stray GH_TOKEN would run the whole pipeline as a human and misattribute it.

    There must be no path from the environment to the returned credential: the
    only source is a JWT signed with the App key. This is the single most
    load-bearing test in the repository, and a refactor is exactly the kind of
    change that could quietly open that path.
    """
    with pytest.raises(SystemExit):
        token.mint(
            {
                "GH_TOKEN": "ghp_a_humans_token",
                "GITHUB_REPOSITORY": "owner/repo",
                # No GITHUB_APP_ID: there is no other way in.
            },
            run=FakeRunner(),
        )


def test_the_minted_token_is_the_one_github_returned(key_file: Path) -> None:
    """And it is never the GH_TOKEN that happened to be in the environment."""
    runner = FakeRunner(
        [
            ("/app ", "agent-demo-bot"),
            ("/repos/owner/repo/installation", "87654321"),
            ("access_tokens", "ghs_minted_by_github"),
            ("/users/", "4707632"),
        ]
    )

    credential = token.mint(
        {
            "GH_TOKEN": "ghp_a_humans_token",
            "GITHUB_APP_ID": "123456",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
        },
        run=runner,
    )

    assert credential.token == "ghs_minted_by_github"
    assert credential.actor == "agent-demo-bot[bot]"
    assert credential.actor_id == 4707632


def test_the_app_jwt_is_sent_as_bearer(key_file: Path) -> None:
    """`gh` would otherwise send `Authorization: token`, which GitHub rejects
    with a misleading "A JSON web token could not be decoded"."""
    runner = FakeRunner(
        [
            ("/app ", "agent-demo-bot"),
            ("/repos/owner/repo/installation", "87654321"),
            ("access_tokens", "ghs_minted"),
            ("/users/", "1"),
        ]
    )

    token.mint(
        {
            "GITHUB_APP_ID": "123456",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
        },
        run=runner,
    )

    app_call = runner.matching("/app ")[0]
    assert "Authorization: Bearer" in " ".join(app_call)


# ------------------------------------------------------------------- redaction


def test_the_credential_never_reprs_its_token() -> None:
    """In bash a `set -x` or an unlucky trace printed it; here it structurally
    cannot appear in a repr, a log line, or a traceback frame summary."""
    credential = token.Credential(token="ghs_secret", actor="a[bot]", actor_id=1)

    assert "ghs_secret" not in repr(credential)
    assert "ghs_secret" not in str(credential)
    assert "ghs_secret" not in f"{credential}"
    assert "<redacted>" in repr(credential)
