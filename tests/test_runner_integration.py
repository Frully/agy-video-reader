from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER_PATH = PACKAGE / "scripts" / "run_antigravity_video.py"
FAKE_AGY = Path(__file__).parent / "fixtures" / "bin" / "fake_agy.py"
FAKE_CLIPBOARD = Path(__file__).parent / "fixtures" / "bin" / "fake_clipboard_bridge.py"
TEST_PROFILE_RUNNER = Path(__file__).parent / "fixtures" / "bin" / "run_macos_profile_for_test.py"
LINUX_PROFILE_RUNNER = Path(__file__).parent / "fixtures" / "bin" / "run_linux_profile_for_test.py"
RUNNER_ENTRY = RUNNER_PATH if sys.platform == "darwin" else TEST_PROFILE_RUNNER
DEFAULT_REQUEST = "Summarize the relevant visual and audio evidence."
MAX_REQUEST_BYTES = 8 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
OBSERVED_REJECTED_VIDEO_BYTES = 56_032_543

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The deterministic PTY integration fixture requires a POSIX host.",
)


@dataclass(frozen=True)
class FakeRun:
    completed: subprocess.CompletedProcess[str]
    home: Path
    output: Path
    events: Path
    prompts: Path
    request_file: Path
    symlink_target: Path

    def event_lines(self) -> list[str]:
        return self.events.read_text(encoding="utf-8").splitlines() if self.events.exists() else []

    def prompt_texts(self) -> list[str]:
        if not self.prompts.exists():
            return []
        return [json.loads(line) for line in self.prompts.read_text(encoding="utf-8").splitlines()]


def fake_command(
    video: Path,
    request_file: Path,
    output: Path | None = None,
    *,
    keep_sanitized_log: bool = False,
    runner_entry: Path = RUNNER_ENTRY,
) -> list[str]:
    command = [
        sys.executable,
        str(runner_entry),
        str(video),
        "--request-file",
        str(request_file),
        "--agy-executable",
        str(FAKE_AGY),
        "--clipboard-bridge",
        str(FAKE_CLIPBOARD),
        "--timeout-seconds",
        "3",
    ]
    if output is not None:
        command += ["--output", str(output)]
    if keep_sanitized_log:
        command.append("--keep-sanitized-log")
    return command


def run_fake(
    tmp_path: Path,
    *,
    scenario: str = "success",
    clipboard_scenario: str = "success",
    request: str | bytes | None = DEFAULT_REQUEST,
    video_size_bytes: int | None = None,
    keep_sanitized_log: bool = False,
    runner_entry: Path = RUNNER_ENTRY,
) -> FakeRun:
    home = tmp_path / "home"
    home.mkdir()
    video = tmp_path / "fixture.mp4"
    if video_size_bytes is None:
        video.write_bytes(b"opaque transport fixture")
    else:
        with video.open("wb") as handle:
            handle.truncate(video_size_bytes)
    request_file = tmp_path / "request.txt"
    if isinstance(request, str):
        request_file.write_text(request, encoding="utf-8")
    elif isinstance(request, bytes):
        request_file.write_bytes(request)
    output = tmp_path / "result.json"
    events = tmp_path / "events.log"
    prompts = tmp_path / "prompts.jsonl"
    symlink_target = tmp_path / "symlink-target.json"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "FAKE_AGY_SCENARIO": scenario,
            "FAKE_CLIPBOARD_SCENARIO": clipboard_scenario,
            "FAKE_AGY_EVENT_LOG": str(events),
            "FAKE_AGY_PROMPT_LOG": str(prompts),
            "FAKE_AGY_SYMLINK_TARGET": str(symlink_target),
        }
    )
    completed = subprocess.run(
        fake_command(
            video,
            request_file,
            output,
            keep_sanitized_log=keep_sanitized_log,
            runner_entry=runner_entry,
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return FakeRun(completed, home, output, events, prompts, request_file, symlink_target)


def error_from(run: FakeRun) -> dict[str, object]:
    return json.loads(run.completed.stderr)["error"]


def analysis_request_from_prompt(prompt: str) -> str:
    match = re.search(
        r"USER_ANALYSIS_REQUEST_JSON\n([^\n]+)\nEND_USER_ANALYSIS_REQUEST_JSON",
        prompt,
    )
    assert match is not None
    return json.loads(match.group(1))["analysis_request"]


def assert_request_rejected_before_upload(run: FakeRun) -> None:
    assert run.completed.returncode != 0
    assert not run.output.exists()
    error = error_from(run)
    assert error["code"] == "REQUEST_INVALID"
    assert error["video_uploaded"] is False
    assert "PASTE" not in run.event_lines()


def test_oversized_video_is_rejected_before_agy_or_clipboard(tmp_path: Path):
    run = run_fake(tmp_path, video_size_bytes=OBSERVED_REJECTED_VIDEO_BYTES)
    assert run.completed.returncode != 0
    assert not run.output.exists()
    error = error_from(run)
    assert error["code"] == "VIDEO_TOO_LARGE"
    assert error["failed_state"] == "PRECHECK"
    assert error["video_uploaded"] is False
    assert str(OBSERVED_REJECTED_VIDEO_BYTES) in str(error["message"])
    assert str(MAX_VIDEO_BYTES) in str(error["message"])
    assert run.event_lines() == []


def test_fake_success_proves_v2_argv_tty_attachment_and_result_order(tmp_path: Path):
    run = run_fake(tmp_path)
    assert run.completed.returncode == 0, run.completed.stderr
    result = json.loads(run.output.read_text(encoding="utf-8"))
    assert result["backend"]["attachment_confirmed"] is True
    assert run.event_lines() == [
        "VERSION",
        "MODELS",
        "EXEC:--model Gemini 3.5 Flash (High) --sandbox --mode accept-edits",
        "TTY_OK",
        "READY",
        "CLIPBOARD_STAGE",
        "PASTE",
        "ATTACHMENT_CONFIRMED",
        "CLIPBOARD_RESTORE",
        "PROMPT",
        "RESULT_WRITE",
        "EDITOR_READY",
        "EOF_1",
        "EOF_2",
    ]
    invocation = "\n".join(run.event_lines())
    for forbidden in ("--print", "--prompt", "--add-dir", "--continue", "--conversation"):
        assert forbidden not in invocation


def test_test_only_posix_ci_wrapper_runs_deterministic_profile(tmp_path: Path):
    run = run_fake(tmp_path, runner_entry=TEST_PROFILE_RUNNER)
    assert run.completed.returncode == 0, run.completed.stderr
    assert "ATTACHMENT_CONFIRMED" in run.event_lines()


def test_linux_profile_wires_xdg_factory_json_recovery_and_posix_pty(tmp_path: Path):
    run = run_fake(tmp_path, runner_entry=LINUX_PROFILE_RUNNER)

    assert run.completed.returncode == 0, run.completed.stderr
    assert "ATTACHMENT_CONFIRMED" in run.event_lines()
    cache_root = run.home / ".cache" / "agy-video-reader"
    assert cache_root.is_dir()
    assert not list(cache_root.rglob("clipboard-backup.json"))


def test_unicode_multiline_request_is_embedded_in_dynamic_prompt(tmp_path: Path):
    request = "请只分析 00:10–00:20。\n第二行：人物说了什么？ 🎬"
    run = run_fake(tmp_path, request=request)
    assert run.completed.returncode == 0, run.completed.stderr
    prompts = run.prompt_texts()
    assert len(prompts) == 1
    assert analysis_request_from_prompt(prompts[0]) == request


def test_schema_failure_gets_one_rewrite_without_reupload(tmp_path: Path):
    run = run_fake(tmp_path, scenario="schema-retry")
    assert run.completed.returncode == 0, run.completed.stderr
    result = json.loads(run.output.read_text(encoding="utf-8"))
    assert result["timeline"][0]["confidence"] == 0.9
    events = run.event_lines()
    assert events.count("PASTE") == 1
    assert events.count("ATTACHMENT_CONFIRMED") == 1
    assert events.count("PROMPT") == 2
    assert events.count("RESULT_WRITE") == 2
    prompts = run.prompt_texts()
    assert analysis_request_from_prompt(prompts[0]) == DEFAULT_REQUEST
    assert "previous `result.json`" in prompts[1]


def test_generation_timeout_rejects_partial_run(tmp_path: Path):
    run = run_fake(tmp_path, scenario="generation-timeout")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "AGY_GENERATION_TIMEOUT"
    assert error["video_uploaded"] is True
    assert "RESULT_WRITE" not in run.event_lines()


def test_tool_approval_is_rejected_without_approval_or_output(tmp_path: Path):
    run = run_fake(tmp_path, scenario="tool-request")
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == "AGY_TOOL_REQUESTED"
    assert "RESULT_WRITE" not in run.event_lines()


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("auth-required", "AGY_AUTH_REQUIRED"),
        ("setup-required", "AGY_SETUP_REQUIRED"),
        ("media-rejected", "MEDIA_REJECTED"),
        ("multiple-attachments", "ATTACHMENT_FAILED"),
        ("wrong-version", "AGY_VERSION_UNSUPPORTED"),
        ("model-unavailable", "AGY_MODEL_UNAVAILABLE"),
    ],
)
def test_control_plane_failures_are_rejected_without_output(
    tmp_path: Path, scenario: str, code: str
):
    run = run_fake(tmp_path, scenario=scenario)
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == code


def test_clipboard_race_retains_only_recovery_backup(tmp_path: Path):
    run = run_fake(tmp_path, clipboard_scenario="changed-externally")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "CLIPBOARD_CHANGED_EXTERNALLY"
    assert error["clipboard_restored"] is False
    runs = run.home / "Library" / "Caches" / "agy-video-reader" / "runs"
    retained = [path for path in runs.rglob("*") if path.is_file()]
    assert [path.name for path in retained] == ["clipboard-backup.plist"]
    assert str(retained[0]) in str(error["next_step"])


def test_clipboard_stage_crash_uses_backup_recovery(tmp_path: Path):
    run = run_fake(tmp_path, clipboard_scenario="stage-crash")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert error["clipboard_restored"] is True
    assert "CLIPBOARD_RECOVER" in run.event_lines()
    runs = run.home / "Library" / "Caches" / "agy-video-reader" / "runs"
    assert not runs.exists() or not any(path.is_file() for path in runs.rglob("*"))


def test_failed_stage_with_original_clipboard_removes_redundant_backup(tmp_path: Path):
    run = run_fake(tmp_path, clipboard_scenario="stage-left-original")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "CLIPBOARD_BACKUP_FAILED"
    assert error["clipboard_restored"] is True
    assert "CLIPBOARD_RECOVER" in run.event_lines()
    runs = run.home / "Library" / "Caches" / "agy-video-reader" / "runs"
    assert not runs.exists() or not any(path.is_file() for path in runs.rglob("*"))


def test_stage_success_without_private_backup_never_sends_paste(tmp_path: Path):
    run = run_fake(tmp_path, clipboard_scenario="stage-missing-backup")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "CLIPBOARD_RESTORE_FAILED"
    assert error["clipboard_restored"] is False
    assert "PASTE" not in run.event_lines()


@pytest.mark.parametrize("request_payload", [None, b"", b"\xff"])
def test_invalid_request_is_rejected_before_upload(tmp_path: Path, request_payload: bytes | None):
    assert_request_rejected_before_upload(run_fake(tmp_path, request=request_payload))


def test_oversized_request_is_rejected_before_upload(tmp_path: Path):
    request = b"x" * (MAX_REQUEST_BYTES + 1)
    assert_request_rejected_before_upload(run_fake(tmp_path, request=request))


def test_control_character_request_is_rejected_before_upload(tmp_path: Path):
    assert_request_rejected_before_upload(run_fake(tmp_path, request=b"question\x1bmore"))


def test_missing_result_file_reports_output_file_missing(tmp_path: Path):
    run = run_fake(tmp_path, scenario="missing-output")
    assert run.completed.returncode != 0 and not run.output.exists()
    error = error_from(run)
    assert error["code"] == "OUTPUT_FILE_MISSING"
    assert error["video_uploaded"] is True
    assert error["clipboard_restored"] is True


def test_result_is_not_accepted_without_editor_ready(tmp_path: Path):
    run = run_fake(tmp_path, scenario="result-without-ready")
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == "OUTPUT_FILE_MISSING"
    assert "RESULT_WRITE" in run.event_lines()


def test_unexpected_workspace_artifact_is_rejected_and_cleaned(tmp_path: Path):
    run = run_fake(tmp_path, scenario="extra-artifact")
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == "AGY_TOOL_REQUESTED"
    workspace = run.home / "Library" / "Caches" / "agy-video-reader" / "workspace"
    assert workspace.exists() and not any(workspace.iterdir())


def test_result_symlink_is_rejected_and_workspace_is_cleaned(tmp_path: Path):
    run = run_fake(tmp_path, scenario="result-symlink")
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == "AGY_TOOL_REQUESTED"
    workspace = run.home / "Library" / "Caches" / "agy-video-reader" / "workspace"
    assert workspace.exists() and not any(workspace.iterdir())
    assert run.symlink_target.is_file()


def test_result_change_after_validation_is_rejected(tmp_path: Path):
    run = run_fake(tmp_path, scenario="late-result-change")
    assert run.completed.returncode != 0 and not run.output.exists()
    assert error_from(run)["code"] == "OUTPUT_JSON_INVALID"
    assert "LATE_RESULT_CHANGE" in run.event_lines()


def test_request_is_absent_from_result_and_sanitized_log(tmp_path: Path):
    secret_request = "私密请求 sentinel-REQUEST-7f20：只看最后一秒。"
    run = run_fake(tmp_path, request=secret_request, keep_sanitized_log=True)
    assert run.completed.returncode == 0, run.completed.stderr
    result_text = run.output.read_text(encoding="utf-8")
    assert secret_request not in result_text
    log_metadata = json.loads(run.completed.stderr)
    log_text = Path(log_metadata["sanitized_log_path"]).read_text(encoding="utf-8")
    assert secret_request not in log_text
    assert "sentinel-REQUEST-7f20" not in log_text
    assert json.loads(log_text)["terminal_excerpt"] == "[OMITTED_AFTER_ANALYSIS_PROMPT]"
