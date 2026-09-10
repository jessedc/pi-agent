"""Tests for image-source decisions that must remain visible in a diff.

The Docker build is optional and can reuse cache, so source tests keep a
floating agent version from silently returning between image builds.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_the_pi_version_is_pinned_to_an_exact_release() -> None:
    """A dist-tag would change model tooling without any repository change."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")

    match = re.search(r"^ARG PI_VERSION=([^\s]+)$", source, re.MULTILINE)

    assert match is not None
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", match.group(1)) is not None


def test_the_toolchain_does_not_live_under_the_home_a_tmpfs_shadows() -> None:
    """At run time /home/pi is an empty tmpfs. A uv or an interpreter installed
    there at build time would vanish, and the venv that symlinks to it with it."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")

    python_dir = re.search(r"^ENV UV_PYTHON_INSTALL_DIR=([^\s]+)$", source, re.MULTILINE)
    assert python_dir is not None
    assert not python_dir.group(1).startswith("/home/")
    assert "UV_UNMANAGED_INSTALL=/usr/local/bin" in source
    assert "/home/pi/.local/bin" not in source


def test_the_gh_wrapper_is_baked_in_and_shadows_the_real_binary() -> None:
    """The token is served from a file only if every `gh` -- the bootstrap's and
    the model's -- resolves to the wrapper first."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")

    assert "COPY --chmod=755 image/bin/gh /opt/agent/bin/gh" in source
    final_path = re.findall(r'^ENV PATH="([^"]+)"$', source, re.MULTILINE)[-1]
    assert final_path.split(":")[0] == "/opt/agent/bin"


def test_the_base_image_is_pinned_by_digest_with_its_tag_beside_it() -> None:
    """A tag can be moved; a digest cannot. The tag stays for humans and for
    Dependabot, which bumps the digest and keeps the tag."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")

    froms = re.findall(r"^FROM (\S+)$", source, re.MULTILINE)
    assert froms == [
        "node:24-bookworm-slim@sha256:"
        + "ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e"
    ]
    assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", froms[0]) is not None


def test_git_and_gh_are_pinned_to_exact_package_versions() -> None:
    """The two apt packages whose behaviour is the bot's behaviour. A build
    that takes whatever the archive serves today cannot be reproduced tomorrow."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")

    assert re.search(r"^ARG GIT_VERSION=\S+$", source, re.MULTILINE) is not None
    assert re.search(r"^ARG GH_VERSION=[0-9]+\.[0-9]+\.[0-9]+$", source, re.MULTILINE) is not None
    assert '"git=${GIT_VERSION}"' in source
    assert '"gh=${GH_VERSION}"' in source
    assert re.search(r"install -y --no-install-recommends gh\b", source) is None
