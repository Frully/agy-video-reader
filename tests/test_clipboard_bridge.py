import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "clipboard_bridge.swift"

FIXTURE_HELPER_SOURCE = r'''
import AppKit
import Foundation

let pasteboard = NSPasteboard.general
let command = CommandLine.arguments.dropFirst().first
if command == "set" || command == "set-and-hold" {
    let text = NSPasteboardItem()
    guard text.setString("plain fixture", forType: .string) else { exit(31) }

    let rich = NSPasteboardItem()
    guard rich.setData("rich fixture".data(using: .utf16)!, forType: NSPasteboard.PasteboardType("public.utf16-external-plain-text")) else { exit(32) }
    guard rich.setString("rich fixture", forType: .string) else { exit(32) }
    guard rich.setData(Data("{\\rtf1\\ansi rich fixture}".utf8), forType: .rtf) else { exit(32) }

    let image = NSPasteboardItem()
    let onePixelPNG = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")!
    guard image.setData(onePixelPNG, forType: .png) else { exit(33) }

    let custom = NSPasteboardItem()
    guard custom.setData(Data([0x00, 0xff, 0x10, 0x42]), forType: NSPasteboard.PasteboardType("com.example.antigravity-test")) else { exit(34) }

    let firstFile = NSPasteboardItem()
    guard firstFile.setString(URL(fileURLWithPath: "/tmp").absoluteString, forType: .fileURL) else { exit(35) }
    let fileList = NSPasteboardItem()
    let fileListData = try PropertyListSerialization.data(fromPropertyList: ["/tmp", "/Applications"], format: .binary, options: 0)
    guard fileList.setData(fileListData, forType: NSPasteboard.PasteboardType("com.example.file-list-test")) else { exit(36) }

    pasteboard.clearContents()
    guard pasteboard.writeObjects([text, rich, image, custom, firstFile, fileList]) else { exit(2) }
    print(pasteboard.changeCount)
    fflush(stdout)
    if command == "set-and-hold" { RunLoop.current.run(until: Date(timeIntervalSinceNow: 60)) }
} else if command == "dump" {
    let items: [[[String: String]]] = (pasteboard.pasteboardItems ?? []).map { item in
        item.types.map { type in
            ["type": type.rawValue, "base64": (item.data(forType: type) ?? Data()).base64EncodedString()]
        }
    }
    let data = try JSONSerialization.data(withJSONObject: items, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} else {
    exit(2)
}
'''


@pytest.fixture(scope="session")
def bridge(tmp_path_factory):
    if platform.system() != "Darwin" or shutil.which("swiftc") is None:
        pytest.skip("The clipboard bridge requires macOS and swiftc")
    executable = tmp_path_factory.mktemp("clipboard-bridge-bin") / "clipboard_bridge"
    subprocess.run(
        ["swiftc", str(SOURCE), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


@pytest.fixture(scope="session")
def clipboard_fixture_helper(tmp_path_factory):
    if platform.system() != "Darwin" or shutil.which("swiftc") is None:
        pytest.skip("Clipboard fixtures require macOS and swiftc")
    build = tmp_path_factory.mktemp("clipboard-fixture-helper")
    source = build / "fixture.swift"
    executable = build / "fixture"
    source.write_text(FIXTURE_HELPER_SOURCE, encoding="utf-8")
    subprocess.run(
        ["swiftc", str(source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


def run_bridge(bridge, *arguments, check=True):
    completed = subprocess.run(
        [str(bridge), *map(str, arguments)],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    if check:
        assert completed.returncode == 0, payload
        assert payload["ok"] is True
    return completed, payload


def test_compiles_and_reports_machine_readable_status(bridge):
    _, payload = run_bridge(bridge, "status")
    assert payload["operation"] == "status"
    assert isinstance(payload["change_count"], int)


def test_invalid_arguments_are_machine_readable(bridge):
    completed, payload = run_bridge(bridge, "unknown", check=False)
    assert completed.returncode == 2
    assert payload == {
        "backup_preserved": False,
        "code": "INVALID_ARGUMENTS",
        "message": "Use stage, restore, recover, or status.",
        "ok": False,
    }


def test_source_uses_file_url_and_not_plain_text_for_staging():
    source = SOURCE.read_text(encoding="utf-8")
    assert "forType: .fileURL" in source
    assert 'forType: .string' not in source
    assert "O_NOFOLLOW" in source
    assert "S_IRUSR | S_IWUSR" in source
    assert "snapshotsEquivalent(restored, value)" in source
    assert "restored == value" not in source


@pytest.mark.skipif(
    platform.system() != "Darwin" or os.environ.get("RUN_CLIPBOARD_TESTS") != "1",
    reason="Set RUN_CLIPBOARD_TESTS=1 on macOS to temporarily exercise the global clipboard",
)
def test_stage_restore_and_external_change_protection(bridge, clipboard_fixture_helper, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"transport-only-test-bytes")
    outer_backup = tmp_path / "outer.plist"
    inner_backup = tmp_path / "inner.plist"
    race_backup = tmp_path / "race.plist"
    failed_backup = tmp_path / "must-not-exist.plist"

    _, outer = run_bridge(bridge, "stage", "--file", video, "--backup", outer_backup)
    current_expected = outer["staged_change_count"]
    fixture_owner = None
    try:
        fixture_owner = subprocess.Popen(
            [clipboard_fixture_helper, "set-and-hold"],
            stdout=subprocess.PIPE,
            text=True,
        )
        current_expected = int(fixture_owner.stdout.readline())
        expected_fixture = json.loads(
            subprocess.run(
                [clipboard_fixture_helper, "dump"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        _, staged = run_bridge(bridge, "stage", "--file", video, "--backup", inner_backup)
        fixture_owner.terminate()
        fixture_owner.wait(timeout=5)
        fixture_owner = None
        assert staged["staged_types"] == ["public.file-url"]
        assert staged["backup_item_count"] == 6
        assert staged["backup_type_counts"] == [1, 3, 1, 1, 1, 1]
        assert stat.S_IMODE(inner_backup.stat().st_mode) == 0o600

        _, restored = run_bridge(
            bridge,
            "recover",
            "--backup",
            inner_backup,
            "--file",
            video,
        )
        actual_fixture = json.loads(
            subprocess.run(
                [clipboard_fixture_helper, "dump"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert actual_fixture == expected_fixture
        assert not inner_backup.exists()
        current_expected = restored["restored_change_count"]

        _, race_staged = run_bridge(bridge, "stage", "--file", video, "--backup", race_backup)
        subprocess.run(["pbcopy"], input="newer clipboard value", text=True, check=True)
        failed, payload = run_bridge(
            bridge,
            "restore",
            "--backup",
            race_backup,
            "--expected-change-count",
            race_staged["staged_change_count"],
            check=False,
        )
        assert failed.returncode == 4
        assert payload["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
        assert payload["backup_preserved"] is True
        assert subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout == "newer clipboard value"

        _, status_payload = run_bridge(bridge, "status")
        _, race_restored = run_bridge(
            bridge,
            "restore",
            "--backup",
            race_backup,
            "--expected-change-count",
            status_payload["change_count"],
        )
        current_expected = race_restored["restored_change_count"]

        unmaterializable = subprocess.run(
            [clipboard_fixture_helper, "set"],
            check=True,
            capture_output=True,
            text=True,
        )
        current_expected = int(unmaterializable.stdout)
        failed, payload = run_bridge(
            bridge,
            "stage",
            "--file",
            video,
            "--backup",
            failed_backup,
            check=False,
        )
        assert failed.returncode == 3
        assert payload["code"] == "CLIPBOARD_BACKUP_FAILED"
        assert not failed_backup.exists()
        _, status_payload = run_bridge(bridge, "status")
        assert status_payload["change_count"] == current_expected
    finally:
        if fixture_owner is not None and fixture_owner.poll() is None:
            fixture_owner.terminate()
            fixture_owner.wait(timeout=5)
        if race_backup.exists():
            _, status_payload = run_bridge(bridge, "status")
            _, recovered = run_bridge(
                bridge,
                "restore",
                "--backup",
                race_backup,
                "--expected-change-count",
                status_payload["change_count"],
            )
            current_expected = recovered["restored_change_count"]
        if inner_backup.exists():
            _, status_payload = run_bridge(bridge, "status")
            _, recovered = run_bridge(
                bridge,
                "restore",
                "--backup",
                inner_backup,
                "--expected-change-count",
                status_payload["change_count"],
            )
            current_expected = recovered["restored_change_count"]
        if outer_backup.exists():
            run_bridge(
                bridge,
                "restore",
                "--backup",
                outer_backup,
                "--expected-change-count",
                current_expected,
            )
