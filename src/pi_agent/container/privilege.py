"""Separate credential bootstrap from the uid that runs model-controlled tools.

Docker Desktop does not reliably enforce the mode of a bind-mounted file, so
the private key is protected by a root-only container directory instead. The
bootstrap reads it as root, then irreversibly becomes the unprivileged account
before any model process can start.
"""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path


class PrivilegeDropError(RuntimeError):
    """The bootstrap could not enter the model's unprivileged account."""


@dataclass(frozen=True, slots=True)
class Account:
    """The identity and home directory a descendant process must inherit."""

    name: str
    uid: int
    gid: int
    home: Path


PI = Account(name="pi", uid=1001, gid=1001, home=Path("/home/pi"))


def drop_to(
    account: Account,
    environ: MutableMapping[str, str],
    *,
    setgroups: Callable[[list[int]], None] = os.setgroups,
    setgid: Callable[[int], None] = os.setgid,
    setuid: Callable[[int], None] = os.setuid,
) -> None:
    """Irreversibly enter `account` and update its process environment."""
    try:
        # Supplementary groups must go first: changing uid can remove the
        # capability needed to change them and silently leave root access.
        setgroups([])
        setgid(account.gid)
        setuid(account.uid)
    except OSError as error:
        raise PrivilegeDropError(f"could not drop privileges to {account.name}: {error}") from error

    # HOME is operational: git config --global must write below the dropped
    # account's home rather than root's now-inaccessible one.
    environ.update(
        {
            "HOME": str(account.home),
            "USER": account.name,
            "LOGNAME": account.name,
        }
    )
