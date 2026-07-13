#!/usr/bin/env python3
"""Prepare one local video for agy without modifying the source.

Files at or below agy's attachment limit are referenced directly. Oversized
files are transcoded once to a full-duration analysis proxy when the resulting
video bitrate remains useful; otherwise they are transcoded into overlapping
segments. The caller owns and must remove the private output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NoReturn


PREPARER_VERSION = "1.0.0"
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi"}
ATTACHMENT_LIMIT_BYTES = 50 * 1024 * 1024
TARGET_PART_BYTES = 47 * 1024 * 1024
MUX_SAFETY_FACTOR = 0.96
AUDIO_BITRATE_KBPS = 96
SEGMENT_OVERLAP_SECONDS = 2.0
MAX_SEGMENTS = 24
MANIFEST_FILENAME = "manifest.json"


class PreparationError(Exception):
    def __init__(self, code: str, message: str, next_step: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "next_step": self.next_step}}


def fail(code: str, message: str, next_step: str) -> NoReturn:
    raise PreparationError(code, message, next_step)


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_stream_count: int
    subtitle_stream_count: int


@dataclass(frozen=True)
class PartPlan:
    index: int
    start_seconds: float
    end_seconds: float
    overlap_seconds: float
    video_bitrate_kbps: int

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class PreparationPlan:
    mode: str
    parts: tuple[PartPlan, ...]
    video_bitrate_floor_kbps: int | None
    audio_bitrate_kbps: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_source(value: str | os.PathLike[str]) -> tuple[Path, os.stat_result]:
    raw = os.fspath(value)
    path = Path(raw).expanduser().resolve(strict=False)
    try:
        info = path.stat()
    except FileNotFoundError:
        fail("VIDEO_NOT_FOUND", "The selected video does not exist.", "Check the local path and retry.")
    except OSError as exc:
        fail("VIDEO_NOT_FOUND", f"The selected video cannot be accessed: {exc}.", "Check file permissions and retry.")
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
        fail("VIDEO_NOT_REGULAR_FILE", "The selected video must be a readable regular file.", "Choose another local file.")
    if info.st_size < 1:
        fail("VIDEO_EMPTY", "The selected video is empty.", "Choose a non-empty local video.")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        fail("VIDEO_FORMAT_UNSUPPORTED", "Only MP4, MOV, WebM, and AVI files are supported.", "Choose a supported local video.")
    return path, info


def resolve_media_tool(name: str, override: str | None = None) -> Path:
    candidate = override or shutil.which(name)
    if not candidate:
        fail(
            "FFMPEG_NOT_FOUND",
            f"{name} is required to prepare an oversized video.",
            "Install ffmpeg with ffprobe on macOS, then retry.",
        )
    path = Path(candidate).expanduser().resolve(strict=False)
    if not path.is_file() or not os.access(path, os.X_OK):
        fail("FFMPEG_NOT_FOUND", f"{name} is not an executable file.", "Install a working ffmpeg distribution and retry.")
    return path


def run_checked(command: list[str], code: str, next_step: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        fail(code, f"Could not execute the media tool: {exc}.", next_step)
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:] or completed.stdout.strip()[-2000:] or "unknown media-tool failure"
        fail(code, f"Media preparation failed: {detail}", next_step)
    return completed


def verify_ffmpeg_capabilities(ffmpeg: Path) -> None:
    completed = run_checked(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        "FFMPEG_CODEC_UNAVAILABLE",
        "Install an ffmpeg build with libx264 and AAC encoders.",
    )
    available = completed.stdout + completed.stderr
    encoder_names = {
        fields[1]
        for line in available.splitlines()
        if len(fields := line.split()) >= 2 and fields[0][0:1] in {"V", "A", "S"}
    }
    if "libx264" not in encoder_names or "aac" not in encoder_names:
        fail(
            "FFMPEG_CODEC_UNAVAILABLE",
            "The installed ffmpeg does not expose both libx264 and AAC encoders.",
            "Install an ffmpeg build with libx264 and AAC encoders.",
        )


def probe_media(path: Path, ffprobe: Path) -> MediaProbe:
    completed = run_checked(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path),
        ],
        "VIDEO_PROBE_FAILED",
        "Verify that the file is a playable video and retry.",
    )
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
        streams = payload.get("streams", [])
        video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
        width = int(video_stream["width"])
        height = int(video_stream["height"])
        codec = str(video_stream.get("codec_name") or "unknown")
        audio_count = sum(stream.get("codec_type") == "audio" for stream in streams)
        subtitle_count = sum(stream.get("codec_type") == "subtitle" for stream in streams)
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail("VIDEO_PROBE_FAILED", f"The media metadata is incomplete: {exc}.", "Choose a playable video with one visual stream.")
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        fail("VIDEO_PROBE_FAILED", "The video duration or dimensions are invalid.", "Choose a playable video and retry.")
    return MediaProbe(duration, width, height, codec, audio_count, subtitle_count)


def output_dimensions(width: int, height: int) -> tuple[int, int]:
    ratio = min(1.0, 1280 / width, 720 / height)
    output_width = max(2, int(width * ratio) // 2 * 2)
    output_height = max(2, int(height * ratio) // 2 * 2)
    return output_width, output_height


def quality_floor_kbps(width: int, height: int) -> int:
    output_width, output_height = output_dimensions(width, height)
    pixels = output_width * output_height
    if pixels <= 640 * 360:
        return 350
    if pixels <= 854 * 480:
        return 550
    return 1000


def available_video_bitrate_kbps(
    duration_seconds: float,
    *,
    target_bytes: int = TARGET_PART_BYTES,
    audio_bitrate_kbps: int = AUDIO_BITRATE_KBPS,
) -> int:
    total_kbps = int((target_bytes * 8 * MUX_SAFETY_FACTOR) / duration_seconds / 1000)
    return max(1, total_kbps - audio_bitrate_kbps)


def build_plan(
    source_size_bytes: int,
    probe: MediaProbe | None,
    *,
    attachment_limit_bytes: int = ATTACHMENT_LIMIT_BYTES,
    target_part_bytes: int = TARGET_PART_BYTES,
    overlap_seconds: float = SEGMENT_OVERLAP_SECONDS,
    max_segments: int = MAX_SEGMENTS,
) -> PreparationPlan:
    if source_size_bytes <= attachment_limit_bytes:
        return PreparationPlan("original", (), None, 0)
    if probe is None:
        raise ValueError("probe is required for oversized media")
    audio_kbps = AUDIO_BITRATE_KBPS if probe.audio_stream_count else 0
    floor_kbps = quality_floor_kbps(probe.width, probe.height)
    full_kbps = available_video_bitrate_kbps(
        probe.duration_seconds,
        target_bytes=target_part_bytes,
        audio_bitrate_kbps=audio_kbps,
    )
    if full_kbps >= floor_kbps:
        return PreparationPlan(
            "compressed_proxy",
            (PartPlan(1, 0.0, probe.duration_seconds, 0.0, full_kbps),),
            floor_kbps,
            audio_kbps,
        )

    maximum_window_seconds = (target_part_bytes * 8 * MUX_SAFETY_FACTOR) / ((floor_kbps + audio_kbps) * 1000)
    core_seconds = max(10.0, maximum_window_seconds - overlap_seconds)
    parts: list[PartPlan] = []
    core_start = 0.0
    while core_start < probe.duration_seconds - 0.001:
        start = max(0.0, core_start - (overlap_seconds if parts else 0.0))
        end = min(probe.duration_seconds, core_start + core_seconds)
        parts.append(PartPlan(len(parts) + 1, start, end, core_start - start, floor_kbps))
        core_start = end
    if len(parts) > max_segments:
        fail(
            "VIDEO_TOO_MANY_SEGMENTS",
            f"Quality-preserving preparation would require {len(parts)} uploads, above the safety cap of {max_segments}.",
            "Use a backend with a larger attachment limit or explicitly narrow the requested time range.",
        )
    return PreparationPlan("segmented_proxy", tuple(parts), floor_kbps, audio_kbps)


def ensure_private_empty_directory(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if sys.platform == "darwin":
        workspace = (Path.home() / "Library" / "Caches" / "agy-video-reader" / "workspace").resolve(strict=False)
        try:
            path.relative_to(workspace)
        except ValueError:
            pass
        else:
            fail(
                "OUTPUT_DIRECTORY_INVALID",
                "Prepared media cannot be stored inside the Antigravity workspace.",
                "Choose a private temporary directory outside the Antigravity cache workspace.",
            )
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            fail("OUTPUT_DIRECTORY_INVALID", "The output path must be a real directory.", "Choose a new private temporary directory.")
        if any(path.iterdir()):
            fail("OUTPUT_DIRECTORY_INVALID", "The output directory must be empty.", "Choose a new private temporary directory.")
    else:
        path.mkdir(mode=0o700, parents=True)
    os.chmod(path, 0o700)
    return path


def assert_source_unchanged(path: Path, expected: os.stat_result, expected_sha256: str) -> None:
    try:
        current = path.stat()
    except OSError as exc:
        fail("VIDEO_CHANGED_DURING_PREPARATION", f"The source became unavailable: {exc}.", "Keep the source stable and retry.")
    expected_signature = (expected.st_size, expected.st_mtime_ns, expected.st_dev, expected.st_ino)
    current_signature = (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino)
    if current_signature != expected_signature or sha256_file(path) != expected_sha256:
        fail(
            "VIDEO_CHANGED_DURING_PREPARATION",
            "The source video changed while the analysis media was being prepared.",
            "Stop writers or downloads touching the file, then retry from the stable original.",
        )


def ffmpeg_base_command(
    ffmpeg: Path,
    source: Path,
    plan: PartPlan,
    video_bitrate_kbps: int,
    passlog: Path,
) -> list[str]:
    scale_filter = "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    return [
        str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-ss", f"{plan.start_seconds:.3f}", "-t", f"{plan.duration_seconds:.3f}",
        "-map", "0:v:0", "-vf", scale_filter, "-fpsmax", "30", "-c:v", "libx264",
        "-preset", "medium", "-pix_fmt", "yuv420p", "-b:v", f"{video_bitrate_kbps}k",
        "-passlogfile", str(passlog),
    ]


def remove_passlogs(passlog: Path) -> None:
    for candidate in passlog.parent.glob(f"{passlog.name}*"):
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink(missing_ok=True)


def encode_part(
    source: Path,
    output: Path,
    part: PartPlan,
    probe: MediaProbe,
    ffmpeg: Path,
    ffprobe: Path,
    *,
    attachment_limit_bytes: int = ATTACHMENT_LIMIT_BYTES,
    target_part_bytes: int = TARGET_PART_BYTES,
) -> tuple[MediaProbe, int]:
    bitrate_kbps = part.video_bitrate_kbps
    passlog = output.parent / f".pass-{part.index:04d}"
    verified = False
    try:
        for attempt in range(2):
            output.unlink(missing_ok=True)
            remove_passlogs(passlog)
            first = ffmpeg_base_command(ffmpeg, source, part, bitrate_kbps, passlog)
            run_checked(
                first + ["-pass", "1", "-an", "-f", "null", os.devnull],
                "VIDEO_TRANSCODE_FAILED",
                "Verify ffmpeg supports libx264 and retry.",
            )
            second = ffmpeg_base_command(ffmpeg, source, part, bitrate_kbps, passlog)
            second += ["-pass", "2"]
            if probe.audio_stream_count:
                second += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k"]
            else:
                second += ["-an"]
            second += [
                "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero", str(output),
            ]
            run_checked(second, "VIDEO_TRANSCODE_FAILED", "Verify ffmpeg supports H.264/AAC MP4 output and retry.")
            os.chmod(output, 0o600)
            actual_size = output.stat().st_size
            if 0 < actual_size <= attachment_limit_bytes:
                output_probe = probe_media(output, ffprobe)
                tolerance = max(1.0, part.duration_seconds * 0.02)
                if abs(output_probe.duration_seconds - part.duration_seconds) > tolerance:
                    fail(
                        "VIDEO_TRANSCODE_FAILED",
                        "The prepared part did not preserve its planned duration.",
                        "Keep the original and inspect the media timestamps before retrying.",
                    )
                verified = True
                return output_probe, bitrate_kbps
            if attempt == 0 and actual_size > 0:
                bitrate_kbps = max(100, int(bitrate_kbps * target_part_bytes / actual_size * 0.90))
        fail(
            "VIDEO_TRANSCODE_TOO_LARGE",
            f"The prepared part still exceeds {attachment_limit_bytes} bytes after one bounded retry.",
            "Use segmentation or a backend with a larger attachment limit.",
        )
    finally:
        remove_passlogs(passlog)
        if not verified:
            output.unlink(missing_ok=True)


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def prepare_video(
    source_value: str | os.PathLike[str],
    output_dir_value: str | os.PathLike[str],
    *,
    ffmpeg_override: str | None = None,
    ffprobe_override: str | None = None,
    attachment_limit_bytes: int = ATTACHMENT_LIMIT_BYTES,
    target_part_bytes: int = TARGET_PART_BYTES,
    max_segments: int = MAX_SEGMENTS,
) -> dict[str, Any]:
    source, source_info = validate_source(source_value)
    output_dir = ensure_private_empty_directory(output_dir_value)
    source_hash = sha256_file(source)
    source_meta: dict[str, Any] = {
        "filename": source.name,
        "size_bytes": source_info.st_size,
        "sha256": source_hash,
    }
    if source_info.st_size <= attachment_limit_bytes:
        assert_source_unchanged(source, source_info, source_hash)
        manifest = {
            "schema_version": "1.0",
            "preparer_version": PREPARER_VERSION,
            "source": source_meta,
            "preparation": {
                "mode": "original",
                "attachment_limit_bytes": attachment_limit_bytes,
                "target_part_bytes": target_part_bytes,
                "complete_duration_preserved": True,
                "transcoded": False,
                "segmented": False,
            },
            "parts": [{
                "index": 1, "path": str(source), "filename": source.name,
                "size_bytes": source_info.st_size, "sha256": source_hash,
                "start_seconds": 0.0, "end_seconds": None, "overlap_seconds": 0.0,
                "video_bitrate_kbps": None, "width": None, "height": None,
            }],
            "quality_warnings": [],
        }
        atomic_json_write(output_dir / MANIFEST_FILENAME, manifest)
        return manifest

    ffmpeg = resolve_media_tool("ffmpeg", ffmpeg_override)
    ffprobe = resolve_media_tool("ffprobe", ffprobe_override)
    verify_ffmpeg_capabilities(ffmpeg)
    probe = probe_media(source, ffprobe)
    source_meta.update({
        "duration_seconds": probe.duration_seconds,
        "width": probe.width,
        "height": probe.height,
        "video_codec": probe.video_codec,
        "audio_stream_count": probe.audio_stream_count,
        "subtitle_stream_count": probe.subtitle_stream_count,
    })
    plan = build_plan(
        source_info.st_size,
        probe,
        attachment_limit_bytes=attachment_limit_bytes,
        target_part_bytes=target_part_bytes,
        max_segments=max_segments,
    )
    part_records: list[dict[str, Any]] = []
    generated: list[Path] = []
    try:
        for part in plan.parts:
            filename = "analysis-proxy.mp4" if plan.mode == "compressed_proxy" else f"part-{part.index:04d}.mp4"
            output = output_dir / filename
            output_probe, actual_bitrate = encode_part(
                source, output, part, probe, ffmpeg, ffprobe,
                attachment_limit_bytes=attachment_limit_bytes,
                target_part_bytes=target_part_bytes,
            )
            generated.append(output)
            part_records.append({
                "index": part.index,
                "path": str(output),
                "filename": filename,
                "size_bytes": output.stat().st_size,
                "sha256": sha256_file(output),
                "start_seconds": round(part.start_seconds, 3),
                "end_seconds": round(part.end_seconds, 3),
                "overlap_seconds": round(part.overlap_seconds, 3),
                "video_bitrate_kbps": actual_bitrate,
                "width": output_probe.width,
                "height": output_probe.height,
            })
        assert_source_unchanged(source, source_info, source_hash)
        warnings = [
            "Analysis uses a transcoded H.264/AAC proxy; fine visual detail, small text, HDR, and fast motion may be degraded.",
        ]
        if probe.audio_stream_count > 1:
            warnings.append("Only the first audio stream is retained in the analysis proxy.")
        if probe.subtitle_stream_count:
            warnings.append("Soft subtitle streams are not retained; burned-in text remains part of the image.")
        if plan.mode == "segmented_proxy":
            warnings.append("The source is split into overlapping parts; cross-boundary relationships require host-side synthesis.")
        manifest = {
            "schema_version": "1.0",
            "preparer_version": PREPARER_VERSION,
            "source": source_meta,
            "preparation": {
                "mode": plan.mode,
                "attachment_limit_bytes": attachment_limit_bytes,
                "target_part_bytes": target_part_bytes,
                "complete_duration_preserved": True,
                "transcoded": True,
                "segmented": plan.mode == "segmented_proxy",
                "video_bitrate_floor_kbps": plan.video_bitrate_floor_kbps,
                "audio_bitrate_kbps": plan.audio_bitrate_kbps,
                "segment_overlap_seconds": SEGMENT_OVERLAP_SECONDS if plan.mode == "segmented_proxy" else 0.0,
            },
            "parts": part_records,
            "quality_warnings": warnings,
        }
        atomic_json_write(output_dir / MANIFEST_FILENAME, manifest)
        return manifest
    except BaseException:
        for output in generated:
            output.unlink(missing_ok=True)
        (output_dir / MANIFEST_FILENAME).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare a local video for agy's 50 MiB attachment limit")
    parser.add_argument("video", help="Local MP4, MOV, WebM, or AVI file")
    parser.add_argument("--output-dir", required=True, help="New or empty private directory for manifest and proxies")
    parser.add_argument("--ffmpeg", help=argparse.SUPPRESS)
    parser.add_argument("--ffprobe", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = prepare_video(
            args.video,
            args.output_dir,
            ffmpeg_override=args.ffmpeg,
            ffprobe_override=args.ffprobe,
        )
    except PreparationError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
