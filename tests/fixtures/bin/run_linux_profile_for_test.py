#!/usr/bin/env python3
"""Test-only launcher for the deterministic native-Linux adapter profile."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[3] / "scripts" / "run_antigravity_video.py"


def main() -> None:
    sys.platform = "linux"
    os.environ["XDG_SESSION_TYPE"] = "x11"
    os.environ["DISPLAY"] = ":99"
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["FAKE_CLIPBOARD_STAGE_TYPES"] = (
        '["text/uri-list","x-special/gnome-copied-files"]'
    )
    runpy.run_path(str(RUNNER), run_name="__main__")


if __name__ == "__main__":
    main()
