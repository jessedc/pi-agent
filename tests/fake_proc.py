"""A recording stand-in for `pi_agent._proc.run`.

Every module that shells out takes a `Runner`, so a test can assert on the argv
that *would* have run -- with no daemon, no credential, and no network. This is
the whole point of the seam: none of the logic it replaces was reachable from a
test while it lived in bash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pi_agent import _proc


class FakeRunner:
    """Answers commands from a script, and records everything it was asked."""

    def __init__(
        self,
        responses: Sequence[tuple[str, str]] | None = None,
        *,
        failures: Sequence[str] = (),
        default: str = "",
    ) -> None:
        # (needle, stdout) pairs, matched in order against the joined argv.
        self.responses = list(responses or [])
        # Joined-argv needles that should exit non-zero.
        self.failures = list(failures)
        self.default = default
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> _proc.Result:
        self.calls.append(tuple(argv))
        self.environments.append(dict(env) if env is not None else None)
        joined = " ".join(argv)

        for needle in self.failures:
            if needle in joined:
                result = _proc.Result(tuple(argv), 1, "", f"fake failure for {needle}")
                if check:
                    raise _proc.CommandError(result)
                return result

        for needle, stdout in self.responses:
            if needle in joined:
                return _proc.Result(tuple(argv), 0, stdout, "")

        return _proc.Result(tuple(argv), 0, self.default, "")

    # ------------------------------------------------------------- assertions

    @property
    def commands(self) -> list[str]:
        """Every call as a single string, for readable assertions."""
        return [" ".join(call) for call in self.calls]

    def matching(self, needle: str) -> list[tuple[str, ...]]:
        """Every recorded call whose joined argv contains `needle`."""
        return [call for call in self.calls if needle in " ".join(call)]
