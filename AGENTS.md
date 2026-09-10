# AGENTS.md

Instructions for coding agents working on **this** repository. Binding, not
advisory. If something here conflicts with a general habit, this file wins.

For what the project *is* and how to run it, read [README.md](README.md).

## The shape of the project

One package, `src/pi_agent/`, in two halves that never import each other:

| Path | Runs | Responsibility |
|---|---|---|
| `host/run.py` | your machine | CLI: parse args, load `.env`, build/exec `docker run` |
| `host/env.py` | your machine | parse `.env` with python-dotenv's grammar, without executing it |
| `host/docker.py` | your machine | the `docker run` argv, as a value |
| `host/tailnet.py` | your machine | `tailscale ip` lookup for the model host |
| `host/checks.py` | your machine | the validation gate, as data |
| `host/transcript.py` | your machine | render a `pi` `.jsonl` session as standalone HTML |
| `container/entrypoint.py` | the image | PID 1: config → token → identity → clone → issue → agent |
| `container/token.py` | the image | RS256 App JWT → installation token → bot identity |
| `container/egress.py` | the image | the nftables allowlist, as a value, installed before the drop |
| `container/identity.py` | the image | git author, committer, credential helper |
| `container/repo.py` | the image | clone or refresh, verify read access |
| `container/issue.py` | the image | validate the one issue a run targets |
| `container/agent.py` | the image | agent definition → leased branch → `pi` → report |
| `container/reconcile.py` | the image | verify remote branch/PR truth and clean owned orphans |
| `container/gh.py` | the image | `gh api` / `gh` calls, and GitHub's two footguns |
| `_proc.py` | both | the subprocess seam |
| `_piconfig.py` | both | pi's `models.json` / `settings.json`, as data |
| `_log.py` | both | tagged progress lines on stderr |

`.pi/agents/issue-to-pr.md` is the bot's prompt, not project source. It is baked
into the image as an artifact and is versioned here; changing it changes what the
bot does, so treat it as a behavioural change and say so. `image/bin/gh` is the
other image artifact: the `gh` wrapper that serves the token from its file.

## Commands

```bash
uv sync                             # install the project editable, plus dev tools
uv run scripts/check.py             # the gate: ruff format --check, ruff check, pytest, mypy, pyright
uv run scripts/check.py --fix       # auto-fix first, then check
uv run scripts/check.py --docker    # also build the image (slow, needs a daemon)
uv run scripts/fix.py               # format + lint fixes only
```

All five gates must be green before you call a change done. Always `uv run`;
never `pip install` into this project, and never re-lock unless a dependency
genuinely changed.

Add `--docker` when you touch the `Dockerfile`, `.dockerignore`, `uv.lock`,
`pyproject.toml`, or anything under `container/`. That is the one class of change
that leaves the gate green and the image broken.

## Invariants

These are the decisions the design rests on. Each is asserted by a test. If a
change requires breaking one, stop and raise it rather than deciding it.

- **Nothing defaults.** Not in the host CLI, not in the container, not in the
  image. An unset variable stops the run and names itself, on the host, before a
  build. A default is a value nobody chose that still ends up in a run.
- **The container gets a closed set of environment variables.**
  `docker.container_env` builds it in one place; `tests/test_docker.py` asserts
  the exact set. Never append a stray `-e`.
- **No token is ever forwarded into the container.** The bot authenticates only
  as its GitHub App. A stray `GH_TOKEN` would attribute the bot's work to a
  human. There is deliberately no code path that accepts a pre-existing token.
- **The credential is a read-only mount, never an image layer, never an env
  var.** Bootstrap reads it from a root-only directory, then the model runs as
  a uid that cannot traverse that directory.
- **The installation token is a 0600 file, never an environment variable.**
  `identity.apply` writes it in exactly one place and tells the environment
  the path (`GH_TOKEN_FILE`), not the value. git's credential helper and the
  `gh` wrapper in `image/bin/` read it per call, so it sits in the environment
  of the one `gh` process using it and of nothing the model's `bash` tool
  spawns. `gh.api` is the one caller that sets `GH_TOKEN` itself, for the
  App-JWT calls before the file exists.
- **Egress is closed before a model starts, and the model cannot reopen it.**
  `container/egress.py` builds an nftables allowlist -- the model endpoint,
  GitHub's published ranges, the container's resolvers, and the hosts
  `EGRESS_ALLOW` names -- and the root bootstrap installs it with the
  `NET_ADMIN` the privilege drop then discards. A destination a run needs is
  configuration, never a hole in the ruleset.
- **Nothing on the developer's machine is mounted in** except the App key
  (read-only), the sessions directory (writable), and the agent definition under
  `DEV=1` (read-only).
- **The agent definition comes from the image, not the checkout.** `pi` runs with
  `--no-extensions`, `--no-approve`, `--no-context-files`, and the definition's
  tool allowlist. Repository guidance is read explicitly as untrusted input; it
  is never appended to the system prompt as higher-priority instructions.
- **The model is configuration, not a rebuild.** `_piconfig` generates pi's
  registry at container start from forwarded variables. `from_environ(as_environ(c)) == c`
  is the property that keeps the two halves from drifting.
- **Every subprocess goes through `_proc.Runner`.** That injectable seam is what
  makes the logic testable without a daemon, a credential, or a network. Never
  call `subprocess` directly outside `_proc.py`.
- **stdout carries payloads; progress and errors go to stderr**, tagged with a
  component name via `_log.Logger`.
- **Identity is settled in code, before a model starts.** Commit authorship, PR
  authorship and push credential are three separate mechanisms. None of them is
  left to the prompt.

## Style

Match the surrounding code; where this differs from your defaults, this wins.

- Python 3.14, `from __future__ import annotations` at the top of every module.
- Full type annotations. `mypy` runs with `disallow_untyped_defs`, and `pyright`
  in standard mode. `ruff` at line-length 100, `E501` ignored (the formatter owns
  wrapping).
- Every module opens with a docstring saying **why the module exists** — what
  went wrong without it, or which decision it encodes. Every public function has
  a one-line docstring; longer ones explain the non-obvious.
- Comments explain decisions, not mechanics. If a line looks arbitrary, say what
  it costs to get wrong. Do not narrate what the code already says.
- Values, not side effects: build argv, environments and JSON documents as
  returned values so a test can assert on them. `frozen=True, slots=True`
  dataclasses for those values.
- Errors are named exceptions (`InvalidEnvFile`, `Unresolvable`, `MissingConfig`,
  `InvalidIssue`, `InvalidDefinition`, `CommandError`). Fatal conditions in an
  entry point use `LOG.die`, whose `NoReturn` is load-bearing for narrowing.
- Report every missing value at once, not one failed run at a time.
- Sections in long modules are marked with a `# ---- name ----` rule.

## Tests

`tests/`, pytest, one file per module. The project is installed editable, so
`import pi_agent` resolves the same way it will inside the image.

- Test names are sentences about behaviour, not about functions:
  `test_no_token_is_ever_forwarded_into_the_container`, not `test_container_env`.
  The docstring says why the behaviour matters.
- Use `tests/fake_proc.FakeRunner` to script and record subprocess calls. Assert
  on the argv that *would* have run.
- No test may touch the network, a docker daemon, a real credential, or the
  developer's `.env`. Use `tmp_path` for anything on disk.
- Any behavioural change ships with a test. Any invariant above that a change
  touches keeps its test — tighten it rather than relaxing it.
- Never weaken a check to get green: no `noqa`, no `xfail`, no narrowed inputs.

## Configuration changes

Adding or renaming a configuration variable touches a fixed set of places. Do all
of them:

1. `.env.example` — with a comment saying what it is and why it has no default.
2. `_piconfig.VARS` (model configuration) **or** the `require(...)` call in
   `host/run.py` and `container/entrypoint.py` (everything else).
3. `docker.container_env` if the container needs it, plus the asserted set in
   `tests/test_docker.py`.
4. The configuration table in `README.md`.

Container-side paths (`/work`, `/work/sessions`, `/run/secrets/github-app.pem`,
`/opt/agent/agents`) are image layout: named once in `host/docker.py` and passed
in as environment, so a mount target cannot disagree with the variable naming it.

## Git

- Commit subjects are imperative and sentence-case, no scope prefix:
  `Generate pi's config from .env, and default nothing`. Say what changed and,
  where it is not obvious, why.
- Add files by explicit path. Never `git add -A`.
- Never commit `.env`, `secrets/`, `*.pem`, or anything under `sessions/`.
- Work on a branch. Do not push or open a PR unless asked.

## Do not

- Add a runtime dependency. There are two — `cryptography` for the App JWT and
  `python-dotenv` for the `.env` grammar — and the image installs from `uv.lock`
  with `--frozen`, so a third means a re-lock and a `--docker` run.
  `python-dotenv` is pinned `<2` because `host/env.py` imports `dotenv.parser`,
  which is below its documented surface.
- Add a default for anything.
- Add `safe.directory` entries in `identity.py`. They would be inert; see the
  docstring on `git_config_commands` and `tests/test_identity.py`.
- Bake credentials, model configuration, or pi settings into the image.
- Move logic into the agent prompt that belongs in code. If a model can get it
  wrong, it should never be asked.
