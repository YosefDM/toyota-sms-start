"""
Drive the Toyota app's UI via adb. UI ONLY — this module never sends an API call to the car
(hard rule). Every action is a simulated long-press on a button in the app's Advanced Remote panel.

Controls are located by **resource-id** (stable across cosmetic updates and resolution-independent),
never by blind coordinates — see the guard model in docs/UI-MAP.md. Before any tap we assert the
Advanced Remote anchor set is present; if it isn't (login expired, a dialog, downtime) we refuse and
raise instead of tapping in the dark.

The authoritative result of a command is Toyota's **push notification** ("The vehicle is now
unlocked" etc.), which the app posts via FCM after the command completes. `await_result()` reads it
from the notification shade (`dumpsys notification --noredact`). The on-screen "Sending…" spinner is
an unreliable success signal and is NOT used to decide success.
"""
from __future__ import annotations
import os
import re
import subprocess
import time

ADB = os.environ.get("ADB_PATH", "adb")
SERIAL = os.environ.get("ADB_SERIAL", "emulator-5554")
PKG = "com.toyota.oneapp"
HOLD_MS = int(os.environ.get("HOLD_MS", "3500"))

# SMS keyword -> canonical command.
KEYWORD_ALIASES: dict[str, str] = {
    "start": "start", "remote start": "start", "engine": "start",
    "lock": "lock",
    "unlock": "unlock", "open": "unlock",
    "trunk": "unlock_trunk", "unlock trunk": "unlock_trunk", "lock trunk": "lock_trunk",
    "lights": "lights", "flash": "lights",
    "horn": "horn",
    "buzzer": "buzzer",
    "hazards": "hazards", "hazard": "hazards",
}

# Canonical command -> (resource-id, disambiguating label | None).
# The two trunk buttons SHARE one resource-id, so they must be told apart by their label.
COMMAND_IDS: dict[str, tuple[str, str | None]] = {
    "start": ("remote_engine_start_button", None),
    "lock": ("remote_door_lock_button", None),
    "unlock": ("remote_door_unlock_button", None),
    "lights": ("remote_lights_button", None),
    "horn": ("remote_horn_button", None),
    "buzzer": ("remote_buzzer_button", None),
    "hazards": ("remote_hazard_button", None),
    "lock_trunk": ("remote_trunk_lock_button", "Lock Trunk"),
    "unlock_trunk": ("remote_trunk_lock_button", "Unlock Trunk"),
}

# All of these ids must be present for us to believe we're on the Advanced Remote panel.
ANCHOR_IDS = {
    "remote_engine_start_button", "remote_door_lock_button", "remote_door_unlock_button",
    "remote_trunk_lock_button", "remote_lights_button", "remote_horn_button",
    "remote_buzzer_button", "remote_hazard_button", "remote_progress_text",
}

# Commands that physically open the car — require a confirmation word to avoid an accidental unlock.
CONFIRM_REQUIRED = {"unlock", "unlock_trunk"}

RESULT_TIMEOUT = int(os.environ.get("RESULT_TIMEOUT", "25"))


class ControlError(Exception):
    """A command could not be carried out."""


class NotLoggedIn(ControlError):
    """The app is on a login / signed-out screen; re-auth (possibly OTP) is needed before any tap."""


class UINavigationError(ControlError):
    """Couldn't reach / confirm the Advanced Remote panel — refuse to tap rather than guess."""


# --------------------------------------------------------------------------- adb plumbing

def _adb(*args: str, timeout: int = 30) -> str:
    cp = subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if cp.returncode != 0:
        raise ControlError(f"adb {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout


def device_online() -> bool:
    try:
        return _adb("get-state", timeout=5).strip() == "device"
    except Exception:
        return False


def _device_now_ms() -> int:
    """Device wall-clock in epoch ms, for matching against a notification's post time."""
    try:
        out = _adb("shell", "date", "+%s%3N", timeout=8).strip()
    except Exception:
        return int(time.time() * 1000)
    m = re.search(r"(\d{10})(\d{3})?", out)
    if not m:
        return int(time.time() * 1000)
    return int(m.group(1)) * 1000 + int(m.group(2) or 0)


# --------------------------------------------------------------------------- UI inspection

def _foreground_activity() -> str:
    out = _adb("shell", "dumpsys", "activity", "activities", timeout=15)
    m = re.search(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)=\S+\s+(\S+/\S+)", out)
    return m.group(1) if m else ""


_NODE_RE = re.compile(r"<node\b([^>]*?)/?>")
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _ui_dump() -> str:
    _adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=20)
    return _adb("shell", "cat", "/sdcard/ui.xml", timeout=15)


def _nodes(xml: str) -> list[dict]:
    out = []
    for m in _NODE_RE.finditer(xml):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        rid = attrs.get("resource-id", "")
        node = {
            "id": rid.split(":id/", 1)[1] if ":id/" in rid else rid,
            "text": attrs.get("text", ""),
            "desc": attrs.get("content-desc", ""),
            "center": None,
        }
        b = _BOUNDS_RE.search(attrs.get("bounds", ""))
        if b:
            x1, y1, x2, y2 = map(int, b.groups())
            node["center"] = ((x1 + x2) // 2, (y1 + y2) // 2)
        out.append(node)
    return out


def _present_ids(nodes: list[dict]) -> set[str]:
    return {n["id"] for n in nodes if n["id"]}


def _find(nodes: list[dict], rid: str, label: str | None = None) -> tuple[int, int] | None:
    """Center of the node with this resource-id (and matching text/desc when `label` disambiguates)."""
    for n in nodes:
        if n["id"] != rid or n["center"] is None:
            continue
        if label is not None and label not in (n["text"], n["desc"]):
            continue
        return n["center"]
    return None


def _find_desc(nodes: list[dict], desc: str) -> tuple[int, int] | None:
    for n in nodes:
        if n["desc"] == desc and n["center"] is not None:
            return n["center"]
    return None


# --------------------------------------------------------------------------- navigation guard

def _assert_logged_in(activity: str) -> None:
    if "LoginActivity" in activity or "FRMainActivity" in activity:
        raise NotLoggedIn("app is signed out (login/OTP screen) — re-auth required before any command")


def ensure_on_advanced_remote() -> list[dict]:
    """Bring up and verify the Advanced Remote panel. Returns the live node list on success.

    Steps: confirm we're in the app and not signed out → if the full 9-button panel is already up,
    done → else select the Remote tab, then tap the ⋯ "More Horizontal" entry → re-verify the anchor
    set. Refuses (raises) rather than tapping if the panel can't be confirmed.
    """
    activity = _foreground_activity()
    _assert_logged_in(activity)
    if PKG not in activity:
        _adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        time.sleep(6)
        _assert_logged_in(_foreground_activity())

    nodes = _nodes(_ui_dump())
    if ANCHOR_IDS <= _present_ids(nodes):
        return nodes

    # Not on the full panel yet. Make sure the Remote tab is selected.
    tab = _find(nodes, "ID_TAB_REMOTE")
    if tab:
        _adb("shell", "input", "tap", str(tab[0]), str(tab[1]))
        time.sleep(2)
        nodes = _nodes(_ui_dump())
        if ANCHOR_IDS <= _present_ids(nodes):
            return nodes

    # Open the Advanced Remote modal via the ⋯ "More Horizontal" control.
    more = _find_desc(nodes, "More Horizontal")
    if more:
        _adb("shell", "input", "tap", str(more[0]), str(more[1]))
        time.sleep(3)
        nodes = _nodes(_ui_dump())
        if ANCHOR_IDS <= _present_ids(nodes):
            return nodes

    missing = ANCHOR_IDS - _present_ids(nodes)
    raise UINavigationError(
        "Advanced Remote panel not confirmed (missing: " + ", ".join(sorted(missing)) +
        "). Possible login expiry, a dialog, or a UI change — refusing to tap."
    )


# --------------------------------------------------------------------------- actions

def parse_command(body: str) -> tuple[str | None, bool]:
    """Return (command, confirmed). Expects messages like 'TOYOTA START' or 'TOYOTA UNLOCK YES'."""
    text = body.strip().lower()
    text = re.sub(r"^toyota[\s:,-]*", "", text)        # drop a leading 'TOYOTA' prefix
    confirmed = bool(re.search(r"\b(yes|confirm|really)\b", text))
    text = re.sub(r"\b(yes|confirm|really)\b", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return KEYWORD_ALIASES.get(text), confirmed


def execute(command: str) -> int:
    """Long-press the button for `command` (UI only). Returns the pre-command device timestamp (ms)
    so the caller can match the result notification. Raises ControlError on any failure."""
    if command not in COMMAND_IDS:
        raise ControlError(f"unknown command: {command}")
    if not device_online():
        raise ControlError("emulator offline")

    rid, label = COMMAND_IDS[command]
    since_ms = _device_now_ms()
    nodes = ensure_on_advanced_remote()

    target = _find(nodes, rid, label)
    if target is None:
        raise UINavigationError(
            f"control for '{command}' (id={rid}" + (f", label={label!r}" if label else "") +
            ") not found on the panel — refusing to tap."
        )

    x, y = target
    # tap-and-hold to activate
    _adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(HOLD_MS),
         timeout=max(20, HOLD_MS // 1000 + 15))
    # The UI may sit on "Sending…" even after the command fires; the car (and its push notification)
    # is the source of truth. We do NOT block on the spinner.
    return since_ms


# --------------------------------------------------------------------------- result feedback (FCM)

_EXTRA_PREFIX_RE = re.compile(r"^String \(\d+\)\s*")


def _extra(block: str, key: str) -> str:
    m = re.search(re.escape(key) + r"=(.*)", block)
    if not m:
        return ""
    val = _EXTRA_PREFIX_RE.sub("", m.group(1).strip()).strip()
    return val.strip('"')


def _toyota_notifications() -> list[dict]:
    """Parse Toyota notifications from the shade: [{when, title, text}], newest info included."""
    try:
        out = _adb("shell", "dumpsys", "notification", "--noredact", timeout=20)
    except Exception:
        return []
    records = []
    for block in re.split(r"NotificationRecord\(", out)[1:]:
        if "pkg=com.toyota.oneapp" not in block[:200]:
            continue
        whens = [int(x) for x in re.findall(r"(?:mWhen|when|creationTime)=(\d{12,13})", block)]
        title = _extra(block, "android.title")
        text = _extra(block, "android.bigText") or _extra(block, "android.text")
        if title or text:
            records.append({"when": max(whens) if whens else 0, "title": title, "text": text})
    return records


def await_result(since_ms: int, timeout: int | None = None, poll: float = 2.0) -> dict | None:
    """Poll the notification shade for a Toyota command-result push posted at/after `since_ms`.

    Returns the newest matching {title, text, when}, or None if none arrives within `timeout` seconds.
    A small skew tolerance absorbs device clock granularity; the per-sender rate limit keeps an older
    command's notification from being mistaken for this one.
    """
    timeout = RESULT_TIMEOUT if timeout is None else timeout
    skew = 2000
    deadline = time.time() + timeout
    best: dict | None = None
    while True:
        for r in _toyota_notifications():
            if r["when"] >= since_ms - skew and (best is None or r["when"] > best["when"]):
                best = r
        if best is not None or time.time() >= deadline:
            return best
        time.sleep(poll)
