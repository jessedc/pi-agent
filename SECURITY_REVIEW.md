# Security Review: Running LLMs in Docker Containers

A review of the `pi-agent` codebase — a system that runs a coding-agent
LLM (`pi`) inside a Docker container to turn GitHub issues into draft pull
requests, authenticating as a GitHub App.

The review covers the current implementation only and is organised by finding
severity. Each finding names the files and code paths involved, explains the
risk, and gives a concrete change to make.

---

## Executive summary

The codebase demonstrates strong security design fundamentals:

- **Privilege separation**: root bootstrap reads the App key, then irreversibly
  drops to an unprivileged `pi` uid (1001) before any model process starts
  (`container/privilege.py`, `container/entrypoint.py`).
- **Closed container boundary**: a single `container_env()` function builds the
  exact environment set, asserted by `tests/test_docker.py`.
- **No human token forwarded**: `GH_TOKEN` is never passed in from the host; it
  is minted inside the container and enters the environment in exactly one place
  (`container/identity.py:apply`).
- **Parsed `.env`, not executed**: `host/env.py` uses `python-dotenv`'s parser
  rather than `source`-ing the file.
- **Agent definition from the image, not the checkout**: prevents prompt
  injection from repository content. Symlink and path-traversal guards in
  `container/agent.py:definition_for`.
- **Cap-drop all + no-new-privileges + tini init**: solid container hardening
  baseline.
- **Token redaction** in `Credential.__repr__`; credential helper avoids writing
  the token to `.git/config`.
- **Transcript renderer** has CSP, HTML escaping, safe-href validation, and no
  remote assets.
- **Pinned versions** for `pi`, `uv`, and Python in the Dockerfile.
- **Telemetry disabled** in pi's settings.

The findings below are improvements to tighten the boundary further, ordered by
impact.

---

## High severity

### 1. No network egress restriction — the model can call any host

**Status**: addressed, by a fourth option. None of the three below fit: the
premise that the container "only needs to reach one host" is wrong (it mints
its token from `api.github.com`, clones from and pushes to `github.com`, and
the agent prompt has the model install the target project's dependencies), so
Option A would stop every run at the token mint; Option B needs a second
container whose lifetime the `exec`ed `docker run` cannot manage; Option C is
outside the tested boundary. Instead `container/egress.py` builds an nftables
allowlist -- the model endpoint on its port, GitHub's ranges from `/meta`, the
container's resolvers on 53, and the hosts `EGRESS_ALLOW` names on 443 -- and
the root bootstrap installs it inside the container's network namespace with
`NET_ADMIN`, after the token mint and before the privilege drop, which discards
the capability. `tests/test_egress.py` asserts the ruleset,
`tests/test_entrypoint.py` the ordering, and `tests/test_docker.py` the
capability and the forwarded `MODEL_IP`. Verified on Docker Desktop: GitHub
reachable, everything else reset. Remaining gap: `EGRESS_ALLOW` hosts are
resolved once, so a CDN answer that rotates mid-run can reject a later
connection. The original finding follows.

**Files**: `host/docker.py:build_argv`

The `docker run` command uses the default bridge network. The only
network-related flag is `--add-host` for the model endpoint. There is no
`--network` flag, no egress firewall, and no proxy. A model that is compromised
or jail-broken — or one that simply follows a malicious instruction in an issue
body or repository file — can open a TCP connection to any reachable host on the
host's network and the internet.

**What is at risk**:
- **Token exfiltration**: `GH_TOKEN` (a valid GitHub installation token, ~1h
  lifetime, Contents+PRs write) is in the environment of every process the model
  spawns. `MODEL_API_KEY` is also present. A `curl` or `bash` one-liner can send
  these to an attacker-controlled host.
- **Data exfiltration**: the cloned repository's source code, issue content, and
  any secrets in the checkout can be sent outbound.
- **C2 channels**: a compromised model can fetch instructions from the internet,
  bypassing the "agent definition comes from the image" boundary.

**Change to make**:

The model only needs to reach one host: the model endpoint on the tailnet.
Restrict the container to that single destination.

Option A (preferred — no daemon network):

```python
# In build_argv, replace the default network with an internal one and
# route only the model host.
argv += ["--network", "none"]
# --add-host is inert under --network none, so switch to a custom network
# with a single allowed route.
```

Option B (custom network + egress proxy):

Create a Docker network with a gateway container that allow-lists a single
destination (the model's tailnet IP:port). The container runs on that network
and can only reach the gateway, which forwards to the model and drops everything
else.

Option C (host firewall):

If a custom network is too complex for this project's scope, document the gap
explicitly and add a host-level `iptables`/`pf` rule that blocks egress from the
container subnet except to the model IP. This is weaker (it is outside the
tested `build_argv` boundary) but is a meaningful interim control.

Whatever option is chosen, add a test in `tests/test_docker.py` asserting that
the network is not the default bridge — the boundary that is not tested is the
boundary that drifts.

---

### 2. Writable root filesystem — the model can install persistence

**Status**: addressed. `build_argv` passes `--read-only` and mounts tmpfs on
`/tmp` (`noexec,nosuid,size=100m`), `/home/pi` and `/work`, the last two owned
by the `pi` uid. The layout change this needed is done: uv is installed to
`/usr/local/bin` and its Python to `/opt/uv/python`, so nothing the image
needs lives under the home directory the tmpfs shadows. `tests/test_docker.py`
asserts the flag and the mount set; `tests/test_dockerfile.py` asserts the
layout. The original finding follows.

**Files**: `host/docker.py:build_argv`, `Dockerfile`

The container's root filesystem is writable. The only writable mount is
`/work/sessions`, but `/opt/app`, `/home/pi`, `/usr/local`, `/opt/agent`, and
every other path in the image are all writable by the `pi` user (uid 1001),
which owns much of the image per the `chown -R pi:pi` in the Dockerfile.

A model with `bash` access can:
- Overwrite `pi`'s own binary or Node modules to change its behaviour on the
  next run (if the container were reused — `--rm` mitigates this, but a
  persistent `sessions/` mount could carry a payload read by the transcript
  renderer).
- Modify `/opt/agent/agents/issue-to-pr.md` at runtime — though the agent
  definition is read before the model starts, so this would not affect the
  current run.
- Write binaries to `/home/pi/.local/bin` (which is on `PATH`), enabling
  shadowing of `git`, `gh`, or other tools for the remainder of the run.

**Change to make**:

Add `--read-only` to `docker run`, and mount `tmpfs` on the directories that
genuinely need writes:

```python
argv += [
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=100m",
    "--tmpfs",
    "/home/pi:rw,nosuid,size=100m",
]
```

`/work` is already a writable mount (`sessions/`). The pi configuration directory
(`/home/pi/.pi/agent`) is written once at startup and then read by `pi`; a
`tmpfs` on `/home/pi` covers it. `/tmp` is needed by `git` and other tools.

The `noexec` flag on `/tmp` prevents executing binaries written there. `/home/pi`
cannot be `noexec` if `pi`'s own binaries live there (the uv install puts
binaries in `/home/pi/.local/bin`), but those are installed at build time and the
`tmpfs` would shadow them — so this needs a layout change: move the uv install to
a path not shadowed by tmpfs, or mount the tmpfs only on `/home/pi/.pi` and
`/tmp`.

At minimum, adding `--read-only` with a `/tmp` tmpfs is a strict improvement
with no layout change. Add a test asserting `--read-only` is present.

---

### 3. No resource limits — a runaway model can exhaust the host

**Status**: addressed. `build_argv` passes `--memory` (with `--memory-swap`
equal to it), `--cpus` and `--pids-limit` from `CONTAINER_MEMORY`,
`CONTAINER_CPUS` and `CONTAINER_PIDS_LIMIT`, which are required in `.env` and
refused when zero; `nofile` and `fsize` ulimits are fixed in `docker.ULIMITS`.
`tests/test_docker.py` asserts each flag. The original finding follows.

**Files**: `host/docker.py:build_argv`

There are no `--memory`, `--cpus`, `--pids-limit`, or `--ulimit` flags. A model
that enters an infinite loop, spawns unbounded processes (the `bash` tool can
fork-bomb), or allocates large files can consume the host's resources, affecting
other containers and the host itself.

**Change to make**:

```python
argv += [
    "--memory",
    "4g",
    "--memory-swap",
    "4g",  # no swap overflow
    "--cpus",
    "4",
    "--pids-limit",
    "500",
    "--ulimit",
    "nofile=4096:8192",
    "--ulimit",
    "fsize=1073741824",  # 1 GiB max file size
]
```

The exact values should be configurable (add to `.env` as optional variables),
but the key point is that limits exist. `--pids-limit` is especially important
given the `bash` tool's ability to spawn processes. Add a test asserting that
resource limits are set.

---

## Medium severity

### 4. Model API key sent over plaintext HTTP

**Files**: `_piconfig.py:Config.base_url`

```python
return f"http://{self.host}:{self.port}/v1"
```

The model endpoint is always `http://`, never `https://`. The `MODEL_API_KEY`
is sent in the `Authorization` header (or equivalent) in cleartext. The security
boundary is the tailnet (Tailscale's WireGuard encryption), which is reasonable
for a trusted overlay network — but it should be an explicit choice, not an
implicit one.

If the model is ever moved to a non-tailnet host, or if the tailnet is bridged to
another network, the API key is exposed. An attacker on the same network segment
can sniff the key and use the model endpoint.

**Change to make**:

Add a `MODEL_SCHEME` variable (or detect from the URL) and support `https://`:
if the scheme is `https`, pi will use TLS. For `http`, require that
`MODEL_HOST` resolves to a tailnet address and log a warning that the
transport is plaintext.

At minimum, document in the README and `.env.example` that `http://` relies on
the tailnet for confidentiality and that `https://` should be used for any
non-tailnet model host.

---

### 5. The GitHub installation token is exposed to all model-spawned processes

**Status**: addressed, as option 1 below. `identity.apply` writes the token to
`~/.gh-token` (mode 0600, in the home tmpfs) and sets `GH_TOKEN_FILE` in the
environment instead of `GH_TOKEN`. The credential helper reads the file per
call, and `image/bin/gh` -- ahead of `/usr/bin/gh` on the image's PATH --
serves it to `gh` in gh's own environment for one call, for the bootstrap's
calls and the model's alike; a `GH_TOKEN` already set (the App-JWT calls) wins.
The agent prompt names the file alongside `GH_TOKEN` in its exposure rule.
Tests: `tests/test_identity.py` (including the helper against real git),
`tests/test_gh_wrapper.py` (the wrapper against real `sh`),
`tests/test_dockerfile.py`. The uid can still read the file; this narrows the
exposure to one process, it does not remove the trust in the uid. The
original finding follows.

**Files**: `container/identity.py:apply`, `container/entrypoint.py:79`

`GH_TOKEN` is set in the process environment before `pi` starts, and is
inherited by every process the model's `bash` tool spawns. This is deliberate
and documented — `git push` needs it — but it means:

- The token is in `/proc/self/environ` for any process to read.
- The token is in the environment of `git`, `gh`, `curl`, `python`, and any other
  tool the model runs, whether it needs it or not.
- A single `env` command in a bash tool reveals the token in cleartext.

The prompt says "Never print, copy, encode, or otherwise expose `GH_TOKEN`", but
prompts are bypassable. The real protection is the container boundary and the
token's limited scope and lifetime.

**Change to make**:

This is a design tradeoff, not a bug — the token must be available for `git
push`. But the exposure surface can be reduced:

1. **Scope the token to the credential helper only**. Instead of putting
   `GH_TOKEN` in the global environment, write it to a file readable only by
   `pi` (mode 0600) and have the credential helper read from that file. The
   `CREDENTIAL_HELPER` string currently uses `${GH_TOKEN}`; change it to read
   from a file:

   ```python
   CREDENTIAL_HELPER = (
       '!f() { echo username=x-access-token; echo "password=$(cat /run/gh-token)"; }; f'
   )
   ```

   Write the token to `/run/gh-token` (mode 0600, owned by pi) in `identity.apply`,
   and remove `GH_TOKEN` from the environment. The `gh` CLI still needs
   `GH_TOKEN` — but `gh` is only called by the bootstrap (before the model
   starts) and by the model itself for `gh pr create`. For the model's `gh` calls,
   a wrapper script could set `GH_TOKEN` from the file only for the duration of
   the `gh` call.

   This is a larger change and may not be worth the complexity for this project,
   but it is the direction that most reduces the token's exposure.

2. **At minimum, add `gh` token scoping**: the model only needs `gh` for
   `pr create` and `pr list`. Consider a wrapper that injects the token only for
   those subcommands.

---

### 6. No seccomp profile beyond Docker's default

**Files**: `host/docker.py:build_argv`

Docker applies a default seccomp profile that blocks a set of dangerous syscalls
(e.g., `kexec_load`, `mount` in most contexts). But the default profile allows
many syscalls that this container has no use for: `ptrace`, `perf_event_open`,
`bpf`, `unshare`, `setns`, `keyctl`, and others that are common kernel-exploit
primitives.

**Change to make**:

Create a custom seccomp profile in JSON that allow-lists only the syscalls the
container needs. At minimum, explicitly deny these high-risk syscalls that the
default allows:

```json
{
  "defaultAction": "SCM_ACT_ERRNO",
  "syscalls": [
    {
      "names": ["ptrace", "perf_event_open", "bpf", "unshare", "setns",
                 "keyctl", "add_key", "request_key", "personality",
                 "process_vm_writev", "process_vm_readv"],
      "action": "SCM_ACT_ERRNO"
    }
  ]
}
```

Or, simpler: add `--security-opt=seccomp=unconfined` is **not** the answer; the
default is already applied. The improvement is to go from the default to a
custom profile. If that is too much for this project, document that the default
seccomp profile is relied upon and that a custom profile is a known future
hardening step.

---

### 7. Supply chain: no integrity verification for installed tooling

**Files**: `Dockerfile`

Two install paths fetch and execute code without verifying its integrity:

1. **uv installer** (line 70):
   ```dockerfile
   RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
   ```
   The installer script is fetched over HTTPS but not checksum-verified. A
   compromise of `astral.sh` (or the CDN) would inject arbitrary code into the
   image. The version is pinned in the URL, but the content at that URL is not
   pinned.

2. **npm install** (line 53):
   ```dockerfile
   RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"
   ```
   npm packages are pinned by version but not by integrity hash. A compromise of
   the npm registry or the package maintainer's account would replace the
   package. npm does verify registry-provided hashes, but those are under the
   registry's control.

3. **gh CLI** (lines 44-49): installed from GitHub's apt repo via a fetched GPG
   key. Standard practice, but the key fetch itself is a trust root.

**Change to make**:

- For **uv**: download the installer, verify its SHA256 against a pinned value
  in the Dockerfile, then execute. Or use uv's standalone binary download which
  supports checksum verification:
  ```dockerfile
  RUN curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
      -o /tmp/uv.tar.gz \
      && echo "<pinned-sha256>  /tmp/uv.tar.gz" | sha256sum -c - \
      && tar -xzf /tmp/uv.tar.gz -C /home/pi/.local/bin --strip-components=1 \
      && rm /tmp/uv.tar.gz
  ```

- For **npm**: use `npm install --package-lock-only` to generate a lockfile, or
  pin the tarball integrity hash. At minimum, run `npm audit` in the build.

- For **gh**: the current approach is standard. Consider pinning the gh version
  explicitly (it currently tracks `stable`).

Add a comment in the Dockerfile noting that the pinned SHA256 values must be
updated alongside the version pins.

---

### 8. No timeout on the container run

**Status**: addressed, inside the container rather than around it. The host
execs docker, so a `signal.alarm` before `execvp` would only kill the docker
client and leave the container running. Instead `RUN_TIMEOUT` (required in
`.env`, seconds) is forwarded and `_proc.run_forwarding_signals` enforces it
around the pi process: SIGTERM, a grace period to flush the transcript, then
SIGKILL, returning status 124. `agent.run` then reconciles the remote as after
any exit, so an orphaned branch is still deleted -- which a kill from outside
would skip. `docker run` also carries `--stop-timeout 30` so a `docker stop`
leaves time for that reconciliation. Tests: `tests/test_proc.py` (real
children), `tests/test_agent.py`, `tests/test_docker.py`, `tests/test_run.py`.
The original finding follows.

**Files**: `host/run.py:main` (execs `docker run`)

The host CLI execs `docker run` with no timeout. A model that loops, hangs on a
network call, or waits for interactive input (despite the prompt's rules against
opening editors) will run until the user manually kills it. The container has no
`--stop-timeout` either.

**Change to make**:

Add a configurable timeout (e.g., `RUN_TIMEOUT` in `.env`, no default per the
project's "nothing defaults" rule). On the host side, wrap the `exec` in a
timeout (using `signal.alarm` before `execvp`, or run `docker` with
`--stop-timeout`). At minimum, add `--stop-timeout=30` to the `docker run`
command so `docker stop` kills the container promptly after SIGTERM.

---

## Low severity

### 9. `DAC_OVERRIDE` capability is broad

**Files**: `host/docker.py:build_argv`

The container adds `--cap-add=DAC_OVERRIDE` so root bootstrap can read the
App key from the 0700 `/run/secrets/` directory. `DAC_OVERRIDE` bypasses all
file permission checks for read, write, and execute — it is one of the most
powerful capabilities.

After the privilege drop, the process is uid 1001 and cannot use the capability
(no-new-privileges prevents reacquiring it). But the capability is present at the
container level for the entire lifetime, so any kernel exploit that regains
privileged execution would have it.

**Change to make**:

Instead of relying on `DAC_OVERRIDE`, make the key readable without it. Change
the key directory's ownership so the bootstrap can read it without bypassing
permissions:

- In the Dockerfile, create a `bootstrap` group and make `/run/secrets/`
  readable by it:
  ```dockerfile
  RUN groupadd --gid 1002 keyreaders \
      && chown root:keyreaders /run/secrets \
      && chmod 750 /run/secrets
  ```
- Have the bootstrap process join the `keyreaders` supplementary group before
  reading the key, then drop it along with uid/gid in `privilege.drop_to`.

This would allow removing `DAC_OVERRIDE` from the capability set, leaving only
`SETUID` and `SETGID` (which are needed for the drop itself).

Alternatively, make the key file itself (not the directory) readable by a
non-root user and mount it with appropriate ownership. Docker's `--mount`
supports `--mount type=bind,...,uid=0,gid=1001,mode=0400` on some platforms.

This is a design change and may not be worth the complexity, but it is the
cleanest way to remove `DAC_OVERRIDE`.

---

### 10. `MODEL_API_KEY` written to `models.json` on disk

**Status**: addressed. `_piconfig.write` creates both files with mode 0600
via `os.open` rather than chmod'ing after the fact, and tightens a stale file
left by an earlier run. `tests/test_piconfig.py` asserts both. The original
finding follows.

**Files**: `_piconfig.py:write`, `container/entrypoint.py:74`

The model API key is written to `models.json` in pi's configuration directory
(`/home/pi/.pi/agent/models.json`) as a plaintext field (`"apiKey"`). Any process
running as `pi` can read it. This is unavoidable if `pi` needs the key to call
the model, but the file is world-readable by default (umask-dependent).

**Change to make**:

After writing `models.json`, chmod it to 0600:

```python
models_path.write_text(json.dumps(models(config), indent=2) + "\n", encoding="utf-8")
models_path.chmod(0o600)
```

The same applies to `settings.json` (though it contains no secret). This
ensures only `pi` can read the key, not any other user that might exist in the
image (there is only `pi`, but defence in depth).

---

### 11. Git trace could leak the token to logs

**Files**: `container/identity.py:CREDENTIAL_HELPER`

The credential helper echoes `password=${GH_TOKEN}` to stdout, which git
captures internally. But if `GIT_TRACE=1` or `GIT_TRACE_PACKET=1` is set in the
environment (which the model's bash tool could do), git may log credential
negotiation details to stderr.

**Change to make**:

Explicitly unset git trace variables in `identity.apply` or
`git_config_commands`:

```python
environ.pop("GIT_TRACE", None)
environ.pop("GIT_TRACE_PACKET", None)
environ.pop("GIT_TRACE_CURL", None)
```

Or set `GIT_TRACE=0` in the environment. Add a test asserting these are unset
before the model starts.

---

### 12. No `--security-opt` for AppArmor / SELinux

**Files**: `host/docker.py:build_argv`

On Linux hosts with AppArmor or SELinux enabled, Docker applies a default
profile. But no profile is explicitly specified, so the effective profile depends
on the host's configuration. A misconfigured host could run the container with
no LSM confinement.

**Change to make**:

On hosts that support it, specify a profile:
```python
argv += ["--security-opt", "apparmor=docker-default"]
```

This is a no-op if AppArmor is not active, but makes the intent explicit. For
SELinux hosts, consider `--security-opt=label=user:pi`. This is platform-specific
and may be out of scope for a project that targets Docker Desktop, but it is
worth documenting.

---

### 13. The `sessions/` mount is a cross-run channel

**Files**: `host/docker.py:build_argv`

Session transcripts for issue N accumulate in `sessions/issue-N/`. These files
are written by the container and read by the host-side transcript renderer. A
compromised model could:

- Write a malicious `.jsonl` file designed to exploit a bug in the transcript
  renderer (the renderer is well-hardened with CSP and escaping, but it is a
  growing attack surface).
- Write files with names outside the expected pattern (e.g., `../../something`).
  The mount is scoped to `CONTAINER_SESSION_DIR`, so this is bounded, but the
  host-side renderer globs `*.jsonl` without further validation.

**Change to make**:

- In the transcript renderer, validate that `.jsonl` files are regular files and
  are under the expected size (add a max file size guard before reading).
- Consider per-run session directories that include the branch lease hash, so
  one run's output cannot masquerade as another's.
- Document that `sessions/` is untrusted model output and should be treated with
  the same caution as any other model-generated content.

---

### 14. `dev_agents_dir` mount bypasses the "definition from the image" invariant

**Files**: `host/docker.py:build_argv`, `host/run.py:build_spec`

When `DEV=1`, the working copy of `.pi/agents/` is mounted over the baked-in
agent directory. This is a development convenience, but it means the agent
definition comes from the host filesystem, not the image. If the host filesystem
is compromised, or if a developer's working copy has been modified by another
process, the model runs under a different prompt than the image intended.

**Change to make**:

This is a known and accepted tradeoff for development. To harden it:

- Log a prominent warning when `DEV=1` is active (currently a single line).
- Consider restricting `DEV=1` to refuse to run when the working copy has
  uncommitted changes to `.pi/agents/`, since that is the most common way a
  developer accidentally runs a half-edited prompt.
- Ensure `DEV=1` cannot be set by the container environment (it is a host-side
  variable, which is correct — verify this is the only path).

No code change is strictly needed, but the risk should be documented in the
README's `DEV=1` section.

---

### 15. No audit log of the docker run command

**Status**: addressed, both ways. `main` logs the rendered command to stderr
and, for an issue run, appends a timestamped entry to
`sessions/<owner>/<repo>/issue-<n>/run-command.txt` before the exec, so the
record exists even for a run that is killed. `docker.redact` masks
`MODEL_API_KEY` in both; `--dry-run` stays unmasked because its contract is
a pasteable command. `tests/test_run.py` and `tests/test_docker.py` assert
it. The original finding follows.

**Files**: `host/run.py:main`

The host CLI can `--dry-run` to print the docker command, but in a real run the
command is exec'd and not logged. There is no persistent record of which image,
which env vars, and which mounts were used for a given run. If a run causes
damage, there is no audit trail of the container configuration.

**Change to make**:

Log the full `docker run` argv to stderr (or to a file in `sessions/`) before
executing, even when not in `--dry-run` mode. The `LOG.log` call at line 146
could include the rendered command. This does not change behaviour but creates
an audit record. Alternatively, write the `docker run` command to
`sessions/issue-N/run-command.txt` before exec.

---

### 16. The base image and apt packages are not pinned to content

**Status**: addressed. `FROM node:24-bookworm-slim@sha256:…` with the tag
kept beside the digest; `docker build --pull` in `docker.build_image`, the
`--docker` gate and CI; a `docker` entry in `.github/dependabot.yml`; and
`git` and `gh` pinned to exact apt versions through `GIT_VERSION` and
`GH_VERSION`. `tests/test_dockerfile.py` asserts the pins, `tests/test_docker.py`
and `tests/test_checks.py` the `--pull`. The rest of the apt install floats,
as the finding accepts. The original finding follows.

**Files**: `Dockerfile`

`FROM node:24-bookworm-slim` names a tag, and `apt-get install` takes whatever
Debian and GitHub's apt repository currently serve for `git`, `gh`, `jq` and the
rest. Two builds of the same commit can therefore differ, and the difference is
invisible: the Dockerfile has no diff, the gate stays green, and the image the
bot runs in has a different toolchain.

This is the base-layer counterpart of finding 7, which covers the uv installer
and the pi npm package. Together they mean the image is pinned at the layers
this project controls and floating at the layers beneath them.

**What is at risk**:
- A tag moved to a compromised or regressed image is picked up by the next
  build with no review.
- A bug seen in one run cannot be reproduced from the commit that produced it,
  because the base layer has moved on.

**Change to make**:

- Pin the base image by digest, and keep the tag beside it for humans:
  ```dockerfile
  FROM node:24-bookworm-slim@sha256:<digest>
  ```
  `docker buildx imagetools inspect node:24-bookworm-slim` prints the current
  digest. Build with `--pull` so a stale local tag cannot shadow the pin.
- Add a `docker` entry to `.github/dependabot.yml` so the digest is bumped by a
  reviewable pull request rather than left to rot.
- Pin the apt packages that matter to the bot's behaviour, `git` and `gh`, with
  `apt-get install <pkg>=<version>`. Debian's archive drops superseded versions,
  so a pin that is not refreshed will eventually fail the build; that is the
  correct failure, and it is loud. The remaining packages are toolchain for the
  model's `bash` tool and can float.

The rest of the apt install cannot be made fully reproducible without an apt
snapshot mirror, which is out of scope for a demonstration.

---

## Informational: things done well

These are worth calling out as patterns to preserve:

- **`container_env()` is a closed set, tested by exact match** — the single most
  important security invariant in the codebase, and it is enforced by
  `test_no_token_is_ever_forwarded_into_the_container`.
- **Privilege drop ordering** (`setgroups` → `setgid` → `setuid`) is correct and
  tested in `tests/test_privilege.py`.
- **`GITHUB_APP_PRIVATE_KEY_FILE` is popped from the environment before the
  privilege drop** — the key path is gone before the model's process can read it.
- **Agent definition validation** (`container/agent.py:definition_for`) checks
  for symlinks, path traversal, and resolves the parent directory. The
  `validate_name` regex prevents dot-traversal.
- **Branch leasing** (`container/reconcile.py:acquire`) uses
  `secrets.token_hex` for cryptographic uniqueness, and reconciliation is based
  on GitHub's remote state, not the model's report.
- **`.dockerignore` is an allowlist** (`*` then `!` exceptions) with explicit
  re-exclusion of `.env`, `secrets/`, and `*.pem`.
- **`Credential.__repr__` redacts the token** — it cannot appear in a traceback
  or log line through the standard repr path.
- **`_piconfig.settings` disables telemetry** — the container has no reason to
  phone home, and it is off by default.
- **Transcript CSP**: `default-src 'none'; script-src 'sha256-...'; style-src
  'unsafe-inline'; base-uri 'none'; form-action 'none'` — no remote assets, no
  inline event handlers, no form submissions.
- **`_safe_href`** in the transcript renderer validates URLs by unescaping up to
  three passes, rejects control characters, `//` network-path references, and
  non-http(s) schemes.
- **`--no-extensions`, `--no-approve`, `--no-context-files`** prevent the
  checkout's own pi configuration from influencing the agent's behaviour.
- **`commit.gpgsign false`** prevents the model from signing commits with a key
  it does not have.

---

## Summary of recommended changes

| # | Severity | Change | Files |
|---|----------|--------|-------|
| 1 | High | ~~Restrict network egress to the model host only~~ (addressed: model, GitHub, DNS, `EGRESS_ALLOW`) | `container/egress.py`, `host/docker.py` |
| 2 | High | ~~Add `--read-only` + `tmpfs` for writable dirs~~ (addressed) | `host/docker.py`, `Dockerfile` |
| 3 | High | ~~Add memory/CPU/pid/file resource limits~~ (addressed) | `host/docker.py` |
| 4 | Medium | Support `https://` for the model endpoint | `_piconfig.py` |
| 5 | Medium | ~~Reduce `GH_TOKEN` exposure surface (file-based helper)~~ (addressed) | `container/identity.py`, `image/bin/gh` |
| 6 | Medium | Add a custom seccomp profile | `host/docker.py` |
| 7 | Medium | Verify checksums for uv/npm installs | `Dockerfile` |
| 8 | Medium | ~~Add a configurable run timeout~~ (addressed: `RUN_TIMEOUT`, enforced in the container) | `_proc.py`, `container/agent.py`, `host/docker.py` |
| 9 | Low | Remove `DAC_OVERRIDE` via key-directory ownership | `Dockerfile`, `container/privilege.py` |
| 10 | Low | ~~`chmod 0600` on `models.json`~~ (addressed) | `_piconfig.py` |
| 11 | Low | Unset `GIT_TRACE*` env vars | `container/identity.py` |
| 12 | Low | Specify an AppArmor/SELinux profile | `host/docker.py` |
| 13 | Low | Validate session files in the transcript renderer | `host/transcript.py` |
| 14 | Low | Document `DEV=1` risk; guard against uncommitted prompts | `host/run.py` |
| 15 | Low | ~~Log the `docker run` command as an audit record~~ (addressed) | `host/run.py`, `host/docker.py` |
| 16 | Low | ~~Pin the base image by digest and the apt packages that matter~~ (addressed) | `Dockerfile`, `.github/dependabot.yml` |

Each change that modifies `build_argv` or `container_env` should ship with a
test in `tests/test_docker.py` asserting the new property, following the
project's convention that the container boundary is defined by what is tested.