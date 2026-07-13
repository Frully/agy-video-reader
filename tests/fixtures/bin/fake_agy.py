#!/usr/bin/env python3
"""Deterministic Antigravity TUI double for the v2 result-file contract."""

from __future__ import annotations

import json
import os
import sys
import termios
import time
import tty
from pathlib import Path


MODEL = "Gemini 3.5 Flash (High)"
VERSION = "1.1.1"
INTERACTIVE_ARGV = ["--model", MODEL, "--sandbox", "--mode", "accept-edits"]
EDITOR_READY = "> Accept-edits mode: file edits auto-approved (shift+tab to cycle)"


def event(name: str) -> None:
    if path := os.environ.get("FAKE_AGY_EVENT_LOG"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(name + "\n")


def record_prompt(prompt: str) -> None:
    if path := os.environ.get("FAKE_AGY_PROMPT_LOG"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(prompt, ensure_ascii=False) + "\n")


def emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def result_payload(*, valid: bool = True) -> dict[str, object]:
    confidence: float | str = 0.9 if valid else "high"
    return {
        "summary": "A deterministic fake video result.",
        "visual_summary": "A blue title appears.",
        "audio_summary": "A voice says alpha.",
        "timeline": [
            {
                "start": "00:00",
                "end": "00:01",
                "visual_event": "Blue title",
                "audio_event": "alpha",
                "on_screen_text": ["BLUE"],
                "confidence": confidence,
                "uncertainties": [],
            }
        ],
        "uncertainties": [],
        "evidence_quality": {
            "visual": "high",
            "audio": "high",
            "temporal": "medium",
        },
    }


def write_result(*, valid: bool = True) -> None:
    result_path = Path.cwd() / "result.json"
    result_path.write_text(
        json.dumps(result_payload(valid=valid), ensure_ascii=False),
        encoding="utf-8",
    )
    event("RESULT_WRITE")


def emit_editor_ready() -> None:
    emit(f"{EDITOR_READY}\r\n")
    event("EDITOR_READY")


def validate_controlling_tty() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        event("TTY_FAILED")
        return False
    os.close(fd)
    event("TTY_OK")
    return True


def handle_prompt(scenario: str, prompt_count: int) -> bool:
    """Handle one submitted prompt; return False when the fake should exit."""
    emit("\x1b[2J\x1b[HGenerating\r\n")
    if scenario == "tool-request":
        emit("Approve this tool? Run shell command [y/N]\r\n")
        return True
    if scenario == "generation-timeout":
        time.sleep(3600)
        return True
    if scenario == "missing-output":
        emit(f"{EDITOR_READY}\r\n")
        event("EDITOR_READY")
        return False
    if scenario == "result-without-ready":
        write_result()
        return False
    if scenario == "extra-artifact":
        write_result()
        (Path.cwd() / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        event("EXTRA_ARTIFACT")
        emit_editor_ready()
        return True
    if scenario == "result-symlink":
        target = Path(os.environ["FAKE_AGY_SYMLINK_TARGET"])
        target.write_text(json.dumps(result_payload()), encoding="utf-8")
        (Path.cwd() / "result.json").symlink_to(target)
        event("RESULT_SYMLINK")
        emit_editor_ready()
        return True

    write_result(valid=not (scenario == "schema-retry" and prompt_count == 1))
    emit_editor_ready()
    return True


def interactive() -> int:
    if not validate_controlling_tty():
        emit("No controlling TTY\r\n")
        return 2

    original_tty = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "success")
    try:
        if scenario == "auth-required":
            emit("Open this authorization URL in a browser:\r\nhttps://example.invalid/oauth\r\n")
        elif scenario == "setup-required":
            emit("Do you trust the files in this workspace? (y/N)\r\n")
        elif scenario != "ready-timeout":
            emit(f"Antigravity\r\n{MODEL}\r\n{EDITOR_READY}\r\n")
            event("READY")

        prompt = ""
        eof_count = 0
        prompt_count = 0
        while True:
            char = sys.stdin.read(1)
            if char == "":
                return 0
            if char == "\x16":
                event("PASTE")
                if scenario == "attachment-timeout":
                    continue
                if scenario == "media-rejected":
                    emit(f"Media could not be attached: unsupported media\r\n{EDITOR_READY}\r\n")
                elif scenario == "multiple-attachments":
                    emit("2 media attached (source: clipboard, video/mp4)\r\n")
                    event("ATTACHMENT_MULTIPLE")
                else:
                    emit("1 media attached (source: clipboard, video/mp4)\r\n")
                    event("ATTACHMENT_CONFIRMED")
                continue
            if char == "\x04":
                eof_count += 1
                event(f"EOF_{eof_count}")
                if eof_count >= 2:
                    if scenario == "late-result-change":
                        payload = result_payload()
                        payload["summary"] = "The result changed after validation."
                        (Path.cwd() / "result.json").write_text(
                            json.dumps(payload), encoding="utf-8"
                        )
                        event("LATE_RESULT_CHANGE")
                    return 0
                continue
            if char == "\x03":
                return 130

            prompt += char
            # The controller sends a multiline bracketed paste followed by one
            # carriage return. Embedded newlines are prompt content, not submit
            # actions.
            if char != "\r":
                continue
            if "Write exactly one" not in prompt and "previous `result.json`" not in prompt:
                continue
            prompt_count += 1
            event("PROMPT")
            record_prompt(prompt)
            should_continue = handle_prompt(scenario, prompt_count)
            prompt = ""
            if not should_continue:
                return 0
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_tty)


def main() -> int:
    args = sys.argv[1:]
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "success")
    if args == ["--version"]:
        event("VERSION")
        emit(f"agy {'9.9.9' if scenario == 'wrong-version' else VERSION}\n")
        return 0
    if args == ["models"]:
        event("MODELS")
        if scenario == "model-unavailable":
            emit("Available models:\n- Claude Sonnet\n")
        else:
            emit(f"Available models:\n- {MODEL}\n")
        return 0

    event("EXEC:" + " ".join(args))
    if args != INTERACTIVE_ARGV:
        event("ARGV_INVALID")
        print(f"unexpected argv: {args!r}", file=sys.stderr)
        return 2
    return interactive()


if __name__ == "__main__":
    raise SystemExit(main())
