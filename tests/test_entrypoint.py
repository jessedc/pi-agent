"""Tests for bootstrap ordering that must hold before a model starts.

Every collaborator is replaced with an in-memory recorder: no credential,
network, subprocess, daemon, or privileged syscall is used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_piconfig import ENVIRON as MODEL_ENVIRON

from pi_agent.container import egress, entrypoint, issue, privilege
from pi_agent.container.token import Credential


def test_egress_is_closed_before_privilege_is_dropped_and_before_a_model_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    environ = {
        **MODEL_ENVIRON,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_APP_PRIVATE_KEY_FILE": "/run/secrets/github-app.pem",
        "WORKDIR": str(tmp_path / "work"),
        "SESSION_DIR": str(tmp_path / "sessions"),
        "PI_CODING_AGENT_DIR": str(tmp_path / "pi"),
        "AGENT_DIR": str(tmp_path / "agents"),
        "AGENT": "issue-to-pr",
        "MODEL_IP": "100.64.0.1",
        "RUN_TIMEOUT": "3600",
    }
    credential = Credential(token="ghs_token", actor="bot[bot]", actor_id=1)

    def mint(_env: dict[str, str]) -> Credential:
        calls.append("mint")
        return credential

    monkeypatch.setattr(entrypoint.token, "mint", mint)

    policy = egress.Policy(
        model_ip="100.64.0.1", model_port=8080, resolvers=("10.0.0.2",), github=(), allowed=()
    )

    def policy_for(token: str, **kwargs: Any) -> egress.Policy:
        assert token == "ghs_token"
        assert kwargs["model_ip"] == "100.64.0.1"
        assert kwargs["model_port"] == 8080
        return policy

    monkeypatch.setattr(entrypoint.egress, "policy_for", policy_for)

    def apply_policy(text: str) -> None:
        assert text == egress.ruleset(policy)
        calls.append("egress")

    monkeypatch.setattr(entrypoint.egress, "apply", apply_policy)

    def drop(_account: privilege.Account, env: dict[str, str]) -> None:
        assert "GITHUB_APP_PRIVATE_KEY_FILE" not in env
        env["HOME"] = "/home/pi"
        calls.append("drop")

    monkeypatch.setattr(entrypoint.privilege, "drop_to", drop)
    monkeypatch.setattr(
        entrypoint._piconfig, "write", lambda *_args: (Path("models"), Path("settings"))
    )

    def apply_identity(_credential: Credential, env: dict[str, str], *, token_file: Path) -> None:
        assert token_file == Path("/home/pi/.gh-token")
        assert "GH_TOKEN" not in env
        calls.append("identity")

    monkeypatch.setattr(entrypoint.identity, "apply", apply_identity)
    monkeypatch.setattr(entrypoint.repo, "clone_or_refresh", lambda *_args: calls.append("clone"))
    monkeypatch.setattr(entrypoint.repo, "verify_access", lambda *_args: calls.append("verify"))
    monkeypatch.setattr(entrypoint.os, "chdir", lambda _path: None)

    def fetch(*_args: Any) -> issue.Issue:
        calls.append("issue")
        return issue.Issue(number="12", title="Title")

    monkeypatch.setattr(entrypoint.issue, "fetch", fetch)

    def run_agent(*_args: Any, **_kwargs: Any) -> int:
        assert "GITHUB_APP_PRIVATE_KEY_FILE" not in environ
        assert _kwargs["repository"] == "owner/repo"
        assert _kwargs["timeout"] == 3600.0
        calls.append("agent")
        return 0

    monkeypatch.setattr(entrypoint.agent, "run", run_agent)

    assert entrypoint.main(["12"], environ=environ) == 0
    assert calls == ["mint", "egress", "drop", "identity", "verify", "clone", "issue", "agent"]
