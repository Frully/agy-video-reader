#!/usr/bin/env python3
"""Transactional Linux clipboard bridge backed by a running CopyQ server.

The process is deliberately short lived.  CopyQ, rather than this bridge, owns
the X11 selection or Wayland clipboard after ``copy()`` returns.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence


BACKUP_SCHEMA_VERSION = 1
MAX_CLIPBOARD_BYTES = 16 * 1024 * 1024
MAX_BACKUP_FILE_BYTES = 24 * 1024 * 1024
MAX_FORMATS = 256
MAX_MIME_LENGTH = 1024
STAGED_TYPES = ("text/uri-list", "x-special/gnome-copied-files")
VOLATILE_MIMES = frozenset({"application/x-copyq-owner"})
# CopyQ's documented Item representation is a JavaScript object.  These names
# have special object semantics, so fail before mutation instead of assigning
# untrusted clipboard metadata to them.
RESERVED_ITEM_KEYS = frozenset({"__proto__", "constructor", "prototype"})
STABLE_ERROR_CODES = frozenset(
    {
        "CLIPBOARD_BACKUP_FAILED",
        "CLIPBOARD_CHANGED_EXTERNALLY",
        "CLIPBOARD_RESTORE_FAILED",
    }
)


@dataclass(frozen=True)
class ClipboardFormat:
    mime: str
    data: bytes


Snapshot = tuple[ClipboardFormat, ...]


@dataclass(frozen=True)
class EvalResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CopyQEvalRunner(Protocol):
    def eval(self, script: str, *, backend: str) -> EvalResult: ...


class BridgeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        clipboard_restored: bool | None = None,
        recovery_required: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.clipboard_restored = clipboard_restored
        self.recovery_required = recovery_required


class CopyQInvocationError(Exception):
    """An invocation failure whose details must not expose clipboard bytes."""


class SubprocessCopyQRunner:
    def __init__(self, executable: str, *, timeout_seconds: int = 30) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise CopyQInvocationError("CopyQ executable is unavailable.")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def eval(self, script: str, *, backend: str) -> EvalResult:
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "xcb" if backend == "x11" else "wayland"
        try:
            completed = subprocess.run(
                [self.executable, "eval", "-"],
                input=script,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CopyQInvocationError(
                "CopyQ could not evaluate the bridge script."
            ) from exc
        return EvalResult(completed.returncode, completed.stdout, completed.stderr)


def resolve_backend(requested: str, environment: Mapping[str, str]) -> str:
    if requested in {"x11", "wayland"}:
        return requested
    session_type = environment.get("XDG_SESSION_TYPE", "").strip().lower()
    has_x11 = bool(environment.get("DISPLAY"))
    has_wayland = bool(environment.get("WAYLAND_DISPLAY"))
    if session_type == "x11" and has_x11:
        return "x11"
    if session_type == "wayland" and has_wayland:
        return "wayland"
    if session_type in {"x11", "wayland"}:
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED",
            "The declared Linux display session is not available.",
        )
    if has_x11 != has_wayland:
        return "x11" if has_x11 else "wayland"
    raise BridgeError(
        "CLIPBOARD_BACKUP_FAILED",
        "An unambiguous X11 or Wayland session is required.",
    )


def _validate_mime(mime: object, *, failure_code: str) -> str:
    if (
        not isinstance(mime, str)
        or not mime
        or len(mime) > MAX_MIME_LENGTH
        or any(character in mime for character in "\x00\r\n")
        or mime in RESERVED_ITEM_KEYS
    ):
        raise BridgeError(failure_code, "Clipboard MIME metadata is invalid.")
    return mime


def _snapshot_from_wire(value: object, *, failure_code: str) -> Snapshot:
    if not isinstance(value, list) or len(value) > MAX_FORMATS:
        raise BridgeError(failure_code, "Clipboard format metadata is invalid.")
    formats: list[ClipboardFormat] = []
    seen: set[str] = set()
    total_bytes = 0
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"mime", "base64"}:
            raise BridgeError(failure_code, "Clipboard format metadata is invalid.")
        mime = _validate_mime(entry["mime"], failure_code=failure_code)
        if mime in seen:
            raise BridgeError(
                failure_code, "Clipboard contains duplicate MIME metadata."
            )
        encoded = entry["base64"]
        if not isinstance(encoded, str):
            raise BridgeError(failure_code, "Clipboard data encoding is invalid.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BridgeError(
                failure_code, "Clipboard data encoding is invalid."
            ) from exc
        total_bytes += len(data)
        if total_bytes > MAX_CLIPBOARD_BYTES:
            raise BridgeError(
                failure_code, "Clipboard data exceeds the 16 MiB safety limit."
            )
        seen.add(mime)
        formats.append(ClipboardFormat(mime, data))
    return tuple(formats)


def _snapshot_to_wire(snapshot: Snapshot) -> list[dict[str, str]]:
    return [
        {"mime": item.mime, "base64": base64.b64encode(item.data).decode("ascii")}
        for item in snapshot
    ]


def snapshot_from_mapping(values: Mapping[str, bytes]) -> Snapshot:
    """Build and validate a deterministic snapshot (also useful to adapters/tests)."""

    return _snapshot_from_wire(
        [
            {"mime": mime, "base64": base64.b64encode(data).decode("ascii")}
            for mime, data in values.items()
        ],
        failure_code="CLIPBOARD_BACKUP_FAILED",
    )


def _without_volatile(snapshot: Snapshot) -> Snapshot:
    return tuple(item for item in snapshot if item.mime not in VOLATILE_MIMES)


def snapshot_fingerprint(snapshot: Snapshot, *, stable: bool = False) -> str:
    value = _without_volatile(snapshot) if stable else snapshot
    canonical = json.dumps(
        _snapshot_to_wire(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _change_count(snapshot: Snapshot) -> int:
    # The shared adapter contract calls this opaque transaction token a
    # change-count.  CopyQ has no cross-platform counter, so use the stable
    # content fingerprint and still perform a full compare-and-swap in CopyQ.
    return int(snapshot_fingerprint(snapshot, stable=True)[:15], 16)


def build_staged_snapshot(video_path: Path) -> Snapshot:
    try:
        resolved = video_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED", "The video file is unavailable."
        ) from exc
    if not resolved.is_file():
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED", "The attachment must be a regular file."
        )
    uri = resolved.as_uri()
    return snapshot_from_mapping(
        {
            "text/uri-list": f"{uri}\r\n".encode("utf-8"),
            "x-special/gnome-copied-files": f"copy\n{uri}".encode("utf-8"),
        }
    )


def _js_literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _copyq_common_script() -> str:
    volatile = _js_literal(sorted(VOLATILE_MIMES))
    reserved = _js_literal(sorted(RESERVED_ITEM_KEYS))
    return f"""
var BYTE_LIMIT = {MAX_CLIPBOARD_BYTES};
var FORMAT_LIMIT = {MAX_FORMATS};
var MIME_LIMIT = {MAX_MIME_LENGTH};
var VOLATILE_MIMES = {volatile};
var RESERVED_ITEM_KEYS = {reserved};

function bridgeError(code, message) {{
    var error = new Error(message);
    error.bridgeCode = code;
    throw error;
}}

function emit(value) {{
    print(JSON.stringify(value) + "\\n");
}}

function requireIsolatedClipboard(failureCode) {{
    if (str(config("copy_clipboard")) === "true" ||
        str(config("copy_selection")) === "true") {{
        bridgeError(
            failureCode,
            "CopyQ clipboard/PRIMARY synchronization must be disabled."
        );
    }}
}}

function decodedLength(encoded) {{
    if (encoded.length === 0) return 0;
    var padding = 0;
    if (encoded.charAt(encoded.length - 1) === "=") padding++;
    if (encoded.charAt(encoded.length - 2) === "=") padding++;
    return Math.floor(encoded.length * 3 / 4) - padding;
}}

function formatNames() {{
    var raw = str(clipboard("?"));
    var names = raw.split("\\n").filter(function(value) {{ return value.length > 0; }});
    if (names.length > FORMAT_LIMIT) {{
        bridgeError("CLIPBOARD_BACKUP_FAILED", "Clipboard has too many MIME formats.");
    }}
    for (var i = 0; i < names.length; ++i) {{
        var mime = names[i];
        if (mime.length > MIME_LIMIT || mime.indexOf("\\r") >= 0 || mime.indexOf("\\0") >= 0 || RESERVED_ITEM_KEYS.indexOf(mime) >= 0) {{
            bridgeError("CLIPBOARD_BACKUP_FAILED", "Clipboard MIME metadata is invalid.");
        }}
        if (names.indexOf(mime) !== i) {{
            bridgeError("CLIPBOARD_BACKUP_FAILED", "Clipboard MIME metadata is duplicated.");
        }}
    }}
    return names;
}}

function readSnapshot() {{
    var names = formatNames();
    var formats = [];
    var total = 0;
    for (var i = 0; i < names.length; ++i) {{
        var mime = names[i];
        if (!hasClipboardFormat(mime)) {{
            bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard changed while it was being read.");
        }}
        var encoded = str(toBase64(clipboard(mime)));
        total += decodedLength(encoded);
        if (total > BYTE_LIMIT) {{
            bridgeError("CLIPBOARD_BACKUP_FAILED", "Clipboard data exceeds the 16 MiB safety limit.");
        }}
        if (!hasClipboardFormat(mime)) {{
            bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard changed while it was being read.");
        }}
        formats.push({{mime: mime, base64: encoded}});
    }}
    if (JSON.stringify(names) !== JSON.stringify(formatNames())) {{
        bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard changed while it was being read.");
    }}
    for (var j = 0; j < formats.length; ++j) {{
        var item = formats[j];
        if (!hasClipboardFormat(item.mime) || str(toBase64(clipboard(item.mime))) !== item.base64) {{
            bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard changed while it was being read.");
        }}
    }}
    return formats;
}}

function stableSnapshot(formats) {{
    return formats.filter(function(item) {{
        return VOLATILE_MIMES.indexOf(item.mime) < 0;
    }});
}}

function sameSnapshot(left, right) {{
    return JSON.stringify(left) === JSON.stringify(right);
}}

function copySnapshot(formats) {{
    var item = {{}};
    for (var i = 0; i < formats.length; ++i) {{
        item[formats[i].mime] = fromBase64(formats[i].base64);
    }}
    copy(item);
}}
"""


def _snapshot_script() -> str:
    return f"""// antigravity-copyq: snapshot
{_copyq_common_script()}
try {{
    requireIsolatedClipboard("CLIPBOARD_BACKUP_FAILED");
    var formats = readSnapshot();
    var total = 0;
    for (var i = 0; i < formats.length; ++i) total += decodedLength(formats[i].base64);
    emit({{ok: true, operation: "snapshot", formats: formats, total_bytes: total}});
}} catch (error) {{
    emit({{
        ok: false,
        operation: "snapshot",
        code: error.bridgeCode || "CLIPBOARD_BACKUP_FAILED",
        message: error.message || "Clipboard snapshot failed."
    }});
    fail();
}}
undefined;
"""


def _stage_script(original: Snapshot, staged: Snapshot) -> str:
    original_wire = _js_literal(_snapshot_to_wire(original))
    staged_wire = _js_literal(_snapshot_to_wire(staged))
    staged_types = _js_literal(list(STAGED_TYPES))
    return f"""// antigravity-copyq: stage
{_copyq_common_script()}
var expectedOriginal = {original_wire};
var expectedStaged = {staged_wire};
var mutationAttempted = false;
try {{
    requireIsolatedClipboard("CLIPBOARD_BACKUP_FAILED");
    var current = readSnapshot();
    if (!sameSnapshot(current, expectedOriginal)) {{
        bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard changed before attachment staging.");
    }}
    requireIsolatedClipboard("CLIPBOARD_BACKUP_FAILED");
    mutationAttempted = true;
    copySnapshot(expectedStaged);
    var actual = stableSnapshot(readSnapshot());
    if (!sameSnapshot(actual, expectedStaged)) {{
        bridgeError("CLIPBOARD_BACKUP_FAILED", "CopyQ did not retain the staged URI formats.");
    }}
    emit({{ok: true, operation: "stage", staged_types: {staged_types}}});
}} catch (error) {{
    emit({{
        ok: false,
        operation: "stage",
        code: error.bridgeCode || "CLIPBOARD_BACKUP_FAILED",
        message: error.message || "Clipboard staging failed.",
        clipboard_restored: mutationAttempted ? false : null,
        recovery_required: mutationAttempted
    }});
    fail();
}}
undefined;
"""


def _restore_script(original: Snapshot, staged: Snapshot, *, operation: str) -> str:
    # application/x-copyq-owner is CopyQ control metadata. CopyQ creates a new
    # value for each copy(), so preserve it for the pre-stage identity check but
    # do not claim that its old bytes can be restored.
    original_wire = _js_literal(_snapshot_to_wire(_without_volatile(original)))
    staged_wire = _js_literal(_snapshot_to_wire(staged))
    return f"""// antigravity-copyq: {operation}
{_copyq_common_script()}
var expectedOriginal = {original_wire};
var expectedStaged = {staged_wire};
try {{
    var current = stableSnapshot(readSnapshot());
    if (sameSnapshot(current, expectedOriginal)) {{
        emit({{
            ok: true,
            operation: {_js_literal(operation)},
            clipboard_restored: true,
            already_restored: true
        }});
    }} else {{
        if (!sameSnapshot(current, expectedStaged)) {{
            bridgeError("CLIPBOARD_CHANGED_EXTERNALLY", "Clipboard no longer contains the staged attachment.");
        }}
        requireIsolatedClipboard("CLIPBOARD_RESTORE_FAILED");
        copySnapshot(expectedOriginal);
        var actual = stableSnapshot(readSnapshot());
        if (!sameSnapshot(actual, stableSnapshot(expectedOriginal))) {{
            bridgeError("CLIPBOARD_RESTORE_FAILED", "CopyQ did not restore the clipboard snapshot.");
        }}
        emit({{
            ok: true,
            operation: {_js_literal(operation)},
            clipboard_restored: true,
            already_restored: false
        }});
    }}
}} catch (error) {{
    emit({{
        ok: false,
        operation: {_js_literal(operation)},
        code: error.bridgeCode || "CLIPBOARD_RESTORE_FAILED",
        message: error.message || "Clipboard restoration failed.",
        clipboard_restored: false
    }});
    fail();
}}
undefined;
"""


def _evaluate_json(
    runner: CopyQEvalRunner,
    script: str,
    *,
    backend: str,
    operation: str,
    default_code: str,
) -> dict[str, object]:
    try:
        result = runner.eval(script, backend=backend)
    except CopyQInvocationError as exc:
        raise BridgeError(
            default_code, "CopyQ could not evaluate the clipboard operation."
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BridgeError(
            default_code, "CopyQ returned an invalid bridge response."
        ) from exc
    if not isinstance(payload, dict):
        raise BridgeError(default_code, "CopyQ returned an invalid bridge response.")
    if result.returncode != 0:
        candidate = payload.get("code")
        code = (
            candidate
            if isinstance(candidate, str) and candidate in STABLE_ERROR_CODES
            else default_code
        )
        candidate_message = payload.get("message")
        message = (
            candidate_message
            if isinstance(candidate_message, str) and len(candidate_message) <= 512
            else "CopyQ returned a clipboard error."
        )
        restored = payload.get("clipboard_restored")
        recovery_required = payload.get("recovery_required")
        raise BridgeError(
            code,
            message,
            clipboard_restored=restored if isinstance(restored, bool) else None,
            recovery_required=(
                recovery_required if isinstance(recovery_required, bool) else None
            ),
        )
    if payload.get("ok") is not True or payload.get("operation") != operation:
        raise BridgeError(default_code, "CopyQ returned an invalid success response.")
    return payload


def read_snapshot(runner: CopyQEvalRunner, *, backend: str) -> Snapshot:
    payload = _evaluate_json(
        runner,
        _snapshot_script(),
        backend=backend,
        operation="snapshot",
        default_code="CLIPBOARD_BACKUP_FAILED",
    )
    snapshot = _snapshot_from_wire(
        payload.get("formats"), failure_code="CLIPBOARD_BACKUP_FAILED"
    )
    reported_total = payload.get("total_bytes")
    actual_total = sum(len(item.data) for item in snapshot)
    if isinstance(reported_total, bool) or not isinstance(reported_total, int):
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED", "CopyQ returned invalid clipboard size metadata."
        )
    if reported_total != actual_total:
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED",
            "CopyQ returned inconsistent clipboard size metadata.",
        )
    return snapshot


def _backup_value(
    backend: str, original: Snapshot, staged: Snapshot
) -> dict[str, object]:
    staged_change_count = _change_count(staged)
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "backend": backend,
        "original": {"formats": _snapshot_to_wire(original)},
        "original_fingerprint": snapshot_fingerprint(original),
        "staged": {"formats": _snapshot_to_wire(staged)},
        "staged_fingerprint": snapshot_fingerprint(staged, stable=True),
        "staged_change_count": staged_change_count,
    }


def _write_backup(path: Path, value: dict[str, object]) -> None:
    serialized = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(serialized) > MAX_BACKUP_FILE_BYTES:
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED", "Clipboard backup exceeds its size limit."
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(serialized):
            count = os.write(descriptor, serialized[written:])
            if count <= 0:
                raise OSError("short backup write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = None
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise BridgeError(
            "CLIPBOARD_BACKUP_FAILED",
            "A private clipboard backup could not be created.",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_backup(path: Path) -> tuple[str, Snapshot, Snapshot, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is unavailable.",
            clipboard_restored=False,
        ) from exc
    getuid = getattr(os, "getuid", None)
    unsafe = (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_BACKUP_FILE_BYTES
        or (getuid is not None and before.st_uid != getuid())
    )
    if unsafe:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is not a private owner-only regular file.",
            clipboard_restored=False,
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            opened_is_unsafe = (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > MAX_BACKUP_FILE_BYTES
                or (getuid is not None and opened.st_uid != getuid())
            )
            if (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ) or opened_is_unsafe:
                raise OSError("backup changed while opening")
            raw = bytearray()
            while len(raw) <= MAX_BACKUP_FILE_BYTES:
                chunk = os.read(
                    descriptor, min(1024 * 1024, MAX_BACKUP_FILE_BYTES + 1 - len(raw))
                )
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_BACKUP_FILE_BYTES:
                raise OSError("backup is too large")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup could not be read safely.",
            clipboard_restored=False,
        ) from exc
    try:
        value = json.loads(bytes(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup is invalid.",
            clipboard_restored=False,
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != BACKUP_SCHEMA_VERSION
    ):
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup schema is invalid.",
            clipboard_restored=False,
        )
    backend = value.get("backend")
    original_container = value.get("original")
    staged_container = value.get("staged")
    if (
        backend not in {"x11", "wayland"}
        or not isinstance(original_container, dict)
        or set(original_container) != {"formats"}
        or not isinstance(staged_container, dict)
        or set(staged_container) != {"formats"}
    ):
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup metadata is invalid.",
            clipboard_restored=False,
        )
    original = _snapshot_from_wire(
        original_container["formats"], failure_code="CLIPBOARD_RESTORE_FAILED"
    )
    staged = _snapshot_from_wire(
        staged_container["formats"], failure_code="CLIPBOARD_RESTORE_FAILED"
    )
    change_count = value.get("staged_change_count")
    if (
        value.get("original_fingerprint") != snapshot_fingerprint(original)
        or value.get("staged_fingerprint") != snapshot_fingerprint(staged, stable=True)
        or isinstance(change_count, bool)
        or not isinstance(change_count, int)
        or change_count != _change_count(staged)
        or tuple(item.mime for item in staged) != STAGED_TYPES
    ):
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The clipboard recovery backup failed integrity validation.",
            clipboard_restored=False,
        )
    return backend, original, staged, change_count


def stage(
    video_path: Path,
    backup_path: Path,
    *,
    backend: str,
    runner: CopyQEvalRunner,
) -> dict[str, object]:
    staged = build_staged_snapshot(video_path)
    original = read_snapshot(runner, backend=backend)
    _write_backup(backup_path, _backup_value(backend, original, staged))
    try:
        _evaluate_json(
            runner,
            _stage_script(original, staged),
            backend=backend,
            operation="stage",
            default_code="CLIPBOARD_BACKUP_FAILED",
        )
    except BridgeError as exc:
        if exc.recovery_required is False:
            try:
                backup_path.unlink()
            except OSError as unlink_error:
                raise BridgeError(
                    "CLIPBOARD_RESTORE_FAILED",
                    "The clipboard was not changed, but its redundant private backup could not be deleted.",
                    clipboard_restored=True,
                    recovery_required=False,
                ) from unlink_error
            exc.clipboard_restored = True
        raise
    return {
        "ok": True,
        "operation": "stage",
        "backend": backend,
        "staged_types": list(STAGED_TYPES),
        "staged_fingerprint": snapshot_fingerprint(staged, stable=True),
        "staged_change_count": _change_count(staged),
    }


def restore(
    backup_path: Path,
    *,
    backend: str,
    runner: CopyQEvalRunner,
    operation: str = "restore",
    expected_change_count: int | None = None,
    video_path: Path | None = None,
) -> dict[str, object]:
    saved_backend, original, staged, saved_change_count = _read_backup(backup_path)
    if backend != saved_backend:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The recovery backup belongs to a different Linux display backend.",
            clipboard_restored=False,
        )
    if (
        expected_change_count is not None
        and expected_change_count != saved_change_count
    ):
        raise BridgeError(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The requested clipboard transaction token does not match the recovery backup.",
            clipboard_restored=False,
        )
    if video_path is not None and build_staged_snapshot(video_path) != staged:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "The recovery video does not match the staged clipboard attachment.",
            clipboard_restored=False,
        )
    _evaluate_json(
        runner,
        _restore_script(original, staged, operation=operation),
        backend=backend,
        operation=operation,
        default_code="CLIPBOARD_RESTORE_FAILED",
    )
    try:
        backup_path.unlink()
    except OSError as exc:
        raise BridgeError(
            "CLIPBOARD_RESTORE_FAILED",
            "Clipboard was restored, but the recovery backup could not be deleted.",
            clipboard_restored=True,
        ) from exc
    return {
        "ok": True,
        "operation": operation,
        "backend": backend,
        "backup_deleted": True,
        "clipboard_restored": True,
        "restored_change_count": _change_count(original),
        "restored_types": [item.mime for item in _without_volatile(original)],
    }


def status(runner: CopyQEvalRunner, *, backend: str) -> dict[str, object]:
    snapshot = read_snapshot(runner, backend=backend)
    return {
        "ok": True,
        "operation": "status",
        "backend": backend,
        "change_count": _change_count(snapshot),
        "fingerprint": snapshot_fingerprint(snapshot, stable=True),
        "types": [item.mime for item in snapshot],
    }


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BridgeError("INVALID_ARGUMENTS", message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=True)
    parser.add_argument("--copyq", default="copyq")
    parser.add_argument("--backend", choices=("auto", "x11", "wayland"), default="auto")
    commands = parser.add_subparsers(dest="operation", required=True)

    stage_parser = commands.add_parser("stage")
    stage_parser.add_argument("--file", required=True, type=Path)
    stage_parser.add_argument("--backup", required=True, type=Path)

    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--expected-change-count", required=True, type=int)

    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--backup", required=True, type=Path)
    recover_parser.add_argument("--file", required=False, type=Path)

    commands.add_parser("status")
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CopyQEvalRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    operation: str | None = None
    backup_path: Path | None = None
    try:
        arguments = _parser().parse_args(argv)
        operation = arguments.operation
        backup_path = getattr(arguments, "backup", None)
        backend = resolve_backend(
            arguments.backend,
            environment if environment is not None else os.environ,
        )
        active_runner = runner or SubprocessCopyQRunner(arguments.copyq)
        if operation == "stage":
            payload = stage(
                arguments.file, arguments.backup, backend=backend, runner=active_runner
            )
        elif operation == "restore":
            payload = restore(
                arguments.backup,
                backend=backend,
                runner=active_runner,
                expected_change_count=arguments.expected_change_count,
            )
        elif operation == "recover":
            payload = restore(
                arguments.backup,
                backend=backend,
                runner=active_runner,
                operation="recover",
                video_path=arguments.file,
            )
        else:
            payload = status(active_runner, backend=backend)
        _emit(payload)
        return 0
    except BridgeError as exc:
        payload: dict[str, object] = {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "backup_preserved": bool(backup_path and backup_path.exists()),
        }
        if operation is not None:
            payload["operation"] = operation
        if exc.clipboard_restored is not None:
            payload["clipboard_restored"] = exc.clipboard_restored
        _emit(payload)
        return 2 if exc.code == "INVALID_ARGUMENTS" else 1
    except CopyQInvocationError:
        restore_operation = operation in {"restore", "recover"}
        _emit(
            {
                "ok": False,
                "operation": operation,
                "code": (
                    "CLIPBOARD_RESTORE_FAILED"
                    if restore_operation
                    else "CLIPBOARD_BACKUP_FAILED"
                ),
                "message": "CopyQ is unavailable.",
                "backup_preserved": bool(backup_path and backup_path.exists()),
                "clipboard_restored": False if restore_operation else None,
            }
        )
        return 1
    except Exception:
        restore_operation = operation in {"restore", "recover"}
        _emit(
            {
                "ok": False,
                "operation": operation,
                "code": (
                    "CLIPBOARD_RESTORE_FAILED"
                    if restore_operation
                    else "CLIPBOARD_BACKUP_FAILED"
                ),
                "message": "The Linux clipboard bridge failed unexpectedly.",
                "backup_preserved": bool(backup_path and backup_path.exists()),
                "clipboard_restored": False if restore_operation else None,
            }
        )
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
