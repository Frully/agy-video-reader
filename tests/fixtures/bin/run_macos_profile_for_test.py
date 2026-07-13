#!/usr/bin/env python3
"""Test-only launcher for the deterministic macOS attachment profile.

This keeps platform bypasses out of the production runner while allowing the
fake PTY/bridge integration suite to run on POSIX CI hosts such as Linux.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[3] / "scripts" / "run_antigravity_video.py"


def main() -> None:
    sys.platform = "darwin"
    runpy.run_path(str(RUNNER), run_name="__main__")


if __name__ == "__main__":
    main()
