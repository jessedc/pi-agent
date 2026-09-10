#!/usr/bin/env python3
"""Apply safe auto-fixes: ruff format + ruff check --fix.

uv run scripts/fix.py
"""

from __future__ import annotations

import sys

from pi_agent.host.checks import fix

if __name__ == "__main__":
    sys.exit(fix())
