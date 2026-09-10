"""Container that turns a GitHub issue into a draft pull request.

The package holds two halves that never meet at runtime:

``pi_agent.host``
    Runs on a developer's machine. Reads ``.env``, resolves the model host on
    the tailnet, and builds the ``docker run`` command line.

``pi_agent.container``
    Runs as the image's entrypoint. Mints a GitHub App token, settles the git
    identity behind it, clones the target repository, validates the issue, and
    hands off to ``pi``.

Neither half imports the other. What they share -- logging and the subprocess
seam -- lives in the private modules beside this one.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
