"""Tests for the one-way boundary between key bootstrap and model tools.

The tests inject the three privileged syscalls so they can prove ordering
without requiring the test runner itself to be root.
"""

from __future__ import annotations

import pytest

from pi_agent.container import privilege


def test_the_bootstrap_uid_is_not_the_model_uid() -> None:
    """The key boundary disappears if bootstrap and model share an identity."""
    assert privilege.PI.uid != 0
    assert privilege.PI.gid != 0


def test_groups_are_dropped_before_gid_and_uid() -> None:
    """Changing uid first can make supplementary root groups impossible to remove."""
    calls: list[tuple[str, object]] = []

    privilege.drop_to(
        privilege.PI,
        {},
        setgroups=lambda groups: calls.append(("groups", groups)),
        setgid=lambda gid: calls.append(("gid", gid)),
        setuid=lambda uid: calls.append(("uid", uid)),
    )

    assert calls == [("groups", []), ("gid", 1001), ("uid", 1001)]


def test_the_dropped_process_is_told_where_its_home_is() -> None:
    environ = {"HOME": "/root", "USER": "root", "LOGNAME": "root"}

    privilege.drop_to(
        privilege.PI,
        environ,
        setgroups=lambda _groups: None,
        setgid=lambda _gid: None,
        setuid=lambda _uid: None,
    )

    assert environ == {"HOME": "/home/pi", "USER": "pi", "LOGNAME": "pi"}


def test_a_failed_drop_is_a_named_error() -> None:
    def refuse(_uid: int) -> None:
        raise OSError("not permitted")

    with pytest.raises(privilege.PrivilegeDropError, match="could not drop privileges to pi"):
        privilege.drop_to(
            privilege.PI,
            {},
            setgroups=lambda _groups: None,
            setgid=lambda _gid: None,
            setuid=refuse,
        )
