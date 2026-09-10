# Toolchain container for the `issue-to-pr` pi agent.
#
# Contains everything the agent needs and nothing it does not: node (for pi),
# Python + uv (for the bootstrap package and the project under test), git, gh,
# and jq.
#
# Build context is this directory:
#
#   docker build -t pi-issue-to-pr:local .
#
# The agent definition under .pi/agents/ is baked in as an image artifact.
#
# The image holds no credentials and no model configuration. Both arrive at run
# time: the GitHub App private key as a read-only mount, and pi's models.json /
# settings.json written by the entrypoint from forwarded environment variables.
# So one image serves any model, and changing model is not a rebuild.

# Pinned by digest: the tag is for humans and Dependabot, the digest is what
# gets built. A tag can be moved under a compromised or regressed image and
# the next build would take it with no diff; a digest cannot. Builds pass
# --pull so a stale local tag cannot shadow the pin. To refresh by hand:
#
#   docker buildx imagetools inspect node:24-bookworm-slim
#
# .github/dependabot.yml bumps it as a reviewable pull request.
FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e

# Pinned: a floating agent can change tool and prompt behavior with no diff.
# Review a new release, change this line, and rebuild through the Docker gate.
ARG PI_VERSION=0.84.3
ARG PYTHON_VERSION=3.14
# Pinned: an unpinned installer makes every rebuild a different toolchain, and
# the uv that builds the package must satisfy the [build-system] bound.
ARG UV_VERSION=0.12.5
# Pinned: the two apt packages whose behaviour is the bot's behaviour. Debian
# and GitHub drop superseded versions from their archives, so a pin left to
# rot fails the build -- loudly, which is the point -- and is refreshed by
# reading the new version's notes, not by a build quietly taking it. The other
# apt packages are toolchain for the model's bash tool and may float.
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG GH_VERSION=2.100.0

# PI_CODING_AGENT_DIR is image layout: it is where pi looks for its registry
# and defaults, and therefore where the entrypoint writes them. Everything that
# is configuration rather than layout -- AGENT_DIR, WORKDIR, SESSION_DIR, the
# model -- is forwarded per run by the host CLI, and defaulted nowhere.
ENV DEBIAN_FRONTEND=noninteractive \
    PI_CODING_AGENT_DIR=/home/pi/.pi/agent \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=manual

# System toolchain. gh comes from GitHub's own apt repo so we track releases.
# jq and less are for the model's bash tool, not for us: nothing in the
# bootstrap package shells out to either, but the agent prompt says to use bash
# freely, and a `command not found` mid-run costs it budget to recover from.
# nftables is for the bootstrap alone: it installs the egress allowlist as
# root, and the model's uid cannot so much as list it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl "git=${GIT_VERSION}" jq openssl gnupg less nftables \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends "gh=${GH_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# pi itself.
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force

# Unprivileged user. The node image already occupies uid 1000 as `node`.
# /opt/app is owned by pi because `uv sync` writes a venv into it.
#
# The uid and gid are also named by the host CLI, which mounts the writable
# tmpfs directories owned by them, and by container/privilege.py, which drops
# to them. All three must agree.
RUN useradd --create-home --shell /bin/bash --uid 1001 --user-group pi \
    && mkdir -p /work /home/pi/.pi/agent /opt/agent /opt/app /run/secrets \
    && chmod 700 /run/secrets \
    && chown -R pi:pi /work /home/pi/.pi /opt/agent /opt/app

# uv, plus the Python the bootstrap package and the target project both pin.
#
# Both live outside /home/pi on purpose. The root filesystem is read-only at run
# time and the home directory is a tmpfs (see host/docker.py), so anything the
# image put under /home/pi would be shadowed by an empty mount -- a venv whose
# interpreter symlink points into it would not start. UV_UNMANAGED_INSTALL is a
# plain copy of the binary: no receipt, no self-update, no shell-profile edit.
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN uv python install --no-bin "${PYTHON_VERSION}"

# The agent definition is an image artifact: the bot's instructions travel with
# the bot, and the checkout it clones cannot change how it behaves.
COPY --chown=pi:pi .pi/agents/ /opt/agent/agents/

# `gh` on PATH is this wrapper, which serves the installation token from the
# file GH_TOKEN_FILE names to the real /usr/bin/gh for one call, so the token
# is in no process's environment but gh's own. See container/identity.py.
COPY --chmod=755 image/bin/gh /opt/agent/bin/gh

USER pi

WORKDIR /opt/app

# Dependencies first, in their own layer: editing the package does not
# re-resolve them. The build context is the package and its lock, and nothing
# else: a documentation edit is not a reason to invalidate this layer.
COPY --chown=pi:pi pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode

# Then the package itself. `--frozen` fails the build if uv.lock is stale
# rather than silently re-resolving, so a run never waits on PyPI — and works
# with no egress to it at all.
COPY --chown=pi:pi src/ ./src/
RUN uv sync --frozen --no-dev --compile-bytecode

# The entrypoint is the installed console script, so container start does no
# resolution work. The gh wrapper goes first so it shadows /usr/bin/gh for the
# bootstrap and the model alike.
ENV PATH="/opt/agent/bin:/opt/app/.venv/bin:${PATH}"

WORKDIR /work

# Trusted bootstrap needs the key, but model-controlled descendants must not.
# The entrypoint mints the short-lived token and irreversibly drops to pi.
USER root
ENTRYPOINT ["pi-agent-container"]
