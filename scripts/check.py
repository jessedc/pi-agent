#!/usr/bin/env python3
"""Validate: format check + lint + tests + static type analysis.

uv run scripts/check.py            # check everything (read-only)
uv run scripts/check.py --fix      # apply safe auto-fixes first, then check
uv run scripts/check.py --docker   # also build the image
"""

from __future__ import annotations

import sys

from pi_agent.host.checks import check

if __name__ == "__main__":
    sys.exit(check())
