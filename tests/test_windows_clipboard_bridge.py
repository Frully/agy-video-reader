from __future__ import annotations

import importlib.util
import io
import json
import stat
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows_clipboard_bridge.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bridge():
    return load_module("antigravity_windows_clipboard_bridge_tests", SOURCE)


class FakeBackend:
    def __init__(self, bridge, formats=(), sequence=100):
        self.bridge = bridge
        self.formats = tuple(formats)
        self._sequence = sequence
        self.replace_calls = 0
        self.snapshot_calls = 0
        self.fail_after_mutation_on_calls: set[int] = set()
        self.race_before_replace_on_calls: dict[int, tuple] = {}
        self.mutate_before_snapshot_on_calls: dict[int, tuple] = {}

    def sequence(self):
        return self._sequence

    def mutate(self, formats):
        self.formats = tuple(formats)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls in self.mutate_before_snapshot_on_calls:
            self.mutate(self.mutate_before_snapshot_on_calls[self.snapshot_calls])
        return self.bridge.ClipboardSnapshot(self._sequence, self.formats)

    def replace(self, formats, *, expected_sequence):
        self.replace_calls += 1
        if self.replace_calls in self.race_before_replace_on_calls:
            self.mutate(self.race_before_replace_on_calls[self.replace_calls])
        if expected_sequence is not None and expected_sequence != self._sequence:
            raise self.bridge.ClipboardSequenceMismatch(
                f"expected {expected_sequence}, found {self._sequence}"
            )
        if self.replace_calls in self.fail_after_mutation_on_calls:
            self.mutate(())
            raise self.bridge.ClipboardBackendError(
                "injected partial replacement",
                may_have_changed=True,
                resulting_sequence=self._sequence,
                resulting_formats=self.formats,
            )
        self.mutate(tuple(formats))
        return self._sequence


def invoke(bridge, backend, *arguments):
    output = io.StringIO()
    exit_code = bridge.main(arguments, backend=backend, stream=output)
    return exit_code, json.loads(output.getvalue())


def fixtures(bridge):
    return (
        bridge.ClipboardFormat(13, None, "fixture\0".encode("utf-16-le")),
        bridge.ClipboardFormat(0xC001, "Unit.Test.Format", b"\x00\xffopaque"),
    )


def make_video(tmp_path: Path) -> Path:
    video = tmp_path / "sample video.mp4"
    video.write_bytes(b"transport-only fixture")
    return video


def test_cf_hdrop_is_exact_unicode_dropfiles(bridge, tmp_path: Path):
    video = make_video(tmp_path)
    payload = bridge.encode_cf_hdrop(video)

    assert struct.unpack("<IiiII", payload[:20]) == (20, 0, 0, 0, 1)
    assert payload[20:].decode("utf-16-le") == f"{video}\0\0"
    assert payload[20:] != str(video).encode("utf-8")


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        ((8, 2, 9, 17), (8,)),
        ((2, 8, 17), (2,)),
        ((17, 2, 8, 9), (17,)),
        ((13, 7, 1), (13,)),
        ((1, 7, 13, 16), (1, 16)),
        ((14, 3), (14,)),
        ((0xC001, 8, 2, 9, 17, 13, 7, 1), (0xC001, 8, 13)),
    ],
)
def test_synthesized_win32_formats_are_canonicalized(
    bridge,
    available: tuple[int, ...],
    expected: tuple[int, ...],
):
    assert bridge.canonical_format_ids(available) == expected


def test_cli_stage_and_restore_round_trip_unknown_registered_format(
    bridge,
    tmp_path: Path,
):
    original = fixtures(bridge)
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"

    exit_code, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 0
    assert staged["ok"] is True
    assert staged["operation"] == "stage"
    assert staged["staged_types"] == ["CF_HDROP"]
    assert staged["backup_format_count"] == 2
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backend.formats[0].format_id == bridge.CF_HDROP
    assert len(backend.formats) == 1

    exit_code, restored = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 0
    assert restored["ok"] is True
    assert restored["operation"] == "restore"
    assert restored["backup_deleted"] is True
    assert backend.formats == original
    assert not backup.exists()


def test_status_protocol_is_machine_readable(bridge):
    backend = FakeBackend(bridge, fixtures(bridge), sequence=0xFFFFFFFF)

    exit_code, payload = invoke(bridge, backend, "status")

    assert exit_code == 0
    assert payload == {
        "change_count": 0xFFFFFFFF,
        "ok": True,
        "operation": "status",
    }


def test_restore_preserves_external_change_and_backup(bridge, tmp_path: Path):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    _, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    backend.mutate(newer)

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 4
    assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert payload["backup_preserved"] is True
    assert payload["clipboard_restored"] is False
    assert backend.formats == newer
    assert backup.is_file()


def test_recover_restores_only_exact_staged_file(bridge, tmp_path: Path):
    original = fixtures(bridge)
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    exit_code, payload = invoke(
        bridge,
        backend,
        "recover",
        "--backup",
        str(backup),
        "--file",
        str(video),
    )

    assert exit_code == 0
    assert payload["operation"] == "recover"
    assert payload["already_restored"] is False
    assert payload["backup_deleted"] is True
    assert backend.formats == original
    assert not backup.exists()


def test_recover_removes_redundant_backup_when_original_is_present(
    bridge,
    tmp_path: Path,
):
    original = fixtures(bridge)
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    backend.mutate(original)

    exit_code, payload = invoke(
        bridge,
        backend,
        "recover",
        "--backup",
        str(backup),
        "--file",
        str(video),
    )

    assert exit_code == 0
    assert payload["already_restored"] is True
    assert backend.formats == original
    assert not backup.exists()


def test_recover_does_not_overwrite_unrelated_newer_content(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    backend.mutate(newer)

    exit_code, payload = invoke(
        bridge,
        backend,
        "recover",
        "--backup",
        str(backup),
        "--file",
        str(video),
    )

    assert exit_code == 4
    assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert backend.formats == newer
    assert backup.is_file()


def test_stage_race_fails_before_mutation_and_removes_stale_backup(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    backend.race_before_replace_on_calls[1] = newer
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert backend.formats == newer
    assert not backup.exists()


def test_partial_stage_failure_restores_original_and_deletes_backup(
    bridge,
    tmp_path: Path,
):
    original = fixtures(bridge)
    backend = FakeBackend(bridge, original)
    backend.fail_after_mutation_on_calls.add(1)
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert payload["clipboard_restored"] is True
    assert backend.formats == original
    assert not backup.exists()


def test_partial_stage_recovery_checks_payload_even_when_sequence_matches(
    bridge,
    tmp_path: Path,
):
    original = fixtures(bridge)
    video = make_video(tmp_path)
    staged_format = bridge.ClipboardFormat(
        bridge.CF_HDROP,
        None,
        bridge.encode_cf_hdrop(video),
    )
    backup_value = bridge.ClipboardBackup(original, staged_format)
    backup = tmp_path / "clipboard-backup.json"
    bridge._write_private_backup(backup, backup_value)
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    backend = FakeBackend(bridge, newer, sequence=123)
    error = bridge.ClipboardBackendError(
        "injected partial replacement",
        may_have_changed=True,
        resulting_sequence=123,
        resulting_formats=(),
    )

    with pytest.raises(bridge.BridgeFailure) as caught:
        bridge._restore_after_failed_stage(backend, backup, backup_value, error)

    assert caught.value.code == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert backend.formats == newer
    assert backend.replace_calls == 0
    assert backup.is_file()


def test_partial_restore_failure_retains_recovery_backup(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    _, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    backend.fail_after_mutation_on_calls.add(2)

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 5
    assert payload["code"] == "CLIPBOARD_RESTORE_FAILED"
    assert payload["clipboard_restored"] is False
    assert backup.is_file()


@pytest.mark.parametrize(
    "format_id",
    [2, 0x0080, 0x0200, 0x02FF, 0x0300, 0x03FF],
)
def test_unsupported_or_owner_dependent_format_fails_before_emptying_clipboard(
    bridge,
    tmp_path: Path,
    format_id: int,
):
    original = (bridge.ClipboardFormat(format_id, None, b"opaque"),)
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "must-not-exist.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert backend.replace_calls == 0
    assert backend.formats == original
    assert not backup.exists()


@pytest.mark.parametrize("conflict", ["same-id", "same-name"])
def test_conflicting_registered_format_identity_fails_before_mutation(
    bridge,
    tmp_path: Path,
    conflict: str,
):
    if conflict == "same-id":
        original = (
            bridge.ClipboardFormat(0xC001, "Example.One", b"one"),
            bridge.ClipboardFormat(0xC001, "Example.Two", b"two"),
        )
    else:
        original = (
            bridge.ClipboardFormat(0xC001, "Example.Name", b"one"),
            bridge.ClipboardFormat(0xC002, "example.name", b"two"),
        )
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "must-not-exist.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert backend.replace_calls == 0
    assert backend.formats == original
    assert not backup.exists()


def test_restore_rejects_wrong_payload_even_when_sequence_matches(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    _, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    backend.formats = newer  # Simulate an adversarial backend without advancing its DWORD.

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 4
    assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert backend.formats == newer
    assert backend.replace_calls == 1
    assert backup.is_file()


def test_restore_uses_sequence_cas_after_payload_precheck(bridge, tmp_path: Path):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    _, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    newer = (bridge.ClipboardFormat(13, None, "raced\0".encode("utf-16-le")),)
    backend.race_before_replace_on_calls[2] = newer

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 4
    assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert backend.formats == newer
    assert backup.is_file()


def test_registered_format_name_is_bounded_before_mutation(bridge, tmp_path: Path):
    original = (
        bridge.ClipboardFormat(
            0xC001,
            "x" * (bridge.MAX_REGISTERED_FORMAT_NAME + 1),
            b"opaque",
        ),
    )
    backend = FakeBackend(bridge, original)
    video = make_video(tmp_path)
    backup = tmp_path / "must-not-exist.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert backend.replace_calls == 0
    assert not backup.exists()


def test_corrupt_backup_is_rejected_without_clipboard_mutation(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    backup = tmp_path / "clipboard-backup.json"
    backup.write_text('{"format_version":1}', encoding="utf-8")
    backup.chmod(0o600)
    before = backend.formats

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(backend.sequence()),
    )

    assert exit_code == 5
    assert payload["code"] == "CLIPBOARD_RESTORE_FAILED"
    assert backend.replace_calls == 0
    assert backend.formats == before
    assert backup.is_file()


def test_checksum_tampering_is_rejected(bridge, tmp_path: Path):
    backend = FakeBackend(bridge, fixtures(bridge))
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"
    _, staged = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )
    value = json.loads(backup.read_text(encoding="utf-8"))
    value["formats"][0]["data_base64"] = "AAAA"
    backup.write_text(json.dumps(value), encoding="utf-8")
    backup.chmod(0o600)

    exit_code, payload = invoke(
        bridge,
        backend,
        "restore",
        "--backup",
        str(backup),
        "--expected-change-count",
        str(staged["staged_change_count"]),
    )

    assert exit_code == 5
    assert payload["code"] == "CLIPBOARD_RESTORE_FAILED"
    assert backend.replace_calls == 1


def test_verification_race_preserves_newer_content_and_backup(
    bridge,
    tmp_path: Path,
):
    backend = FakeBackend(bridge, fixtures(bridge))
    newer = (bridge.ClipboardFormat(13, None, "newer\0".encode("utf-16-le")),)
    # stage snapshots once, then snapshots again to verify the replacement.
    backend.mutate_before_snapshot_on_calls[2] = newer
    video = make_video(tmp_path)
    backup = tmp_path / "clipboard-backup.json"

    exit_code, payload = invoke(
        bridge,
        backend,
        "stage",
        "--file",
        str(video),
        "--backup",
        str(backup),
    )

    assert exit_code == 4
    assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert backend.formats == newer
    assert backup.is_file()


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("unknown",),
        ("stage", "--file", "x"),
        ("restore", "--backup", "/x", "--expected-change-count", "-1"),
        ("status", "--extra", "x"),
    ],
)
def test_invalid_arguments_always_return_json(bridge, arguments):
    backend = FakeBackend(bridge)

    exit_code, payload = invoke(bridge, backend, *arguments)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENTS"


def test_import_and_default_backend_failure_do_not_touch_macos_clipboard(bridge):
    if sys.platform == "win32":
        pytest.skip("This assertion is for non-Windows deterministic runs")
    output = io.StringIO()

    exit_code = bridge.main(["status"], stream=output)
    payload = json.loads(output.getvalue())

    assert exit_code == 3
    assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"


def test_native_source_has_no_text_path_fallback():
    source = SOURCE.read_text(encoding="utf-8")
    assert "CF_HDROP = 15" in source
    assert 'struct.pack("<IiiII", 20, 0, 0, 0, 1)' in source
    assert "SetClipboardData" in source
    assert "staged = ClipboardFormat(CF_HDROP" in source
    assert "powershell" not in source.lower()
