"""Drive one agent run inside the container.

The agent definition is an artifact baked into the image at ``AGENT_DIR``, not
something read out of the repository being worked on. The bot's instructions
therefore travel with the bot, and the checkout it clones cannot change how it
behaves.

This module reads the definition's frontmatter for its tool allowlist and
context rules, and passes the body to ``pi`` as the system prompt -- the same
shape the ``pi-subagents`` orchestrator builds for a child process, minus the
orchestrator. A container run should be deterministic rather than dependent on a
parent model choosing to call the right tool.

Which model runs the agent is not the definition's business. It comes from the
same configuration that generates pi's registry, so a definition cannot name a
model the registry has never heard of -- and pointing the bot at a different
model is one edit to ``.env`` rather than three files and a rebuild.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pi_agent import _log, _proc
from pi_agent.container import reconcile

LOG = _log.Logger("run")

FRONTMATTER_FENCE = "---"
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class InvalidDefinition(ValueError):
    """The agent definition is missing, unfenced, or has an empty prompt."""


def validate_name(agent: str) -> str:
    """Return a bare agent identifier, or reject path-like configuration."""
    # Requiring an alphanumeric first character makes dot traversal impossible
    # even if a future platform gives dots or dashes path-like meaning.
    if AGENT_NAME.fullmatch(agent) is None:
        raise InvalidDefinition(f"agent must be a bare definition name, not a path: {agent!r}")
    return agent


def definition_for(agent: str, agent_dir: Path) -> Path:
    """Resolve a regular, non-symlink definition directly below `agent_dir`."""
    name = validate_name(agent)
    definition = agent_dir / f"{name}.md"
    if definition.is_symlink():
        raise InvalidDefinition(f"agent definition must not be a symlink: {definition}")
    if not definition.is_file():
        raise InvalidDefinition(f"no agent definition at {definition}")

    # The name guard already prevents traversal. Resolving the complete file is
    # defence in depth against a future relaxation and against symlink escapes.
    if definition.resolve().parent != agent_dir.resolve():
        raise InvalidDefinition(f"agent definition escapes its baked directory: {definition}")
    return definition


def split_definition(text: str) -> tuple[dict[str, str], str]:
    """Split an agent markdown file into its frontmatter and system prompt.

    Frontmatter is the block between the first two fence lines, holding flat
    ``key: value`` pairs -- the same subset ``pi-subagents`` parses.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        raise InvalidDefinition("agent definition does not start with a frontmatter fence")

    frontmatter: dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER_FENCE:
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if separator:
            frontmatter[key.strip()] = value.strip()

    return frontmatter, "\n".join(lines[body_start:]).strip()


def session_dir_for(issue: str, root: Path) -> Path:
    """Return the directory pi will write this run's session transcript into.

    pi's default is a per-cwd folder under its own config directory, which dies
    with the container. Pointing `--session-dir` at a bind-mounted path is what
    makes a run auditable afterwards. Note that pi writes session files flat
    into this directory -- the per-issue split is ours, so several attempts at
    one issue accumulate in one place.
    """
    session_dir = root / f"issue-{issue}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def build_argv(
    frontmatter: dict[str, str],
    prompt_file: Path,
    task: str,
    session_dir: Path,
    *,
    model: str,
    thinking: str,
) -> list[str]:
    """Translate the agent's frontmatter and the model config into a `pi` line."""
    argv = ["pi", "--print", "--session-dir", str(session_dir), "--system-prompt", str(prompt_file)]

    # Model and thinking are always passed. pi would fall back to whatever its
    # settings.json says, which is generated from the same two values -- but a
    # run should say what it ran on, in a line the transcript keeps.
    argv += ["--model", model, "--thinking", thinking]

    if tools := frontmatter.get("tools"):
        # A comma-separated allowlist; pi wants it without the spaces.
        argv += ["--tools", tools.replace(" ", "")]

    if frontmatter.get("inheritSkills") != "true":
        argv.append("--no-skills")
    if frontmatter.get("inheritProjectContext") != "true":
        argv.append("--no-context-files")

    # The cloned repo's own .pi/ directory is never trusted for extensions or
    # settings; the definition we run came from the image, not the checkout.
    argv += ["--no-approve", "--no-extensions", f"Task: {task}"]
    return argv


def task_for(issue: str, branch: str) -> str:
    """The one instruction handed to the model."""
    return (
        f"Implement GitHub issue #{issue} in this repository and open a draft pull request for it. "
        f"Use exactly branch {branch}; no other remote branch belongs to this run."
    )


def report_repository_state(repo: Path, *, run: _proc.Runner = _proc.run) -> None:
    """Show what the run actually did, whatever the model claimed it did."""
    LOG.log("post-run state:")
    for args in (["status", "--short", "--branch"], ["worktree", "list"]):
        run(["git", "-C", str(repo), *args], check=False, capture=False)


def report_session_files(session_dir: Path) -> None:
    """Name the transcripts this run left behind, so they can be read later."""
    transcripts = sorted(session_dir.glob("*.jsonl"))
    if not transcripts:
        LOG.log(f"no session transcript written to {session_dir}")
        return
    LOG.log("session transcript:")
    for transcript in transcripts:
        LOG.log(f"  {transcript}")


def run(
    issue: str,
    repo: Path,
    *,
    agent: str,
    agent_dir: Path,
    session_root: Path,
    model: str,
    thinking: str,
    repository: str,
    timeout: float,
    process: _proc.Process = _proc.run_forwarding_signals,
    command: _proc.Runner = _proc.run,
) -> int:
    """Run one issue through the agent and return pi's exit status.

    `timeout` bounds the model process alone, in seconds. Everything around it
    -- the lease before, the reconciliation after -- still runs when it fires,
    which is the point of enforcing it here rather than on the container.
    """
    definition = definition_for(agent, agent_dir)

    frontmatter, prompt = split_definition(definition.read_text(encoding="utf-8"))
    if not prompt:
        raise InvalidDefinition(f"agent {agent} has an empty system prompt")

    session_dir = session_dir_for(issue, session_root)
    lease = reconcile.acquire(issue, repo, run=command)

    LOG.log(f"agent={agent} model={model} thinking={thinking}")
    LOG.log(f"definition={definition}")
    LOG.log(f"issue: #{issue}")
    LOG.log(f"session dir: {session_dir}")
    LOG.log(f"leased branch: {lease.branch}")

    with tempfile.TemporaryDirectory(prefix="pi-agent-") as tmpdir:
        # A fixed leaf makes this write independent of configured input even if
        # the identifier rules are loosened in the future.
        prompt_file = Path(tmpdir) / "system-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        prompt_file.chmod(0o600)

        argv = build_argv(
            frontmatter,
            prompt_file,
            task_for(issue, lease.branch),
            session_dir,
            model=model,
            thinking=thinking,
        )
        LOG.log(" ".join(argv[:-1]) + ' "Task: ..."')

        # Forwarded rather than plain: this process is PID 1, so without a
        # handler a `docker stop` would SIGKILL pi mid-write and lose the
        # transcript that makes the run auditable.
        status = process(argv, timeout=timeout)

    if status == 0:
        LOG.log("agent run finished")
    elif status == _proc.TIMED_OUT:
        LOG.log(f"agent run stopped at its {timeout:g}s deadline (RUN_TIMEOUT)")
    else:
        LOG.log(f"agent run exited with status {status}")
    remote = reconcile.finish(repository, repo, lease, run=command)
    if remote.pull_request is not None:
        draft = "draft" if remote.pull_request.is_draft else "not draft"
        LOG.log(
            f"remote PR: {remote.pull_request.url} on {remote.branch} "
            f"({remote.pull_request.state.lower()}, {draft})"
        )
    elif remote.deleted:
        LOG.log(f"deleted orphan remote branch: {remote.branch}")
    elif remote.deletion_error is not None:
        LOG.log(f"could not delete orphan remote branch {remote.branch}: {remote.deletion_error}")
    else:
        LOG.log(f"no remote branch or PR for this run: {remote.branch}")
    for branch in remote.unexpected:
        LOG.log(f"remote branch created during the run but not owned by it: {branch}")

    report_repository_state(repo, run=command)
    report_session_files(session_dir)
    if status == 0 and not remote.successful:
        return 1
    return status
