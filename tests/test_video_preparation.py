from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
PREPARER_PATH = PACKAGE / "scripts" / "prepare_agy_video.py"
SOURCE_FIXTURE = PACKAGE / "tests" / "fixtures" / "videos" / "47f1a8d6-cf0b-4e32-a12d-6d5799d67101.mp4"


def load_preparer():
    spec = importlib.util.spec_from_file_location("agy_video_preparer_tests", PREPARER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preparer():
    return load_preparer()


def test_small_video_uses_original_without_media_tools(preparer, tmp_path: Path):
    output_dir = tmp_path / "prepared"
    manifest = preparer.prepare_video(
        SOURCE_FIXTURE,
        output_dir,
        ffmpeg_override="/must/not/be/read",
        ffprobe_override="/must/not/be/read",
    )
    assert manifest["preparation"] == {
        "mode": "original",
        "attachment_limit_bytes": preparer.ATTACHMENT_LIMIT_BYTES,
        "target_part_bytes": preparer.TARGET_PART_BYTES,
        "complete_duration_preserved": True,
        "transcoded": False,
        "segmented": False,
    }
    assert manifest["parts"][0]["path"] == str(SOURCE_FIXTURE.resolve())
    assert manifest["parts"][0]["sha256"] == manifest["source"]["sha256"]
    assert (output_dir / preparer.MANIFEST_FILENAME).stat().st_mode & 0o777 == 0o600


def test_plan_uses_one_proxy_when_full_duration_bitrate_is_usable(preparer):
    probe = preparer.MediaProbe(240.0, 1920, 1080, "h264", 1, 0)
    plan = preparer.build_plan(100 * 1024 * 1024, probe)
    assert plan.mode == "compressed_proxy"
    assert len(plan.parts) == 1
    assert plan.parts[0].start_seconds == 0
    assert plan.parts[0].end_seconds == probe.duration_seconds
    assert plan.parts[0].video_bitrate_kbps >= plan.video_bitrate_floor_kbps


def test_balanced_profile_caps_large_video_at_480p(preparer):
    assert preparer.output_dimensions(1920, 1080) == (854, 480)
    assert preparer.quality_floor_kbps(1920, 1080) == 550
    assert preparer.output_dimensions(640, 360) == (640, 360)
    assert preparer.quality_floor_kbps(640, 360) == 350


def test_balanced_profile_uses_four_parts_for_38_minute_video(preparer):
    probe = preparer.MediaProbe(38 * 60 + 28, 1280, 720, "h264", 1, 0)
    plan = preparer.build_plan(235_400_000, probe)
    assert plan.mode == "segmented_proxy"
    assert plan.video_bitrate_floor_kbps == 550
    assert len(plan.parts) == 4


def test_plan_segments_long_video_with_overlap_and_complete_coverage(preparer):
    assert preparer.SEGMENT_OVERLAP_SECONDS == 5.0
    probe = preparer.MediaProbe(3600.0, 1920, 1080, "h264", 1, 0)
    plan = preparer.build_plan(600 * 1024 * 1024, probe)
    assert plan.mode == "segmented_proxy"
    assert 1 < len(plan.parts) <= preparer.MAX_SEGMENTS
    assert plan.parts[0].start_seconds == 0
    assert plan.parts[-1].end_seconds == probe.duration_seconds
    for previous, current in zip(plan.parts, plan.parts[1:]):
        assert current.start_seconds < previous.end_seconds
        assert current.overlap_seconds == pytest.approx(preparer.SEGMENT_OVERLAP_SECONDS)
        assert current.video_bitrate_kbps == plan.video_bitrate_floor_kbps


def test_plan_caps_excessive_segment_count(preparer):
    probe = preparer.MediaProbe(12 * 3600.0, 1920, 1080, "h264", 1, 0)
    with pytest.raises(preparer.PreparationError) as caught:
        preparer.build_plan(8 * 1024 * 1024 * 1024, probe)
    assert caught.value.code == "VIDEO_TOO_MANY_SEGMENTS"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the representative transcode",
)
def test_representative_transcode_is_private_complete_and_below_limit(preparer, tmp_path: Path):
    source = tmp_path / "oversized.mp4"
    source.write_bytes(SOURCE_FIXTURE.read_bytes())
    with source.open("ab") as stream:
        stream.truncate(1200 * 1024)

    output_dir = tmp_path / "prepared"
    limit = 1024 * 1024
    target = 900 * 1024
    manifest = preparer.prepare_video(
        source,
        output_dir,
        attachment_limit_bytes=limit,
        target_part_bytes=target,
    )
    assert manifest["preparation"]["mode"] == "compressed_proxy"
    assert manifest["preparation"]["profile"] == "balanced-480p"
    assert manifest["preparation"]["max_output_width"] == 854
    assert manifest["preparation"]["max_output_height"] == 480
    assert manifest["preparation"]["max_output_fps"] == 30
    assert manifest["preparation"]["complete_duration_preserved"] is True
    assert len(manifest["parts"]) == 1
    proxy = Path(manifest["parts"][0]["path"])
    assert 0 < proxy.stat().st_size <= limit
    assert proxy.stat().st_mode & 0o777 == 0o600
    assert manifest["parts"][0]["end_seconds"] == pytest.approx(10.0)
    assert manifest["parts"][0]["width"] <= preparer.MAX_OUTPUT_WIDTH
    assert manifest["parts"][0]["height"] <= preparer.MAX_OUTPUT_HEIGHT
    assert manifest["quality_warnings"]


def test_output_directory_must_be_empty(preparer, tmp_path: Path):
    output_dir = tmp_path / "prepared"
    output_dir.mkdir()
    (output_dir / "unrelated.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(preparer.PreparationError) as caught:
        preparer.prepare_video(SOURCE_FIXTURE, output_dir)
    assert caught.value.code == "OUTPUT_DIRECTORY_INVALID"
    assert (output_dir / "unrelated.txt").read_text(encoding="utf-8") == "keep"


def test_failed_transcode_removes_partial_output(preparer, tmp_path: Path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "partial.mp4"
    part = preparer.PartPlan(1, 0.0, 10.0, 0.0, 500)
    probe = preparer.MediaProbe(10.0, 640, 360, "h264", 1, 0)

    calls = 0

    def fake_run(command, code, next_step):
        nonlocal calls
        calls += 1
        if calls == 2:
            output.write_bytes(b"partial")
            raise preparer.PreparationError(code, "forced failure", next_step)
        return None

    monkeypatch.setattr(preparer, "run_checked", fake_run)
    with pytest.raises(preparer.PreparationError):
        preparer.encode_part(
            source,
            output,
            part,
            probe,
            Path("/fake/ffmpeg"),
            Path("/fake/ffprobe"),
        )
    assert not output.exists()


def test_source_change_is_detected(preparer, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"before")
    expected = source.stat()
    expected_hash = preparer.sha256_file(source)
    source.write_bytes(b"after")
    with pytest.raises(preparer.PreparationError) as caught:
        preparer.assert_source_unchanged(source, expected, expected_hash)
    assert caught.value.code == "VIDEO_CHANGED_DURING_PREPARATION"
