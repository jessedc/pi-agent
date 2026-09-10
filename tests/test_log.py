"""Tests for the shared progress/failure reporting.

Two properties matter enough to pin: stdout stays clean (it carries payloads --
a credential as JSON, a docker command line, a transcript path), and colour
disappears when nothing is watching, because an audited container run has its
stderr redirected to a file.
"""

from __future__ import annotations

import io

import pytest

from pi_agent import _log


class FakeTTY(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


# ------------------------------------------------------------------- tagging


def test_log_tags_the_line_with_the_component() -> None:
    stream = io.StringIO()

    _log.Logger("token", stream).log("minting")

    assert stream.getvalue() == "[token] minting\n"


def test_log_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """stdout is the payload channel; a stray progress line would corrupt it."""
    _log.Logger("token", io.StringIO()).log("minting")

    assert capsys.readouterr().out == ""


# -------------------------------------------------------------------- colour


def test_colour_is_used_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = FakeTTY()

    _log.Logger("run", stream).log("hello")

    assert _log.CYAN in stream.getvalue()
    assert _log.RESET in stream.getvalue()


def test_colour_is_dropped_when_stderr_is_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The usual shape of an audited run: `docker logs > run.txt`."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()

    _log.Logger("run", stream).log("hello")

    assert "\033" not in stream.getvalue()


def test_no_colour_environment_variable_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    stream = FakeTTY()

    _log.Logger("run", stream).log("hello")

    assert "\033" not in stream.getvalue()


# ----------------------------------------------------------------------- die


def test_die_exits_non_zero() -> None:
    with pytest.raises(SystemExit) as exit_info:
        _log.Logger("token", io.StringIO()).die("no usable credential")

    assert exit_info.value.code == 1


def test_die_reports_the_reason() -> None:
    stream = io.StringIO()

    with pytest.raises(SystemExit):
        _log.Logger("token", stream).die("no usable credential")

    assert "no usable credential" in stream.getvalue()
    assert "[token]" in stream.getvalue()
