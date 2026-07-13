#!/usr/bin/env python3
"""Compatibility entrypoint for the split platform attachment adapters."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from attachment_adapters import (  # noqa: E402,F401
    AttachmentAdapter,
    AttachmentFailure,
    AttachmentTransaction,
    LinuxURIListClipboardAdapter,
    LinuxWaylandURIListClipboardAdapter,
    LinuxX11URIListClipboardAdapter,
    MacOSFileURLClipboardAdapter,
    UnsupportedAttachmentAdapter,
    WindowsCFHDropClipboardAdapter,
    create_attachment_adapter,
)


__all__ = [
    "AttachmentAdapter",
    "AttachmentFailure",
    "AttachmentTransaction",
    "LinuxURIListClipboardAdapter",
    "LinuxWaylandURIListClipboardAdapter",
    "LinuxX11URIListClipboardAdapter",
    "MacOSFileURLClipboardAdapter",
    "UnsupportedAttachmentAdapter",
    "WindowsCFHDropClipboardAdapter",
    "create_attachment_adapter",
]
