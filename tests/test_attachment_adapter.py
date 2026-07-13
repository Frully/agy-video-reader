from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
ADAPTER_PATH = PACKAGE / "scripts" / "attachment_adapter.py"
RUNNER_PATH = PACKAGE / "scripts" / "run_antigravity_video.py"
FAKE_CLIPBOARD = Path(__file__).parent / "fixtures" / "bin" / "fake_clipboard_bridge.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def adapters():
    return load_module("antigravity_attachment_adapter_tests", ADAPTER_PATH)


@pytest.fixture(scope="module")
def runner():
    return load_module("antigravity_attachment_runner_tests", RUNNER_PATH)


def test_factory_selects_macos_adapter(adapters):
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    assert adapter.name == "macos-file-url-clipboard"
    assert adapter.verification_status == "implemented-native-evidence"


def test_factory_selects_implemented_unverified_windows_adapter(adapters):
    adapter = adapters.create_attachment_adapter(
        platform_name="win32",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    assert adapter.name == "windows-cf-hdrop-clipboard"
    assert adapter.verification_status == "implemented-unverified"
    assert adapter.recovery_filename == "clipboard-backup.json"


@pytest.mark.parametrize(
    ("environment", "expected_name"),
    [
        ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, "linux-x11-uri-list-clipboard"),
        (
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            "linux-wayland-uri-list-clipboard",
        ),
        ({"DISPLAY": ":1"}, "linux-x11-uri-list-clipboard"),
        ({"WAYLAND_DISPLAY": "wayland-1"}, "linux-wayland-uri-list-clipboard"),
    ],
)
def test_factory_selects_implemented_unverified_linux_adapter(
    adapters,
    environment: dict[str, str],
    expected_name: str,
):
    adapter = adapters.create_attachment_adapter(
        platform_name="linux",
        bridge_override=str(FAKE_CLIPBOARD),
        environment=environment,
    )
    assert adapter.name == expected_name
    assert adapter.verification_status == "implemented-unverified"


@pytest.mark.parametrize("platform_name", ["cygwin", "unknown"])
def test_unknown_platforms_fail_closed_without_creating_state(
    adapters,
    tmp_path: Path,
    platform_name: str,
):
    cache_root = tmp_path / "must-not-exist"
    adapter = adapters.create_attachment_adapter(
        platform_name=platform_name,
        bridge_override=str(FAKE_CLIPBOARD),
    )
    assert adapter.name == "unsupported"
    with pytest.raises(adapters.AttachmentFailure) as caught:
        adapter.prepare(cache_root)
    assert caught.value.code == "ATTACHMENT_ADAPTER_UNAVAILABLE"
    assert caught.value.clipboard_restored is None
    assert not cache_root.exists()


@pytest.mark.parametrize(
    ("environment", "platform_release"),
    [
        ({"WSL_DISTRO_NAME": "Ubuntu", "DISPLAY": ":0"}, "6.8.0-generic"),
        ({"DISPLAY": ":0"}, "5.15.153.1-microsoft-standard-WSL2"),
    ],
)
def test_wsl_does_not_silently_select_native_linux_adapter(
    adapters,
    tmp_path: Path,
    environment: dict[str, str],
    platform_release: str,
):
    adapter = adapters.create_attachment_adapter(
        platform_name="linux",
        bridge_override=str(FAKE_CLIPBOARD),
        environment=environment,
        platform_release=platform_release,
    )
    assert adapter.name == "unsupported"
    with pytest.raises(adapters.AttachmentFailure) as caught:
        adapter.prepare(tmp_path / "must-not-exist")
    assert caught.value.code == "ATTACHMENT_ADAPTER_UNAVAILABLE"
    assert "WSL" in caught.value.message


def test_linux_ambiguous_or_headless_session_fails_before_creating_state(
    adapters,
    tmp_path: Path,
):
    adapter = adapters.create_attachment_adapter(
        platform_name="linux",
        bridge_override=str(FAKE_CLIPBOARD),
        environment={"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
    )
    assert adapter.name == "linux-undetermined-uri-list-clipboard"
    with pytest.raises(adapters.AttachmentFailure) as caught:
        adapter.prepare(tmp_path / "must-not-exist")
    assert caught.value.code == "ATTACHMENT_ADAPTER_UNAVAILABLE"
    assert not (tmp_path / "must-not-exist").exists()


def test_linux_prepare_uses_non_mutating_copyq_preflight(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    linux_module = sys.modules[adapters.LinuxURIListClipboardAdapter.__module__]
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "ok": True,
                    "copy_clipboard": "false",
                    "copy_selection": "false",
                }
            ),
            "",
        )

    monkeypatch.setattr(linux_module.shutil, "which", lambda _name: "/opt/copyq")
    monkeypatch.setattr(linux_module.subprocess, "run", fake_run)
    adapter = adapters.LinuxWaylandURIListClipboardAdapter()

    adapter.prepare(tmp_path / "unused-cache")

    assert captured["argv"] == ["/opt/copyq", "eval", "-"]
    assert "config('copy_clipboard')" in captured["input"]
    assert "config('copy_selection')" in captured["input"]
    assert captured["env"]["QT_QPA_PLATFORM"] == "wayland"
    assert adapter.command_prefix is not None
    assert adapter.command_prefix[-2:] == ("--backend", "wayland")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (0, "not-json"),
        (
            1,
            json.dumps(
                {
                    "ok": False,
                    "copy_clipboard": "false",
                    "copy_selection": "false",
                }
            ),
        ),
        (
            0,
            json.dumps(
                {
                    "ok": True,
                    "copy_clipboard": "true",
                    "copy_selection": "false",
                }
            ),
        ),
        (
            0,
            json.dumps(
                {
                    "ok": True,
                    "copy_clipboard": "false",
                    "copy_selection": "true",
                }
            ),
        ),
    ],
)
def test_linux_prepare_fails_closed_on_invalid_or_primary_syncing_copyq(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
):
    linux_module = sys.modules[adapters.LinuxURIListClipboardAdapter.__module__]
    monkeypatch.setattr(linux_module.shutil, "which", lambda _name: "/opt/copyq")
    monkeypatch.setattr(
        linux_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, returncode, stdout, ""
        ),
    )
    adapter = adapters.LinuxX11URIListClipboardAdapter()

    with pytest.raises(adapters.AttachmentFailure) as caught:
        adapter.prepare(tmp_path / "unused-cache")

    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"
    assert adapter.command_prefix is None


def test_runner_checks_platform_adapter_before_reading_inputs(
    runner,
    tmp_path: Path,
):
    args = argparse.Namespace(
        video=str(tmp_path / "missing.mp4"),
        request_file=str(tmp_path / "missing-request.txt"),
        output=str(tmp_path / "must-not-exist.json"),
        timeout_seconds=1,
        keep_sanitized_log=False,
        agy_executable=str(tmp_path / "missing-agy"),
        clipboard_bridge=None,
    )
    controller = runner.VideoRunner(args)
    controller.cache_root = tmp_path / "must-not-create-cache"
    controller.workspace = controller.cache_root / "workspace"
    controller.lock = runner.Lock(controller.cache_root / "clipboard.lock")
    controller.attachment_adapter = runner.create_attachment_adapter(platform_name="unknown")

    with pytest.raises(runner.RunnerError) as caught:
        controller.run()

    assert caught.value.code == "ATTACHMENT_ADAPTER_UNAVAILABLE"
    assert caught.value.state is runner.TUIState.PRECHECK
    assert caught.value.video_uploaded is False
    assert caught.value.clipboard_restored is None
    assert not controller.cache_root.exists()


def test_windows_runner_gate_precedes_inputs_agy_and_cache(
    runner,
    tmp_path: Path,
):
    args = argparse.Namespace(
        video=str(tmp_path / "missing.mp4"),
        request_file=str(tmp_path / "missing-request.txt"),
        output=str(tmp_path / "must-not-exist.json"),
        timeout_seconds=1,
        keep_sanitized_log=False,
        agy_executable=str(tmp_path / "missing-agy"),
        clipboard_bridge=None,
    )
    controller = runner.VideoRunner(args)
    controller.platform_name = "win32"
    controller.cache_root = tmp_path / "must-not-create-cache"
    controller.workspace = controller.cache_root / "workspace"
    controller.lock = runner.Lock(controller.cache_root / "clipboard.lock")
    controller.attachment_adapter = runner.create_attachment_adapter(platform_name="win32")

    with pytest.raises(runner.RunnerError) as caught:
        controller.run()

    assert caught.value.code == "ATTACHMENT_ADAPTER_UNAVAILABLE"
    assert "CF_HDROP adapter is implemented" in caught.value.message
    assert caught.value.state is runner.TUIState.PRECHECK
    assert caught.value.video_uploaded is False
    assert not controller.cache_root.exists()


def test_platform_cache_roots_are_not_macos_hard_coded(runner):
    assert runner._default_cache_root(
        "linux", {"XDG_CACHE_HOME": "/tmp/xdg-cache"}
    ) == Path("/tmp/xdg-cache/agy-video-reader")
    assert runner._default_cache_root(
        "win32", {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}
    ) == Path("C:/Users/Test/AppData/Local/agy-video-reader")
    assert runner._default_cache_root(
        "linux", {"XDG_CACHE_HOME": "relative/cache"}
    ) == Path.home() / ".cache" / "agy-video-reader"


@pytest.mark.parametrize(
    ("platform_name", "environment", "staged_types"),
    [
        ("win32", {}, ["CF_HDROP"]),
        (
            "linux",
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            ["text/uri-list", "x-special/gnome-copied-files"],
        ),
        (
            "linux",
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"},
            ["text/uri-list", "x-special/gnome-copied-files"],
        ),
    ],
)
def test_unverified_platform_transactions_follow_shared_safety_protocol(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    environment: dict[str, str],
    staged_types: list[str],
):
    monkeypatch.setenv("FAKE_CLIPBOARD_STAGE_TYPES", json.dumps(staged_types))
    adapter = adapters.create_attachment_adapter(
        platform_name=platform_name,
        bridge_override=str(FAKE_CLIPBOARD),
        environment=environment,
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / adapter.recovery_filename
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    transaction.stage()
    assert transaction.paste_bytes == b"\x16"
    assert transaction.restore_required is True
    assert recovery.is_file()

    transaction.restore()
    assert transaction.clipboard_restored is True
    assert transaction.restore_required is False
    assert not recovery.exists()


def test_linux_pre_mutation_race_requires_no_recovery(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "stage-pre-mutation-race")
    adapter = adapters.create_attachment_adapter(
        platform_name="linux",
        bridge_override=str(FAKE_CLIPBOARD),
        environment={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / adapter.recovery_filename
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.stage()

    assert caught.value.code == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert transaction.clipboard_restored is True
    assert transaction.restore_required is False
    assert not recovery.exists()


def test_macos_transaction_hides_bridge_state_and_restores(adapters, tmp_path: Path):
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    transaction.stage()
    assert transaction.paste_bytes == b"\x16"
    assert recovery.is_file()
    assert transaction.clipboard_restored is None
    assert transaction.restore_required is True

    transaction.restore()
    assert transaction.clipboard_restored is True
    assert transaction.restore_required is False
    assert not recovery.exists()


def test_macos_transaction_recovers_after_stage_crash(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "stage-crash")
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.stage()
    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"
    assert recovery.is_file()
    assert transaction.restore_required is True

    transaction.restore()
    assert transaction.clipboard_restored is True
    assert not recovery.exists()


def test_macos_transaction_rejects_non_file_url_stage_confirmation(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "invalid-stage-success")
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.stage()

    assert caught.value.code == "CLIPBOARD_BACKUP_FAILED"
    assert transaction.restore_required is True
    transaction.restore()
    assert transaction.clipboard_restored is True
    assert not recovery.exists()


def test_macos_transaction_rejects_stage_success_without_private_backup(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "stage-missing-backup")
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.stage()

    assert caught.value.code == "CLIPBOARD_RESTORE_FAILED"
    assert transaction.clipboard_restored is False
    assert transaction.restore_required is True
    assert not recovery.exists()


def test_macos_transaction_preserves_newer_clipboard_and_recovery_backup(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "changed-externally")
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)
    transaction.stage()

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.restore()

    assert caught.value.code == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert transaction.clipboard_restored is False
    assert transaction.restore_required is True
    assert recovery.is_file()
    assert str(recovery) in caught.value.next_step


def test_macos_transaction_fails_closed_if_backup_disappears_after_stage(
    adapters,
    tmp_path: Path,
):
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)
    transaction.stage()
    recovery.unlink()

    assert transaction.restore_required is True
    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.restore()

    assert caught.value.code == "CLIPBOARD_RESTORE_FAILED"
    assert transaction.clipboard_restored is False


def test_macos_transaction_rejects_false_success_from_restore_bridge(
    adapters,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = adapters.create_attachment_adapter(
        platform_name="darwin",
        bridge_override=str(FAKE_CLIPBOARD),
    )
    adapter.prepare(tmp_path / "cache")
    video = tmp_path / "input.mp4"
    recovery = tmp_path / "clipboard-backup.plist"
    video.write_bytes(b"opaque transport fixture")
    transaction = adapter.transaction(video_path=video, recovery_path=recovery)
    transaction.stage()
    monkeypatch.setenv("FAKE_CLIPBOARD_SCENARIO", "invalid-restore-success")

    with pytest.raises(adapters.AttachmentFailure) as caught:
        transaction.restore()

    assert caught.value.code == "CLIPBOARD_RESTORE_FAILED"
    assert transaction.clipboard_restored is None
    assert recovery.is_file()
