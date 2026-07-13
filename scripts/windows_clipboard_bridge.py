#!/usr/bin/env python3
"""Transactional CF_HDROP clipboard bridge for Windows.

The Win32 backend is intentionally isolated behind a small protocol so the
state machine and backup format can be tested without touching a real
clipboard.  The native path is implemented but remains target-OS unverified.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import os
import stat
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator, Protocol, Sequence


BACKUP_FORMAT_VERSION = 1
CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
MAX_FORMATS = 256
MAX_CLIPBOARD_BYTES = 64 * 1024 * 1024
MAX_BACKUP_BYTES = 96 * 1024 * 1024
REGISTERED_FORMAT_FIRST = 0xC000
PRIVATE_FORMAT_FIRST = 0x0200
PRIVATE_FORMAT_LAST = 0x02FF
GDI_FORMAT_FIRST = 0x0300
GDI_FORMAT_LAST = 0x03FF
MAX_REGISTERED_FORMAT_NAME = 1023

# These formats carry GDI handles or owner callbacks rather than opaque
# HGLOBAL bytes.  A generic serializer cannot restore their semantics.
UNSUPPORTED_HANDLE_FORMATS = {
    CF_BITMAP,
    CF_METAFILEPICT,
    CF_PALETTE,
    CF_ENHMETAFILE,
    0x0080,  # CF_OWNERDISPLAY
    0x0082,  # CF_DSPBITMAP
    0x0083,  # CF_DSPMETAFILEPICT
    0x008E,  # CF_DSPENHMETAFILE
}

TEXT_CONVERSION_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT})
BITMAP_CONVERSION_FORMATS = frozenset({CF_BITMAP, CF_DIB, CF_PALETTE, CF_DIBV5})
METAFILE_CONVERSION_FORMATS = frozenset({CF_METAFILEPICT, CF_ENHMETAFILE})


class BridgeFailure(Exception):
    """Machine-readable bridge failure."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int,
        *,
        backup_preserved: bool = False,
        clipboard_restored: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.backup_preserved = backup_preserved
        self.clipboard_restored = clipboard_restored


class ClipboardBackendError(Exception):
    """Native backend failure, optionally after clipboard mutation."""

    def __init__(
        self,
        message: str,
        *,
        may_have_changed: bool = False,
        resulting_sequence: int | None = None,
        resulting_formats: tuple[ClipboardFormat, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.may_have_changed = may_have_changed
        self.resulting_sequence = resulting_sequence
        self.resulting_formats = resulting_formats


class ClipboardSequenceMismatch(ClipboardBackendError):
    """The clipboard changed before a guarded replacement."""


@dataclass(frozen=True)
class ClipboardFormat:
    format_id: int
    name: str | None
    data: bytes


@dataclass(frozen=True)
class ClipboardSnapshot:
    sequence: int
    formats: tuple[ClipboardFormat, ...]


@dataclass(frozen=True)
class ClipboardBackup:
    original: tuple[ClipboardFormat, ...]
    staged: ClipboardFormat


class ClipboardBackend(Protocol):
    def sequence(self) -> int: ...

    def snapshot(self) -> ClipboardSnapshot: ...

    def replace(
        self,
        formats: Sequence[ClipboardFormat],
        *,
        expected_sequence: int | None,
    ) -> int: ...


def encode_cf_hdrop(path: Path) -> bytes:
    """Encode one absolute path as Unicode DROPFILES/CF_HDROP bytes."""

    value = str(path)
    if not path.is_absolute() or not value or "\x00" in value:
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            "The staged file path must be an absolute path without NUL characters.",
            2,
        )
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + (value + "\x00\x00").encode("utf-16-le")


def _format_digest(formats: Sequence[ClipboardFormat]) -> str:
    digest = hashlib.sha256()
    for item in formats:
        name = (item.name or "").encode("utf-8")
        digest.update(struct.pack("<IIQ", item.format_id, len(name), len(item.data)))
        digest.update(name)
        digest.update(item.data)
    return digest.hexdigest()


def _formats_equal(
    left: Sequence[ClipboardFormat],
    right: Sequence[ClipboardFormat],
) -> bool:
    return tuple(left) == tuple(right)


def canonical_format_ids(format_ids: Sequence[int]) -> tuple[int, ...]:
    """Collapse Win32's synthesized conversion entries to one restorable source.

    EnumClipboardFormats returns the explicitly stored format followed by the
    formats Windows can synthesize from it. Keeping every enumerated ID would
    misclassify a safe DIB as an unsafe bitmap and would make restoration add
    redundant explicit text formats. Windows enumerates the actual stored
    format before its synthesized conversions, so preserve the first member of
    each group. If that first member is a handle-based format, leave it in place
    so normal validation fails closed rather than silently saving a converted
    representation in place of the original.
    """

    values = tuple(format_ids)

    def selected(group: frozenset[int]) -> int | None:
        candidates = [value for value in values if value in group]
        if not candidates:
            return None
        return candidates[0]

    keep_text = selected(TEXT_CONVERSION_FORMATS)
    keep_bitmap = selected(BITMAP_CONVERSION_FORMATS)
    keep_metafile = selected(METAFILE_CONVERSION_FORMATS)
    result: list[int] = []
    for value in values:
        if value in TEXT_CONVERSION_FORMATS and value != keep_text:
            continue
        if value in BITMAP_CONVERSION_FORMATS and value != keep_bitmap:
            continue
        if value in METAFILE_CONVERSION_FORMATS and value != keep_metafile:
            continue
        result.append(value)
    return tuple(result)


def _serialized_format(item: ClipboardFormat) -> dict[str, object]:
    return {
        "format_id": item.format_id,
        "name": item.name,
        "data_base64": base64.b64encode(item.data).decode("ascii"),
        "sha256": hashlib.sha256(item.data).hexdigest(),
    }


def _serialize_backup(backup: ClipboardBackup) -> bytes:
    root = {
        "format_version": BACKUP_FORMAT_VERSION,
        "formats": [_serialized_format(item) for item in backup.original],
        "snapshot_sha256": _format_digest(backup.original),
        "staged": _serialized_format(backup.staged),
    }
    raw = json.dumps(root, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BACKUP_BYTES:
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "The clipboard recovery backup exceeds the safe size limit; the clipboard was not changed.",
            3,
        )
    return raw


def _parse_format(value: object, *, restore_error: bool) -> ClipboardFormat:
    code = "CLIPBOARD_RESTORE_FAILED" if restore_error else "CLIPBOARD_BACKUP_FAILED"
    exit_code = 5 if restore_error else 3
    if not isinstance(value, dict) or set(value) != {
        "format_id",
        "name",
        "data_base64",
        "sha256",
    }:
        raise BridgeFailure(
            code,
            "The clipboard recovery backup contains an invalid format entry.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )
    format_id = value["format_id"]
    name = value["name"]
    encoded = value["data_base64"]
    expected_hash = value["sha256"]
    if (
        isinstance(format_id, bool)
        or not isinstance(format_id, int)
        or not 0 < format_id <= 0xFFFF
        or (
            name is not None
            and (
                not isinstance(name, str)
                or not name
                or len(name) > MAX_REGISTERED_FORMAT_NAME
                or "\x00" in name
            )
        )
        or not isinstance(encoded, str)
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
    ):
        raise BridgeFailure(
            code,
            "The clipboard recovery backup contains invalid format metadata.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )
    if (format_id >= REGISTERED_FORMAT_FIRST) != (name is not None):
        raise BridgeFailure(
            code,
            "The clipboard recovery backup has inconsistent registered-format metadata.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise BridgeFailure(
            code,
            "The clipboard recovery backup contains invalid base64 data.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        ) from exc
    if not data or hashlib.sha256(data).hexdigest() != expected_hash:
        raise BridgeFailure(
            code,
            "The clipboard recovery backup failed its data checksum.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )
    return ClipboardFormat(format_id, name if isinstance(name, str) else None, data)


def _validate_format_set(formats: Sequence[ClipboardFormat], *, restore_error: bool) -> None:
    code = "CLIPBOARD_RESTORE_FAILED" if restore_error else "CLIPBOARD_BACKUP_FAILED"
    exit_code = 5 if restore_error else 3
    if len(formats) > MAX_FORMATS:
        raise BridgeFailure(
            code,
            "The clipboard has too many formats to preserve safely.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )
    total = 0
    format_ids: set[int] = set()
    registered_names: set[str] = set()
    for item in formats:
        total += len(item.data)
        registered_metadata_ok = (
            (item.format_id >= REGISTERED_FORMAT_FIRST) == (item.name is not None)
        )
        serializable_handle = (
            item.format_id not in UNSUPPORTED_HANDLE_FORMATS
            and not PRIVATE_FORMAT_FIRST <= item.format_id <= PRIVATE_FORMAT_LAST
            and not GDI_FORMAT_FIRST <= item.format_id <= GDI_FORMAT_LAST
        )
        valid_name = item.name is None or (
            0 < len(item.name) <= MAX_REGISTERED_FORMAT_NAME and "\x00" not in item.name
        )
        folded_name = item.name.casefold() if item.name is not None else None
        if (
            item.format_id in format_ids
            or (folded_name is not None and folded_name in registered_names)
            or not item.data
            or not registered_metadata_ok
            or not serializable_handle
            or not valid_name
        ):
            raise BridgeFailure(
                code,
                "The clipboard format set contains duplicate, empty, owner-dependent, or non-HGLOBAL data.",
                exit_code,
                backup_preserved=restore_error,
                clipboard_restored=False if restore_error else None,
            )
        format_ids.add(item.format_id)
        if folded_name is not None:
            registered_names.add(folded_name)
    if total > MAX_CLIPBOARD_BYTES:
        raise BridgeFailure(
            code,
            "The clipboard data exceeds the safe backup size limit.",
            exit_code,
            backup_preserved=restore_error,
            clipboard_restored=False if restore_error else None,
        )


def _write_private_backup(path: Path, backup: ClipboardBackup) -> None:
    raw = _serialize_backup(backup)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "A private clipboard backup file could not be created.",
            3,
        ) from exc
    completed = False
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("backup is not a single regular file")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short backup write")
            offset += written
        os.fsync(descriptor)
        completed = True
    except OSError as exc:
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "The private clipboard backup could not be secured.",
            3,
        ) from exc
    finally:
        os.close(descriptor)
        if not completed:
            path.unlink(missing_ok=True)


def _read_private_backup(path: Path) -> ClipboardBackup:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is missing or unsafe.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    try:
        info = os.fstat(descriptor)
        private_mode = os.name == "nt" or stat.S_IMODE(info.st_mode) == 0o600
        owner_ok = not hasattr(os, "getuid") or info.st_uid == os.getuid()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not private_mode
            or not owner_ok
            or info.st_size <= 0
            or info.st_size > MAX_BACKUP_BYTES
        ):
            raise OSError("unsafe backup metadata")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise OSError("short backup read")
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is missing, incomplete, or unsafe.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    finally:
        os.close(descriptor)
    try:
        root = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is not valid UTF-8 JSON.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    if not isinstance(root, dict) or set(root) != {
        "format_version",
        "formats",
        "snapshot_sha256",
        "staged",
    } or root["format_version"] != BACKUP_FORMAT_VERSION:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup has an unsupported schema.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        )
    raw_formats = root["formats"]
    if not isinstance(raw_formats, list):
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup has no valid format list.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        )
    original = tuple(_parse_format(item, restore_error=True) for item in raw_formats)
    staged = _parse_format(root["staged"], restore_error=True)
    _validate_format_set(original, restore_error=True)
    if staged.format_id != CF_HDROP or staged.name is not None:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup does not identify a CF_HDROP stage.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        )
    if root["snapshot_sha256"] != _format_digest(original):
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup failed its snapshot checksum.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        )
    return ClipboardBackup(original, staged)


def _delete_backup(path: Path, *, clipboard_restored: bool) -> None:
    try:
        path.unlink()
    except OSError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard was restored, but its recovery backup could not be deleted.",
            5,
            backup_preserved=True,
            clipboard_restored=clipboard_restored,
        ) from exc


class Win32ClipboardBackend:
    """Opaque-HGLOBAL Win32 clipboard backend implemented with ctypes."""

    def __init__(self) -> None:
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise ClipboardBackendError("The Win32 clipboard API is unavailable on this platform.")
        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        w = self.wintypes
        self.user32.CreateWindowExW.argtypes = [
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            w.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = w.HWND
        self.user32.DestroyWindow.argtypes = [w.HWND]
        self.user32.DestroyWindow.restype = w.BOOL
        self.user32.OpenClipboard.argtypes = [w.HWND]
        self.user32.OpenClipboard.restype = w.BOOL
        self.user32.CloseClipboard.restype = w.BOOL
        self.user32.EmptyClipboard.restype = w.BOOL
        self.user32.EnumClipboardFormats.argtypes = [w.UINT]
        self.user32.EnumClipboardFormats.restype = w.UINT
        self.user32.GetClipboardData.argtypes = [w.UINT]
        self.user32.GetClipboardData.restype = w.HANDLE
        self.user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
        self.user32.SetClipboardData.restype = w.HANDLE
        self.user32.GetClipboardSequenceNumber.restype = w.DWORD
        self.user32.GetClipboardFormatNameW.argtypes = [w.UINT, w.LPWSTR, ctypes.c_int]
        self.user32.GetClipboardFormatNameW.restype = ctypes.c_int
        self.user32.RegisterClipboardFormatW.argtypes = [w.LPCWSTR]
        self.user32.RegisterClipboardFormatW.restype = w.UINT
        self.kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = w.HMODULE
        self.kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = w.HGLOBAL
        self.kernel32.GlobalFree.argtypes = [w.HGLOBAL]
        self.kernel32.GlobalFree.restype = w.HGLOBAL
        self.kernel32.GlobalLock.argtypes = [w.HGLOBAL]
        self.kernel32.GlobalLock.restype = w.LPVOID
        self.kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
        self.kernel32.GlobalUnlock.restype = w.BOOL
        self.kernel32.GlobalSize.argtypes = [w.HGLOBAL]
        self.kernel32.GlobalSize.restype = ctypes.c_size_t

    def _error(self, action: str) -> ClipboardBackendError:
        return ClipboardBackendError(f"{action} failed with Win32 error {ctypes.get_last_error()}.")

    def _create_owner_window(self):
        instance = self.kernel32.GetModuleHandleW(None)
        window = self.user32.CreateWindowExW(
            0,
            "STATIC",
            "AntigravityClipboardOwner",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not window:
            raise self._error("CreateWindowExW")
        return window

    @contextlib.contextmanager
    def _opened(self) -> Iterator[None]:
        window = self._create_owner_window()
        opened = False
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.user32.OpenClipboard(window):
                    opened = True
                    break
                time.sleep(0.02)
            if not opened:
                raise self._error("OpenClipboard")
            yield
        finally:
            if opened:
                self.user32.CloseClipboard()
            self.user32.DestroyWindow(window)

    def sequence(self) -> int:
        return int(self.user32.GetClipboardSequenceNumber())

    def _registered_name(self, format_id: int) -> str | None:
        if format_id < REGISTERED_FORMAT_FIRST:
            return None
        buffer = ctypes.create_unicode_buffer(1024)
        length = self.user32.GetClipboardFormatNameW(format_id, buffer, len(buffer))
        if length <= 0 or length >= len(buffer):
            raise self._error("GetClipboardFormatNameW")
        return buffer.value

    def snapshot(self) -> ClipboardSnapshot:
        result: list[ClipboardFormat] = []
        total = 0
        with self._opened():
            available: list[int] = []
            previous = 0
            while True:
                ctypes.set_last_error(0)
                format_id = int(self.user32.EnumClipboardFormats(previous))
                if format_id == 0:
                    if ctypes.get_last_error() != 0:
                        raise self._error("EnumClipboardFormats")
                    break
                if len(available) >= MAX_FORMATS:
                    raise ClipboardBackendError("The clipboard exposes too many formats.")
                available.append(format_id)
                previous = format_id
            for format_id in canonical_format_ids(available):
                if (
                    format_id in UNSUPPORTED_HANDLE_FORMATS
                    or PRIVATE_FORMAT_FIRST <= format_id <= PRIVATE_FORMAT_LAST
                    or GDI_FORMAT_FIRST <= format_id <= GDI_FORMAT_LAST
                ):
                    raise ClipboardBackendError(
                        f"Clipboard format {format_id} cannot be losslessly serialized as HGLOBAL bytes."
                    )
                handle = self.user32.GetClipboardData(format_id)
                if not handle:
                    raise self._error("GetClipboardData")
                size = int(self.kernel32.GlobalSize(handle))
                if size <= 0 or total + size > MAX_CLIPBOARD_BYTES:
                    raise ClipboardBackendError("A clipboard format has an invalid or oversized HGLOBAL.")
                pointer = self.kernel32.GlobalLock(handle)
                if not pointer:
                    raise self._error("GlobalLock")
                try:
                    data = ctypes.string_at(pointer, size)
                finally:
                    self.kernel32.GlobalUnlock(handle)
                result.append(ClipboardFormat(format_id, self._registered_name(format_id), data))
                total += size
            sequence = self.sequence()
        _validate_format_set(result, restore_error=False)
        return ClipboardSnapshot(sequence, tuple(result))

    def _allocate(self, data: bytes):
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
        if not handle:
            raise self._error("GlobalAlloc")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            self.kernel32.GlobalFree(handle)
            raise self._error("GlobalLock")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            self.kernel32.GlobalUnlock(handle)
        return handle

    def _resolved_format_id(self, item: ClipboardFormat) -> int:
        if item.name is None:
            return item.format_id
        result = int(self.user32.RegisterClipboardFormatW(item.name))
        if result == 0:
            raise self._error("RegisterClipboardFormatW")
        return result

    def replace(
        self,
        formats: Sequence[ClipboardFormat],
        *,
        expected_sequence: int | None,
    ) -> int:
        _validate_format_set(formats, restore_error=False)
        resolved: list[int] = []
        for item in formats:
            format_id = self._resolved_format_id(item)
            if format_id in resolved:
                raise ClipboardBackendError(
                    "Multiple clipboard formats resolve to the same registered identifier."
                )
            resolved.append(format_id)
        prepared: list[tuple[int, object]] = []
        try:
            for format_id, item in zip(resolved, formats, strict=True):
                prepared.append((format_id, self._allocate(item.data)))
        except ClipboardBackendError:
            for _, handle in prepared:
                self.kernel32.GlobalFree(handle)
            raise

        transferred: set[int] = set()
        mutated = False
        resulting_sequence: int | None = None
        try:
            with self._opened():
                current = self.sequence()
                if expected_sequence is not None and current != expected_sequence:
                    raise ClipboardSequenceMismatch(
                        f"Expected clipboard sequence {expected_sequence}, found {current}."
                    )
                try:
                    if not self.user32.EmptyClipboard():
                        raise self._error("EmptyClipboard")
                    mutated = True
                    for index, (format_id, handle) in enumerate(prepared):
                        if not self.user32.SetClipboardData(format_id, handle):
                            raise self._error("SetClipboardData")
                        transferred.add(index)
                    resulting_sequence = self.sequence()
                except ClipboardBackendError as exc:
                    # Capture identity while OpenClipboard still excludes other
                    # writers. Reading it after CloseClipboard would let a new
                    # clipboard value masquerade as our partial replacement.
                    partial_sequence = self.sequence() if mutated else None
                    partial_formats = (
                        tuple(formats[index] for index in sorted(transferred))
                        if mutated
                        else None
                    )
                    raise ClipboardBackendError(
                        str(exc),
                        may_have_changed=mutated,
                        resulting_sequence=partial_sequence,
                        resulting_formats=partial_formats,
                    ) from exc
        except ClipboardSequenceMismatch:
            raise
        finally:
            for index, (_, handle) in enumerate(prepared):
                if index not in transferred:
                    self.kernel32.GlobalFree(handle)
        assert resulting_sequence is not None
        return resulting_sequence


def _require_absolute(value: str, description: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute():
        raise BridgeFailure("INVALID_ARGUMENTS", f"{description} must be absolute.", 2)
    return path


def _validate_stage_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            "The staged file must be an existing regular file.",
            2,
        ) from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            "The staged file must not be a link, directory, pipe, or device.",
            2,
        )


def _verified_snapshot(
    backend: ClipboardBackend,
    expected_sequence: int,
    expected_formats: Sequence[ClipboardFormat],
    *,
    restore_error: bool,
) -> ClipboardSnapshot:
    try:
        actual = backend.snapshot()
    except ClipboardBackendError as exc:
        code = "CLIPBOARD_RESTORE_FAILED" if restore_error else "CLIPBOARD_BACKUP_FAILED"
        raise BridgeFailure(
            code,
            "The clipboard contents could not be verified after replacement.",
            5 if restore_error else 3,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    if actual.sequence != expected_sequence:
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed before replacement could be verified; newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        )
    if not _formats_equal(actual.formats, expected_formats):
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED" if restore_error else "CLIPBOARD_BACKUP_FAILED",
            "The clipboard replacement did not match the expected formats and bytes.",
            5 if restore_error else 3,
            backup_preserved=True,
            clipboard_restored=False,
        )
    return actual


def _restore_after_failed_stage(
    backend: ClipboardBackend,
    backup_path: Path,
    backup: ClipboardBackup,
    error: ClipboardBackendError,
) -> None:
    if not error.may_have_changed:
        backup_path.unlink(missing_ok=True)
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "The CF_HDROP attachment could not be staged; the clipboard was not modified.",
            3,
            clipboard_restored=True,
        ) from error
    try:
        current = backend.snapshot()
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "Attachment staging failed and clipboard recovery identity could not be checked.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    if (
        error.resulting_sequence is None
        or error.resulting_formats is None
        or current.sequence != error.resulting_sequence
        or not _formats_equal(current.formats, error.resulting_formats)
    ):
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed after failed staging; newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        ) from error
    try:
        restored_sequence = backend.replace(backup.original, expected_sequence=current.sequence)
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "Attachment staging failed and the original clipboard could not be restored.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    _verified_snapshot(
        backend,
        restored_sequence,
        backup.original,
        restore_error=True,
    )
    _delete_backup(backup_path, clipboard_restored=True)
    raise BridgeFailure(
        "CLIPBOARD_BACKUP_FAILED",
        "The CF_HDROP attachment could not be staged; the original clipboard was restored.",
        3,
        clipboard_restored=True,
    ) from error


def stage(
    backend: ClipboardBackend,
    *,
    file_path: Path,
    backup_path: Path,
) -> dict[str, object]:
    _validate_stage_file(file_path)
    staged = ClipboardFormat(CF_HDROP, None, encode_cf_hdrop(file_path))
    try:
        original = backend.snapshot()
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "The clipboard could not be materialized safely and was not changed.",
            3,
        ) from exc
    _validate_format_set(original.formats, restore_error=False)
    backup = ClipboardBackup(original.formats, staged)
    _write_private_backup(backup_path, backup)
    try:
        staged_sequence = backend.replace([staged], expected_sequence=original.sequence)
    except ClipboardSequenceMismatch as exc:
        backup_path.unlink(missing_ok=True)
        raise BridgeFailure(
            "CLIPBOARD_BACKUP_FAILED",
            "The clipboard changed before attachment staging; newer content was not modified.",
            3,
        ) from exc
    except ClipboardBackendError as exc:
        _restore_after_failed_stage(backend, backup_path, backup, exc)
        raise AssertionError("unreachable")
    _verified_snapshot(backend, staged_sequence, [staged], restore_error=False)
    return {
        "ok": True,
        "operation": "stage",
        "staged_change_count": staged_sequence,
        "backup_format_count": len(original.formats),
        "staged_types": ["CF_HDROP"],
    }


def restore(
    backend: ClipboardBackend,
    *,
    backup_path: Path,
    expected_change_count: int,
) -> dict[str, object]:
    backup = _read_private_backup(backup_path)
    try:
        current = backend.snapshot()
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The staged clipboard could not be inspected before restoration.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    if (
        current.sequence != expected_change_count
        or not _formats_equal(current.formats, [backup.staged])
    ):
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard sequence or staged CF_HDROP payload changed. Newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        )
    try:
        restored_sequence = backend.replace(
            backup.original,
            expected_sequence=expected_change_count,
        )
    except ClipboardSequenceMismatch as exc:
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed after attachment staging. Newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The original clipboard formats could not be restored completely.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    _verified_snapshot(
        backend,
        restored_sequence,
        backup.original,
        restore_error=True,
    )
    _delete_backup(backup_path, clipboard_restored=True)
    return {
        "ok": True,
        "operation": "restore",
        "restored_change_count": restored_sequence,
        "restored_format_count": len(backup.original),
        "backup_deleted": True,
    }


def recover(
    backend: ClipboardBackend,
    *,
    backup_path: Path,
    file_path: Path,
) -> dict[str, object]:
    backup = _read_private_backup(backup_path)
    staged = ClipboardFormat(CF_HDROP, None, encode_cf_hdrop(file_path))
    if staged != backup.staged:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The recovery file path does not match the staged CF_HDROP backup.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        )
    try:
        current = backend.snapshot()
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard could not be inspected for conservative recovery.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    if _formats_equal(current.formats, backup.original):
        _delete_backup(backup_path, clipboard_restored=True)
        return {
            "ok": True,
            "operation": "recover",
            "already_restored": True,
            "restored_change_count": current.sequence,
            "backup_deleted": True,
        }
    if not _formats_equal(current.formats, [staged]):
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard no longer contains the staged video. Newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        )
    try:
        restored_sequence = backend.replace(
            backup.original,
            expected_sequence=current.sequence,
        )
    except ClipboardSequenceMismatch as exc:
        raise BridgeFailure(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed during recovery; newer content was left untouched.",
            4,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    except ClipboardBackendError as exc:
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED",
            "The original clipboard formats could not be recovered completely.",
            5,
            backup_preserved=True,
            clipboard_restored=False,
        ) from exc
    _verified_snapshot(
        backend,
        restored_sequence,
        backup.original,
        restore_error=True,
    )
    _delete_backup(backup_path, clipboard_restored=True)
    return {
        "ok": True,
        "operation": "recover",
        "already_restored": False,
        "restored_change_count": restored_sequence,
        "backup_deleted": True,
    }


def _options(command: str, argv: Sequence[str]) -> dict[str, str]:
    allowed = {
        "stage": {"--file", "--backup"},
        "restore": {"--backup", "--expected-change-count"},
        "recover": {"--backup", "--file"},
        "status": set(),
    }
    if command not in allowed or len(argv) % 2:
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            "Use stage, restore, recover, or status with --name value options.",
            2,
        )
    parsed: dict[str, str] = {}
    for index in range(0, len(argv), 2):
        key, value = argv[index], argv[index + 1]
        if key not in allowed[command] or key in parsed or not value:
            raise BridgeFailure(
                "INVALID_ARGUMENTS",
                f"Unknown, duplicate, or empty option for {command}.",
                2,
            )
        parsed[key] = value
    required = allowed[command]
    if set(parsed) != required:
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            f"Missing a required option for {command}.",
            2,
        )
    return parsed


def _default_backend(command: str) -> ClipboardBackend:
    try:
        return Win32ClipboardBackend()
    except ClipboardBackendError as exc:
        restore_command = command in {"restore", "recover"}
        raise BridgeFailure(
            "CLIPBOARD_RESTORE_FAILED" if restore_command else "CLIPBOARD_BACKUP_FAILED",
            "The native Win32 clipboard API is unavailable.",
            5 if restore_command else 3,
            backup_preserved=restore_command,
            clipboard_restored=False if restore_command else None,
        ) from exc


def dispatch(
    argv: Sequence[str],
    *,
    backend: ClipboardBackend | None = None,
) -> dict[str, object]:
    if not argv:
        raise BridgeFailure(
            "INVALID_ARGUMENTS",
            "Use stage, restore, recover, or status.",
            2,
        )
    command = argv[0]
    options = _options(command, argv[1:])
    selected = backend if backend is not None else _default_backend(command)
    if command == "status":
        try:
            change_count = selected.sequence()
        except ClipboardBackendError as exc:
            raise BridgeFailure(
                "CLIPBOARD_BACKUP_FAILED",
                "The Windows clipboard sequence number is unavailable.",
                3,
            ) from exc
        return {"ok": True, "operation": "status", "change_count": change_count}
    backup_path = _require_absolute(options["--backup"], "The backup path")
    if command == "stage":
        file_path = _require_absolute(options["--file"], "The staged file path")
        return stage(selected, file_path=file_path, backup_path=backup_path)
    if command == "restore":
        raw_expected = options["--expected-change-count"]
        try:
            expected = int(raw_expected, 10)
        except ValueError as exc:
            raise BridgeFailure(
                "INVALID_ARGUMENTS",
                "The expected change count must be a non-negative integer.",
                2,
            ) from exc
        if expected < 0 or expected > 0xFFFFFFFF:
            raise BridgeFailure(
                "INVALID_ARGUMENTS",
                "The expected change count must be a non-negative DWORD.",
                2,
            )
        return restore(
            selected,
            backup_path=backup_path,
            expected_change_count=expected,
        )
    file_path = _require_absolute(options["--file"], "The recovery file path")
    return recover(selected, backup_path=backup_path, file_path=file_path)


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: ClipboardBackend | None = None,
    stream: IO[str] | None = None,
) -> int:
    output = stream if stream is not None else sys.stdout
    try:
        payload = dispatch(sys.argv[1:] if argv is None else argv, backend=backend)
        exit_code = 0
    except BridgeFailure as exc:
        payload = {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "backup_preserved": exc.backup_preserved,
        }
        if exc.clipboard_restored is not None:
            payload["clipboard_restored"] = exc.clipboard_restored
        exit_code = exc.exit_code
    except Exception:
        payload = {
            "ok": False,
            "code": "INTERNAL_ERROR",
            "message": "The Windows clipboard bridge failed unexpectedly.",
            "backup_preserved": True,
        }
        exit_code = 70
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
