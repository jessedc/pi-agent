"""The host CLI: build the image if needed, then run one issue.

    uv run pi-agent 12                  # work issue #12 of the configured repo
    uv run pi-agent example/widgets 12  # work issue #12 of that repository
    uv run pi-agent 12 --build          # rebuild the image first
    uv run pi-agent 12 --dry-run        # print the docker command and stop
    uv run pi-agent shell               # interactive shell, bot credential live
    DEV=1 uv run pi-agent 12            # use the working copy of the agent definition

The repository a run works on is named either on the command line or as
`GITHUB_REPOSITORY` in `.env`. One image and one credential serve every
repository the App is installed on, so which one a run targets is an argument
rather than a reconfiguration -- and transcripts are filed under the repository
they came from, so issue #12 of two projects cannot land in one directory.

The rest of the configuration comes from `.env` (see .env.example), and only
from there: this module holds no default for any of it. A default is a value
nobody chose that still ends up in a run -- the image tag it built last month, a
tailnet host that was decommissioned -- and it is discovered by reading source,
not configuration. An unset variable stops the run and names itself.

Relative paths in `.env` are resolved against the directory that file is in, not
the current one, so the CLI behaves the same from any directory.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from pi_agent import _log, _piconfig
from pi_agent.host import docker, env, tailnet

LOG = _log.Logger("run")

ISSUE = re.compile(r"^#?[0-9]+$")

# `owner/repo`, as GitHub spells it: no scheme, no `.git`, no third segment.
# Checked because this value is not only passed to the container -- it also names
# the directory transcripts are filed under, so a path-shaped value would write
# somewhere nobody named.
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Resource limits, in the units `docker run` takes. Checked here so a typo is
# reported against the variable that holds it, before a build, rather than by
# docker naming the flag it was passed as. Zero is refused by shape rather than
# by docker, because to docker a zero limit means no limit.
MEMORY = re.compile(r"^[1-9][0-9]*[bkmgBKMG]?$")
CPUS = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
PIDS = re.compile(r"^[1-9][0-9]*$")
SECONDS = re.compile(r"^[1-9][0-9]*$")

# One host name per entry of EGRESS_ALLOW. Resolved inside the container, where
# the answer is the one the run will get; shaped here, before a build.
HOST_NAME = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)

ExecFn = Callable[[str, Sequence[str]], NoReturn]

# The audit record: appended to, one entry per attempt, beside the transcripts
# those attempts leave. A run that caused damage can then be matched to the
# exact image, mounts, limits and egress list it ran with.
RUN_COMMAND_FILE = "run-command.txt"


def _exec(program: str, argv: Sequence[str]) -> NoReturn:
    """Replace this process with `program`. Injectable so tests can observe it."""
    os.execvp(program, list(argv))  # noqa: S606


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    A run always names its issue, and the only other accepted positional is
    `shell`. A repository may precede either; without one, the run targets the
    repository named in the env file. The two are never ambiguous -- an issue
    number has no slash in it. Validated before a docker build, so a typo costs
    a second.
    """
    parser = argparse.ArgumentParser(
        prog="pi-agent",
        description="Turn a GitHub issue into a draft pull request, in a container.",
    )
    parser.add_argument(
        "repository",
        nargs="?",
        help="owner/repo to work in (default: GITHUB_REPOSITORY from the env file)",
    )
    parser.add_argument("target", help="issue number to work, or 'shell' for a credential shell")
    parser.add_argument("--build", action="store_true", help="rebuild the image first")
    parser.add_argument("--dry-run", action="store_true", help="print the docker command and exit")
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"), help="configuration file (default: ./.env)"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.target != "shell" and not ISSUE.match(args.target):
        # A lone `owner/repo` is the likely mistake, and argparse would otherwise
        # report it as a bad issue number without saying what is missing.
        hint = " -- name the issue after the repository" if "/" in args.target else ""
        parser.error(f"issue must be a number, got: {args.target}{hint}")
    args.target = args.target.lstrip("#")
    return args


def resolve(root: Path, value: str) -> Path:
    """Resolve a configured path against the directory holding the env file."""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def session_path(sessions_dir: Path, repository: str) -> Path:
    """Where this repository's transcripts are filed: `<SESSIONS_DIR>/owner/repo`.

    The container writes `issue-<n>/` inside whatever it is given, so filing by
    repository happens here, on the host, where the repository is chosen. Without
    it, issue #12 of two repositories would accumulate in one `issue-12`
    directory and a transcript would not say which project it came from.
    """
    owner, _, name = repository.partition("/")
    return sessions_dir / owner / name


def build_spec(args: argparse.Namespace, environ: dict[str, str], root: Path) -> docker.RunSpec:
    """Turn the configuration into the docker run this invocation needs."""

    def require(name: str) -> str:
        value = environ.get(name)
        if not value:
            LOG.die(f"set {name} in {args.env_file}")
        return value

    def require_matching(name: str, shape: re.Pattern[str], expected: str) -> str:
        value = require(name)
        if not shape.match(value):
            LOG.die(f"{name} must be {expected}, got: {value}")
        return value

    configured_repository = environ.get("GITHUB_REPOSITORY")
    repository = args.repository or configured_repository
    if not repository:
        LOG.die(f"name a repository as owner/repo, or set GITHUB_REPOSITORY in {args.env_file}")
    if not REPOSITORY.match(repository):
        LOG.die(f"repository must be owner/repo, got: {repository}")

    app_id = require("GITHUB_APP_ID")
    key_path = resolve(root, require("GITHUB_APP_PRIVATE_KEY_FILE"))
    if not os.access(key_path, os.R_OK):
        LOG.die(f"cannot read app key at {key_path}")

    # The model configuration is validated on the host, where the message is
    # read, rather than inside a container that has already spent a minute
    # minting a token and cloning.
    try:
        pi = _piconfig.Config.from_environ(environ)
    except _piconfig.MissingConfig as error:
        LOG.die(f"{args.env_file}: {error}")

    try:
        model_ip = tailnet.resolve(pi.host, override=environ.get("MODEL_IP"))
    except tailnet.Unresolvable as error:
        LOG.die(str(error))

    # A pinned installation id belongs to the repository it was looked up for.
    # Carrying it onto another one would mint a token for the wrong installation
    # and surface a minute later as "cannot read <repo>", naming the repository
    # rather than the stale pin. Resolving it costs one API call.
    installation_id = environ.get("GITHUB_APP_INSTALLATION_ID")
    if installation_id and repository != configured_repository:
        LOG.log(
            f"ignoring GITHUB_APP_INSTALLATION_ID: it is pinned for "
            f"{configured_repository or 'another repository'}, not {repository}"
        )
        installation_id = None

    sessions_path = session_path(resolve(root, require("SESSIONS_DIR")), repository)
    sessions_path.mkdir(parents=True, exist_ok=True)

    cpus = require_matching("CONTAINER_CPUS", CPUS, "a number of CPUs such as 4 or 1.5")
    if float(cpus) <= 0:
        LOG.die(f"CONTAINER_CPUS must be greater than zero, got: {cpus}")
    limits = docker.Limits(
        memory=require_matching("CONTAINER_MEMORY", MEMORY, "a size such as 4g or 512m"),
        cpus=cpus,
        pids=require_matching("CONTAINER_PIDS_LIMIT", PIDS, "a positive whole number"),
    )

    run_timeout = require_matching("RUN_TIMEOUT", SECONDS, "a positive number of seconds")

    # Extra egress is optional and additive: unset, the container reaches the
    # model and GitHub and nothing else.
    egress_allow = environ.get("EGRESS_ALLOW") or None
    for name in (egress_allow or "").split(","):
        if egress_allow and not HOST_NAME.match(name.strip()):
            LOG.die(f"EGRESS_ALLOW must be comma-separated host names, got: {name.strip()!r}")

    dev_agents_dir = root / ".pi" / "agents" if environ.get("DEV") == "1" else None
    if dev_agents_dir is not None:
        LOG.log("DEV=1: using .pi/agents from the working copy")

    return docker.RunSpec(
        image=require("IMAGE"),
        positional=args.target,
        repository=repository,
        agent=require("AGENT"),
        pi=pi,
        model_ip=model_ip,
        sessions_path=sessions_path,
        app_id=app_id,
        key_path=key_path,
        limits=limits,
        run_timeout=run_timeout,
        installation_id=installation_id,
        egress_allow=egress_allow,
        dev_agents_dir=dev_agents_dir,
    )


def record_run_command(session_dir: Path, command: str, *, now: datetime | None = None) -> Path:
    """Append the (redacted) command to the issue's audit file, and return it."""
    session_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    record = session_dir / RUN_COMMAND_FILE
    with record.open("a", encoding="utf-8") as f:
        f.write(f"# {stamp}\n{command}\n")
    return record


def main(argv: Sequence[str] | None = None, *, exec_fn: ExecFn = _exec) -> int:
    """Run one issue in the container, replacing this process with docker."""
    args = parse_args(argv)

    env_file = args.env_file.expanduser().resolve()
    try:
        env.load(env_file, os.environ)
    except env.InvalidEnvFile as error:
        LOG.die(str(error))
    root = env_file.parent

    spec = build_spec(args, dict(os.environ), root)
    argv_out = docker.build_argv(spec)

    if args.dry_run:
        print(docker.render(argv_out))
        return 0

    if args.build or not docker.image_exists(spec.image):
        LOG.log(f"building {spec.image}...")
        docker.build_image(spec.image, root)

    LOG.log(f"session transcripts: {spec.sessions_path}/issue-<n>/")

    # The audit record, before the exec that ends this process: what is about
    # to run, with the model key masked, on stderr and -- for an issue run, in
    # the directory its transcript will land in -- on disk.
    audited = docker.render(docker.redact(argv_out))
    LOG.log(f"running: {audited}")
    if not spec.interactive:
        record = record_run_command(spec.sessions_path / f"issue-{args.target}", audited)
        LOG.log(f"run command recorded: {record}")

    # exec rather than spawn: the docker client must be the process attached to
    # the terminal, so Ctrl-C forwarding, the -it TTY and the exit code all pass
    # through without a Python process in the middle.
    exec_fn(argv_out[0], argv_out)


if __name__ == "__main__":
    raise SystemExit(main())
