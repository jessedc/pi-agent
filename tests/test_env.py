"""Tests for reading `.env` without executing it.

`set -a; source .env` accepted anything shell accepts. The replacement parses
python-dotenv's grammar and expands nothing, so a value that would have needed a
shell survives as the literal characters written -- and every line that is not
`KEY=value` is named, with its number, all of them at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent.host import env

# ------------------------------------------------------------------ accepted


def test_plain_assignments() -> None:
    assert env.parse("GITHUB_REPOSITORY=example/agent-demo\n") == {
        "GITHUB_REPOSITORY": "example/agent-demo"
    }


def test_blank_lines_and_comments_are_skipped() -> None:
    assert env.parse("# a comment\n\n  \nA=1\n") == {"A": "1"}


def test_an_export_prefix_is_allowed() -> None:
    assert env.parse("export A=1\n") == {"A": "1"}


def test_the_value_may_contain_equals_signs() -> None:
    assert env.parse("A=b=c\n") == {"A": "b=c"}


@pytest.mark.parametrize("line", ["A='b'", 'A="b"'])
def test_surrounding_quotes_are_stripped(line: str) -> None:
    assert env.parse(line) == {"A": "b"}


def test_an_inline_comment_needs_whitespace_before_it() -> None:
    """Exactly bash's rule, so a .env written for `source` keeps its meaning.

    dotenv's `re.sub(r"\\s+#.*", "", part)` is that rule. This is the pin that
    catches the grammar drifting underneath us.
    """
    assert env.parse("A=b #note\n") == {"A": "b"}
    assert env.parse("A=b\t#note\n") == {"A": "b"}
    assert env.parse("A=b#c\n") == {"A": "b#c"}


@pytest.mark.parametrize("line", ['A="b # c"', "A='b # c'"])
def test_a_hash_inside_quotes_is_part_of_the_value(line: str) -> None:
    assert env.parse(line) == {"A": "b # c"}


def test_a_comment_may_follow_a_quoted_value() -> None:
    assert env.parse('A="b # c" # outside') == {"A": "b # c"}


def test_a_comment_may_abut_a_closing_quote() -> None:
    """dotenv's comment pattern makes the whitespace before `#` optional after
    a closing quote. The whitespace rule still applies to unquoted values, which
    `test_an_inline_comment_needs_whitespace_before_it` pins.
    """
    assert env.parse('A="b"#comment') == {"A": "b"}


def test_a_single_quoted_value_survives_an_inline_hash() -> None:
    """Single quotes are the escape hatch for a value containing ` #`."""
    assert env.parse("A='$HOME # my home'") == {"A": "$HOME # my home"}


def test_a_single_quoted_value_is_taken_literally() -> None:
    assert env.parse("A='$literal'") == {"A": "$literal"}


def test_a_double_quoted_value_may_contain_an_escaped_quote() -> None:
    assert env.parse(r'A="say \"hi\""') == {"A": 'say "hi"'}


def test_a_double_quoted_escape_is_decoded() -> None:
    """Double quotes decode C escapes, as every other dotenv tool does.

    So `A="C:\\new\\path"` is `C:`, a newline, then `ew\\path`, not a Windows
    path. A path like that must be single-quoted.
    """
    assert env.parse(r'A="a\nb"') == {"A": "a\nb"}
    assert env.parse(r'A="C:\path"') == {"A": r"C:\path"}  # `\p` is not an escape
    assert env.parse(r"A='C:\new\path'") == {"A": r"C:\new\path"}


def test_shell_syntax_reaches_the_value_untouched() -> None:
    """Shell syntax is stored literally, and not refused either.

    `.env` is never executed, and nothing downstream uses a shell. A value that
    only means something after expansion is not refused by name and line number
    either: that check would have to know whether a value was single-quoted, and
    dotenv's unquoting discards the fact. Expansion is not merely disabled here
    -- `dotenv.parser` has no interpolation pass at all.
    """
    assert env.parse("A=$(whoami)") == {"A": "$(whoami)"}
    assert env.parse("A=`whoami`") == {"A": "`whoami`"}
    assert env.parse("A=${HOME}/x") == {"A": "${HOME}/x"}
    assert env.parse("A=$HOME/x") == {"A": "$HOME/x"}


def test_braced_expansion_is_not_interpolated() -> None:
    """Guards the entry point, not just the output.

    `dotenv_values` would resolve `${A}` against the values already parsed and
    the ambient environment. Swapping `parse_stream` for it to "simplify" would
    change what reaches the container, silently, and this is what would fail.
    """
    assert env.parse("A=1\nB=${A}\n")["B"] == "${A}"


def test_crlf_endings_are_handled() -> None:
    assert env.parse("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}


def test_the_shipped_example_parses() -> None:
    """Catches drift between .env.example and this parser."""
    example = Path(__file__).parent.parent / ".env.example"

    values = env.parse(example.read_text(encoding="utf-8"), source=str(example))

    assert values["GITHUB_REPOSITORY"]
    assert values["GITHUB_APP_ID"]
    assert values["GITHUB_APP_PRIVATE_KEY_FILE"]


# ------------------------------------------------------------------ refused


def test_a_line_with_no_equals_is_refused_with_its_number() -> None:
    """dotenv returns `{"nonsense": None}` for this and logs nothing."""
    with pytest.raises(env.InvalidEnvFile, match=r":2: expected KEY=value"):
        env.parse("A=1\nnonsense\n")


def test_an_invalid_variable_name_is_refused() -> None:
    """dotenv accepts `2FOO` as a key; `docker run -e` would not."""
    with pytest.raises(env.InvalidEnvFile, match="not a valid variable name"):
        env.parse("2FOO=1")


@pytest.mark.parametrize("line", ['A="b', "A='b"])
def test_an_unmatched_quote_is_refused(line: str) -> None:
    """Still refused, with its line number; dotenv reports only *that* it failed."""
    with pytest.raises(env.InvalidEnvFile, match=r":1: could not parse"):
        env.parse(line)


def test_text_after_a_closing_quote_is_refused() -> None:
    with pytest.raises(env.InvalidEnvFile, match=r":1: could not parse"):
        env.parse('A="b" trailing')


def test_every_bad_line_is_reported_at_once() -> None:
    """A broken file takes one run to fix, not one run per broken line.

    The same rule `_piconfig.from_environ` follows for missing variables.
    """
    with pytest.raises(env.InvalidEnvFile) as caught:
        env.parse('A="b\nnonsense\n2FOO=1\n')

    message = str(caught.value)
    assert ":1: could not parse" in message
    assert ":2: expected KEY=value" in message
    assert ":3: not a valid variable name" in message


def test_a_blank_line_does_not_shift_the_reported_line_number() -> None:
    """dotenv marks a binding before the blank lines it then skips.

    Without correcting for them the error names the last empty line above the
    broken one, which is the kind of hint that costs an hour.
    """
    with pytest.raises(env.InvalidEnvFile, match=r":4: could not parse"):
        env.parse('A=1\n\n\nB="b')


# ------------------------------------------------------------------- load


def test_load_fills_in_what_the_environment_lacks(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=from_file\n")
    environ: dict[str, str] = {}

    env.load(path, environ)

    assert environ["A"] == "from_file"


def test_the_ambient_environment_wins(tmp_path: Path) -> None:
    """The opposite of `set -a; source`, where the file wins.

    `SESSIONS_DIR=/tmp/x pi-agent 12` is a one-off override, and it works even
    when .env also sets the variable.
    """
    path = tmp_path / ".env"
    path.write_text("SESSIONS_DIR=/from/file\n")
    environ = {"SESSIONS_DIR": "/from/the/command/line"}

    env.load(path, environ)

    assert environ["SESSIONS_DIR"] == "/from/the/command/line"


def test_a_missing_file_says_what_to_do(tmp_path: Path) -> None:
    """dotenv treats a missing file as an empty one; this must not."""
    with pytest.raises(env.InvalidEnvFile, match="copy .env.example"):
        env.load(tmp_path / "absent", {})
