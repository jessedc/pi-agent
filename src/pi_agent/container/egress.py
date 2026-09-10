"""Close the container's network before a model starts.

A run has to reach three places: the model endpoint, GitHub, and the resolver
that names GitHub. Everything else -- a host an issue body tells the model to
curl, the wider internet -- is a channel for the installation token, the model
key and the checkout to leave through, and docker draws no line against it: the
default bridge routes anywhere the host can.

So the line is drawn here, inside the container's own network namespace, by the
root bootstrap with the NET_ADMIN capability the host grants it. The rules are
an nftables allowlist built as a value from the model configuration, GitHub's
published address ranges and the container's resolvers, and installed before
the privilege drop -- which discards the capability, so no process the model
controls can list or loosen them. A destination the target project genuinely
needs, such as a package registry, is configuration (``EGRESS_ALLOW``): resolved
here, once, and allowed on 443 only.

Blocked connections are rejected rather than dropped. A model that tries one
loses a tool call to an immediate reset, not a timeout's worth of budget.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pi_agent import _log, _proc
from pi_agent.container import gh

LOG = _log.Logger("egress")

RESOLV_CONF = Path("/etc/resolv.conf")

# The /meta keys whose ranges a run talks to: git over HTTPS, the REST API, and
# github.com itself. The rest -- pages, packages, actions, hooks -- it does not.
GITHUB_RANGES = ("git", "api", "web")
HTTPS = 443
DNS = 53

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str], Iterable[str]]


class EgressError(RuntimeError):
    """The policy could not be built or installed; the run must not go on open."""


@dataclass(frozen=True, slots=True)
class Policy:
    """Every destination a run may open a connection to, and nothing else."""

    model_ip: str
    model_port: int
    # The nameservers docker gave the container, on 53.
    resolvers: tuple[str, ...]
    # GitHub's published ranges, on 443.
    github: tuple[str, ...]
    # Addresses of the EGRESS_ALLOW hosts, on 443.
    allowed: tuple[str, ...]

    def summary(self) -> str:
        """One line for the run's stderr, so a transcript says what was open."""
        return (
            f"model {self.model_ip}:{self.model_port}, {len(self.github)} GitHub ranges, "
            f"{len(self.allowed)} allowed addresses, DNS via {', '.join(self.resolvers)}"
        )


# ---------------------------------------------------------------- the inputs


def _address(value: str, what: str) -> Address:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise EgressError(f"{what} is not an IP address: {value!r}") from error


def _network(value: str, what: str) -> Network:
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as error:
        raise EgressError(f"{what} is not an address range: {value!r}") from error


def resolvers(resolv_conf: Path = RESOLV_CONF) -> tuple[str, ...]:
    """The nameservers docker wrote for this container: the only DNS a run gets."""
    try:
        text = resolv_conf.read_text(encoding="utf-8")
    except OSError as error:
        raise EgressError(f"cannot read {resolv_conf}: {error}") from error

    found: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            found.append(str(_address(parts[1], f"nameserver in {resolv_conf}")))
    if not found:
        raise EgressError(f"no nameserver in {resolv_conf}; GitHub could not be resolved")
    return tuple(dict.fromkeys(found))


def github_ranges(
    token: str,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> tuple[str, ...]:
    """GitHub's address ranges for git, the API and the site, from `/meta`.

    Fetched per run rather than baked in: the ranges change, and a stale list
    is a clone that fails with a reset and no explanation.
    """
    try:
        meta = json.loads(gh.api("/meta", token, env=env, run=run))
    except _proc.CommandError as error:
        raise EgressError(f"could not fetch GitHub's address ranges: {error}") from error
    except json.JSONDecodeError as error:
        raise EgressError(f"GitHub's /meta was not JSON: {error}") from error

    ranges: list[str] = []
    for key in GITHUB_RANGES:
        values = meta.get(key) if isinstance(meta, dict) else None
        if not isinstance(values, list) or not values:
            raise EgressError(f"GitHub's /meta has no {key!r} ranges")
        ranges += [str(_network(str(value), f"/meta {key} range")) for value in values]
    return tuple(dict.fromkeys(ranges))


def allowed_hosts(spec: str | None) -> tuple[str, ...]:
    """`EGRESS_ALLOW` as the host names it lists; unset or empty is none."""
    return tuple(name.strip() for name in (spec or "").split(",") if name.strip())


def system_resolve(name: str) -> list[str]:
    """Every address the container's resolver returns for `name`."""
    infos = socket.getaddrinfo(name, HTTPS, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def resolve_all(names: Iterable[str], *, resolve: Resolver = system_resolve) -> tuple[str, ...]:
    """The addresses behind the allowed hosts, or the name that has none.

    A name that does not resolve is an error rather than an empty allowance:
    the run would otherwise proceed and fail later, inside a tool call, with a
    reset that names nothing.
    """
    addresses: list[str] = []
    for name in names:
        try:
            found = list(resolve(name))
        except OSError as error:
            raise EgressError(f"cannot resolve EGRESS_ALLOW host {name}: {error}") from error
        if not found:
            raise EgressError(f"EGRESS_ALLOW host {name} resolved to no address")
        addresses += [str(_address(item, f"address of {name}")) for item in found]
    return tuple(dict.fromkeys(addresses))


def policy_for(
    token: str,
    *,
    model_ip: str,
    model_port: int,
    allow: str | None,
    resolv_conf: Path = RESOLV_CONF,
    resolve: Resolver = system_resolve,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> Policy:
    """Everything one run may reach, gathered while the network is still open."""
    return Policy(
        model_ip=str(_address(model_ip, "MODEL_IP")),
        model_port=model_port,
        resolvers=resolvers(resolv_conf),
        github=github_ranges(token, env=env, run=run),
        allowed=resolve_all(allowed_hosts(allow), resolve=resolve),
    )


# --------------------------------------------------------------- the ruleset


def _family(value: str) -> str:
    """The nft match keyword for an address or range: `ip` or `ip6`."""
    return "ip" if _network(value, "address").version == 4 else "ip6"


def _set(name: str, kind: str, members: list[str]) -> str:
    return (
        f"    set {name} {{ type {kind}; flags interval; elements = {{ {', '.join(members)} }} }}"
    )


def ruleset(policy: Policy) -> str:
    """The nftables ruleset, as the text `nft -f` reads.

    One `inet` table covers both address families. A set is emitted only when
    it has members: nft rejects an empty element list, and a container without
    IPv6 would otherwise fail to start.
    """
    https4 = [item for item in (*policy.github, *policy.allowed) if _family(item) == "ip"]
    https6 = [item for item in (*policy.github, *policy.allowed) if _family(item) == "ip6"]

    lines = ["table inet egress {"]
    if https4:
        lines.append(_set("https4", "ipv4_addr", https4))
    if https6:
        lines.append(_set("https6", "ipv6_addr", https6))
    lines += [
        "    chain output {",
        "        type filter hook output priority filter; policy drop;",
        '        oif "lo" accept',
    ]
    for resolver in policy.resolvers:
        family = _family(resolver)
        lines.append(f"        {family} daddr {resolver} udp dport {DNS} accept")
        lines.append(f"        {family} daddr {resolver} tcp dport {DNS} accept")
    lines.append(
        f"        {_family(policy.model_ip)} daddr {policy.model_ip} "
        f"tcp dport {policy.model_port} accept"
    )
    if https4:
        lines.append(f"        ip daddr @https4 tcp dport {HTTPS} accept")
    if https6:
        lines.append(f"        ip6 daddr @https6 tcp dport {HTTPS} accept")
    lines += [
        # A reset fails a blocked connection at once; the plain reject covers
        # UDP and anything else with an ICMP unreachable.
        "        meta l4proto tcp reject with tcp reset",
        "        reject",
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def apply(text: str, *, run: _proc.Runner = _proc.run) -> None:
    """Install the ruleset in this network namespace, or refuse to continue.

    `nft -f` reads a file rather than an argument so the ruleset arrives
    verbatim, and the file is gone before any unprivileged process exists.
    """
    with tempfile.NamedTemporaryFile("w", prefix="egress-", suffix=".nft", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        try:
            run(["nft", "-f", f.name], check=True)
        except (_proc.CommandError, FileNotFoundError) as error:
            raise EgressError(f"could not install the egress policy: {error}") from error
