"""macOS file-URL clipboard adapter."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .base import AttachmentFailure, BridgeAttachmentTransaction


class MacOSFileURLClipboardAdapter:
    name = "macos-file-url-clipboard"
    verification_status = "implemented-native-evidence"
    recovery_filename = "clipboard-backup.plist"

    def __init__(self, bridge_override: str | None = None) -> None:
        self.bridge_override = bridge_override
        self.command_prefix: tuple[str, ...] | None = None

    def assert_supported(self) -> None:
        return

    def prepare(self, cache_root: Path) -> None:
        if self.bridge_override:
            candidate = Path(self.bridge_override).expanduser().resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise AttachmentFailure(
                    "CLIPBOARD_BACKUP_FAILED",
                    "The clipboard bridge override is not executable.",
                )
            self.command_prefix = (str(candidate),)
            return

        source = Path(__file__).resolve().parents[1] / "clipboard_bridge.swift"
        swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
        if not source.is_file() or not Path(swiftc).exists():
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The native clipboard bridge cannot be built.",
                next_step="Install the macOS command-line developer tools and retry.",
            )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        binary = cache_root / "bin" / f"clipboard-bridge-{digest}"
        if not binary.exists():
            binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = binary.with_name(binary.name + f".{os.getpid()}.tmp")
            try:
                completed = subprocess.run(
                    [swiftc, str(source), "-O", "-o", str(temporary)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                temporary.unlink(missing_ok=True)
                raise AttachmentFailure(
                    "CLIPBOARD_BACKUP_FAILED",
                    f"Clipboard bridge compilation failed: {exc}.",
                ) from exc
            if completed.returncode != 0:
                temporary.unlink(missing_ok=True)
                raise AttachmentFailure(
                    "CLIPBOARD_BACKUP_FAILED",
                    f"Clipboard bridge compilation failed with exit status {completed.returncode}.",
                )
            os.chmod(temporary, 0o700)
            os.replace(temporary, binary)
        self.command_prefix = (str(binary),)

    def transaction(
        self,
        *,
        video_path: Path,
        recovery_path: Path,
    ) -> BridgeAttachmentTransaction:
        if self.command_prefix is None:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The macOS attachment adapter was not prepared.",
            )
        return BridgeAttachmentTransaction(
            command_prefix=self.command_prefix,
            video_path=video_path,
            recovery_path=recovery_path,
            expected_staged_types=("public.file-url",),
            backup_security="posix",
        )
