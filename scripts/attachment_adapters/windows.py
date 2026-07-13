"""Windows CF_HDROP clipboard adapter (implemented, target-OS unverified)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import AttachmentFailure, BridgeAttachmentTransaction


class WindowsCFHDropClipboardAdapter:
    name = "windows-cf-hdrop-clipboard"
    verification_status = "implemented-unverified"
    recovery_filename = "clipboard-backup.json"

    def __init__(self, bridge_override: str | None = None) -> None:
        self.bridge_override = bridge_override
        self.command_prefix: tuple[str, ...] | None = None

    def assert_supported(self) -> None:
        return

    def prepare(self, cache_root: Path) -> None:
        del cache_root
        if self.bridge_override:
            candidate = Path(self.bridge_override).expanduser().resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise AttachmentFailure(
                    "CLIPBOARD_BACKUP_FAILED",
                    "The Windows clipboard bridge override is not executable.",
                )
            self.command_prefix = (str(candidate),)
            return
        bridge = Path(__file__).resolve().parents[1] / "windows_clipboard_bridge.py"
        if not bridge.is_file():
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The Windows CF_HDROP clipboard bridge is missing.",
            )
        self.command_prefix = (sys.executable, str(bridge))

    def transaction(
        self,
        *,
        video_path: Path,
        recovery_path: Path,
    ) -> BridgeAttachmentTransaction:
        if self.command_prefix is None:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The Windows attachment adapter was not prepared.",
            )
        return BridgeAttachmentTransaction(
            command_prefix=self.command_prefix,
            video_path=video_path,
            recovery_path=recovery_path,
            expected_staged_types=("CF_HDROP",),
            backup_security="windows",
            max_staged_change_count=0xFFFFFFFF,
        )
