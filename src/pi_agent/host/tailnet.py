"""Resolve the model host on the tailnet.

The model lives on the tailnet, and Docker Desktop routes container traffic
through the host -- so a host alias for the tailscale IP is all the container
needs, and no tailscale daemon runs inside it.
"""

from __future__ import annotations

from pi_agent import _proc


class Unresolvable(RuntimeError):
    """The model host could not be resolved on the tailnet."""


def short_name(model_host: str) -> str:
    """The machine name tailscale knows, from a full MagicDNS name."""
    return model_host.partition(".")[0]


def resolve(
    model_host: str,
    *,
    override: str | None = None,
    run: _proc.Runner = _proc.run,
) -> str:
    """Return the tailnet IPv4 for `model_host`.

    "tailscale is not installed", "tailscale is down" and "no such machine" are
    distinct paths that each end in one message by choice, and each is reachable
    from a test.
    """
    if override:
        return override

    try:
        result = run(["tailscale", "ip", "-4", short_name(model_host)], check=False)
    except FileNotFoundError as error:
        raise Unresolvable(
            f"cannot resolve {model_host} on the tailnet — tailscale is not installed"
        ) from error

    address = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
    if not result.ok or not address:
        raise Unresolvable(f"cannot resolve {model_host} on the tailnet — is tailscale up?")
    return address
