"""
Drive the Toyota app's UI via adb. UI ONLY — this module never sends an API call to the car
(hard rule). Every action is a simulated long-press on a button in the app's "Advanced Remote" panel.

Coordinates are for a pixel_7 AVD (1080px wide) on software GPU. If the layout/resolution/app build
changes, re-dump with `adb shell uiautomator dump` and update COMMAND_COORDS (see docs/FINDINGS.md).
"""
from __future__ import annotations
import os
import re
import subprocess
import time
from dataclasses import dataclass

ADB = os.environ.get("ADB_PATH", "adb")
SERIAL = os.environ.get("ADB_SERIAL", "emulator-5554")
PKG = "com.toyota.oneapp"
DASHBOARD_ACTIVITY = f"{PKG}/.features.dashboard.dashboard.presentation.OADashboardActivity"
HOLD_MS = int(os.environ.get("HOLD_MS", "3500"))

# Button centers on the Advanced Remote panel (x, y).
COMMAND_COORDS: dict[str, tuple[int, int]] = {
    "start": (208, 650),
    "lock": (540, 650),
    "unlock": (871, 650),
    "lock_trunk": (208, 932),
    "unlock_trunk": (540, 932),
    "lights": (871, 932),
    "horn": (208, 1214),
    "buzzer": (540, 1214),
    "hazards": (871, 1214),
}

# SMS keyword -> command. Keep these obvious and forgiving.
KEYWORD_ALIASES: dict[str, str] = {
    "start": "start", "remote start": "start", "engine": "start",
    "lock": "lock",
    "unlock": "unlock", "open": "unlock",
    "trunk": "unlock_trunk", "unlock trunk": "unlock_trunk", "lock trunk": "lock_trunk",
    "lights": "lights", "flash": "lights",
    "horn": "horn",
    "hazards": "hazards", "hazard": "hazards",
}

# Commands that physically open the car — require a confirmation word to avoid an accidental unlock.
CONFIRM_REQUIRED = {"unlock", "unlock_trunk"}


class ControlError(Exception):
    pass


def _adb(*args: str, timeout: int = 30) -> str:
    cp = subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if cp.returncode != 0:
        raise ControlError(f"adb {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout


def parse_command(body: str) -> tuple[str | None, bool]:
    """Return (command, confirmed). Expects messages like 'TOYOTA START' or 'TOYOTA UNLOCK YES'."""
    text = body.strip().lower()
    text = re.sub(r"^toyota[\s:,-]*", "", text)        # drop a leading 'TOYOTA' prefix
    confirmed = bool(re.search(r"\b(yes|confirm|really)\b", text))
    text = re.sub(r"\b(yes|confirm|really)\b", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return KEYWORD_ALIASES.get(text), confirmed


def device_online() -> bool:
    try:
        out = _adb("get-state", timeout=5)
        return out.strip() == "device"
    except Exception:
        return False


def _foreground_activity() -> str:
    out = _adb("shell", "dumpsys", "activity", "activities", timeout=15)
    m = re.search(r"topResumedActivity=.*?(\S+/\S+)\s", out)
    return m.group(1) if m else ""


def ensure_on_advanced_remote() -> None:
    """Make sure the app is foreground and the Advanced Remote panel (with the buttons) is showing."""
    if PKG not in _foreground_activity():
        _adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        time.sleep(6)
    # Confirm a known control is present; if not, try to open the Advanced Remote entry from the dashboard.
    if not _ui_has_text("Hazards"):
        coord = _ui_find_text("Advanced Remote")
        if coord:
            _adb("shell", "input", "tap", str(coord[0]), str(coord[1]))
            time.sleep(3)
    if not _ui_has_text("Hazards"):
        raise ControlError("Advanced Remote panel not visible; may need (re)login or re-navigation")


def _ui_dump() -> str:
    _adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=20)
    return _adb("shell", "cat", "/sdcard/ui.xml", timeout=15)


def _ui_has_text(needle: str) -> bool:
    try:
        return needle in _ui_dump()
    except Exception:
        return False


def _ui_find_text(needle: str) -> tuple[int, int] | None:
    try:
        xml = _ui_dump()
    except Exception:
        return None
    for m in re.finditer(r'(?:text|content-desc)="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        if m.group(1).strip() == needle:
            x1, y1, x2, y2 = map(int, m.groups()[1:])
            return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def execute(command: str) -> None:
    """Long-press the button for `command`. Raises ControlError on failure."""
    if command not in COMMAND_COORDS:
        raise ControlError(f"unknown command: {command}")
    if not device_online():
        raise ControlError("emulator offline")
    ensure_on_advanced_remote()
    x, y = COMMAND_COORDS[command]
    # tap-and-hold to activate
    _adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(HOLD_MS), timeout=max(20, HOLD_MS // 1000 + 15))
    # NOTE: the UI can sit on "Sending..." even after the command fires; the car is the source of truth.
    # We do not block on the UI flipping to success.
