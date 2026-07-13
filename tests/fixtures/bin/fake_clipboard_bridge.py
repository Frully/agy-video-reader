#!/usr/bin/env python3
"""Filesystem-only test double for clipboard_bridge.swift."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def event(name: str) -> None:
    if path := os.environ.get("FAKE_AGY_EVENT_LOG"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(name + "\n")
    if path := os.environ.get("FAKE_CLIPBOARD_GLOBAL_LOG"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{os.getppid()} {name}\n")


def option(name: str) -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        raise SystemExit(f"missing {name}")


def main() -> int:
    command = sys.argv[1]
    staged_types = json.loads(
        os.environ.get("FAKE_CLIPBOARD_STAGE_TYPES", '["public.file-url"]')
    )
    if command == "status":
        print(json.dumps({"ok": True, "operation": "status", "change_count": 101}))
        return 0
    if command == "stage":
        backup = Path(option("--backup"))
        backup.write_bytes(b"opaque fake clipboard backup")
        backup.chmod(0o600)
        event("CLIPBOARD_STAGE")
        if os.environ.get("FAKE_CLIPBOARD_SCENARIO") == "stage-crash":
            return 70
        if os.environ.get("FAKE_CLIPBOARD_SCENARIO") == "stage-left-original":
            print(json.dumps({
                "ok": False,
                "code": "CLIPBOARD_BACKUP_FAILED",
                "message": "The original clipboard was left unchanged.",
                "backup_preserved": True,
                "clipboard_restored": True,
            }))
            return 3
        if os.environ.get("FAKE_CLIPBOARD_SCENARIO") == "stage-pre-mutation-race":
            backup.unlink()
            print(json.dumps({
                "ok": False,
                "code": "CLIPBOARD_CHANGED_EXTERNALLY",
                "message": "Clipboard changed before attachment staging.",
                "backup_preserved": False,
                "clipboard_restored": True,
            }))
            return 4
        if os.environ.get("FAKE_CLIPBOARD_SCENARIO") == "invalid-stage-success":
            print(json.dumps({
                "ok": True,
                "operation": "stage",
                "staged_change_count": 101,
                "staged_types": ["public.utf8-plain-text"],
            }))
            return 0
        if os.environ.get("FAKE_CLIPBOARD_SCENARIO") == "stage-missing-backup":
            backup.unlink()
            print(json.dumps({
                "ok": True,
                "operation": "stage",
                "staged_change_count": 101,
                "staged_types": staged_types,
            }))
            return 0
        print(
            json.dumps(
                {
                    "ok": True,
                    "operation": "stage",
                    "staged_change_count": 101,
                    "backup_item_count": 1,
                    "backup_type_counts": [1],
                    "staged_types": staged_types,
                }
            )
        )
        return 0
    if command == "recover":
        backup = Path(option("--backup"))
        backup.unlink(missing_ok=True)
        event("CLIPBOARD_RECOVER")
        print(json.dumps({
            "ok": True,
            "operation": "recover",
            "already_restored": False,
            "restored_change_count": 102,
            "backup_deleted": True,
        }))
        return 0
    if command == "restore":
        backup = Path(option("--backup"))
        scenario = os.environ.get("FAKE_CLIPBOARD_SCENARIO", "success")
        if scenario == "invalid-restore-success":
            print(json.dumps({
                "ok": True,
                "operation": "restore",
                "restored_change_count": 102,
                "backup_deleted": False,
            }))
            return 0
        if scenario == "changed-externally":
            print(
                json.dumps(
                    {
                        "ok": False,
                        "code": "CLIPBOARD_CHANGED_EXTERNALLY",
                        "message": "New clipboard content was preserved.",
                        "backup_preserved": True,
                        "clipboard_restored": False,
                    }
                )
            )
            return 4
        backup.unlink(missing_ok=True)
        event("CLIPBOARD_RESTORE")
        print(
            json.dumps(
                {
                    "ok": True,
                    "operation": "restore",
                    "restored_change_count": 102,
                    "restored_item_count": 1,
                    "restored_type_counts": [1],
                    "backup_deleted": True,
                }
            )
        )
        return 0
    print(json.dumps({"ok": False, "code": "INVALID_ARGUMENTS"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
