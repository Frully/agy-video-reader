"""Small shared contract for platform clipboard attachment adapters."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Literal, Protocol


BRIDGE_ERROR_CODES = {
    "CLIPBOARD_BACKUP_FAILED",
    "CLIPBOARD_CHANGED_EXTERNALLY",
    "CLIPBOARD_RESTORE_FAILED",
}


class AttachmentFailure(Exception):
    """Adapter error that the runner maps onto its stable error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        clipboard_restored: bool | None = None,
        next_step: str = "Review the attachment error before retrying.",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.clipboard_restored = clipboard_restored
        self.next_step = next_step


class AttachmentTransaction(Protocol):
    paste_bytes: bytes
    recovery_path: Path
    clipboard_restored: bool | None

    @property
    def restore_required(self) -> bool: ...

    def stage(self) -> None: ...

    def restore(self) -> None: ...


class AttachmentAdapter(Protocol):
    name: str
    verification_status: str
    recovery_filename: str

    def assert_supported(self) -> None: ...

    def prepare(self, cache_root: Path) -> None: ...

    def transaction(
        self,
        *,
        video_path: Path,
        recovery_path: Path,
    ) -> AttachmentTransaction: ...


class UnsupportedAttachmentAdapter:
    """Fail-closed sentinel for selectors with no concrete implementation."""

    name = "unsupported"
    verification_status = "unavailable"
    recovery_filename = "clipboard-backup"

    def __init__(self, platform_name: str, *, message: str | None = None) -> None:
        self.platform_name = platform_name
        self.message = message

    def assert_supported(self) -> None:
        raise AttachmentFailure(
            "ATTACHMENT_ADAPTER_UNAVAILABLE",
            self.message or f"No attachment adapter is implemented for platform selector {self.platform_name!r}.",
            next_step=(
                "Use a platform with a concrete adapter. Do not fall back to pasting a textual path."
            ),
        )

    def prepare(self, cache_root: Path) -> None:
        del cache_root
        self.assert_supported()

    def transaction(
        self,
        *,
        video_path: Path,
        recovery_path: Path,
    ) -> AttachmentTransaction:
        del video_path, recovery_path
        self.assert_supported()
        raise AssertionError("unreachable")


class BridgeAttachmentTransaction:
    """Transaction protocol shared by the three native bridge frontends."""

    paste_bytes = b"\x16"

    def __init__(
        self,
        *,
        command_prefix: tuple[str, ...],
        video_path: Path,
        recovery_path: Path,
        expected_staged_types: tuple[str, ...],
        backup_security: Literal["posix", "windows"],
        max_staged_change_count: int | None = None,
    ) -> None:
        self.command_prefix = command_prefix
        self.video_path = video_path
        self.recovery_path = recovery_path
        self.expected_staged_types = expected_staged_types
        self.backup_security = backup_security
        self.max_staged_change_count = max_staged_change_count
        self.clipboard_restored: bool | None = None
        self._staged_change_count: int | None = None

    @property
    def restore_required(self) -> bool:
        if self._staged_change_count is not None and self.clipboard_restored is not True:
            return True
        if not self.recovery_path.exists():
            return False
        # A failed stage can leave a complete recovery file. Recover verifies
        # whether the bridge's staged value is still current before restoring.
        return self.clipboard_restored is not True or self._staged_change_count is None

    def _command(
        self,
        operation: str,
        arguments: list[str],
        default_code: str,
    ) -> dict[str, object]:
        argv = [*self.command_prefix, operation, *arguments]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AttachmentFailure(default_code, f"Clipboard bridge failed: {exc}.") from exc
        try:
            payload = json.loads(completed.stdout or completed.stderr)
        except json.JSONDecodeError:
            payload = {}
        if completed.returncode != 0:
            proposed = payload.get("code", default_code) if isinstance(payload, dict) else default_code
            code = proposed if isinstance(proposed, str) and proposed in BRIDGE_ERROR_CODES else default_code
            message = (
                payload.get("message", "Clipboard bridge returned an error.")
                if isinstance(payload, dict)
                else "Clipboard bridge returned an error."
            )
            restored = payload.get("clipboard_restored") if isinstance(payload, dict) else None
            self.clipboard_restored = restored if isinstance(restored, bool) else None
            raise AttachmentFailure(
                code,
                str(message),
                clipboard_restored=self.clipboard_restored,
                next_step="Do not overwrite the clipboard. Follow the recovery-backup instruction.",
            )
        if not isinstance(payload, dict):
            raise AttachmentFailure(default_code, "Clipboard bridge returned an invalid status.")
        if payload.get("ok") is not True or payload.get("operation") != operation:
            raise AttachmentFailure(default_code, "Clipboard bridge returned an invalid success status.")
        if operation in {"restore", "recover"}:
            restored_change_count = payload.get("restored_change_count")
            if (
                payload.get("backup_deleted") is not True
                or isinstance(restored_change_count, bool)
                or not isinstance(restored_change_count, int)
                or restored_change_count < 0
                or self.recovery_path.exists()
            ):
                raise AttachmentFailure(
                    "CLIPBOARD_RESTORE_FAILED",
                    "Clipboard bridge returned an invalid restoration confirmation.",
                )
        return payload

    def _require_private_recovery_backup(self) -> None:
        try:
            info = self.recovery_path.lstat()
        except OSError as exc:
            self.clipboard_restored = False
            raise AttachmentFailure(
                "CLIPBOARD_RESTORE_FAILED",
                "The clipboard bridge reported success without a usable recovery backup.",
                clipboard_restored=False,
                next_step=(
                    "Do not paste the attachment. The original clipboard cannot be restored automatically "
                    "because its private backup is missing or inaccessible."
                ),
            ) from exc
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        file_attributes = getattr(info, "st_file_attributes", 0)
        unsafe = (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size <= 0
            or bool(file_attributes & reparse_attribute)
        )
        if self.backup_security == "posix":
            getuid = getattr(os, "getuid", None)
            unsafe = unsafe or stat.S_IMODE(info.st_mode) != 0o600
            if getuid is not None:
                unsafe = unsafe or info.st_uid != getuid()
        if unsafe:
            self.clipboard_restored = False
            raise AttachmentFailure(
                "CLIPBOARD_RESTORE_FAILED",
                "The clipboard bridge created an unsafe recovery backup.",
                clipboard_restored=False,
                next_step=(
                    "Do not paste the attachment. Preserve the runtime directory for inspection; "
                    "the clipboard backup is not a private single-owner regular file."
                ),
            )

    def stage(self) -> None:
        payload = self._command(
            "stage",
            ["--file", str(self.video_path), "--backup", str(self.recovery_path)],
            "CLIPBOARD_BACKUP_FAILED",
        )
        try:
            change_count = payload["staged_change_count"]
            staged_types = payload["staged_types"]
            if (
                isinstance(change_count, bool)
                or not isinstance(change_count, int)
                or change_count < 0
                or (
                    self.max_staged_change_count is not None
                    and change_count > self.max_staged_change_count
                )
                or staged_types != list(self.expected_staged_types)
            ):
                raise ValueError("invalid stage confirmation")
            self._staged_change_count = change_count
        except (KeyError, TypeError, ValueError) as exc:
            raise AttachmentFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "Clipboard bridge returned an invalid stage confirmation.",
            ) from exc
        self._require_private_recovery_backup()

    def restore(self) -> None:
        if not self.recovery_path.exists():
            if self._staged_change_count is not None and self.clipboard_restored is not True:
                self.clipboard_restored = False
                raise AttachmentFailure(
                    "CLIPBOARD_RESTORE_FAILED",
                    "The clipboard recovery backup disappeared after attachment staging.",
                    clipboard_restored=False,
                    next_step=(
                        "The original clipboard cannot be restored automatically because its private backup "
                        "is missing. Replace the clipboard contents deliberately before retrying."
                    ),
                )
            if self.clipboard_restored is None:
                self.clipboard_restored = True
            return
        try:
            if self._staged_change_count is None:
                self._command(
                    "recover",
                    ["--backup", str(self.recovery_path), "--file", str(self.video_path)],
                    "CLIPBOARD_RESTORE_FAILED",
                )
            else:
                self._command(
                    "restore",
                    [
                        "--backup",
                        str(self.recovery_path),
                        "--expected-change-count",
                        str(self._staged_change_count),
                    ],
                    "CLIPBOARD_RESTORE_FAILED",
                )
            self.clipboard_restored = True
        except AttachmentFailure as exc:
            self.clipboard_restored = exc.clipboard_restored
            status_command = [*self.command_prefix, "status"]
            if os.name == "nt":
                rendered_status = subprocess.list2cmdline(status_command)
            else:
                rendered_status = shlex.join(status_command)
            exc.next_step = (
                f"Preserve {self.recovery_path}. Inspect the current clipboard with {rendered_status}, "
                "then retry deliberate recovery only if it will not overwrite newer clipboard data."
            )
            raise
