"""Read recently available email, Telegram and Android phone notifications."""
from __future__ import annotations

import shutil
import subprocess
import platform
from pathlib import Path

from core import outcome
from actions.open_app import open_app as _open_app


def _adb_notifications() -> tuple[list[str], str]:
    adb = shutil.which("adb")
    if not adb:
        common = Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        if common.exists():
            adb = str(common)
    if not adb:
        return [], "ADB not installed or phone bridge not configured"

    try:
        devices = subprocess.run(
            [adb, "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        )
        if "device" not in devices.stdout:
            return [], "no Android phone is connected over USB"

        result = subprocess.run(
            [adb, "shell", "dumpsys", "notification", "--noredact"],
            capture_output=True,
            text=True,
            timeout=12,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return [], "Android notification service could not be read"

        lines = []
        for line in result.stdout.splitlines():
            low = line.lower()
            if "android.app.notification" in low or "package=" in low:
                clean = line.strip()
                if clean and clean not in lines:
                    lines.append(clean)
        return lines[:20], ""
    except subprocess.TimeoutExpired:
        return [], "the Android phone did not answer in time"
    except Exception as exc:
        return [], f"phone notification read failed: {exc}"


def inbox_report(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    source = str(params.get("source", "all")).lower().strip()
    opened = []
    unavailable = []

    if source in ("all", "telegram"):
        try:
            telegram = _open_app({"app_name": "Telegram"}, player=player)
            opened.append("Telegram")
        except Exception as exc:
            unavailable.append(f"Telegram: {exc}")

    if source in ("all", "email", "gmail"):
        try:
            import webbrowser
            webbrowser.open("https://mail.google.com/mail/u/0/#inbox")
            opened.append("Gmail")
        except Exception as exc:
            unavailable.append(f"Gmail: {exc}")

    notifications, phone_error = _adb_notifications() if source in ("all", "phone", "notifications") else ([], "")
    if notifications:
        opened.append(f"phone notifications ({len(notifications)} entries)")
    elif phone_error:
        unavailable.append(phone_error)

    details = {}
    if opened:
        details["opened"] = ", ".join(opened)
    if unavailable:
        details["unavailable"] = "; ".join(unavailable)
    if notifications:
        details["phone_notifications"] = "\n".join(notifications)

    if unavailable and not opened:
        return outcome.failed("inbox_report", "; ".join(unavailable), **details)
    if unavailable:
        return outcome.unverified(
            "inbox_report",
            "some inbox sources were opened, but other sources could not be read",
            **details,
        )
    return outcome.done(
        "inbox_report",
        opened=", ".join(opened),
        notification_count=len(notifications),
        verified=True,
        **({"phone_notifications": "\n".join(notifications)} if notifications else {}),
    )


TOOL = {
    "name": "inbox_report",
    "description": (
        "Opens Gmail and Telegram and, when an Android phone is connected over "
        "USB with ADB, reads current phone notification entries. Use for "
        "requests about unread email, Telegram messages or phone notifications."
    ),
    "brief": "Reports available Gmail, Telegram and Android phone inboxes.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {
                "type": "STRING",
                "description": "all | email | gmail | telegram | phone | notifications",
            }
        },
    },
    "handler": inbox_report,
}
