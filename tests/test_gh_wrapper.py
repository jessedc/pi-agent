"""Tests for the `gh` wrapper the image puts ahead of /usr/bin/gh.

It is the second consumer of the token file (git's credential helper is the
first), and the one the model's `gh pr create` goes through. Proved against a
real `sh`, with the real binary's path swapped for a recorder, because the
property that matters -- the token reaches gh's environment and no other --
is a property of the shell script, not of gh.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / "image" / "bin" / "gh"


def test_the_wrapper_execs_the_real_binary_by_absolute_path() -> None:
    """A PATH lookup from inside the wrapper would find the wrapper."""
    source = WRAPPER.read_text(encoding="utf-8")

    assert source.startswith("#!/bin/sh\n")
    assert 'exec /usr/bin/gh "$@"' in source
    assert "set -eu" in source


@pytest.fixture
def wrapper_with_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """The wrapper, pointed at a stand-in gh that records its environment."""
    seen = tmp_path / "seen"
    recorder = tmp_path / "real-gh"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}\nenv >> {seen}\n')
    recorder.chmod(0o755)

    wrapper = tmp_path / "gh"
    wrapper.write_text(WRAPPER.read_text(encoding="utf-8").replace("/usr/bin/gh", str(recorder)))
    wrapper.chmod(0o755)
    return wrapper, seen


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not installed")
def test_the_token_reaches_gh_from_the_file_and_the_arguments_pass_through(
    tmp_path: Path, wrapper_with_recorder: tuple[Path, Path]
) -> None:
    wrapper, seen = wrapper_with_recorder
    token_file = tmp_path / ".gh-token"
    token_file.write_text("ghs_from_the_file")

    subprocess.run(
        [str(wrapper), "pr", "create", "--draft"],
        env={"PATH": "/usr/bin:/bin", "GH_TOKEN_FILE": str(token_file)},
        check=True,
    )

    recorded = seen.read_text().splitlines()
    assert recorded[:3] == ["pr", "create", "--draft"]
    assert "GH_TOKEN=ghs_from_the_file" in recorded


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not installed")
def test_a_token_already_in_the_environment_wins(
    tmp_path: Path, wrapper_with_recorder: tuple[Path, Path]
) -> None:
    """The bootstrap's App-JWT calls set a placeholder GH_TOKEN and put the real
    authorization in a header; the file does not exist yet at that point."""
    wrapper, seen = wrapper_with_recorder

    subprocess.run(
        [str(wrapper), "api", "/app"],
        env={
            "PATH": "/usr/bin:/bin",
            "GH_TOKEN": "unused-placeholder",
            "GH_TOKEN_FILE": str(tmp_path / "absent"),
        },
        check=True,
    )

    assert "GH_TOKEN=unused-placeholder" in seen.read_text().splitlines()


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not installed")
def test_without_a_file_or_a_token_gh_is_still_run_and_refuses_on_its_own(
    wrapper_with_recorder: tuple[Path, Path],
) -> None:
    """The wrapper adds a credential when it has one; it never invents one."""
    wrapper, seen = wrapper_with_recorder

    subprocess.run([str(wrapper), "--version"], env={"PATH": "/usr/bin:/bin"}, check=True)

    assert not any(line.startswith("GH_TOKEN") for line in seen.read_text().splitlines())
