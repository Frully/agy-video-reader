from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER_PATH = PACKAGE / "scripts" / "run_antigravity_video.py"
FAKE_AGY = Path(__file__).parent / "fixtures" / "bin" / "fake_agy.py"
FAKE_CLIPBOARD = Path(__file__).parent / "fixtures" / "bin" / "fake_clipboard_bridge.py"
TUI_FIXTURES = Path(__file__).parent / "fixtures" / "tui"


def load_runner():
    spec = importlib.util.spec_from_file_location("antigravity_video_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return load_runner()


def error_code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def test_state_enum_preserves_the_required_success_order(runner):
    expected = [
        "PRECHECK",
        "STARTING_AGY",
        "WAITING_FOR_READY",
        "STAGING_CLIPBOARD",
        "SENDING_PASTE",
        "WAITING_FOR_VIDEO_CONFIRMATION",
        "RESTORING_CLIPBOARD",
        "SENDING_ANALYSIS_PROMPT",
        "WAITING_FOR_RESULT_FILE",
        "VALIDATING_RESULT",
        "CLEANUP",
        "DONE",
    ]
    actual = [state.name for state in runner.TUIState]
    positions = [actual.index(name) for name in expected]
    assert positions == sorted(positions)
    assert "GENERATING" not in actual
    assert "CAPTURING_RESULT" not in actual
    assert "FAILED" in actual


def test_terminal_buffer_renders_ansi_redraws_and_bounds_scrollback(runner):
    try:
        terminal = runner.TerminalBuffer(rows=6, cols=100, max_scrollback=256)
    except TypeError:
        terminal = runner.TerminalBuffer()
    terminal.feed(
        b"Starting...\rReady      \x1b[K\r\n"
        b"> Accept-edits mode: file edits auto-approved (shift+tab to cycle)"
    )
    rendered = terminal.normalized_text
    assert "Ready" in rendered
    assert "> Accept-edits mode: file edits auto-approved (shift+tab to cycle)" in rendered
    assert "\x1b" not in rendered

    terminal.feed(b"x" * 2_000)
    raw = getattr(terminal, "raw_text", getattr(terminal, "raw_buffer", b""))
    assert len(raw) <= 2_000


@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("1 media attached (source: clipboard, video/mp4)", "video/mp4"),
        ("1 media attached (source: clipboard, video/quicktime)", "video/quicktime"),
        ("1 media attached (source: clipboard, video/webm)\nlater: 1 image(s)", "video/webm"),
        ("▸ 📎 1 media attached (clipboard, 2.9 MB, video/webm)  (ctrl+o to expand)", "video/webm"),
        ("1 media attached (source: clipboard, image/png)", None),
        ("1 media attached (source: upload, video/mp4)", None),
    ],
)
def test_attachment_confirmation_requires_one_clipboard_video(runner, screen, expected):
    assert runner.attachment_confirmation(screen) == expected


def test_sanitized_tui_fixtures_cover_ready_and_attachment(runner):
    ready = (TUI_FIXTURES / "agy-ready.txt").read_text(encoding="utf-8")
    attached = (TUI_FIXTURES / "agy-attachment-confirmed.txt").read_text(encoding="utf-8")
    terminal = runner.TerminalBuffer()
    terminal.feed(ready.replace("\n", "\r\n").encode())
    assert "Gemini 3.5 Flash (High)" in terminal.normalized_text
    assert "> Accept-edits mode: file edits auto-approved (shift+tab to cycle)" in terminal.normalized_text
    assert runner._ready(terminal.normalized_text)
    assert runner._current_editor_ready(terminal.current_text)
    assert runner.attachment_confirmation(attached) == "video/webm"


def test_multiple_attachments_are_rejected(runner):
    with pytest.raises(runner.RunnerError) as caught:
        runner.attachment_confirmation("2 media attached (source: clipboard, video/mp4)")
    assert error_code(caught.value) == "ATTACHMENT_FAILED"


def test_validate_video_path_enforces_50_mib_boundary(runner, tmp_path):
    video = tmp_path / "fixture.mp4"
    with video.open("wb") as handle:
        handle.truncate(runner.MAX_VIDEO_BYTES)
    assert runner.validate_video_path(video) == video.resolve()

    with video.open("wb") as handle:
        handle.truncate(runner.MAX_VIDEO_BYTES + 1)
    with pytest.raises(runner.RunnerError) as caught:
        runner.validate_video_path(video)
    assert error_code(caught.value) == "VIDEO_TOO_LARGE"
    assert str(runner.MAX_VIDEO_BYTES + 1) in caught.value.message
    assert str(runner.MAX_VIDEO_BYTES) in caught.value.message


def test_load_analysis_request_accepts_utf8_and_multiline(runner, tmp_path):
    request = tmp_path / "request.txt"
    request.write_text("  请概括主要事件。\n并说明音画关系。  ", encoding="utf-8")
    assert runner.load_analysis_request(request) == "请概括主要事件。\n并说明音画关系。"


@pytest.mark.parametrize("content", [b"", b" \t\r\n"])
def test_load_analysis_request_rejects_empty_content(runner, tmp_path, content):
    request = tmp_path / "request.txt"
    request.write_bytes(content)
    with pytest.raises(runner.RunnerError) as caught:
        runner.load_analysis_request(request)
    assert error_code(caught.value) == "REQUEST_INVALID"


def test_load_analysis_request_rejects_invalid_utf8(runner, tmp_path):
    request = tmp_path / "request.txt"
    request.write_bytes(b"analyze \xff video")
    with pytest.raises(runner.RunnerError) as caught:
        runner.load_analysis_request(request)
    assert error_code(caught.value) == "REQUEST_INVALID"


@pytest.mark.parametrize("control", [b"\x00", b"\x1b", b"\x7f"])
def test_load_analysis_request_rejects_control_characters(runner, tmp_path, control):
    request = tmp_path / "request.txt"
    request.write_bytes(b"analyze" + control + b"video")
    with pytest.raises(runner.RunnerError) as caught:
        runner.load_analysis_request(request)
    assert error_code(caught.value) == "REQUEST_INVALID"


def test_load_analysis_request_enforces_8_kib_boundary(runner, tmp_path):
    request = tmp_path / "request.txt"
    request.write_bytes(b"a" * runner.MAX_REQUEST_BYTES)
    assert runner.load_analysis_request(request) == "a" * runner.MAX_REQUEST_BYTES

    request.write_bytes(b"a" * (runner.MAX_REQUEST_BYTES + 1))
    with pytest.raises(runner.RunnerError) as caught:
        runner.load_analysis_request(request)
    assert error_code(caught.value) == "REQUEST_INVALID"


def test_parse_result_file_accepts_one_utf8_json_object(runner):
    payload = {"summary": "视频摘要", "timeline": []}
    assert runner.parse_result_file(json.dumps(payload, ensure_ascii=False).encode("utf-8")) == payload


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{not json}",
        b"[]",
        b'"text"',
    ],
)
def test_parse_result_file_rejects_invalid_content(runner, raw):
    with pytest.raises(runner.RunnerError) as caught:
        runner.parse_result_file(raw)
    assert error_code(caught.value) == "OUTPUT_JSON_INVALID"


@pytest.mark.skipif(sys.platform != "darwin", reason="v2 controlling-terminal profile is macOS-only")
def test_pty_process_provides_a_controlling_terminal(runner, tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "fd = os.open('/dev/tty', os.O_RDWR)\n"
        "os.write(fd, b'CONTROLLING_TTY_OK\\n')\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)

    controller = runner.PTYProcess.start(probe, tmp_path)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "CONTROLLING_TTY_OK" not in controller.terminal.normalized_text:
            controller.read(0.1)
            if controller.process.poll() is not None:
                controller.read(0)
                break
        assert "CONTROLLING_TTY_OK" in controller.terminal.normalized_text
    finally:
        assert controller.stop()


def test_fake_agy_exposes_exact_preflight_profile():
    version = subprocess.run(
        [sys.executable, str(FAKE_AGY), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    models = subprocess.run(
        [sys.executable, str(FAKE_AGY), "models"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout.strip() == "agy 1.1.1"
    assert "Gemini 3.5 Flash (High)" in models.stdout


def test_fake_agy_scenario_switches_are_deterministic(monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "wrong-version")
    completed = subprocess.run(
        [sys.executable, str(FAKE_AGY), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "agy 9.9.9"


def test_fake_clipboard_bridge_stage_and_restore(tmp_path):
    video = tmp_path / "input.mp4"
    backup = tmp_path / "clipboard.backup"
    video.write_bytes(b"not decoded by the fixture")
    staged = subprocess.run(
        [
            sys.executable,
            str(FAKE_CLIPBOARD),
            "stage",
            "--file",
            str(video),
            "--backup",
            str(backup),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    staged_payload = json.loads(staged.stdout)
    assert staged_payload["staged_types"] == ["public.file-url"]
    assert backup.exists()

    restored = subprocess.run(
        [
            sys.executable,
            str(FAKE_CLIPBOARD),
            "restore",
            "--backup",
            str(backup),
            "--expected-change-count",
            str(staged_payload["staged_change_count"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(restored.stdout)["backup_deleted"] is True
    assert not backup.exists()
