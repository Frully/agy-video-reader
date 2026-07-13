"""Authenticated blind-video smoke tests.

These tests upload complete fixture videos to Antigravity.  They are intentionally
excluded from normal test runs.  Set ANTIGRAVITY_VIDEO_E2E=1 only after reviewing
that cost/privacy implication and providing the private blind-test fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER = PACKAGE / "scripts" / "run_antigravity_video.py"
FIXTURES = Path(
    os.environ.get("ANTIGRAVITY_VIDEO_FIXTURES", Path(__file__).parent / "fixtures")
)
GROUND_TRUTH = FIXTURES / "ground-truth.json"
VIDEOS = FIXTURES / "videos"
MODEL = "Gemini 3.5 Flash (High)"


def enabled() -> bool:
    return os.environ.get("ANTIGRAVITY_VIDEO_E2E") == "1"


def authenticated_prerequisites() -> tuple[bool, str]:
    if not enabled():
        return False, "set ANTIGRAVITY_VIDEO_E2E=1 to permit real video uploads"
    if platform.system() != "Darwin":
        return False, "authenticated video E2E is supported only on macOS"
    agy = shutil.which("agy")
    if not agy:
        return False, "agy is not installed"
    try:
        version = subprocess.run(
            [agy, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
        models = subprocess.run(
            [agy, "models"], capture_output=True, text=True, timeout=20, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"agy authentication/preflight is unavailable: {exc}"
    if "1.1.1" not in version.stdout + version.stderr:
        return False, "agy 1.1.1 is required"
    if MODEL not in models.stdout + models.stderr:
        return False, f"{MODEL} is unavailable or authentication is incomplete"
    if not GROUND_TRUTH.is_file() or not VIDEOS.is_dir():
        return False, "private blind-test videos and ground-truth.json are required"
    return True, ""


PREREQUISITES_OK, SKIP_REASON = authenticated_prerequisites()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cases() -> list[dict]:
    if not PREREQUISITES_OK:
        return []
    document = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    raw_cases = document.get("fixtures", document)
    if isinstance(raw_cases, dict):
        return [dict(expectations, filename=filename) for filename, expectations in raw_cases.items()]
    return list(raw_cases)


def normalized_text(value: object) -> str:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, list):
        return "\n".join(normalized_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(normalized_text(item) for item in value.values())
    return str(value).casefold()


def assert_ordered_needles(haystack: object, needles: list[str]) -> None:
    text = normalized_text(haystack)
    offset = 0
    for needle in needles:
        found = text.find(str(needle).casefold(), offset)
        assert found >= 0, f"expected {needle!r} after offset {offset}"
        offset = found + len(str(needle))


def timestamp_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def test_blind_fixture_manifest_is_complete_and_differential():
    document = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    manifest = document["fixtures"]
    assert [case["id"] for case in manifest] == ["A", "B", "C"]
    assert manifest[0]["visual_sequence"] == manifest[1]["visual_sequence"]
    assert manifest[0]["audio_sequence"] != manifest[1]["audio_sequence"]
    assert manifest[0]["audio_sequence"] == manifest[2]["audio_sequence"]
    assert manifest[0]["visual_sequence"] != manifest[2]["visual_sequence"]
    for case in manifest:
        assert str(uuid.UUID(Path(case["filename"]).stem)) == Path(case["filename"]).stem
        video = VIDEOS / case["filename"]
        assert video.is_file() and video.stat().st_size > 0
        assert sha256(video) == case["sha256"]
        assert len(case["visual_sequence"]) == len(case["audio_sequence"]) == len(case["intervals"]) == 4
        assert not any((VIDEOS / f"{video.stem}{suffix}").exists() for suffix in (".txt", ".srt", ".vtt", ".json", ".wav", ".aiff", ".png", ".jpg"))


def assert_timeline(case: dict, result: dict) -> None:
    timeline = result["timeline"]
    for index, (visual, audio, interval) in enumerate(zip(
        case["visual_sequence"], case["audio_sequence"], case["intervals"]
    )):
        matching = [item for item in timeline if visual.casefold() in normalized_text(item.get("visual_event"))
                    or visual.casefold() in normalized_text(item.get("on_screen_text"))]
        assert matching, f"missing visual state {index}: {visual}"
        item = matching[0]
        assert audio.casefold() in normalized_text(item.get("audio_event")), f"missing paired audio state {index}: {audio}"
        start, end = timestamp_seconds(item["start"]), timestamp_seconds(item["end"])
        assert start is not None and end is not None
        assert start <= interval["end_seconds"] + 1.5
        assert end >= interval["start_seconds"] - 1.5


def assert_semantics(case: dict, result: dict) -> None:
    assert_ordered_needles({"visual_summary": result["visual_summary"], "timeline": result["timeline"]}, case["visual_sequence"])
    assert_ordered_needles({"audio_summary": result["audio_summary"], "timeline": result["timeline"]}, case["audio_sequence"])
    assert_timeline(case, result)


def run_case(case: dict, output: Path, *, check_semantics: bool = True) -> dict:
    video = VIDEOS / case["filename"]
    request = output.with_suffix(".request.txt")
    request.write_text(
        "Describe the complete visual and audio sequence in chronological order, "
        "pair each visible title/card with the audio heard at the same time, and include approximate timestamps.",
        encoding="utf-8",
    )
    request.chmod(0o600)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            str(video),
            "--request-file",
            str(request),
            "--timeout-seconds",
            os.environ.get("ANTIGRAVITY_VIDEO_E2E_TIMEOUT", "300"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("ANTIGRAVITY_VIDEO_E2E_PROCESS_TIMEOUT", "420")),
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["backend"]["attachment_confirmed"] is True
    assert result["backend"]["attachment_mime"].startswith("video/")
    assert result["source"]["sha256"] == case["sha256"]
    if check_semantics:
        assert_semantics(case, result)
    return result


@pytest.mark.skipif(not PREREQUISITES_OK, reason=SKIP_REASON)
def test_authenticated_video_matrix(tmp_path: Path):
    manifest = cases()
    results = {case["id"]: run_case(case, tmp_path / f"{case['id']}.json") for case in manifest}
    assert manifest[0]["visual_sequence"] == manifest[1]["visual_sequence"]
    assert manifest[0]["audio_sequence"] == manifest[2]["audio_sequence"]
    assert results["A"]["source"]["sha256"] != results["B"]["source"]["sha256"]
    assert results["A"]["source"]["sha256"] != results["C"]["source"]["sha256"]


@pytest.mark.skipif(
    not PREREQUISITES_OK or os.environ.get("ANTIGRAVITY_VIDEO_CANARY") != "1",
    reason="set both ANTIGRAVITY_VIDEO_E2E=1 and ANTIGRAVITY_VIDEO_CANARY=1 to permit ten uploads",
)
def test_authenticated_ten_run_canary(tmp_path: Path):
    case = cases()[0]
    passes = 0
    for attempt in range(10):
        result = run_case(case, tmp_path / f"canary-{attempt}.json", check_semantics=False)
        try:
            assert_semantics(case, result)
            passes += 1
        except AssertionError:
            pass
    assert passes >= 9
