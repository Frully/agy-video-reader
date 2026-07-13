from __future__ import annotations

import base64
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "linux_clipboard_bridge.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bridge():
    return load_module("antigravity_linux_clipboard_bridge_tests", SOURCE)


def snapshot_payload(bridge, values: dict[str, bytes]) -> dict[str, object]:
    snapshot = bridge.snapshot_from_mapping(values)
    return {
        "ok": True,
        "operation": "snapshot",
        "formats": bridge._snapshot_to_wire(snapshot),
        "total_bytes": sum(len(value) for value in values.values()),
    }


class FakeCopyQRunner:
    def __init__(self, bridge, *outcomes) -> None:
        self.bridge = bridge
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str]] = []

    def eval(self, script: str, *, backend: str):
        self.calls.append((script, backend))
        assert self.outcomes, "unexpected CopyQ eval"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, tuple):
            returncode, payload = outcome
        else:
            returncode, payload = 0, outcome
        return self.bridge.EvalResult(returncode, json.dumps(payload), "ignored stderr")


def test_staged_payload_is_file_uri_only_and_portably_encoded(bridge, tmp_path: Path):
    video = tmp_path / "演示 clip #1.mp4"
    video.write_bytes(b"opaque video fixture")

    staged = bridge.build_staged_snapshot(video)
    values = {item.mime: item.data for item in staged}
    uri = video.resolve().as_uri()

    assert tuple(values) == ("text/uri-list", "x-special/gnome-copied-files")
    assert values["text/uri-list"] == f"{uri}\r\n".encode()
    assert values["x-special/gnome-copied-files"] == f"copy\n{uri}".encode()
    assert "text/plain" not in values
    assert "%23" in uri
    assert "%20" in uri
    assert "%E6%BC%94%E7%A4%BA" in uri


def test_snapshot_round_trips_binary_data_and_preserves_mime_priority(bridge):
    first = bridge.snapshot_from_mapping(
        {"z/example": b"\x00\xff\x10", "a/example": "hello".encode("utf-16")}
    )
    second = bridge.snapshot_from_mapping(
        {"a/example": "hello".encode("utf-16"), "z/example": b"\x00\xff\x10"}
    )

    assert [item.mime for item in first] == ["z/example", "a/example"]
    assert [item.mime for item in second] == ["a/example", "z/example"]
    assert first != second
    assert bridge.snapshot_fingerprint(first) != bridge.snapshot_fingerprint(second)
    assert (
        bridge._snapshot_from_wire(
            bridge._snapshot_to_wire(first), failure_code="CLIPBOARD_BACKUP_FAILED"
        )
        == first
    )


@pytest.mark.parametrize(
    ("requested", "environment", "expected"),
    [
        ("x11", {}, "x11"),
        ("wayland", {}, "wayland"),
        ("auto", {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, "x11"),
        (
            "auto",
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"},
            "wayland",
        ),
        ("auto", {"DISPLAY": ":0"}, "x11"),
        ("auto", {"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
    ],
)
def test_backend_resolution_is_explicit_and_deterministic(
    bridge, requested: str, environment: dict[str, str], expected: str
):
    assert bridge.resolve_backend(requested, environment) == expected


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
        {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
    ],
)
def test_auto_backend_fails_closed_when_session_is_ambiguous(bridge, environment):
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.resolve_backend("auto", environment)
    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"


def test_stage_backs_up_every_mime_before_one_copyq_mutation(bridge, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    original_values = {
        "application/octet-stream": b"\x00\xff\x10\x42",
        "text/plain": b"original text",
        "text/rtf": b"{\\rtf1 original}",
    }
    runner = FakeCopyQRunner(
        bridge,
        snapshot_payload(bridge, original_values),
        {"ok": True, "operation": "stage", "staged_types": list(bridge.STAGED_TYPES)},
    )

    receipt = bridge.stage(video, backup, backend="x11", runner=runner)

    assert receipt["staged_types"] == ["text/uri-list", "x-special/gnome-copied-files"]
    assert isinstance(receipt["staged_change_count"], int)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    saved_backend, original, staged, token = bridge._read_backup(backup)
    assert saved_backend == "x11"
    assert original == bridge.snapshot_from_mapping(original_values)
    assert tuple(item.mime for item in staged) == bridge.STAGED_TYPES
    assert token == receipt["staged_change_count"]
    assert len(runner.calls) == 2
    assert runner.calls[0][0].startswith("// antigravity-copyq: snapshot")
    assert runner.calls[1][0].startswith("// antigravity-copyq: stage")
    assert "copy(item);" in runner.calls[1][0]
    stage_script = runner.calls[1][0]
    assert stage_script.rindex(
        'requireIsolatedClipboard("CLIPBOARD_BACKUP_FAILED")'
    ) < stage_script.index("mutationAttempted = true")


def test_oversize_snapshot_fails_before_backup_or_mutation(
    bridge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "must-not-exist.json"
    runner = FakeCopyQRunner(
        bridge, snapshot_payload(bridge, {"binary/test": b"x" * 4097})
    )
    monkeypatch.setattr(bridge, "MAX_CLIPBOARD_BYTES", 4096)

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.stage(video, backup, backend="wayland", runner=runner)

    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"
    assert not backup.exists()
    assert len(runner.calls) == 1
    assert "antigravity-copyq: snapshot" in runner.calls[0][0]


@pytest.mark.parametrize(
    "formats",
    [
        [
            {"mime": "text/plain", "base64": "eA=="},
            {"mime": "text/plain", "base64": "eQ=="},
        ],
        [{"mime": "text/plain", "base64": "not base64!"}],
        [{"mime": "bad\nmime", "base64": "eA=="}],
        [{"mime": "__proto__", "base64": "eA=="}],
        [{"mime": "constructor", "base64": "eA=="}],
    ],
)
def test_invalid_snapshot_fails_before_backup_or_mutation(
    bridge, tmp_path: Path, formats
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "must-not-exist.json"
    runner = FakeCopyQRunner(
        bridge,
        {"ok": True, "operation": "snapshot", "formats": formats, "total_bytes": 1},
    )

    with pytest.raises(bridge.BridgeError):
        bridge.stage(video, backup, backend="x11", runner=runner)

    assert not backup.exists()
    assert len(runner.calls) == 1


def test_stage_pre_mutation_race_deletes_redundant_backup(
    bridge, tmp_path: Path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    runner = FakeCopyQRunner(
        bridge,
        snapshot_payload(bridge, {"text/plain": b"before"}),
        (
            1,
            {
                "ok": False,
                "operation": "stage",
                "code": "CLIPBOARD_CHANGED_EXTERNALLY",
                "message": "Clipboard changed before attachment staging.",
                "clipboard_restored": None,
                "recovery_required": False,
            },
        ),
    )

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.stage(video, backup, backend="x11", runner=runner)

    assert caught.value.code == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert caught.value.clipboard_restored is True
    assert not backup.exists()
    assert len(runner.calls) == 2


def test_stage_verify_failure_is_explicit_and_keeps_recovery_backup(
    bridge, tmp_path: Path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    runner = FakeCopyQRunner(
        bridge,
        snapshot_payload(bridge, {"text/plain": b"before"}),
        (
            1,
            {
                "ok": False,
                "operation": "stage",
                "code": "CLIPBOARD_BACKUP_FAILED",
                "message": "CopyQ did not retain the staged URI formats.",
                "clipboard_restored": False,
                "recovery_required": True,
            },
        ),
    )

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.stage(video, backup, backend="x11", runner=runner)

    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"
    assert caught.value.clipboard_restored is False
    assert backup.is_file()
    script = runner.calls[1][0]
    assert script.index("mutationAttempted = true") < script.index(
        "copySnapshot(expectedStaged)"
    )
    assert "clipboard_restored: mutationAttempted ? false : null" in script
    assert "recovery_required: mutationAttempted" in script


def test_copyq_item_builder_rejects_object_prototype_keys_before_mutation(bridge):
    script = bridge._snapshot_script()

    assert '"__proto__"' in script
    assert '"constructor"' in script
    assert "RESERVED_ITEM_KEYS.indexOf(mime) >= 0" in script
    assert script.index("RESERVED_ITEM_KEYS.indexOf(mime) >= 0") < script.index(
        "function copySnapshot"
    )


def make_backup(
    bridge, path: Path, video: Path, original_values: dict[str, bytes], backend="x11"
):
    original = bridge.snapshot_from_mapping(original_values)
    staged = bridge.build_staged_snapshot(video)
    value = bridge._backup_value(backend, original, staged)
    bridge._write_backup(path, value)
    return original, staged, value["staged_change_count"]


def test_restore_uses_content_cas_restores_binary_mimes_and_deletes_backup(
    bridge, tmp_path: Path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    original, _, token = make_backup(
        bridge,
        backup,
        video,
        {
            "application/x-copyq-owner": b"old-owner-token",
            "application/example": b"\x00\xffbinary",
            "text/plain": b"original",
        },
    )
    runner = FakeCopyQRunner(
        bridge, {"ok": True, "operation": "restore", "clipboard_restored": True}
    )

    receipt = bridge.restore(
        backup,
        backend="x11",
        runner=runner,
        expected_change_count=token,
    )

    assert receipt["clipboard_restored"] is True
    assert receipt["backup_deleted"] is True
    assert receipt["restored_types"] == [
        item.mime
        for item in original
        if item.mime != "application/x-copyq-owner"
    ]
    assert not backup.exists()
    script = runner.calls[0][0]
    assert script.startswith("// antigravity-copyq: restore")
    assert "stableSnapshot(readSnapshot())" in script
    assert base64.b64encode(b"\x00\xffbinary").decode() in script
    assert base64.b64encode(b"old-owner-token").decode() not in script
    assert "copy(item);" in script
    assert "if (sameSnapshot(current, expectedOriginal))" in script
    assert script.index(
        'requireIsolatedClipboard("CLIPBOARD_RESTORE_FAILED")'
    ) < script.index("copySnapshot(expectedOriginal)")


def test_restore_rejects_newer_clipboard_and_preserves_backup(bridge, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    _, _, token = make_backup(bridge, backup, video, {"text/plain": b"original"})
    runner = FakeCopyQRunner(
        bridge,
        (
            1,
            {
                "ok": False,
                "operation": "restore",
                "code": "CLIPBOARD_CHANGED_EXTERNALLY",
                "message": "Clipboard no longer contains the staged attachment.",
                "clipboard_restored": False,
            },
        ),
    )

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.restore(
            backup,
            backend="x11",
            runner=runner,
            expected_change_count=token,
        )

    assert caught.value.code == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert caught.value.clipboard_restored is False
    assert backup.is_file()
    assert len(runner.calls) == 1


def test_recover_validates_video_and_reports_recover_operation(bridge, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    other_video = tmp_path / "other.mp4"
    other_video.write_bytes(b"other")
    backup = tmp_path / "clipboard-backup.json"
    make_backup(bridge, backup, video, {"text/plain": b"original"})
    unused_runner = FakeCopyQRunner(bridge)

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.restore(
            backup,
            backend="x11",
            runner=unused_runner,
            operation="recover",
            video_path=other_video,
        )
    assert caught.value.code == "CLIPBOARD_RESTORE_FAILED"
    assert not unused_runner.calls
    assert backup.exists()

    runner = FakeCopyQRunner(
        bridge, {"ok": True, "operation": "recover", "clipboard_restored": True}
    )
    receipt = bridge.restore(
        backup,
        backend="x11",
        runner=runner,
        operation="recover",
        video_path=video,
    )
    assert receipt["operation"] == "recover"
    assert not backup.exists()
    recover_script = runner.calls[0][0]
    assert recover_script.startswith("// antigravity-copyq: recover")
    assert "if (sameSnapshot(current, expectedOriginal))" in recover_script
    assert recover_script.index(
        'requireIsolatedClipboard("CLIPBOARD_RESTORE_FAILED")'
    ) < recover_script.index("copySnapshot(expectedOriginal)")


def test_restore_rejects_non_private_backup_before_copyq(bridge, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backup = tmp_path / "clipboard-backup.json"
    make_backup(bridge, backup, video, {"text/plain": b"original"})
    backup.chmod(0o644)
    runner = FakeCopyQRunner(bridge)

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.restore(backup, backend="x11", runner=runner)

    assert caught.value.code == "CLIPBOARD_RESTORE_FAILED"
    assert caught.value.clipboard_restored is False
    assert not runner.calls
    assert backup.exists()


def test_status_is_one_read_only_eval_with_stable_fingerprint(bridge):
    values = {
        "application/x-copyq-owner": b"volatile-session-token",
        "text/plain": b"current",
    }
    runner = FakeCopyQRunner(bridge, snapshot_payload(bridge, values))

    receipt = bridge.status(runner, backend="wayland")

    assert receipt["operation"] == "status"
    assert receipt["types"] == ["application/x-copyq-owner", "text/plain"]
    assert len(receipt["fingerprint"]) == 64
    assert len(runner.calls) == 1
    assert runner.calls[0][0].startswith("// antigravity-copyq: snapshot")
    assert runner.calls[0][0].count("copySnapshot(") == 1


def test_subprocess_runner_passes_script_on_stdin_and_selects_qt_backend(
    bridge, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "{}\n", "")

    monkeypatch.setattr(bridge.shutil, "which", lambda executable: "/opt/Copy Q/copyq")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    runner = bridge.SubprocessCopyQRunner("copyq")

    result = runner.eval("// clipboard script", backend="wayland")

    assert result.returncode == 0
    assert captured["argv"] == ["/opt/Copy Q/copyq", "eval", "-"]
    assert captured["input"] == "// clipboard script"
    assert captured["env"]["QT_QPA_PLATFORM"] == "wayland"
    assert captured["capture_output"] is True
    assert captured["text"] is True


@pytest.mark.parametrize(
    ("operation", "arguments", "expected_code", "expected_restored"),
    [
        (
            "stage",
            ["stage", "--file", "/missing.mp4", "--backup", "/backup.json"],
            "CLIPBOARD_BACKUP_FAILED",
            None,
        ),
        (
            "restore",
            ["restore", "--backup", "/backup.json", "--expected-change-count", "1"],
            "CLIPBOARD_RESTORE_FAILED",
            False,
        ),
        (
            "recover",
            ["recover", "--backup", "/backup.json", "--file", "/missing.mp4"],
            "CLIPBOARD_RESTORE_FAILED",
            False,
        ),
    ],
)
def test_cli_missing_copyq_uses_operation_specific_failure(
    bridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    arguments: list[str],
    expected_code: str,
    expected_restored: bool | None,
):
    monkeypatch.setattr(bridge.shutil, "which", lambda _executable: None)

    exit_code = bridge.main(["--backend", "x11", "--copyq", "missing", *arguments])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["operation"] == operation
    assert payload["code"] == expected_code
    assert payload["clipboard_restored"] is expected_restored
