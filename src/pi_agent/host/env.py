"""Read `.env` without executing it.

`source`-ing a `.env` runs it as shell. Parsing it instead is what stops a
`.env` from being a script. The grammar is `python-dotenv`'s, not this
project's: it is the one every other dotenv tool implements, and a second
implementation of it would be a liability.

What this module adds is the half python-dotenv deliberately does not do. It
logs a warning and drops a line it cannot parse, and it accepts names no shell
would give back (`2FOO`, `a-b`); a half-loaded `.env` is exactly the silent
surprise this module exists to prevent. So every unparseable line, every bare
`KEY` with no value, and every illegal name is collected and raised together as
one `InvalidEnvFile` -- a broken file takes one run to fix, not one run per
broken line.

Nothing is expanded. `dotenv.parser` performs no `${VAR}` interpolation at all,
so `$(whoami)`, backticks and `${HOME}` survive as the literal characters
written. Nothing here or downstream evaluates them -- `_proc.Runner` never uses
a shell, `docker.build_argv` returns an argv list, and `docker.container_env`
forwards a closed set of names -- but nor are they refused by name and line
number: that refusal would have to know whether a value was single-quoted, and
that fact does not survive the parse.

Precedence is the opposite of `source`: the ambient environment wins, so a
one-off override such as `SESSIONS_DIR=/tmp/x pi-agent 12` works even when
`.env` also sets it, which is the usual dotenv convention.
"""

from __future__ import annotations

import io
import re
from collections.abc import MutableMapping
from pathlib import Path

from dotenv.parser import Binding, parse_stream

# python-dotenv takes any run of non-space, non-`=`, non-`#` characters as a
# name, so `2FOO=1` and `a-b=1` are bindings to it. Neither is a variable a
# shell or `docker run -e` hands back, so they are still refused here.
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class InvalidEnvFile(ValueError):
    """The env file is missing, or lines in it are not `KEY=value`."""


def _lineno(binding: Binding) -> int:
    """The line the binding actually starts on.

    `dotenv` marks a binding *before* skipping the blank lines in front of it,
    so `original.line` is where the previous binding ended. Adding back the
    newlines it then skipped is the difference between naming the broken line
    and naming the last empty one above it.
    """
    text = binding.original.string
    skipped = text[: len(text) - len(text.lstrip())]
    return binding.original.line + skipped.count("\n")


def parse(text: str, *, source: str = "<env>") -> dict[str, str]:
    """Parse `KEY=value` lines, naming every line that is not one."""
    values: dict[str, str] = {}
    problems: list[str] = []

    for binding in parse_stream(io.StringIO(text)):
        # Bound to locals so both type checkers narrow them on the branches.
        key, value = binding.key, binding.value
        lineno = _lineno(binding)

        # `error` first: a line dotenv rejected also has no key.
        if binding.error:
            broken = binding.original.string.strip()
            problems.append(f"{source}:{lineno}: could not parse: {broken}")
        elif key is None:
            continue  # a blank line, or a comment
        elif value is None:
            problems.append(f"{source}:{lineno}: expected KEY=value, got: {key}")
        elif not KEY.match(key):
            problems.append(f"{source}:{lineno}: not a valid variable name: {key!r}")
        else:
            values[key] = value

    if problems:
        raise InvalidEnvFile("\n".join(problems))

    return values


def load(path: Path, environ: MutableMapping[str, str]) -> dict[str, str]:
    """Parse `path` and fill in anything `environ` does not already set."""
    if not path.is_file():
        raise InvalidEnvFile(f"no .env found at {path} — copy .env.example and fill it in")

    values = parse(path.read_text(encoding="utf-8"), source=str(path))
    for key, value in values.items():
        environ.setdefault(key, value)
    return values
