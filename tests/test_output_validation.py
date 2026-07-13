from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER_PATH = PACKAGE / "scripts" / "run_antigravity_video.py"
SCHEMA_PATH = PACKAGE / "references" / "output-schema.json"

EXPECTED_BACKEND = {
    "provider": "antigravity-cli",
    "cli_version": "1.1.1",
    "model": "Gemini 3.5 Flash (High)",
    "attachment_confirmed": True,
    "attachment_mime": "video/mp4",
}
EXPECTED_SOURCE = {
    "filename": "fixture.mp4",
    "size_bytes": 4096,
    "sha256": "a" * 64,
}


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "antigravity_video_output_validation_runner", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return load_runner()


def semantic_payload() -> dict:
    return {
        "summary": "A title card appears while a short phrase is spoken.",
        "visual_summary": "A red title card appears, followed by a blue card.",
        "audio_summary": "Two words are spoken with no music.",
        "timeline": [
            {
                "start": "00:00",
                "end": "00:02",
                "visual_event": "A red card appears.",
                "audio_event": "The first word is spoken.",
                "on_screen_text": ["RED"],
                "confidence": 0.95,
                "uncertainties": [],
            },
            {
                "start": "00:02",
                "end": "00:04",
                "visual_event": "A blue card appears.",
                "audio_event": "The second word is spoken.",
                "on_screen_text": ["BLUE"],
                "confidence": 0.9,
                "uncertainties": ["Boundary is approximate."],
            },
        ],
        "uncertainties": ["All timestamps are approximate."],
        "evidence_quality": {
            "visual": "high",
            "audio": "high",
            "temporal": "medium",
        },
    }


def final_payload() -> dict:
    return {
        "schema_version": "1.0",
        "backend": copy.deepcopy(EXPECTED_BACKEND),
        "source": copy.deepcopy(EXPECTED_SOURCE),
        **semantic_payload(),
    }


def assert_output_invalid(runner, payload: dict) -> None:
    with pytest.raises(runner.RunnerError) as caught:
        runner.validate_output_payload(payload)
    assert caught.value.code == "OUTPUT_JSON_INVALID"
    assert getattr(caught.value.state, "name", caught.value.state) == "VALIDATING_RESULT"


def test_schema_document_is_valid_json_and_declares_closed_top_level_contract():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "backend",
        "source",
        "summary",
        "visual_summary",
        "audio_summary",
        "timeline",
        "uncertainties",
        "evidence_quality",
    }


def test_valid_final_payload_is_returned_in_schema_shape(runner):
    result = runner.validate_output_payload(final_payload())
    assert result == final_payload()


def test_evidence_quality_is_canonicalized_before_closed_enum_validation(runner):
    payload = final_payload()
    payload["evidence_quality"] = {
        "visual": " High\n",
        "audio": "MEDIUM",
        "temporal": " unknown\t",
    }
    result = runner.validate_output_payload(payload)
    assert result["evidence_quality"] == {
        "visual": "high",
        "audio": "medium",
        "temporal": "unknown",
    }


def test_invalid_or_missing_evidence_quality_degrades_to_unknown(runner):
    payload = final_payload()
    payload["evidence_quality"] = {
        "visual": "excellent",
        "audio": {"level": "high"},
        "extra": "high",
    }
    assert runner.validate_output_payload(payload)["evidence_quality"] == {
        "visual": "unknown",
        "audio": "unknown",
        "temporal": "unknown",
    }


def test_runner_injects_trusted_envelope_fields_into_model_payload(runner):
    model_result = semantic_payload()
    model_result.update(
        {
            "schema_version": "attacker-controlled",
            "backend": {
                "provider": "other",
                "attachment_confirmed": False,
            },
            "source": {
                "filename": "/private/source.mp4",
                "size_bytes": 1,
                "sha256": "0" * 64,
            },
        }
    )
    normalized = runner.validate_output_payload(
        model_result,
        expected_backend=EXPECTED_BACKEND,
        expected_source=EXPECTED_SOURCE,
    )
    assert normalized["schema_version"] == "1.0"
    assert normalized["backend"] == EXPECTED_BACKEND
    assert normalized["source"] == EXPECTED_SOURCE
    assert "/private/source.mp4" not in json.dumps(normalized)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("summary"),
        lambda value: value.update({"summary": "..."}),
        lambda value: value.update({"unexpected": True}),
        lambda value: value["timeline"][0].update({"unexpected": True}),
        lambda value: value["timeline"][0].update({"confidence": 1.01}),
        lambda value: value["timeline"][0].update({"confidence": -0.01}),
        lambda value: value["timeline"][0].update({"visual_event": ""}),
        lambda value: value["backend"].update({"attachment_confirmed": False}),
        lambda value: value["backend"].update({"attachment_mime": "image/png"}),
        lambda value: value["source"].update({"filename": "/tmp/fixture.mp4"}),
        lambda value: value["source"].update({"sha256": "not-a-sha256"}),
    ],
)
def test_schema_violations_fail_closed(runner, mutate):
    payload = final_payload()
    mutate(payload)
    assert_output_invalid(runner, payload)


@pytest.mark.parametrize(
    "timestamp",
    ["0:00", "00:60", "00:00:60", "1.5", "soon", "", 12],
)
def test_invalid_timestamps_are_rejected(runner, timestamp):
    payload = final_payload()
    payload["timeline"][0]["start"] = timestamp
    assert_output_invalid(runner, payload)


def test_timestamp_may_be_null_when_model_cannot_support_one(runner):
    payload = final_payload()
    payload["timeline"][0]["start"] = None
    payload["timeline"][0]["end"] = None
    assert runner.validate_output_payload(payload)["timeline"][0]["start"] is None


def test_each_timeline_interval_must_not_run_backwards(runner):
    payload = final_payload()
    payload["timeline"][0]["start"] = "00:05"
    payload["timeline"][0]["end"] = "00:04"
    assert_output_invalid(runner, payload)


def test_timeline_start_times_must_be_non_decreasing(runner):
    payload = final_payload()
    payload["timeline"][0].update({"start": "00:10", "end": "00:12"})
    payload["timeline"][1].update({"start": "00:09", "end": "00:13"})
    assert_output_invalid(runner, payload)


def test_mixed_minute_and_hour_timestamps_sort_chronologically(runner):
    payload = final_payload()
    payload["timeline"][0].update({"start": "59:58", "end": "59:59"})
    payload["timeline"][1].update({"start": "1:00:00", "end": "1:00:01.250"})
    assert runner.validate_output_payload(payload)["timeline"] == payload["timeline"]
