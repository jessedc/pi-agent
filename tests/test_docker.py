"""Tests for the docker command line.

This is the boundary the whole design rests on: the container is the bot, and
nothing on the developer's machine crosses into it except the App key. In bash
that boundary was six scattered `-e` and `-v` appends and a prose claim in the
README. Here it is one function and a closed set.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fake_proc import FakeRunner

from pi_agent import _piconfig
from pi_agent.container import privilege
from pi_agent.host import docker

PI = _piconfig.Config(
    host="model-box.example.ts.net",
    port=8080,
    provider="llamacpp",
    api="openai-completions",
    api_key="no-api-key",
    model_id="DeepSeek-V4-Flash-0731-UD-IQ2_M",
    model_name="DeepSeek V4 Flash 0731 (GB10)",
    context_window=262144,
    reasoning=True,
    supports_developer_role=False,
    thinking="high",
)

SPEC = docker.RunSpec(
    image="pi-issue-to-pr:local",
    positional="12",
    repository="example/agent-demo",
    agent="issue-to-pr",
    pi=PI,
    model_ip="100.64.0.1",
    sessions_path=Path("/home/user/repo/sessions"),
    app_id="4707632",
    key_path=Path("/home/user/repo/secrets/bot.pem"),
    limits=docker.Limits(memory="4g", cpus="4", pids="500"),
    run_timeout="3600",
)


# ----------------------------------------------------------- the credential


def test_the_key_is_mounted_read_only() -> None:
    """The bot may read its key. Nothing in the container may rewrite it."""
    argv = docker.build_argv(SPEC)

    assert f"{SPEC.key_path}:{docker.CONTAINER_KEY_PATH}:ro" in argv
    assert docker.CONTAINER_KEY_PATH.startswith(f"{docker.CONTAINER_KEY_DIR}/")


def test_the_container_cannot_regain_the_privilege_it_drops() -> None:
    """Only bootstrap capabilities are granted, and exec cannot reacquire more."""
    argv = docker.build_argv(SPEC)

    assert "--cap-drop=ALL" in argv
    assert "--cap-add=SETUID" in argv
    assert "--cap-add=SETGID" in argv
    assert "--cap-add=DAC_OVERRIDE" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_the_bootstrap_can_close_the_network_and_only_the_bootstrap() -> None:
    """NET_ADMIN is what installs the egress allowlist. The privilege drop
    discards it and no-new-privileges keeps it gone, so the model cannot list
    the rules, let alone loosen them."""
    argv = docker.build_argv(SPEC)

    assert "--cap-add=NET_ADMIN" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_no_token_is_ever_forwarded_into_the_container() -> None:
    """The README's load-bearing claim, made enforceable.

    A stray GH_TOKEN would silently run the whole pipeline as a human,
    attributing the bot's commits and pull requests to them.
    """
    env = docker.container_env(SPEC)

    assert set(env) == {
        "GITHUB_REPOSITORY",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_FILE",
        "WORKDIR",
        "SESSION_DIR",
        "AGENT_DIR",
        "AGENT",
        "MODEL_IP",
        "RUN_TIMEOUT",
        *_piconfig.VARS.values(),
    }
    assert not any("TOKEN" in name for name in env)


def test_the_host_key_path_never_becomes_an_environment_value() -> None:
    """The container is told the container-side path, not the host's."""
    argv = docker.build_argv(SPEC)

    assert f"GITHUB_APP_PRIVATE_KEY_FILE={docker.CONTAINER_KEY_PATH}" in argv
    assert f"GITHUB_APP_PRIVATE_KEY_FILE={SPEC.key_path}" not in argv


# ------------------------------------------------------------ configuration


def test_the_container_is_told_every_value_it_refuses_to_default() -> None:
    """The container defaults nothing, so a variable dropped here is a dead run.

    `from_environ` raising is the same failure the entrypoint would hit after a
    token mint and a clone -- caught here instead, with no daemon involved.
    """
    env = docker.container_env(SPEC)

    assert _piconfig.Config.from_environ(env) == SPEC.pi
    assert env["WORKDIR"] == docker.CONTAINER_WORKDIR
    assert env["AGENT_DIR"] == docker.CONTAINER_AGENT_DIR
    assert env["AGENT"] == "issue-to-pr"


def test_the_model_is_configuration_not_an_image_layer() -> None:
    """Changing model must not change the image: same tag, different -e values."""
    other = replace(PI, model_id="Qwen3-Coder-30B", context_window=131072)
    argv = docker.build_argv(replace(SPEC, pi=other))

    assert "-e MODEL_ID=Qwen3-Coder-30B" in docker.render(argv)
    assert argv[-2:] == [SPEC.image, "12"]


# ---------------------------------------------------------------- sessions


def test_the_session_mount_maps_host_path_to_container_path() -> None:
    """A swap here is invisible in bash and fatal at runtime."""
    argv = docker.build_argv(SPEC)

    assert f"{SPEC.sessions_path}:{docker.CONTAINER_SESSION_DIR}" in argv
    assert f"SESSION_DIR={docker.CONTAINER_SESSION_DIR}" in argv


# ------------------------------------------------------------------ options


def test_pid_one_is_an_init_that_reaps() -> None:
    """The model's bash tool leaves orphans over a long run; Python would not
    reap them, so tini takes PID 1 and forwards signals to the entrypoint."""
    assert "--init" in docker.build_argv(SPEC)


def test_the_model_host_is_aliased_to_its_tailnet_address() -> None:
    argv = docker.build_argv(SPEC)

    assert argv[argv.index("--add-host") + 1] == ("model-box.example.ts.net:100.64.0.1")


def test_the_installation_id_is_passed_only_when_set() -> None:
    """A bash `[[ -n ... ]] && arr+=(...)` that nothing has checked in a year."""
    assert "GITHUB_APP_INSTALLATION_ID" not in docker.container_env(SPEC)

    pinned = docker.container_env(replace(SPEC, installation_id="876"))
    assert pinned["GITHUB_APP_INSTALLATION_ID"] == "876"


def test_the_egress_allowlist_names_the_address_the_model_alias_resolves_to() -> None:
    """--add-host and the container's nftables rule must agree on one address,
    so the container is told it rather than left to parse /etc/hosts."""
    env = docker.container_env(SPEC)
    argv = docker.build_argv(SPEC)

    assert env["MODEL_IP"] == SPEC.model_ip
    assert argv[argv.index("--add-host") + 1].endswith(f":{env['MODEL_IP']}")


def test_extra_egress_is_forwarded_only_when_configured() -> None:
    """Unset means the model reaches GitHub and the model endpoint, full stop."""
    assert "EGRESS_ALLOW" not in docker.container_env(SPEC)

    opened = docker.container_env(replace(SPEC, egress_allow="pypi.org,files.pythonhosted.org"))
    assert opened["EGRESS_ALLOW"] == "pypi.org,files.pythonhosted.org"


def test_a_tty_is_requested_only_for_a_shell() -> None:
    assert "-it" not in docker.build_argv(SPEC)

    shell = replace(SPEC, positional="shell")
    assert "-it" in docker.build_argv(shell)


def test_dev_mounts_the_working_copy_of_the_agent_definition() -> None:
    """Prompt iteration without a rebuild -- and nothing else changes."""
    plain = docker.build_argv(SPEC)
    dev = docker.build_argv(replace(SPEC, dev_agents_dir=Path("/home/user/repo/.pi/agents")))

    added = [item for item in dev if item not in plain]
    assert added == [f"/home/user/repo/.pi/agents:{docker.CONTAINER_AGENT_DIR}:ro"]


def test_the_image_and_the_positional_come_last_in_that_order() -> None:
    """docker treats everything after the image as the container's own argv."""
    argv = docker.build_argv(SPEC)

    assert argv[-2:] == ["pi-issue-to-pr:local", "12"]


def test_the_container_is_removed_after_the_run() -> None:
    assert "--rm" in docker.build_argv(SPEC)


# ------------------------------------------------------------------ writes


def tmpfs_mounts(argv: list[str]) -> dict[str, set[str]]:
    """Each --tmpfs target and the options it was mounted with."""
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--tmpfs"]
    return {
        target: set(options.split(",")) for target, _, options in (m.partition(":") for m in mounts)
    }


def test_the_image_is_read_only_at_run_time() -> None:
    """A model with bash could otherwise rewrite pi, a tool on PATH, or the agent
    definition -- and on a persistent mount, do so for the next run too."""
    assert "--read-only" in docker.build_argv(SPEC)


def test_a_run_may_write_only_to_its_home_its_workdir_and_tmp() -> None:
    """Exactly the places a clone, a git config and a prompt file need. Each is
    a tmpfs, so it dies with the container rather than with a `docker rm`."""
    mounts = tmpfs_mounts(docker.build_argv(SPEC))

    assert set(mounts) == {"/tmp", docker.CONTAINER_HOME, docker.CONTAINER_WORKDIR}
    assert all("rw" in options and "nosuid" in options for options in mounts.values())


def test_nothing_runs_from_tmp() -> None:
    """The place an installer script fetched by a bash tool would drop to."""
    mounts = tmpfs_mounts(docker.build_argv(SPEC))

    assert "noexec" in mounts["/tmp"]
    assert any(option.startswith("size=") for option in mounts["/tmp"])


def test_the_clone_and_the_home_directory_can_run_what_they_hold() -> None:
    """docker mounts a tmpfs noexec unless told otherwise, which would stop the
    target project's own scripts and every binary its gate installs."""
    mounts = tmpfs_mounts(docker.build_argv(SPEC))

    for target in (docker.CONTAINER_HOME, docker.CONTAINER_WORKDIR):
        assert "exec" in mounts[target]
        assert "noexec" not in mounts[target]


def test_the_writable_mounts_belong_to_the_account_the_bootstrap_drops_to() -> None:
    """The host mounts them, the container writes to them after `setuid`, and the
    Dockerfile creates the account: three places naming one uid, checked here."""
    mounts = tmpfs_mounts(docker.build_argv(SPEC))

    assert privilege.PI.uid == docker.PI_UID
    assert privilege.PI.gid == docker.PI_GID
    assert str(privilege.PI.home) == docker.CONTAINER_HOME
    for target in (docker.CONTAINER_HOME, docker.CONTAINER_WORKDIR):
        assert f"uid={privilege.PI.uid}" in mounts[target]
        assert f"gid={privilege.PI.gid}" in mounts[target]


def test_the_sessions_mount_sits_inside_the_workdir_tmpfs() -> None:
    """docker mounts by path length, so the bind mount lands on top of the tmpfs
    and a transcript reaches the host rather than dying with the container."""
    assert docker.CONTAINER_SESSION_DIR.startswith(f"{docker.CONTAINER_WORKDIR}/")


# ------------------------------------------------------------------- limits


def test_a_runaway_run_is_bounded_in_memory_cpu_and_processes() -> None:
    """The model's bash tool can loop, fork-bomb and allocate. Each of those
    must exhaust the container, not the host it shares with everything else."""
    argv = docker.build_argv(SPEC)

    assert argv[argv.index("--memory") + 1] == "4g"
    assert argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--pids-limit") + 1] == "500"


def test_the_memory_limit_cannot_spill_into_swap() -> None:
    """Without this, --memory is a point at which the host starts thrashing
    rather than a limit; docker's default swap allowance is another --memory."""
    argv = docker.build_argv(SPEC)

    assert argv[argv.index("--memory-swap") + 1] == argv[argv.index("--memory") + 1]


def test_open_files_and_file_size_are_capped_per_process() -> None:
    """A single process filling the host's disk is the failure a memory limit
    does not cover."""
    argv = docker.build_argv(SPEC)

    ulimits = [argv[i + 1] for i, item in enumerate(argv) if item == "--ulimit"]
    assert ulimits == list(docker.ULIMITS)
    assert any(item.startswith("nofile=") for item in ulimits)
    assert any(item.startswith("fsize=") for item in ulimits)


def test_the_model_deadline_is_forwarded_for_the_container_to_enforce() -> None:
    """The host execs docker, so nothing on the host outlives that to enforce a
    deadline; the container does it, around pi, and still reconciles after."""
    assert docker.container_env(SPEC)["RUN_TIMEOUT"] == "3600"


def test_docker_stop_leaves_time_to_flush_and_reconcile() -> None:
    """docker's default grace is ten seconds: enough for pi to write its
    transcript, not always for the gh calls that delete an orphaned branch."""
    argv = docker.build_argv(SPEC)

    assert argv[argv.index("--stop-timeout") + 1] == "30"


def test_the_limits_are_configuration_not_constants() -> None:
    """Memory and CPU depend on the machine; a value baked in here would be a
    default, and nothing here defaults."""
    other = replace(SPEC, limits=docker.Limits(memory="512m", cpus="1.5", pids="100"))
    rendered = docker.render(docker.build_argv(other))

    assert "--memory 512m --memory-swap 512m --cpus 1.5 --pids-limit 100" in rendered


# ------------------------------------------------------------------- audit


def test_the_audit_form_masks_the_model_key_and_nothing_else() -> None:
    """The record goes beside the transcript on disk; the key must not."""
    argv = docker.build_argv(SPEC)

    masked = docker.redact(argv)

    assert "-e MODEL_API_KEY=REDACTED" in docker.render(masked)
    assert "no-api-key" not in docker.render(masked)
    assert [item for item in masked if item != "MODEL_API_KEY=REDACTED"] == [
        item for item in argv if item != "MODEL_API_KEY=no-api-key"
    ]


def test_a_value_that_merely_mentions_the_variable_is_left_alone() -> None:
    """Only the `-e NAME=value` pair is a secret, not a positional or a path."""
    argv = ["docker", "run", "MODEL_API_KEY=not-an-env-flag", "-e", "MODEL_ID=MODEL_API_KEY"]

    assert docker.redact(argv) == argv


def test_dry_run_output_is_pasteable_not_redacted() -> None:
    """`--dry-run` exists to be pasted; the audit record is the masked form."""
    assert "MODEL_API_KEY=no-api-key" in docker.render(docker.build_argv(SPEC))


# -------------------------------------------------------------------- build


def test_the_image_is_built_with_pull_so_the_digest_pin_is_honoured() -> None:
    """The base image is pinned by digest. A stale local copy of the tag would
    otherwise satisfy the build, and the pin would be a comment."""
    runner = FakeRunner()

    docker.build_image("pi-issue-to-pr:local", Path("/home/user/repo"), run=runner)

    assert runner.calls == [
        (
            "docker",
            "build",
            "--pull",
            "-f",
            "/home/user/repo/Dockerfile",
            "-t",
            "pi-issue-to-pr:local",
            "/home/user/repo",
        )
    ]


# ------------------------------------------------------------------- render


def test_render_quotes_paths_with_spaces() -> None:
    spec = replace(SPEC, sessions_path=Path("/home/a b/sessions"))

    assert "'/home/a b/sessions:/work/sessions'" in docker.render(docker.build_argv(spec))
