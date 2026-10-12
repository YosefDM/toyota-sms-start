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
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger("toyota_control")

ADB = os.environ.get("ADB_PATH", "adb")
SERIAL = os.environ.get("ADB_SERIAL", "emulator-5554")
PKG = "com.toyota.oneapp"
HOLD_MS = int(os.environ.get("HOLD_MS", "3500"))

# SMS keyword -> canonical command. (help/status are info-only; app.py handles them separately.)
KEYWORD_ALIASES: dict[str, str] = {
    "help": "help", "?": "help", "commands": "help", "menu": "help", "list": "help",
    "status": "status", "stat": "status", "info": "status",
    "start": "start", "remote start": "start", "engine": "start",
    # NB: deliberately NO "stop" alias — the carrier/TextGrid intercepts STOP as the opt-out
    # keyword, so it never reaches us, and treating an opt-out as an engine command would be wrong.
    "shutoff": "shutoff", "shut off": "shutoff", "engine off": "shutoff",
    "stop engine": "shutoff", "kill engine": "shutoff",
    "lock": "lock",
    "unlock": "unlock", "open": "unlock",
    "trunk": "unlock_trunk", "unlock trunk": "unlock_trunk", "lock trunk": "lock_trunk",
    "lights": "lights", "flash": "lights",
    "horn": "horn",
    "buzzer": "buzzer",
    "hazards": "hazards", "hazard": "hazards",
}

HELP_TEXT = (
    "Toyota SMS commands (a leading TOYOTA is optional):\n"
    "STATUS - range, tires, doors, windows, trunk\n"
    "START - remote start\n"
    "LOCK\n"
    "UNLOCK YES - (needs YES to confirm)\n"
    "LIGHTS, HAZARDS, HORN, BUZZER\n"
    "TRUNK YES - unlock trunk (needs YES)\n"
    "LOCK TRUNK - lock trunk\n"
    "SHUTOFF - stop a running remote start\n"
    "? - this list"
)

# Canonical command -> (resource-id, disambiguating label | None).
# The two trunk buttons SHARE one resource-id, so they must be told apart by their label.
# The engine tile is EITHER Start or Stop, depending on whether a remote start is running.
ENGINE_START_ID = "remote_engine_start_button"
ENGINE_STOP_ID = "remote_engine_stop_button"

COMMAND_IDS: dict[str, tuple[str, str | None]] = {
    "start": (ENGINE_START_ID, None),
    "shutoff": (ENGINE_STOP_ID, None),
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
    "remote_door_lock_button", "remote_door_unlock_button",
    "remote_trunk_lock_button", "remote_lights_button", "remote_horn_button",
    "remote_buzzer_button", "remote_hazard_button", "remote_progress_text",
}

# ...plus the engine tile, which is NOT a fixed id: while a remote start is running the app swaps
# the Start button for a Stop button, so exactly ONE of these is on the panel at any time.
# Requiring `remote_engine_start_button` outright is what made EVERY command fail for the ~10
# minutes after a successful START (five such SMS failures, 2026-10-07..11): the panel was fine,
# the guard was just asking for a button the app had legitimately replaced.
ENGINE_ANCHOR_IDS = {ENGINE_START_ID, ENGINE_STOP_ID}


def _panel_confirmed(ids: set[str]) -> bool:
    """True when `ids` (the resource-ids in a dump) prove we're on the Advanced Remote panel:
    every fixed anchor, plus either engine button."""
    return ANCHOR_IDS <= ids and bool(ENGINE_ANCHOR_IDS & ids)


# Commands that physically open the car — require a confirmation word to avoid an accidental unlock.
CONFIRM_REQUIRED = {"unlock", "unlock_trunk"}

RESULT_TIMEOUT = int(os.environ.get("RESULT_TIMEOUT", "25"))


class ControlError(Exception):
    """A command could not be carried out."""


class NotLoggedIn(ControlError):
    """The app is on a login / signed-out screen; re-auth (possibly OTP) is needed before any tap."""


class UINavigationError(ControlError):
    """Couldn't reach / confirm the Advanced Remote panel — refuse to tap rather than guess."""


class CommandNotApplicable(ControlError):
    """The panel is fine, but this command doesn't apply right now (e.g. START while the engine is
    already remote-running). Carries a complete, user-facing sentence — not an error to dress up."""


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


_DUMP_ATTEMPTS = int(os.environ.get("DUMP_ATTEMPTS", "3"))


def _ui_dump() -> str:
    """Dump the current view hierarchy, retrying a transient failure.

    `uiautomator dump` intermittently fails with an EMPTY error message while the app is mid-
    animation or the device is busy — that produced the 2026-10-09 SMS "Couldn't send lights: adb
    shell uiautomator dump /sdcard/ui.xml failed:". It succeeds on a retry a moment later, so a
    bare first failure must never become the user's answer.
    """
    last: Exception | None = None
    for attempt in range(_DUMP_ATTEMPTS):
        try:
            _adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=20)
            xml = _adb("shell", "cat", "/sdcard/ui.xml", timeout=15)
            if "<node" in xml:
                return xml
            last = ControlError("uiautomator produced no node tree")
        except ControlError as e:
            last = e
        if attempt < _DUMP_ATTEMPTS - 1:
            time.sleep(1.5)
    raise ControlError(f"couldn't read the screen after {_DUMP_ATTEMPTS} attempts ({last})")


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


_DASHBOARD_TABS = {"ID_TAB_STATUS", "ID_TAB_REMOTE", "ID_TAB_HEALTH"}


def _on_dashboard(nodes: list[dict]) -> bool:
    """True when the dashboard shell (the Status/Remote/Health tab bar) is visible — i.e. no bottom
    sheet or modal is covering it."""
    return bool(_DASHBOARD_TABS & _present_ids(nodes))


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


def _find_text(nodes: list[dict], text: str, contains: bool = False) -> tuple[int, int] | None:
    for n in nodes:
        if n["center"] is None:
            continue
        if (text in n["text"]) if contains else (n["text"] == text):
            return n["center"]
    return None


def _screen_size() -> tuple[int, int]:
    try:
        m = re.search(r"(\d+)x(\d+)", _adb("shell", "wm", "size", timeout=8))
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return (1080, 1920)


def _scroll_up() -> None:
    """Scroll the panel content up to reveal controls below the fold (the Remote quick-buttons and
    the ⋯ entry sit under the subscription banner / bottom nav on the dashboard)."""
    w, h = _screen_size()
    x = w // 2
    _adb("shell", "input", "swipe", str(x), str(int(h * 0.72)), str(x), str(int(h * 0.33)), "400")
    time.sleep(1.5)


# --------------------------------------------------------------------------- caution dialog

# Remote start (and remote climate) raises a one-time "Caution" safety bottom-sheet that must be
# CONFIRMED before the command is actually sent to the car — until confirmed, the long-press opens
# this modal and sends nothing, which silently swallowed START (the modal then blocked every later
# command too). Unlike the rest of the UI this sheet carries NO resource-ids, so it's matched by its
# visible text. We tick "Do not show this message again" so it stops reappearing, then tap Continue.
_CAUTION_CHECKBOX_DESC = "Do not show this message again"
_CAUTION_CHECKBOX_LABEL = "Don't tell me again."


def _caution_present(nodes: list[dict]) -> bool:
    """The remote-start Caution sheet is up: its title/warning text AND a Continue button are shown."""
    has_continue = _find_text(nodes, "Continue") is not None
    has_caution = any(
        ("Caution" in n["text"]) or ("safe, ventilated" in n["text"]) for n in nodes
    )
    return has_continue and has_caution


def _dismiss_caution_dialog(max_wait: float = 6.0) -> bool:
    """If the remote-start Caution sheet appears, tick 'do not show again' and tap Continue to proceed.

    Returns True if a dialog was handled. `max_wait` lets START wait for the sheet to animate in; other
    commands pass 0 for a single cheap check (they never raise it, but we stay defensive). Matched by
    text because the sheet has no resource-ids.
    """
    deadline = time.time() + max_wait
    nodes = _nodes(_ui_dump())
    while not _caution_present(nodes) and time.time() < deadline:
        time.sleep(1.0)
        nodes = _nodes(_ui_dump())
    if not _caution_present(nodes):
        return False

    # Tick "Do not show this message again" so the sheet stops appearing (best-effort — the command
    # still goes through via Continue even if this specific tap misses; the code re-handles it anyway).
    box = _find_desc(nodes, _CAUTION_CHECKBOX_DESC) or _find_text(nodes, _CAUTION_CHECKBOX_LABEL)
    if box:
        _adb("shell", "input", "tap", str(box[0]), str(box[1]))
        time.sleep(1.0)

    # Tap Continue to actually send the remote-start command.
    nodes = _nodes(_ui_dump())
    cont = _find_text(nodes, "Continue")
    if cont:
        _adb("shell", "input", "tap", str(cont[0]), str(cont[1]))
        time.sleep(2.0)
    return True


# --------------------------------------------------------------------------- navigation guard

def _assert_logged_in(activity: str) -> None:
    if "LoginActivity" in activity or "FRMainActivity" in activity:
        raise NotLoggedIn("app is signed out (login/OTP screen) — re-auth required before any command")


def _ensure_dashboard() -> list[dict]:
    """Return to the dashboard shell (Status/Remote/Health tabs), closing any bottom-sheet/modal that
    covers the tabs — e.g. the Advanced Remote sheet is left open after a command, and it hides the tab
    bar. Without this, read_status() can't find the Status tab and silently returns nothing. Presses
    BACK to dismiss the overlay (only while the tabs are NOT visible, so we never background the app from
    the dashboard root); relaunches the app as a last resort. Returns the live node list."""
    nodes = _nodes(_ui_dump())
    for _ in range(3):
        if _on_dashboard(nodes):
            return nodes
        _adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1.5)
        nodes = _nodes(_ui_dump())
    if not _on_dashboard(nodes):
        _adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        time.sleep(6)
        nodes = _nodes(_ui_dump())
    return nodes


def _try_reach_advanced_remote() -> list[dict] | None:
    """One full pass to bring up + confirm the Advanced Remote panel. Returns the node list on success,
    or None if the anchor set couldn't be confirmed this pass (the caller may wait and retry). Raises
    NotLoggedIn immediately if the app is on a sign-in screen.

    Steps: confirm we're in the app and not signed out → if the full 9-button panel is already up, done
    → else select the Remote tab, then tap the ⋯ "More Horizontal" entry → re-verify the anchor set.
    """
    activity = _foreground_activity()
    _assert_logged_in(activity)
    if PKG not in activity:
        _adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        time.sleep(6)
        _assert_logged_in(_foreground_activity())

    nodes = _nodes(_ui_dump())
    if _panel_confirmed(_present_ids(nodes)):
        return nodes

    # Not on the full panel yet. Make sure the Remote tab is selected.
    tab = _find(nodes, "ID_TAB_REMOTE")
    if tab:
        _adb("shell", "input", "tap", str(tab[0]), str(tab[1]))
        time.sleep(2)
        nodes = _nodes(_ui_dump())
        if _panel_confirmed(_present_ids(nodes)):
            return nodes

    # The quick buttons and the ⋯ "More Horizontal" entry sit below the fold on the Remote tab, so
    # scroll the panel up (a few times) to reveal them, then open the Advanced Remote modal — it
    # carries the full resource-id set for every command. Re-dump after each step.
    for _ in range(4):
        more = _find_desc(nodes, "More Horizontal")
        if more:
            _adb("shell", "input", "tap", str(more[0]), str(more[1]))
            time.sleep(3)
            nodes = _nodes(_ui_dump())
            if _panel_confirmed(_present_ids(nodes)):
                return nodes
        _scroll_up()
        nodes = _nodes(_ui_dump())
        if _panel_confirmed(_present_ids(nodes)):
            return nodes
    return None


# A command issued right after START lands while the app is still animating through the remote-start
# flow (Caution sheet → "Sending…" → climate), so the first navigation pass can miss the panel. We
# retry the WHOLE navigation a few times with a short settle-wait instead of erroring — the panel
# reliably reappears within a few seconds. (A single pass is what produced the 2026-10-07 "Couldn't
# send unlock: Advanced Remote panel not confirmed" SMS even though the panel was fine moments later.)
_PANEL_ATTEMPTS = int(os.environ.get("PANEL_ATTEMPTS", "3"))
_PANEL_RETRY_WAIT = float(os.environ.get("PANEL_RETRY_WAIT", "4"))


def ensure_on_advanced_remote() -> list[dict]:
    """Bring up and verify the Advanced Remote panel, retrying the whole navigation with a settle-wait
    so a command landing mid-transition (e.g. right after START) recovers instead of failing. Returns
    the live node list on success; raises UINavigationError only after every attempt fails, or
    NotLoggedIn immediately if the app is signed out.
    """
    for attempt in range(_PANEL_ATTEMPTS):
        try:
            reached = _try_reach_advanced_remote()
        except NotLoggedIn:
            raise  # a sign-out is not transient — surface it immediately
        except ControlError:
            # A dump fails for the SAME reason this retry loop exists (mid-animation / busy device),
            # so let it fall through to the settle-wait instead of escaping as a hard error.
            reached = None
        if reached is not None:
            return reached
        if attempt < _PANEL_ATTEMPTS - 1:
            time.sleep(_PANEL_RETRY_WAIT)  # let a transient post-command transition settle, then retry

    try:
        present = _present_ids(_nodes(_ui_dump()))
    except ControlError as e:
        raise UINavigationError(
            f"Couldn't read the screen after {_PANEL_ATTEMPTS} attempts ({e}) — refusing to tap."
        ) from e
    missing = sorted(ANCHOR_IDS - present)
    if not (ENGINE_ANCHOR_IDS & present):
        missing.append("an engine button (start or stop)")
    raise UINavigationError(
        f"Advanced Remote panel not confirmed after {_PANEL_ATTEMPTS} attempts (missing: " +
        ", ".join(missing) + "). Possible login expiry, a dialog, or a UI change — refusing to tap."
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
        # The engine tile is a toggle, so a missing Start/Stop button is the car's STATE, not a
        # broken panel — say which it is instead of emitting a resource-id at the owner.
        present = _present_ids(nodes)
        if command == "start" and ENGINE_STOP_ID in present:
            raise CommandNotApplicable(
                "The engine is already running from a remote start — it shuts off automatically "
                "about 10 minutes after it started. Reply SHUTOFF to stop it now."
            )
        if command == "shutoff" and ENGINE_START_ID in present:
            # The tile only flips once the app refreshes remote status, so a Start tile is NOT proof
            # the engine is off — right after a START it still reads Start for a while. Re-check
            # after a settle, and if it still shows Start, report what we can SEE rather than
            # asserting the car's state and leaving a running engine with no way to stop it.
            time.sleep(5)
            nodes = ensure_on_advanced_remote()
            target = _find(nodes, rid, label)
            if target is None:
                raise CommandNotApplicable(
                    "I can't see a Stop button on the Remote panel, so I haven't shut anything off. "
                    "If you just started the engine, give it a few seconds and send SHUTOFF again; "
                    "a remote start also stops on its own about 10 minutes in."
                )
        raise UINavigationError(
            f"control for '{command}' (id={rid}" + (f", label={label!r}" if label else "") +
            ") not found on the panel — refusing to tap."
        )

    x, y = target
    # tap-and-hold to activate
    _adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(HOLD_MS),
         timeout=max(20, HOLD_MS // 1000 + 15))

    # Remote start (and climate) interposes a "Caution" safety sheet that must be confirmed before the
    # request is sent — handle it (tick "don't show again" + Continue). START waits for it to appear;
    # other commands never raise it, so they just do one cheap check as a safety net.
    # The command has ALREADY been sent at this point, so a failure to read the screen must not
    # become the owner's answer: `_ui_dump` now raises when it can't get a node tree, and before
    # this guard that turned a delivered command into "Couldn't send lock: couldn't read the
    # screen". Non-start commands never raise the Caution sheet anyway.
    try:
        _dismiss_caution_dialog(max_wait=6.0 if command == "start" else 0.0)
    except ControlError as e:
        logger.warning("couldn't re-read the screen after the tap (the command was already sent): %s", e)

    # The UI may sit on "Sending…" even after the command fires; the car (and its push notification)
    # is the source of truth. We do NOT block on the spinner.
    return since_ms


# --------------------------------------------------------------------------- result feedback (FCM)

_EXTRA_PREFIX_RE = re.compile(r"^String \(\d+\)\s*")


def _extra(block: str, key: str) -> str:
    m = re.search(re.escape(key) + r"=(.*)", block)
    if not m:
        return ""
    val = m.group(1).strip()
    # dumpsys renders CharSequence extras in a few shapes; unwrap them:
    #   String (12) "the text"   |   String (the text)   |   the text
    mm = re.match(r'^String \(\d+\)\s*"?(.*?)"?$', val) or re.match(r"^String \((.*)\)$", val)
    if mm:
        val = mm.group(1)
    return _EXTRA_PREFIX_RE.sub("", val).strip().strip('"')


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


# Result codes Toyota puts in its own push text, as observed live. Used ONLY to reject a push that
# demonstrably belongs to a DIFFERENT command — anything unrecognised is still accepted, so a command
# whose code we haven't catalogued yet keeps working, and so do the shared failure pushes ([D1],
# [70], ...), which are the legitimate answer to whatever we just sent.
_RESULT_CODES: dict[str, re.Pattern[str]] = {
    "lock": re.compile(r"\[DL1\]"),
    "unlock": re.compile(r"\[DL0\]"),
    "start": re.compile(r"\[RES1\]"),
    # [RES0] observed live 2026-10-11: "Remote Engine Stop Request was successful. [RES0]" — it is
    # in the same RES family as start, so cataloguing it keeps the two from answering each other.
    "shutoff": re.compile(r"\[RES0\]"),
    "hazards": re.compile(r"\[HZ1\]"),
}


def result_is_success(command: str | None, text: str) -> bool | None:
    """Did this push say the command SUCCEEDED? True / False / None when we can't tell.

    Toyota pushes failures down the same channel as successes ("...keyfob was detected [70]"), so
    the mere arrival of a push is not an outcome — only its result code is. None means this command
    has no catalogued success code (horn, lights, trunk, ...), i.e. the text is the only answer we
    have; callers that need a verdict must not treat that as success.

    Added for the voice path, which answers in TONES rather than words and so has to *decide*
    rather than hand the sentence to the owner to read.
    """
    mine = _RESULT_CODES.get(command or "")
    if mine is None:
        return None
    return bool(mine.search(text or ""))


def _is_other_commands_result(text: str, command: str | None) -> bool:
    """True when this push carries another command's success code and not our own.

    Without this check a result is matched on timestamp alone, so a push still landing from the
    PREVIOUS command gets delivered as this one's answer — on 2026-10-08 a START was answered
    "The vehicle is now locked. [DL1]", i.e. the owner was told the wrong thing about the car.
    """
    if not text or command is None:
        return False  # no command given means no expectation to check against
    mine = _RESULT_CODES.get(command)
    if mine is not None and mine.search(text):
        return False
    return any(cmd != command and pat.search(text) for cmd, pat in _RESULT_CODES.items())


# The app logs its OWN outcome for a command the moment Toyota accepts it, as an analytics event
# ("local#app#remote_lock_success"). That is a LOCAL signal — reading the log is observation, the
# same kind the screen is — and it does not depend on the FCM push coming back. Only these three
# commands emit one; the full `remote_*` event vocabulary was read out of the shipped APK's DEX.
_APP_SUCCESS_EVENTS = {
    "lock": "remote_lock_success",
    "unlock": "remote_unlock_success",
    "start": "remote_start_success",
}


def _logcat_since(since_ms: int) -> str:
    """Log lines posted at/after `since_ms` (device epoch ms), or "" if they can't be read.

    The timestamp is formatted ON THE DEVICE so it matches logcat's own clock and timezone.
    EVERY argument containing a space must be quoted: `adb shell` joins its arguments into one
    command string and the device's shell re-splits it, so an unquoted "+%m-%d %H:%M:%S.000"
    arrives as two arguments and toybox answers `date: Max 1 argument`.
    """
    try:
        stamp = _adb("shell", "date", "-d", f"@{since_ms // 1000}",
                     "'+%m-%d %H:%M:%S.000'", timeout=10).strip()
        if not stamp:
            return ""
        return _adb("shell", "logcat", "-d", "-t", f"'{stamp}'", timeout=30)
    except Exception:
        return ""


def app_reported_success(command: str, since_ms: int) -> bool:
    """True when the app itself logged this command as successful since `since_ms`.

    The fallback for when Toyota's push never arrives. On 2026-10-11 the rig's FCM connection went
    down (GMS stuck retrying) while commands kept working, so every reply became the useless "No
    confirmation came back within 25s" even though the car had done what it was told.
    """
    event = _APP_SUCCESS_EVENTS.get(command)
    return bool(event) and event in _logcat_since(since_ms)


def await_result(since_ms: int, timeout: int | None = None, poll: float = 2.0,
                 command: str | None = None) -> dict | None:
    """Poll the notification shade for a Toyota command-result push posted at/after `since_ms`.

    Returns the newest matching {title, text, when}, or None if none arrives within `timeout`
    seconds. A small skew tolerance absorbs device clock granularity. Pushes carrying another
    command's result code are skipped, so a straggler from the previous command can't be reported
    as this one's outcome.
    """
    timeout = RESULT_TIMEOUT if timeout is None else timeout
    skew = 2000
    deadline = time.time() + timeout
    best: dict | None = None
    while True:
        for r in _toyota_notifications():
            if r["when"] < since_ms - skew:
                continue
            if _is_other_commands_result(r.get("text", ""), command):
                # Log it: if a command whose code we haven't catalogued (shutoff, the trunk pair)
                # ever reuses a known family, this is the only way the mismatch becomes visible
                # instead of silently becoming "no confirmation came back".
                logger.info("ignoring a push carrying another command's code while waiting "
                            "for %r: %s", command, r.get("text", "")[:90])
                continue
            if best is None or r["when"] > best["when"]:
                best = r
        if best is not None or time.time() >= deadline:
            return best
        time.sleep(poll)


# --------------------------------------------------------------------------- status (read-only)

def _text_by_id(nodes: list[dict], rid: str) -> str | None:
    for n in nodes:
        if n["id"] == rid and n["text"]:
            return n["text"]
    return None


def _pair_value(nodes: list[dict], label: str, values: tuple[str, ...]) -> str | None:
    """The value for a status tile: the matching text node sitting just below its label."""
    labels = [n for n in nodes if n["text"] == label and n["center"]]
    for ln in labels:
        lx, ly = ln["center"]
        best = None
        for n in nodes:
            if n["text"] in values and n["center"]:
                vx, vy = n["center"]
                if abs(vx - lx) <= 180 and 0 < vy - ly <= 130:
                    if best is None or (vy - ly) < best[0]:
                        best = (vy - ly, n["text"])
        if best:
            return best[1]
    return None


def read_status() -> dict:
    """Navigate to the Status tab and read the vehicle status (read-only, no actuation).

    Returns a dict with any of: vehicle, range, tires, doors, windows, trunk.
    Scrolls the tab because the door/window/trunk tiles sit below the fold.
    """
    activity = _foreground_activity()
    _assert_logged_in(activity)
    if PKG not in activity:
        _adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        time.sleep(6)
        _assert_logged_in(_foreground_activity())

    # Close any open sheet/modal (e.g. the Advanced Remote panel left up after a command) so the tab
    # bar is reachable, then select the Status tab. Skipping this was why STATUS silently returned
    # nothing whenever the app was sitting on the Advanced Remote modal.
    nodes = _ensure_dashboard()
    st = _find(nodes, "ID_TAB_STATUS")
    if st:
        _adb("shell", "input", "tap", str(st[0]), str(st[1]))
        time.sleep(3)

    # Each tile carries a stable sub-title resource-id with the status word — read by id (robust;
    # the text nodes have degenerate bounds so spatial pairing is unreliable). Tiles span scroll
    # positions, so collect across a few dumps, scrolling to bring the lower ones into the tree.
    by_id = {
        "tires": "vehicle_status_tire_pressure_tile_sub_title",
        "doors": "vehicle_status_door_tile_sub_title",
        "windows": "vehicle_status_window_tile_sub_title",
        "trunk": "vehicle_status_trunk_tile_sub_title",
    }
    out: dict = {}
    for _ in range(5):
        nodes = _nodes(_ui_dump())
        if "vehicle" not in out:
            v = _text_by_id(nodes, "dashboard_vehicle_name")
            if v:
                out["vehicle"] = v
        if "range" not in out:
            rv = _text_by_id(nodes, "fuel_view_range_value_text")
            if rv:
                out["range"] = f"{rv} {_text_by_id(nodes, 'fuel_view_range_unit_text') or 'mi'}"
        for key, rid in by_id.items():
            if key not in out:
                t = _text_by_id(nodes, rid)
                if t:
                    out[key] = t
        if all(k in out for k in ("range", "tires", "doors", "windows", "trunk")):
            break
        _scroll_up()
    return out


def format_status(st: dict) -> str:
    """One SMS from a read_status() dict."""
    if not any(k in st for k in ("range", "doors", "windows", "trunk", "tires")):
        return "Couldn't read the vehicle status — the Status tab didn't load. Try again in a moment."
    lines = [st.get("vehicle", "Vehicle status") + ":"]
    if st.get("range"):
        lines.append(f"Range: {st['range']}")
    if st.get("tires"):
        lines.append(f"Tires: {st['tires']}")
    if st.get("doors"):
        lines.append(f"Doors: {st['doors']}")
    if st.get("windows"):
        lines.append(f"Windows: {st['windows']}")
    if st.get("trunk"):
        lines.append(f"Trunk: {st['trunk']}")
    return "\n".join(lines)
