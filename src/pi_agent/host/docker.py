"""Build the `docker run` command line for one agent run.

The container is the bot. It holds no credentials of its own, and nothing on the
developer's machine is mounted into it -- not the checkout, not an SSH key, not
the `gh` keyring. The only identity that crosses the boundary is the GitHub App
private key, read-only, and the only things that come back are a branch, a draft
PR, and a session transcript on the one writable mount.

Assembling that as a value rather than a bash array is what lets a test assert
the boundary holds. `--dry-run` prints what this builds.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pi_agent import _piconfig, _proc

# Paths inside the container: the image's layout, which the host has to know
# because it mounts onto it. Named here once and passed in as environment, so
# the container defaults to nothing and a mount target cannot silently disagree
# with the variable that names it.
CONTAINER_WORKDIR = "/work"
CONTAINER_SESSION_DIR = "/work/sessions"
CONTAINER_KEY_DIR = "/run/secrets"
CONTAINER_KEY_PATH = f"{CONTAINER_KEY_DIR}/github-app.pem"
CONTAINER_AGENT_DIR = "/opt/agent/agents"

# The account the bootstrap drops to, and its home. The writable mounts below
# are created owned by it, so the Dockerfile's `useradd` and the container's
# `privilege.PI` name the same numbers; a test cross-checks the latter.
CONTAINER_HOME = "/home/pi"
PI_UID = 1001
PI_GID = 1001

# The only places a run writes, as tmpfs: they exist for the container's
# lifetime and die with it. Everything else in the image is read-only, so the
# model cannot alter a binary, a library or the agent definition -- for this
# run, or for a later one through anything but the sessions mount.
#
# The three carry no size except /tmp: they are charged to the container's
# memory limit, which is the bound that matters, and a clone plus a venv has
# to fit in it. /tmp is small and noexec because nothing legitimate runs from
# there, and it is where an installer script would try to. The other two say
# `exec` because docker's tmpfs default is noexec, and the clone has to run
# its own scripts and the binaries its gate installs.
TMPFS = (
    "/tmp:rw,noexec,nosuid,size=100m",
    f"{CONTAINER_HOME}:rw,exec,nosuid,uid={PI_UID},gid={PI_GID},mode=0700",
    f"{CONTAINER_WORKDIR}:rw,exec,nosuid,uid={PI_UID},gid={PI_GID},mode=0755",
)

# Per-process limits that do not depend on the machine, so they are decisions
# rather than configuration. A model's bash tool can open files and grow them
# without bound; these turn "the host's disk is full" into an error the model
# sees and can recover from.
ULIMITS = (
    "nofile=4096:8192",
    # 1 GiB: larger than any file a pull request should contain.
    "fsize=1073741824",
)

# How long `docker stop` waits after SIGTERM before it kills the container.
# The entrypoint forwards the signal to pi, lets it flush, then reconciles
# the remote branch; docker's default ten seconds is enough for the flush and
# not always for the `gh` calls after it.
STOP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Limits:
    """What one run may consume, in docker's own units.

    Memory and CPU depend on the machine the daemon runs on, and the pid limit
    on how much parallelism the target project's own gate needs, so all three
    are configuration -- named in `.env`, defaulted nowhere.
    """

    memory: str
    cpus: str
    pids: str


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Everything one `docker run` needs, resolved to absolute host paths."""

    image: str
    positional: str
    repository: str
    agent: str
    pi: _piconfig.Config
    model_ip: str
    sessions_path: Path
    app_id: str
    key_path: Path
    limits: Limits
    # The model process's deadline, in seconds, enforced inside the container.
    run_timeout: str
    installation_id: str | None = None
    egress_allow: str | None = None
    dev_agents_dir: Path | None = None

    @property
    def interactive(self) -> bool:
        """A credential-verification shell needs a TTY; a run does not."""
        return self.positional == "shell"

    @property
    def model_host(self) -> str:
        """The name the container resolves the model by, aliased at run time."""
        return self.pi.host


def container_env(spec: RunSpec) -> dict[str, str]:
    """The environment the container is given.

    Deliberately a closed set. The README's claim that "a human token is never
    forwarded into the container" lives or dies here, so it is built in one
    place and asserted in one test -- rather than being a property of six
    scattered `-e` appends that nobody rechecks.
    """
    env = {
        "GITHUB_REPOSITORY": spec.repository,
        "GITHUB_APP_ID": spec.app_id,
        "GITHUB_APP_PRIVATE_KEY_FILE": CONTAINER_KEY_PATH,
        "WORKDIR": CONTAINER_WORKDIR,
        "SESSION_DIR": CONTAINER_SESSION_DIR,
        "AGENT_DIR": CONTAINER_AGENT_DIR,
        "AGENT": spec.agent,
        # The address behind --add-host, told to the container explicitly so
        # its egress allowlist names the same one the alias resolves to.
        "MODEL_IP": spec.model_ip,
        "RUN_TIMEOUT": spec.run_timeout,
        # The model configuration: the container writes pi's models.json and
        # settings.json from exactly these, so the set travels whole.
        **_piconfig.as_environ(spec.pi),
    }
    if spec.installation_id:
        env["GITHUB_APP_INSTALLATION_ID"] = spec.installation_id
    if spec.egress_allow:
        env["EGRESS_ALLOW"] = spec.egress_allow
    return env


def build_argv(spec: RunSpec) -> list[str]:
    """The full `docker run` command line."""
    argv = [
        "docker",
        "run",
        "--rm",
        # tini as PID 1: it reaps the orphans the model's bash tool leaves
        # behind over a long run, and forwards signals to our entrypoint.
        "--init",
        # Bootstrap gets only what it needs to read the mounted key, close the
        # container's network and become pi. no-new-privileges prevents a
        # later exec from climbing back.
        "--cap-drop=ALL",
        "--cap-add=SETUID",
        "--cap-add=SETGID",
        "--cap-add=DAC_OVERRIDE",
        # For the nftables allowlist container/egress.py installs. Discarded
        # by the privilege drop, so the model cannot loosen what it cannot see.
        "--cap-add=NET_ADMIN",
        "--security-opt=no-new-privileges",
        "--add-host",
        f"{spec.model_host}:{spec.model_ip}",
    ]

    # The model's bash tool can loop, fork and allocate without bound. These
    # make a runaway run the container's problem rather than the host's:
    # memory-swap equal to memory means the limit is a limit, not a slowdown,
    # and the pid limit is what stops a fork bomb.
    argv += [
        "--memory",
        spec.limits.memory,
        "--memory-swap",
        spec.limits.memory,
        "--cpus",
        spec.limits.cpus,
        "--pids-limit",
        spec.limits.pids,
    ]
    for ulimit in ULIMITS:
        argv += ["--ulimit", ulimit]
    argv += ["--stop-timeout", str(STOP_TIMEOUT_SECONDS)]

    # The image is what was built and reviewed; a run must not be able to
    # change it. The sessions mount below sits inside the /work tmpfs, which
    # docker mounts first because it is the shorter path.
    argv.append("--read-only")
    for mount in TMPFS:
        argv += ["--tmpfs", mount]

    for name, value in container_env(spec).items():
        argv += ["-e", f"{name}={value}"]

    # The container is --rm, so anything worth keeping has to land on a mount.
    argv += ["-v", f"{spec.sessions_path}:{CONTAINER_SESSION_DIR}"]
    # The credential: read-only, and never in the environment.
    argv += ["-v", f"{spec.key_path}:{CONTAINER_KEY_PATH}:ro"]

    # DEV=1 mounts the working copy of the agent definition over the baked-in
    # one, so prompt edits can be tried without a rebuild.
    if spec.dev_agents_dir is not None:
        argv += ["-v", f"{spec.dev_agents_dir}:{CONTAINER_AGENT_DIR}:ro"]

    if spec.interactive:
        argv.append("-it")

    argv += [spec.image, spec.positional]
    return argv


def render(argv: list[str]) -> str:
    """The command line as a user could paste it."""
    return shlex.join(argv)


# The one value on the command line that is a secret. The App key is a mount
# path and the installation token is minted inside; this is what is left.
SECRET_VARS = frozenset({_piconfig.VARS["api_key"]})
REDACTED = "REDACTED"


def redact(argv: list[str]) -> list[str]:
    """The same argv with every secret `-e NAME=value` masked.

    For the audit record: a run's exact configuration is worth keeping beside
    its transcript, its model key is not. `--dry-run` deliberately does not
    use this -- its contract is a command the user can paste.
    """
    masked = list(argv)
    for index, item in enumerate(masked):
        name, separator, _ = item.partition("=")
        if index > 0 and masked[index - 1] == "-e" and separator and name in SECRET_VARS:
            masked[index] = f"{name}={REDACTED}"
    return masked


def image_exists(image: str, *, run: _proc.Runner = _proc.run) -> bool:
    """Whether the image is already built locally."""
    return run(["docker", "image", "inspect", image], check=False).ok


def build_image(
    image: str,
    context: Path,
    *,
    env: Mapping[str, str] | None = None,
    run: _proc.Runner = _proc.run,
) -> None:
    """Build the image from the Dockerfile in `context`.

    `--pull` because the base image is pinned by digest: without it a stale
    local copy of the tag would satisfy the build and the pin would be a
    comment.
    """
    run(
        ["docker", "build", "--pull", "-f", str(context / "Dockerfile"), "-t", image, str(context)],
        env=env,
        check=True,
        capture=False,
    )
