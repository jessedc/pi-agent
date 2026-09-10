"""Progress and failure reporting, on stderr, tagged with a component name.

Every entry point writes progress to stderr and keeps stdout clean, because
stdout carries payloads: a credential as JSON, a rendered docker command, a
transcript path. A component tag (``[token]``, ``[bootstrap]``, ``[run]``) says
which stage of a run produced a line, which matters when the whole pipeline
logs into one ``docker logs`` stream.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import NoReturn, TextIO

CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


def use_colour(stream: TextIO) -> bool:
    """Report whether `stream` should carry ANSI colour.

    Honours the NO_COLOR convention, and stays plain when stderr is redirected
    to a file -- which is the usual shape of an audited container run.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


@dataclass(frozen=True, slots=True)
class Logger:
    """Writes tagged progress lines for one stage of a run."""

    component: str
    stream: TextIO = sys.stderr

    def _tag(self, colour: str) -> str:
        if use_colour(self.stream):
            return f"{colour}[{self.component}]{RESET}"
        return f"[{self.component}]"

    def log(self, message: str) -> None:
        """Write a progress line."""
        print(f"{self._tag(CYAN)} {message}", file=self.stream)

    def die(self, message: str) -> NoReturn:
        """Report a fatal problem and stop.

        NoReturn is load-bearing rather than decoration: it is what lets the
        type checkers narrow past a guard, e.g. that a key really is an RSA key
        after an isinstance check.
        """
        print(f"{self._tag(RED)} {message}", file=self.stream)
        raise SystemExit(1)
