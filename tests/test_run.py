"""Tests for the host CLI.

The CLI's last act is to exec docker. Injecting the exec makes the whole host
path assertable without starting a container, and `--dry-run` makes it
inspectable by a human too.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from pi_agent import _log, _piconfig
from pi_agent.host import env, run

# A complete .env: nothing the CLI reads has a default, so a fixture
# that omits one variable tests the error path rather than the happy one.
ENV = """\
GITHUB_REPOSITORY=example/agent-demo
GITHUB_APP_ID=4707632
GITHUB_APP_PRIVATE_KEY_FILE=./secrets/bot.pem
IMAGE=pi-issue-to-pr:local
SESSIONS_DIR=./sessions
AGENT=issue-to-pr
CONTAINER_MEMORY=4g
CONTAINER_CPUS=4
CONTAINER_PIDS_LIMIT=500
RUN_TIMEOUT=3600
MODEL_HOST=model-box.example.ts.net
MODEL_PORT=8080
MODEL_IP=100.64.0.1
MODEL_PROVIDER=llamacpp
MODEL_API=openai-completions
MODEL_API_KEY=no-api-key
MODEL_SUPPORTS_DEVELOPER_ROLE=false
MODEL_ID=DeepSeek-V4-Flash-0731-UD-IQ2_M
MODEL_NAME="DeepSeek V4 Flash 0731 (GB10)"
MODEL_CONTEXT_WINDOW=262144
MODEL_REASONING=true
THINKING_LEVEL=high
"""

ENV_NAMES = tuple(line.partition("=")[0] for line in ENV.splitlines() if line)


class Execed(Exception):
    """Raised by the fake exec so a test can see what would have run."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = list(argv)
        super().__init__(" ".join(argv))


def fake_exec(program: str, argv: Sequence[str]) -> NoReturn:
    raise Execed(argv)


@pytest.fixture
def reported(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """The CLI's stderr, as a stream a test can read.

    `run.LOG` was built with the real stderr, so pytest's capture fixtures
    never see it -- the same reason `_log` takes its stream as a parameter.
    """
    stream = io.StringIO()
    monkeypatch.setattr(run, "LOG", _log.Logger("run", stream))
    return stream


def image_is_present(image: str) -> bool:
    """Stand in for the daemon lookup `main` does before it execs."""
    return True


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo-shaped directory with a usable .env and key."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "bot.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (tmp_path / ".env").write_text(ENV)
    # A test that execs rather than `--dry-run`s reaches the image lookup, which
    # would shell out to a docker daemon -- and no test may touch one.
    monkeypatch.setattr(run.docker, "image_exists", image_is_present)
    # The ambient environment wins over the file, so a variable left over from
    # the developer's own shell would quietly decide what these tests assert.
    for name in (*ENV_NAMES, "DEV", "GITHUB_APP_INSTALLATION_ID", "EGRESS_ALLOW"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# ---------------------------------------------------------------- arguments


@pytest.mark.parametrize("target", ["12", "#12"])
def test_an_issue_number_is_accepted_with_or_without_a_hash(target: str) -> None:
    assert run.parse_args([target]).target == "12"


def test_shell_is_the_only_other_positional() -> None:
    assert run.parse_args(["shell"]).target == "shell"


@pytest.mark.parametrize("target", ["twelve", "12a", "issue-12"])
def test_a_non_number_fails_before_anything_is_built(target: str) -> None:
    """A typo should cost a second, not a docker build."""
    with pytest.raises(SystemExit):
        run.parse_args([target])


def test_a_repository_may_be_named_before_the_issue() -> None:
    """One image and one credential serve every repository the App is on."""
    args = run.parse_args(["example/widgets", "#12"])

    assert args.repository == "example/widgets"
    assert args.target == "12"


def test_a_repository_may_be_named_before_a_shell() -> None:
    args = run.parse_args(["example/widgets", "shell"])

    assert (args.repository, args.target) == ("example/widgets", "shell")


def test_naming_only_a_repository_says_the_issue_is_missing() -> None:
    """Without the hint, argparse reports `owner/repo` as a bad issue number."""
    with pytest.raises(SystemExit):
        run.parse_args(["example/widgets"])


def test_a_repository_defaults_to_none_so_the_env_file_decides() -> None:
    assert run.parse_args(["12"]).repository is None


# ------------------------------------------------------------------ anchoring


def test_relative_paths_resolve_against_the_env_file_not_the_cwd(
    project: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell wrapper gets this anchor from `$0`; a console script has no equivalent.

    Without it, `GITHUB_APP_PRIVATE_KEY_FILE=./secrets/...` breaks the moment
    anyone runs the CLI from a subdirectory.
    """
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))

    with pytest.raises(Execed) as execed:
        run.main(["12", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert f"{project / 'secrets' / 'bot.pem'}:/run/secrets/github-app.pem:ro" in execed.value.argv


def test_the_sessions_directory_is_created(project: Path) -> None:
    with pytest.raises(Execed):
        run.main(["12", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert (project / "sessions" / "example" / "agent-demo").is_dir()


# ------------------------------------------------------------- the repository


def test_the_named_repository_is_what_the_container_works_on(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: a run targets a repository without editing .env."""
    run.main(
        ["example/widgets", "12", "--dry-run", "--env-file", str(project / ".env")],
        exec_fn=fake_exec,
    )

    printed = capsys.readouterr().out
    assert "GITHUB_REPOSITORY=example/widgets" in printed
    assert "GITHUB_REPOSITORY=example/agent-demo" not in printed


def test_without_a_named_repository_the_env_file_decides(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "GITHUB_REPOSITORY=example/agent-demo" in capsys.readouterr().out


def test_a_named_repository_needs_no_repository_in_the_env_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .env can configure the credential and the model and name no repository."""
    kept = [line for line in ENV.splitlines() if not line.startswith("GITHUB_REPOSITORY=")]
    (project / ".env").write_text("\n".join(kept) + "\n")

    run.main(
        ["example/widgets", "12", "--dry-run", "--env-file", str(project / ".env")],
        exec_fn=fake_exec,
    )

    assert "GITHUB_REPOSITORY=example/widgets" in capsys.readouterr().out


@pytest.mark.parametrize(
    "repository", ["widgets", "example/widgets/extra", "../etc", "example/", ".x/y"]
)
def test_a_repository_that_is_not_owner_slash_repo_stops_the_run(
    project: Path, reported: io.StringIO, repository: str
) -> None:
    """It names a clone target and a transcript directory; a path-shaped value
    would write somewhere nobody named."""
    with pytest.raises(SystemExit):
        run.main(
            [repository, "12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec
        )

    assert repository in reported.getvalue()


def test_a_bad_repository_in_the_env_file_is_caught_too(
    project: Path, reported: io.StringIO
) -> None:
    """The check belongs to the value used, not to the way it was supplied."""
    (project / ".env").write_text(
        ENV.replace("GITHUB_REPOSITORY=example/agent-demo", "GITHUB_REPOSITORY=../etc")
    )

    with pytest.raises(SystemExit):
        run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "owner/repo" in reported.getvalue()


def test_transcripts_are_filed_under_the_repository_they_came_from(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise issue #12 of two repositories shares one `issue-12` directory."""
    run.main(
        ["example/widgets", "12", "--dry-run", "--env-file", str(project / ".env")],
        exec_fn=fake_exec,
    )

    sessions = project / "sessions" / "example" / "widgets"
    assert sessions.is_dir()
    assert f"{sessions}:/work/sessions" in capsys.readouterr().out


def test_a_pinned_installation_id_does_not_follow_a_run_onto_another_repository(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It was looked up for the configured repository. Reused, it mints a token
    for the wrong installation and fails a minute later as an unreadable repo."""
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "876")

    run.main(
        ["example/widgets", "12", "--dry-run", "--env-file", str(project / ".env")],
        exec_fn=fake_exec,
    )

    assert "GITHUB_APP_INSTALLATION_ID" not in capsys.readouterr().out


def test_a_pinned_installation_id_is_kept_for_the_repository_it_was_pinned_for(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "876")

    run.main(
        ["example/agent-demo", "12", "--dry-run", "--env-file", str(project / ".env")],
        exec_fn=fake_exec,
    )

    assert "GITHUB_APP_INSTALLATION_ID=876" in capsys.readouterr().out


# -------------------------------------------------------------------- dry run


def test_dry_run_prints_the_command_and_execs_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    printed = capsys.readouterr().out
    assert status == 0
    assert printed.startswith("docker run --rm --init")
    assert printed.rstrip().endswith("pi-issue-to-pr:local 12")


def test_dry_run_does_not_build_the_image(project: Path) -> None:
    """It must be usable with no docker daemon at all."""

    def explode(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("docker was consulted during --dry-run")

    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=explode)


# -------------------------------------------------------------------- audit


def test_a_run_records_its_command_beside_the_transcript_with_the_key_masked(
    project: Path, reported: io.StringIO
) -> None:
    """A run that caused damage can be matched to the exact image, mounts and
    limits it ran with; the model key is the one thing not worth keeping."""
    with pytest.raises(Execed) as execed:
        run.main(["12", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    record = project / "sessions" / "example" / "agent-demo" / "issue-12" / "run-command.txt"
    text = record.read_text()
    assert text.startswith("# 20")
    assert "docker run --rm --init" in text
    assert "MODEL_API_KEY=REDACTED" in text
    assert "no-api-key" not in text
    assert "no-api-key" not in reported.getvalue()
    # What was recorded is what ran, key aside.
    assert "MODEL_API_KEY=no-api-key" in " ".join(execed.value.argv)
    assert f"run command recorded: {record}" in reported.getvalue()


def test_attempts_at_one_issue_accumulate_in_the_record(project: Path) -> None:
    """Transcripts accumulate per issue; the record must not overwrite itself."""
    for _ in range(2):
        with pytest.raises(Execed):
            run.main(["12", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    record = project / "sessions" / "example" / "agent-demo" / "issue-12" / "run-command.txt"
    assert record.read_text().count("docker run ") == 2


def test_dry_run_records_nothing(project: Path) -> None:
    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert not list((project / "sessions").rglob("run-command.txt"))


def test_a_shell_is_logged_but_has_no_issue_to_record_under(
    project: Path, reported: io.StringIO
) -> None:
    with pytest.raises(Execed):
        run.main(["shell", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "running: docker run" in reported.getvalue()
    assert not list((project / "sessions").rglob("run-command.txt"))


def test_the_record_is_timestamped_per_entry(tmp_path: Path) -> None:
    when = datetime(2026, 9, 8, 18, 0, 0, tzinfo=UTC)

    record = run.record_run_command(tmp_path / "issue-12", "docker run x", now=when)

    assert record.read_text() == "# 2026-09-08T18:00:00+00:00\ndocker run x\n"


# ----------------------------------------------------------------- the .env


def test_the_ambient_environment_beats_the_env_file(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE", "some-other:tag")

    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "some-other:tag" in capsys.readouterr().out


def test_a_missing_env_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run.main(["12", "--env-file", str(tmp_path / "absent")], exec_fn=fake_exec)


def test_dev_mounts_the_working_copy(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEV", "1")

    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert f"{project / '.pi' / 'agents'}:/opt/agent/agents:ro" in capsys.readouterr().out


# ------------------------------------------------------------------ defaults


@pytest.mark.parametrize(
    "omitted",
    [
        "IMAGE",
        "SESSIONS_DIR",
        "AGENT",
        "GITHUB_REPOSITORY",
        "CONTAINER_MEMORY",
        "CONTAINER_CPUS",
        "CONTAINER_PIDS_LIMIT",
        "RUN_TIMEOUT",
    ],
)
def test_an_unset_variable_stops_the_run_and_names_itself(
    project: Path, reported: io.StringIO, omitted: str
) -> None:
    """No fallbacks: a value nobody set must not become a value nobody chose."""
    kept = [line for line in ENV.splitlines() if not line.startswith(f"{omitted}=")]
    (project / ".env").write_text("\n".join(kept) + "\n")

    with pytest.raises(SystemExit):
        run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert omitted in reported.getvalue()


def test_an_unset_model_variable_is_reported_with_the_others(
    project: Path, reported: io.StringIO
) -> None:
    """Eleven variables fixed one failed run at a time is how people go back to
    hardcoding, so the missing ones are named together."""
    kept = [
        line
        for line in ENV.splitlines()
        if not line.startswith(("MODEL_PORT=", "MODEL_CONTEXT_WINDOW="))
    ]
    (project / ".env").write_text("\n".join(kept) + "\n")

    with pytest.raises(SystemExit):
        run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "MODEL_PORT" in reported.getvalue()
    assert "MODEL_CONTEXT_WINDOW" in reported.getvalue()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CONTAINER_MEMORY", "0"),
        ("CONTAINER_MEMORY", "4 GB"),
        ("CONTAINER_MEMORY", "lots"),
        ("CONTAINER_CPUS", "0"),
        ("CONTAINER_CPUS", "0.0"),
        ("CONTAINER_CPUS", "four"),
        ("CONTAINER_PIDS_LIMIT", "0"),
        ("CONTAINER_PIDS_LIMIT", "-1"),
        ("CONTAINER_PIDS_LIMIT", "1.5"),
        ("RUN_TIMEOUT", "0"),
        ("RUN_TIMEOUT", "1h"),
    ],
)
def test_a_limit_that_is_zero_or_malformed_stops_the_run_and_names_itself(
    project: Path, reported: io.StringIO, name: str, value: str
) -> None:
    """To docker, a zero limit is no limit. A run that asked for a bound must
    not silently get none, and a typo must be reported against its variable."""
    lines = [line for line in ENV.splitlines() if not line.startswith(f"{name}=")]
    (project / ".env").write_text("\n".join([*lines, f"{name}={value}"]) + "\n")

    with pytest.raises(SystemExit):
        run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert name in reported.getvalue()


def test_the_limits_reach_the_docker_command(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "--memory 4g --memory-swap 4g --cpus 4 --pids-limit 500" in capsys.readouterr().out


def test_the_resolved_model_address_reaches_the_container(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The container's egress rule names it; a run must not have to guess."""
    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "-e MODEL_IP=100.64.0.1" in capsys.readouterr().out


def test_extra_egress_is_forwarded_when_configured(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EGRESS_ALLOW", "pypi.org, files.pythonhosted.org")

    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "EGRESS_ALLOW=pypi.org, files.pythonhosted.org" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["https://pypi.org", "pypi.org/simple", "10.0.0.0/8", "a b"])
def test_an_egress_entry_that_is_not_a_host_name_stops_the_run(
    project: Path, reported: io.StringIO, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A URL or a range would fail to resolve inside the container, after a
    token mint; a host name is the only thing the allowlist resolves."""
    monkeypatch.setenv("EGRESS_ALLOW", value)

    with pytest.raises(SystemExit):
        run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    assert "EGRESS_ALLOW" in reported.getvalue()


def test_the_model_configuration_reaches_the_container(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.env` is the only place a model is chosen; this is the whole path."""
    run.main(["12", "--dry-run", "--env-file", str(project / ".env")], exec_fn=fake_exec)

    printed = capsys.readouterr().out
    configured = _piconfig.Config.from_environ(env.parse(ENV))
    for name, value in _piconfig.as_environ(configured).items():
        assert f"{name}={value}" in printed


def test_the_example_env_names_everything_the_cli_requires() -> None:
    """A .env.example that is missing a variable is a broken first run."""
    example = Path(__file__).parent.parent / ".env.example"
    declared = {
        line.partition("=")[0]
        for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert declared >= {
        "IMAGE",
        "SESSIONS_DIR",
        "AGENT",
        "CONTAINER_MEMORY",
        "CONTAINER_CPUS",
        "CONTAINER_PIDS_LIMIT",
        "RUN_TIMEOUT",
        *_piconfig.VARS.values(),
    }
