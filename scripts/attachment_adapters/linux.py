"""Linux X11/Wayland URI-list adapters (implemented, native-unverified)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from .base import AttachmentFailure, BridgeAttachmentTransaction


LinuxBackend = Literal["x11", "wayland"]


def select_linux_backend(environment: Mapping[str, str]) -> LinuxBackend | None:
    """Select an explicit display protocol; never silently cross-fallback."""

    session_type = environment.get("XDG_SESSION_TYPE", "").strip().lower()
    has_wayland = bool(environment.get("WAYLAND_DISPLAY"))
    has_x11 = bool(environment.get("DISPLAY"))
    if session_type == "wayland":
        return "wayland" if has_wayland else None
    if session_type == "x11":
        return "x11" if has_x11 else None
    if has_wayland and not has_x11:
        return "wayland"
    if has_x11 and not has_wayland:
        return "x11"
    return None


class LinuxURIListClipboardAdapter:
    verification_status = "implemented-unverified"
    recovery_filename = "clipboard-backup.json"

    def __init__(
        self,
        *,
        backend: LinuxBackend | None,
        bridge_override: str | None = None,
    ) -> None:
        self.backend = backend
        self.bridge_override = bridge_override
        self.command_prefix: tuple[str, ...] | None = None
        suffix = backend or "undetermined"
        self.name = f"linux-{suffix}-uri-list-clipboard"

    def assert_supported(self) -> None:
        if self.backend is None:
            raise AttachmentFailure(
                "ATTACHMENT_ADAPTER_UNAVAILABLE",
                "The Linux clipboard adapter requires an unambiguous X11 or Wayland graphical session.",
                next_step=(
                    "Set a consistent XDG_SESSION_TYPE plus DISPLAY or WAYLAND_DISPLAY. "
                    "Do not fall back to pasting a textual path."
                ),
            )

    def prepare(self, cache_root: Path) -> None:
        del cache_root
        self.assert_supported()
        assert self.backend is not None
        if self.bridge_override:
            candidate = Path(self.bridge_override).expanduser().resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise AttachmentFailure(
                    "CLIPBOARD_BACKUP_FAILED",
                    "The Linux clipboard bridge override is not executable.",
                )
            self.command_prefix = (str(candidate),)
            return
        copyq = shutil.which("copyq")
        bridge = Path(__file__).resolve().parents[1] / "linux_clipboard_bridge.py"
        if not copyq or not bridge.is_file():
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The Linux URI-list adapter requires CopyQ and its bundled bridge.",
                next_step="Install CopyQ for the active graphical session, start it, and retry.",
            )
        preflight_script = """
var result = {
    ok: true,
    copy_clipboard: str(config('copy_clipboard')),
    copy_selection: str(config('copy_selection'))
};
print(JSON.stringify(result) + '\\n');
undefined;
"""
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "xcb" if self.backend == "x11" else "wayland"
        try:
            preflight = subprocess.run(
                [copyq, "eval", "-"],
                input=preflight_script,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                f"CopyQ preflight failed: {exc}.",
            ) from exc
        try:
            status = json.loads(preflight.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "CopyQ returned an invalid non-mutating preflight response.",
            ) from exc
        if preflight.returncode != 0 or not isinstance(status, dict) or status.get("ok") is not True:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The running CopyQ server did not pass its non-mutating scripting preflight.",
                next_step="Start CopyQ in the selected display session and retry.",
            )
        if any(
            str(status.get(option, "")).casefold() == "true"
            for option in ("copy_clipboard", "copy_selection")
        ):
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "CopyQ clipboard/PRIMARY synchronization must be disabled for a transactional attachment.",
                next_step=(
                    "Disable CopyQ's clipboard-to-selection and selection-to-clipboard synchronization, "
                    "then retry."
                ),
            )
        self.command_prefix = (
            sys.executable,
            str(bridge),
            "--copyq",
            copyq,
            "--backend",
            self.backend,
        )

    def transaction(
        self,
        *,
        video_path: Path,
        recovery_path: Path,
    ) -> BridgeAttachmentTransaction:
        if self.command_prefix is None:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The Linux attachment adapter was not prepared.",
            )
        return BridgeAttachmentTransaction(
            command_prefix=self.command_prefix,
            video_path=video_path,
            recovery_path=recovery_path,
            expected_staged_types=("text/uri-list", "x-special/gnome-copied-files"),
            backup_security="posix",
        )


class LinuxX11URIListClipboardAdapter(LinuxURIListClipboardAdapter):
    def __init__(self, bridge_override: str | None = None) -> None:
        super().__init__(backend="x11", bridge_override=bridge_override)


class LinuxWaylandURIListClipboardAdapter(LinuxURIListClipboardAdapter):
    def __init__(self, bridge_override: str | None = None) -> None:
        super().__init__(backend="wayland", bridge_override=bridge_override)
