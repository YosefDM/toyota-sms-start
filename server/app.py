"""TextGrid inbound SMS *and* phone-call webhooks → tap the Toyota app's button. UI-only (see
toyota_control).

Two control channels, one car:
  - **SMS** (`/sms`): text a keyword (`TOYOTA START`, `LOCK`, …) and get the result as a reply.
  - **Voice** (`/voice`): CALL the number and key a per-command secret code on the keypad; the
    car's own confirmation is read back before the call ends. See `voice.py` for the flow and for
    what TextGrid's voice API actually supports.

Security:
  - verifies TextGrid's request signature (X-TextGrid-Signature) against the raw body
  - allowlists sender/caller numbers (ALLOWED_NUMBERS)
  - requires a confirmation word for car-opening commands (unlock / unlock trunk) on SMS. The
    phone path has NO confirming keypress: on a speed dial it would be stored in the same contact
    as the code and autosent by the same press, so it distinguished nothing. There, the
    per-command secret code is what makes an unlock deliberate (see `voice_code`).
  - the voice path adds a per-command secret code, and locks a caller out after repeated wrong ones
Run behind HTTPS (point the TextGrid number's smsUrl at PUBLIC_URL and its voiceUrl at
VOICE_PUBLIC_URL); expose via a tunnel (e.g. Cloudflare Tunnel) in front of uvicorn on 127.0.0.1.

TIMING: tapping the app takes 15-30s, which is longer than TextGrid's inbound-webhook
timeout, so a synchronous TwiML reply gets discarded. Instead we answer the webhook
INSTANTLY with an EMPTY reply (no up-front ack), run the slow work in a background thread,
and send the ONE real result as a SEPARATE outbound SMS via the TextGrid send API
(Twilio-compatible). HELP, the confirmation prompt, and errors are instant, so they reply inline.

Env (see .env.example):
  TEXTGRID_WEBHOOK_SECRET — the number's Webhook Secret, to validate inbound signatures
  PUBLIC_URL              — the exact https smsUrl configured on the TextGrid number
  VOICE_PUBLIC_URL        — the exact https voiceUrl configured on the number (must end in /voice)
  VOICE_CODES             — "digits:command,…" the phone keypad codes (see voice.py)
  VOICE_REQUIRE_SIGNATURE — verify the signature on voice webhooks too (default on; see /voice)
  ALLOWED_NUMBERS         — comma-separated E.164 numbers permitted to command the car
  TEXTGRID_ACCOUNT_SID, TEXTGRID_AUTH_TOKEN, TEXTGRID_PHONE_NUMBER, TEXTGRID_BASE_URL
                          — outbound send (to deliver the result after the ack)
  ADB_PATH, ADB_SERIAL, HOLD_MS, RATE_LIMIT_SECONDS, RESULT_TIMEOUT — passed to toyota_control
"""
from __future__ import annotations
import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

import textgrid_auth as tg
import toyota_control as tc
import voice as v

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("toyota_sms")

TEXTGRID_WEBHOOK_SECRET = os.environ.get("TEXTGRID_WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # the exact smsUrl set on the TextGrid number
ALLOWED_NUMBERS = {n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()}
RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT_SECONDS", "8"))
# Dev-only escape hatch: run WITHOUT signature verification. Off by default. Never set in production —
# without the signature, `From` is an unauthenticated, forgeable form value, so the allowlist alone would
# let anyone who learns the URL unlock the car.
ALLOW_UNSIGNED = os.environ.get("ALLOW_UNSIGNED", "").strip().lower() in ("1", "true", "yes")

# Outbound send (TextGrid's Twilio-compatible REST API) — used to deliver the result after the ack.
TEXTGRID_ACCOUNT_SID = os.environ.get("TEXTGRID_ACCOUNT_SID", "")
TEXTGRID_AUTH_TOKEN = os.environ.get("TEXTGRID_AUTH_TOKEN", "")
TEXTGRID_FROM = os.environ.get("TEXTGRID_PHONE_NUMBER", "")
TEXTGRID_BASE_URL = os.environ.get("TEXTGRID_BASE_URL", "https://api.textgrid.com")

# A command whose confirmation we're waiting on: sender -> (command, asked_at). Lets the owner
# answer the confirmation prompt with a bare "YES" instead of retyping the whole command — a plain
# "Yes" used to come back "Unknown command" (2026-10-06), which is a trap on a numeric keypad.
CONFIRM_WINDOW_SECONDS = int(os.environ.get("CONFIRM_WINDOW_SECONDS", "120"))

# There is ONE car and ONE emulator, so UI work must not overlap: two threads driving the same
# screen fight over navigation, and the second command's result poll can pick up the first's push.
# That overlap is what answered a START with "The vehicle is now locked" (2026-10-08) and what let a
# STATUS read collide with a command (2026-10-06). The stale-claim timeout keeps a wedged worker
# from locking the car out for good.
UI_CLAIM_TIMEOUT = int(os.environ.get("UI_CLAIM_TIMEOUT", "180"))

# A remote start the car refuses because it isn't secured is the one failure we can fix without the
# owner: lock it, start it again. Costs ~60s more on the one command that failed, and only ever
# runs once. Set to 0 to report the refusal and do nothing instead.
AUTO_LOCK_RETRY = os.environ.get("AUTO_LOCK_RETRY", "1").strip().lower() not in ("0", "false", "no")

# --- Voice (phone-call) path ---------------------------------------------------------------
# The exact voiceUrl configured on the TextGrid number. The <Gather>/<Redirect> URLs we hand back
# are built from it, and each is signature-verified against the URL it was requested at, so this
# must match byte-for-byte and its path must be the one FastAPI serves: `/voice`.
VOICE_PUBLIC_URL = os.environ.get("VOICE_PUBLIC_URL", "").rstrip("/")
# TextGrid's voice doc says nothing about signing inbound voice webhooks (unlike SMS, where the
# scheme is documented and proven). If live calls come back 403 with "no X-TextGrid-Signature" in
# the log, confirm that in the dashboard and set this to 0 — the voice path still requires an
# allowlisted caller ID, a per-command secret code, and survives brute force via the lockout.
VOICE_REQUIRE_SIGNATURE = os.environ.get("VOICE_REQUIRE_SIGNATURE", "1").strip().lower() not in (
    "0", "false", "no")
# How long the caller is parked in the say/pause/redirect hold loop before we give up on Toyota's
# push.
#
# This MUST be measured against a whole attempt, not just the push wait. The clock starts at
# DISPATCH (`job["created"]` is set before the worker thread starts), and from there one attempt is:
#   tc.execute        ~15-30s  (panel navigation + a HOLD_MS long-press + the Caution sheet)
#   tc.await_result    <=RESULT_TIMEOUT (25s default)
#   app_reported_success a few more seconds of logcat reads, only when no push arrived
# which is ~50-70s. The old hard-coded 45s was described as "slightly longer than RESULT_TIMEOUT",
# but RESULT_TIMEOUT is only the last leg -- so a start whose push landed late in its own window
# got the `partial` tone ("state unknown") even though it had SUCCEEDED. That is precisely the
# "most misleading thing this path could do" that the design is supposed to avoid, so the budget is
# now derived instead of guessed.
_TAP_BUDGET = int(os.environ.get("VOICE_TAP_BUDGET", "35"))     # navigate + long-press + Caution
_FALLBACK_BUDGET = 10                                            # the logcat app-event fallback
_ATTEMPT_BUDGET = _TAP_BUDGET + tc.RESULT_TIMEOUT + _FALLBACK_BUDGET
VOICE_HOLD_SECONDS = int(os.environ.get("VOICE_HOLD_SECONDS", str(_ATTEMPT_BUDGET)))
# An auto-lock-and-retry is THREE attempts back to back (start, lock, start), so it needs three
# attempt budgets -- not the old hard-coded 150s, which was under even the optimistic total and so
# reintroduced the same false `partial` on the exact path that takes longest.
VOICE_RETRY_HOLD_SECONDS = int(os.environ.get("VOICE_RETRY_HOLD_SECONDS", str(3 * _ATTEMPT_BUDGET)))
# Generous: with a speed-dial pause string the handset may still be counting commas when the
# gather opens, and a human on a keypad is slower than this prompt is short.
VOICE_GATHER_TIMEOUT = int(os.environ.get("VOICE_GATHER_TIMEOUT", "10"))
VOICE_FIRST_PAUSE = int(os.environ.get("VOICE_FIRST_PAUSE", "8"))
VOICE_LOOP_PAUSE = int(os.environ.get("VOICE_LOOP_PAUSE", "5"))

if VOICE_PUBLIC_URL and not VOICE_PUBLIC_URL.endswith("/voice"):
    # The <Gather>/<Redirect> URLs are this value plus "/code" and "/wait", and those have
    # to land on the routes below. Point it at /sms, or at the bare host, and every call gets as far
    # as the prompt and then 404s on the digits — which looks like TextGrid dropping the DTMF.
    logger.error("VOICE_PUBLIC_URL is %r but must end in '/voice' — the callback URLs are built "
                 "from it and the digits will 404", VOICE_PUBLIC_URL)

try:
    VOICE_CODES = v.parse_codes(os.environ.get("VOICE_CODES", ""))
    VOICE_CODES_ERROR = ""
except v.CodeTableError as e:
    # Don't crash the process — a bad code table must not take the WORKING SMS path down with it.
    VOICE_CODES, VOICE_CODES_ERROR = {}, str(e)
    logger.error("VOICE_CODES is invalid, so the phone path is disabled: %s", e)

# FastAPI publishes /docs, /redoc and /openapi.json by default. On a box whose hostname is the one
# thing an attacker is most likely to learn, that hands them the complete endpoint map and every
# parameter name for free -- the first thing anyone probing this would fetch. Nothing here is a
# public API: the only legitimate clients are TextGrid's webhooks, which need no discovery. Set
# EXPOSE_API_DOCS=1 if you ever want them locally.
_DOCS = os.environ.get("EXPOSE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="Toyota SMS Remote",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
_voice_throttle = v.Throttle()
_voice_jobs = v.Jobs()
_last_cmd_at: dict[str, float] = {}
_pending_confirm: dict[str, tuple[str, float]] = {}
_ui_lock = threading.Lock()
_ui_holder: int | None = None        # token of the live claim, or None when the screen is free
_ui_busy_since: float | None = None  # when that claim was taken or last refreshed
_ui_token_seq = 0


def _claim_ui() -> int | None:
    """Take the exclusive right to drive the app's UI.

    Returns a token to hand back to `_release_ui`, or None if another worker still holds the claim.
    The token matters: when a claim is STOLEN from a wedged worker, that worker may still finish
    later, and a token-less release would clear the *new* holder's claim — letting a third command
    drive the screen alongside it, which is the exact overlap this lock exists to prevent.

    The token is a counter and NOT the held-since timestamp (which it used to be), so that
    `_touch_ui` can push the staleness clock forward without changing the holder's identity. The
    auto-lock-and-retry sequence is what needs that: three commands back to back can run ~100s,
    and a claim that cannot be refreshed would start looking wedged partway through its own work.
    """
    global _ui_holder, _ui_busy_since, _ui_token_seq
    with _ui_lock:
        now = time.time()
        if _ui_holder is not None and now - (_ui_busy_since or 0) < UI_CLAIM_TIMEOUT:
            return None
        if _ui_holder is not None:
            logger.warning("stealing a UI claim held %.0fs — previous worker looks wedged",
                           now - (_ui_busy_since or now))
        _ui_token_seq += 1
        _ui_holder, _ui_busy_since = _ui_token_seq, now
        return _ui_holder


def _touch_ui(token: int | None) -> None:
    """Say "still working" — resets the staleness clock without releasing the claim.

    Called between the legs of a multi-command sequence. Without it, a legitimately long but
    perfectly healthy worker becomes stealable mid-sequence and a second command can start driving
    the same screen.
    """
    global _ui_busy_since
    with _ui_lock:
        if token is not None and _ui_holder == token:
            _ui_busy_since = time.time()


def _release_ui(token: int | None) -> None:
    """Release the claim only if `token` is still the live one (see `_claim_ui`)."""
    global _ui_holder, _ui_busy_since
    with _ui_lock:
        if token is not None and _ui_holder == token:
            _ui_holder, _ui_busy_since = None, None


def _twiml(text: str) -> Response:
    """A minimal TwiML SMS reply (TextGrid executes it like Twilio does) — used for the instant ack."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(text)}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


def _empty_twiml() -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    media_type="application/xml")


def _signature_problem(request: Request, raw: bytes, signed_url: str, *,
                       require: bool = True) -> str | None:
    """Decide whether this webhook request is allowed to act on the car.

    Returns None to proceed, or a short reason: ``"bad-signature"`` (it is not from TextGrid, or
    not for this URL) or ``"unconfigured"`` (no secret, so nothing can be verified and we refuse).
    The caller turns the reason into its own kind of reply — SMS answers in TwiML, voice has to
    answer in speech.

    `require=False` is for the voice path, where TextGrid's own doc never mentions signing inbound
    voice webhooks; see VOICE_REQUIRE_SIGNATURE.
    """
    if TEXTGRID_WEBHOOK_SECRET and require:
        sig = request.headers.get("X-TextGrid-Signature", "")
        if not tg.verify_signature(TEXTGRID_WEBHOOK_SECRET, signed_url, raw, sig):
            # Say WHICH it is: a missing header means "TextGrid doesn't sign this hook" (flip
            # VOICE_REQUIRE_SIGNATURE), a present-but-wrong one means a URL mismatch or an
            # impostor. Guessing between those cost a live debugging session on the SMS path.
            logger.error("%s: signature rejected (%s) for signed_url=%s", request.url.path,
                         "no X-TextGrid-Signature header" if not sig else "header did not match",
                         signed_url)
            return "bad-signature"
        return None
    if ALLOW_UNSIGNED or not require:
        logger.warning("%s: serving WITHOUT signature verification (ALLOW_UNSIGNED=%s, require=%s)"
                       " — the allowlist and (on voice) the code are the only gate.",
                       request.url.path, ALLOW_UNSIGNED, require)
        return None
    logger.error("Refusing request: TEXTGRID_WEBHOOK_SECRET unset (set it, or ALLOW_UNSIGNED=1 for "
                 "local dev only).")
    return "unconfigured"


def _tg_auth_header() -> str:
    return "Basic " + base64.b64encode(
        f"{TEXTGRID_ACCOUNT_SID}:{TEXTGRID_AUTH_TOKEN}".encode()).decode()


def send_sms(to: str, body: str) -> str | None:
    """Send an outbound SMS via TextGrid's /2010-04-01 Twilio-compatible endpoint (HTTP Basic).

    Returns the message sid so delivery can be checked, or None if the send itself failed.
    """
    if not (TEXTGRID_ACCOUNT_SID and TEXTGRID_AUTH_TOKEN and TEXTGRID_FROM):
        logger.error("Outbound SMS not configured (TEXTGRID_ACCOUNT_SID/AUTH_TOKEN/PHONE_NUMBER) — can't deliver: %s", body[:60])
        return None
    url = f"{TEXTGRID_BASE_URL}/2010-04-01/Accounts/{TEXTGRID_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": TEXTGRID_FROM, "Body": body}).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": _tg_auth_header(),
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error("outbound SMS to %s failed: %r | body=%s", to[-4:], e, body.replace("\n", " | "))
        return None
    # Parse OUTSIDE the request's try: the send already succeeded, so a surprising body must not be
    # reported as a failed send — that would also skip the delivery check in `deliver()`.
    try:
        sid = (json.loads(raw or "{}") or {}).get("sid")
    except Exception as e:
        logger.warning("outbound SMS to %s was accepted but its response didn't parse (%r) — "
                       "can't verify delivery", to[-4:], e)
        sid = None
    # Log the BODY, not just the status code: without it, diagnosing which error text the owner
    # actually received meant going to TextGrid's API, because the journal never recorded it.
    logger.info("outbound SMS to %s sid=%s: %s", to[-4:], sid, body.replace("\n", " | "))
    return sid


def _message_status(sid: str) -> tuple[str, str]:
    """(status, error_message) for a sent message, or ("", "") if it can't be read."""
    url = (f"{TEXTGRID_BASE_URL}/2010-04-01/Accounts/"
           f"{urllib.parse.quote(TEXTGRID_ACCOUNT_SID, safe='')}/Messages/"
           f"{urllib.parse.quote(sid, safe='')}.json")
    req = urllib.request.Request(url, headers={"Authorization": _tg_auth_header()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            m = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        return (m.get("status") or ""), (m.get("error_message") or "")
    except Exception as e:
        logger.warning("couldn't read status of sid=%s: %r", sid, e)
        return "", ""


def deliver(to: str, body: str) -> None:
    """Send a reply AND make sure it actually went out, resending once if it didn't.

    TextGrid accepts the POST (HTTP 201) and only then runs its content filter, so a blocked reply
    looked like a success here and vanished silently: on 2026-10-06 two identical "Vehicle was
    started..." confirmations came back "failed - Blocked by Textgrid Content Filter" and the owner
    was never told anything. The resend carries a timestamp so the retry isn't byte-identical.

    What trips the filter is still unknown and is NOT simply duplicate content: the carrier log for
    2026-10-12..14 shows the very same "The vehicle is now locked. [DL1]" delivered on the 12th and
    blocked on the 14th, so it behaves more like a velocity or sampling rule than a content rule.
    """
    sid = send_sms(to, body)
    if not sid:
        return
    # The filter verdict often lands well after the message has sat queued/accepted, so keep
    # looking for ~65s rather than ~16s, and say so loudly if it never reaches a terminal state —
    # a silently-non-terminal reply is exactly the failure this function was added to catch.
    status = ""
    for wait in (6, 10, 20, 30):
        time.sleep(wait)
        status, err = _message_status(sid)
        # TextGrid appends the reason to the status itself ("failed - Blocked by Textgrid Content
        # Filter"), so comparing the whole string never matched "failed" and the resend below — the
        # entire point of this function — never once fired. Three car confirmations were lost that
        # way (2026-10-12..14) while the log claimed only "delivery unconfirmed". Match the verb.
        verdict = status.split(" - ", 1)[0].strip().lower()
        if verdict in ("delivered", "sent"):
            return
        if verdict in ("failed", "undelivered"):
            logger.error("reply sid=%s was not delivered (status=%s err=%s) — resending", sid, status, err)
            send_sms(to, f"{body} ({time.strftime('%H:%M')})")
            return
    logger.warning("reply sid=%s never reached a terminal status (last=%r) — delivery unconfirmed",
                   sid, status)


def _run_bg(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


# Blocking screens (signed-out / update wall / rooted-device wall) don't self-clear, so a burst of
# commands arriving while one is up would otherwise text the owner once per failed command. One
# alert per reason per cooldown window is enough to say "come look" without being noise.
_ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", str(30 * 60)))
_last_alert_at: dict[str, float] = {}


def _alert_operator(reason: str, detail: str) -> None:
    """Tell the owner the rig hit a state no retry will clear, so they go look instead of finding
    out only when a command silently fails. `reason` is a short stable tag (one per blocking-screen
    type) used for the per-reason cooldown; `detail` is what actually goes in the text."""
    now = time.time()
    last = _last_alert_at.get(reason, 0.0)
    if now - last < _ALERT_COOLDOWN_SECONDS:
        logger.info("alert '%s' suppressed (sent %ds ago, cooldown %ds)", reason, int(now - last), _ALERT_COOLDOWN_SECONDS)
        return
    _last_alert_at[reason] = now
    if not ALLOWED_NUMBERS:
        logger.error("can't alert the operator — ALLOWED_NUMBERS is empty")
        return
    for number in ALLOWED_NUMBERS:
        _run_bg(deliver, number, f"[Toyota rig] {detail}")


def _blocking_screen_reply(e: tc.ControlError, verb: str) -> str:
    """Owner-facing sentence for a blocking-screen exception, AND fires the one-per-cooldown
    operator alert. `verb` reads naturally into "... before I can {verb}." (e.g. "read status",
    "send commands")."""
    if isinstance(e, tc.NotLoggedIn):
        reason, text = "signed_out", "the app is signed out and needs re-authentication"
    elif isinstance(e, tc.UpdateRequired):
        reason, text = "update_required", "Toyota is refusing the installed app version (update wall)"
    elif isinstance(e, tc.RootedDevice):
        reason, text = "rooted_dialog", "the app's rooted-device wall is up (bypass needs re-matching to the installed build)"
    else:
        reason, text = "control_error", str(e)
    _alert_operator(reason, f"Car commands are blocked: {text}. Needs the rig looked at.")
    return f"Car app problem: {text}, before I can {verb}. The operator has been alerted."


def _do_status(to: str, token: int | None = None) -> None:
    try:
        reply = tc.format_status(tc.read_status())
    except (tc.NotLoggedIn, tc.UpdateRequired, tc.RootedDevice) as e:
        reply = _blocking_screen_reply(e, "read status")
    except tc.ControlError as e:
        reply = f"Couldn't read status: {e}"
    except Exception as e:  # never let the thread die silently
        logger.exception("status failed")
        reply = f"Status failed: {e}"
    finally:
        _release_ui(token)
    deliver(to, reply)


def _attempt(command: str) -> tuple[str, str, str]:
    """Run ONE command to its verdict: ``(sentence, outcome, push_text)``.

    `outcome` is ``"done"`` (the car confirmed it), ``"failed"`` (refused, or never went out) or
    ``"partial"`` (it went out and we genuinely do not know — e.g. the FCM push never arrived).
    Only the three are distinguished, because a tone cannot hedge and guessing "done" from silence
    would be a lie about a car. `push_text` is Toyota's own wording when a push arrived and "" when
    none did, so a caller can ask *why* it failed without re-parsing the sentence.

    Holds no lock and releases nothing — `_command_reply` owns the UI claim across however many
    attempts it makes.
    """
    pretty = command.replace("_", " ")
    try:
        since_ms = tc.execute(command)
    except (tc.NotLoggedIn, tc.UpdateRequired, tc.RootedDevice) as e:
        return _blocking_screen_reply(e, "send commands"), "failed", ""
    except tc.CommandNotApplicable as e:
        return str(e), "failed", ""  # already a complete sentence for the owner
    except tc.ControlError as e:
        return f"Couldn't send {pretty}: {e}", "failed", ""
    except Exception as e:
        logger.exception("command failed")
        return f"{pretty} failed: {e}", "failed", ""

    try:
        result = tc.await_result(since_ms, command=command)
    except Exception:
        logger.exception("reading the result notification failed")
        result = None

    if result and result.get("text"):
        push = result["text"]
        # The push is the car's own word, but it carries refusals too ("...keyfob was detected
        # [70]"), so the verdict is its result CODE, not its arrival.
        verdict = tc.result_is_success(command, push)
        return push, ("partial" if verdict is None else ("done" if verdict else "failed")), push
    if tc.app_reported_success(command, since_ms):
        # The app's event means TOYOTA ACCEPTED the request, not that the car acted — a car-side
        # refusal ("keyfob was detected [70]") only ever arrives on the push that is, by
        # definition, missing here. Don't promise more than acceptance.
        return (f"{pretty.capitalize()} was accepted by the Toyota app, but the car's own "
                f"confirmation didn't arrive — I can't confirm the car actually did it."), "partial", ""
    return (f"Sent '{pretty}' to the car. No confirmation came back within "
            f"{tc.RESULT_TIMEOUT}s — it may still be completing."), "partial", ""


def _command_reply(command: str, token: int | None = None,
                   on_stage=None) -> tuple[str, str]:
    """Drive the car and return ``(sentence, outcome)``, releasing the UI claim first.

    Shared by both channels: the SMS worker texts the sentence, the voice worker plays a tone for
    the outcome (it must never say the car's state out loud).

    **Auto-lock-and-retry.** A remote start that the car refuses because it isn't secured is a
    failure the owner can do nothing about from a basic phone except send LOCK and then START
    again — two messages and a minute, usually in a driveway in the cold. So we do it for them:
    on that specific refusal we lock the car and start ONCE more. `on_stage("retrying")`, when
    given, lets the voice path tell the caller why the call is suddenly taking longer.

    It is one retry, never a loop, and it is honest about what it did: "secured" covers an open
    window or an ajar trunk as well as unlocked doors, so a lock is a good guess and not a
    guarantee. If the second start is refused the same way, the reply says the lock didn't help
    and names the likely cause.

    Everything slow happens BEFORE the claim is released, and nothing about *delivery* happens
    here — `deliver` spends up to ~16s confirming the SMS went out, which must not keep the next
    command waiting on the screen.
    """
    try:
        reply, outcome, push = _attempt(command)

        if not (command == "start" and outcome == "failed"
                and AUTO_LOCK_RETRY and tc.refused_as_unsecured(push)):
            return reply, outcome

        logger.info("start refused as unsecured (%r) — locking and retrying once", push[:90])
        if on_stage:
            on_stage("retrying")
        # Refresh the claim between legs: this sequence is three commands and can run ~100s, which
        # would otherwise start looking wedged to `_claim_ui` partway through its own work.
        _touch_ui(token)

        lock_reply, lock_outcome, _ = _attempt("lock")
        if lock_outcome != "done":
            return (f"The car refused the remote start because it isn't secured, so I tried to "
                    f"lock it first, but the lock didn't confirm either: {lock_reply}"), "failed"

        _touch_ui(token)
        retry_reply, retry_outcome, retry_push = _attempt("start")

        if retry_outcome == "done":
            return f"The car wasn't locked, so I locked it and started it: {retry_reply}", "done"
        if tc.refused_as_unsecured(retry_push):
            # Locking was the one thing we could fix; "secured" means more than the doors.
            # ASCII only, deliberately: one em-dash in an SMS forces the whole message to UCS-2,
            # which cuts a segment from 160 characters to 70 — and this is the longest reply here.
            return (f"I locked the car and tried the start again, but it still says the vehicle "
                    f"isn't secured. Check for an open window, the trunk, or a door that didn't "
                    f"latch: {retry_reply}"), "failed"
        return (f"The car wasn't locked, so I locked it and tried the start again: "
                f"{retry_reply}"), retry_outcome
    finally:
        _release_ui(token)


def _do_command(to: str, command: str, token: int | None = None) -> None:
    """SMS worker: drive the car, then text the result."""
    deliver(to, _command_reply(command, token)[0])


@app.get("/health")
def health():
    """Liveness for the watchdog, and deliberately unauthenticated.

    It does leak one bit to anyone who knows the hostname -- whether the rig is currently up. That
    is accepted rather than fixed: gating it on the caller's IP looks like protection and is not,
    because uvicorn takes the client address from X-Forwarded-For (cloudflared connects from
    127.0.0.1, which is inside the default trusted-proxy set), so a caller can influence what this
    code would be checking. A spoofable check that also risks breaking the watchdog is worse than
    an honest one-bit leak.
    """
    return {"ok": True, "device_online": tc.device_online()}


@app.post("/sms")
async def sms(request: Request):
    # Read the RAW body first — TextGrid signs it verbatim, and parsing the form would
    # consume the stream before we could verify.
    raw = await request.body()

    # 1) verify the request really came from TextGrid for the URL we expect. Fail CLOSED with no secret.
    problem = _signature_problem(request, raw, PUBLIC_URL)
    if problem == "bad-signature":
        return Response(status_code=403, content="bad signature")
    if problem:
        return Response(status_code=503,
                        content="server not configured: TEXTGRID_WEBHOOK_SECRET is required")

    # 2) parse the Twilio-style form params from the raw body
    form = dict(parse_qsl(raw.decode("utf-8", errors="replace")))
    sender = form.get("From", "")
    body = form.get("Body", "")

    # 3) allowlist the sender
    if ALLOWED_NUMBERS and sender not in ALLOWED_NUMBERS:
        return _twiml("Sorry, this number isn't authorized.")

    # 4) parse the command
    logger.info("inbound SMS from %s: %s", sender[-4:], body.replace("\n", " | ")[:160])
    command, confirmed = tc.parse_command(body)

    # A bare "YES" (nothing left after stripping the confirmation word) answers the last
    # confirmation prompt, so the owner doesn't have to retype "TOYOTA UNLOCK YES" on a keypad.
    if not command and confirmed:
        # Peek, don't pop: the request can still bounce off the rate limit or the UI claim below,
        # and consuming the confirmation there would make the owner's next YES answer "nothing is
        # waiting" — on a keypad phone that is a trap. It is cleared once the command dispatches.
        pending = _pending_confirm.get(sender)
        if pending and time.time() - pending[1] <= CONFIRM_WINDOW_SECONDS:
            command = pending[0]
        else:
            _pending_confirm.pop(sender, None)
            return _twiml("Nothing is waiting for confirmation. Send the command first, e.g. 'TOYOTA UNLOCK'.")

    if not command:
        # NB: tells them "?" — the carrier intercepts HELP, so a HELP text never reaches us at all.
        return _twiml("Unknown command. Reply ? for the list. (STATUS, START, LOCK, UNLOCK, ...)")

    # HELP is instant — reply synchronously.
    if command == "help":
        return _twiml(tc.HELP_TEXT)

    # STATUS and car commands take 15-30s (longer than TextGrid's webhook timeout), so the webhook
    # returns an EMPTY reply (no up-front ack) and the background worker delivers the one real result
    # as a separate outbound SMS.
    if command == "status":
        token = _claim_ui()
        if token is None:
            return _twiml("One moment — still working on your last command.")
        _run_bg(_do_status, sender, token)
        return _empty_twiml()

    # confirmation gate for car-opening actions (instant reply)
    pretty = command.replace("_", " ")
    if command in tc.CONFIRM_REQUIRED and not confirmed:
        _pending_confirm[sender] = (command, time.time())
        return _twiml(f"Reply YES to confirm {pretty}.")

    # per-sender rate limit (instant reply)
    now = time.time()
    if now - _last_cmd_at.get(sender, 0) < RATE_LIMIT_SECONDS:
        return _twiml("One moment — still processing your last command.")

    # one car, one screen: never drive the UI from two workers at once
    token = _claim_ui()
    if token is None:
        return _twiml("One moment — still working on your last command.")
    _last_cmd_at[sender] = now

    # The command is going out now, so retire any pending confirmation — including one left by a
    # DIFFERENT command the owner then abandoned. Otherwise a stray later YES could still fire that
    # abandoned car-opening action (e.g. TRUNK) without a fresh prompt.
    _pending_confirm.pop(sender, None)

    _run_bg(_do_command, sender, command, token)
    return _empty_twiml()


# ==================================================================== voice (phone-call) path
# Designed around ONE primary gesture: a speed-dial contact whose number carries the code after a
# pause string — `+15550001234,,4821`. The owner presses one key on a basic phone, the handset
# dials, waits out the commas, autosends the DTMF, and the engine starts. Four consequences shape
# the code below:
#   1. The digits can land WHILE the greeting is still playing, so the prompt lives INSIDE the
#      <Gather> (barge-in). A prompt spoken before the gather would swallow an autosent code.
#   2. The call answers in TONES, never in words about the car — see voice.py. The owner may be
#      holding the phone where anyone can hear it, and "the vehicle is now unlocked" is a state
#      disclosure they explicitly don't want.
#   3. The owner hangs up as soon as they hear the accepted tone — or immediately. The command runs
#      on a background thread, so `_command_reply` completes regardless; the result tone is a
#      courtesy, never the mechanism.
#   4. All codes are one fixed length, because TextGrid's <Gather> can only end on `numDigits`
#      (no documented finishOnKey), so there is no "#" for the handset to send.

def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def _voice_url(suffix: str = "") -> str:
    return VOICE_PUBLIC_URL + suffix


def _tone(name: str) -> Response:
    """Play one tone and end the call."""
    return _xml(v.tone(name, VOICE_PUBLIC_URL))


def _voice_form(raw: bytes) -> dict[str, str]:
    return dict(parse_qsl(raw.decode("utf-8", errors="replace")))


def _lockout_line(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    return (f"Too many incorrect codes. This line is locked for about {minutes} "
            f"{'minute' if minutes == 1 else 'minutes'}. Goodbye.")


def _voice_gate(request: Request, raw: bytes, path: str) -> tuple[dict[str, str], str | None]:
    """Shared front door for every voice endpoint: (form, refusal_twiml).

    When the second element is not None it is the complete TwiML to return — the call is being
    turned away. VOICE_PUBLIC_URL is checked FIRST because the signature is computed against it:
    without it every call would be refused as "not from TextGrid", which points the next person at
    entirely the wrong problem. Then signature (is this even TextGrid), the rest of the config, the
    caller allowlist, and the lockout. These refusals are SPOKEN on purpose: none of them says
    anything about the car, and a tone the owner can't interpret would turn a misconfigured line
    into an unanswerable support call.
    """
    if not VOICE_PUBLIC_URL:
        logger.error("a call arrived but VOICE_PUBLIC_URL is unset — set it to the number's "
                     "voiceUrl (it is the signed URL, the <Gather> action base, AND the base for "
                     "the tone URLs)")
        return {}, v.say("Phone control isn't configured. Goodbye.")

    problem = _signature_problem(request, raw, _voice_url(path),
                                 require=VOICE_REQUIRE_SIGNATURE)
    if problem:
        # Say nothing useful: an unverified caller learns only that the line exists.
        return {}, v.say("Sorry, this line isn't available right now. Goodbye.")

    form = _voice_form(raw)
    caller = form.get("From", "")

    if VOICE_CODES_ERROR:
        return form, v.say("Phone control is misconfigured. Goodbye.")
    if not VOICE_CODES:
        logger.error("a call arrived but VOICE_CODES is empty — nothing is reachable by phone")
        return form, v.say("Phone control isn't set up yet. Goodbye.")
    # An EMPTY allowlist must switch the phone path off, not wave everyone through. `if
    # ALLOWED_NUMBERS and caller not in ...` (the SMS idiom) fails open, and on this channel that is
    # much worse than on SMS: the wrong-code lockout is keyed on the caller, so an unrestricted
    # caller can rotate caller IDs and reset their guess budget on every call, leaving the codes --
    # including unlock -- with no effective attempt limit at all. SMS has no secret to guess, so it
    # keeps its own idiom; here the allowlist IS part of the rate limit.
    if not ALLOWED_NUMBERS:
        logger.error("a call arrived but ALLOWED_NUMBERS is empty — the phone path needs an "
                     "allowlist, because the wrong-code lockout is counted per caller")
        return form, v.say("Phone control isn't set up yet. Goodbye.")
    if caller not in ALLOWED_NUMBERS:
        logger.warning("voice: rejecting call from un-allowlisted %s", caller[-4:] or "?")
        return form, v.say("Sorry, this number isn't authorized. Goodbye.")

    locked = _voice_throttle.locked_for(caller)
    if locked:
        return form, v.say(_lockout_line(locked))
    return form, None


def _voice_prompt() -> str:
    """The opening gather. Deliberately says "your code", not "Toyota" or "your car": a spoofed
    caller ID gets you this far, and it should teach nothing."""
    n = v.code_length(VOICE_CODES)
    return v.gather(
        v.say_frag(f"Enter your {n} digit code."),
        _voice_url("/code"),
        num_digits=n,
        timeout=VOICE_GATHER_TIMEOUT,
        after=v.say_frag("I didn't get a code. Goodbye."),
    )


def _voice_dispatch(call_sid: str, caller: str, command: str) -> str:
    """Claim the screen, start the command in the background, play `accepted`, park the call."""
    now = time.time()
    if now - _last_cmd_at.get(caller, 0) < RATE_LIMIT_SECONDS:
        return v.say("That command just went out. Give it a moment and call back. Goodbye.")
    token = _claim_ui()
    if token is None:
        return v.say("The car is still working on the last command. Try again shortly. Goodbye.")
    _last_cmd_at[caller] = now
    _voice_jobs.start(call_sid, command)
    _run_bg(_do_voice_command, call_sid, command, token)
    logger.info("voice: dispatching %s for call %s", command, call_sid[-6:] or "?")
    # `accepted` means exactly "your code was right and the command is on its way" — not that the
    # car did anything. That distinction is the whole reason there is a second tone.
    return v.hold(v.tone_frag("accepted", VOICE_PUBLIC_URL), _voice_url("/wait"),
                  pause=VOICE_FIRST_PAUSE)


def _do_voice_command(call_sid: str, command: str, token: int | None = None) -> None:
    """Voice worker: drive the car, then file the verdict for the hold loop to sound out."""
    reply, outcome = _command_reply(
        command, token, on_stage=lambda stage: _voice_jobs.set_stage(call_sid, stage))
    # The sentence never reaches the caller on this channel, so the log is the only place it is
    # written down — without it a tone-only call would be undiagnosable after the fact.
    logger.info("voice: %s for call %s -> %s | %s", command, call_sid[-6:] or "?", outcome, reply)
    _voice_jobs.finish(call_sid, outcome, reply)


@app.get("/voice/tone/{name}.wav")
def voice_tone(name: str):
    """Serve a synthesised confirmation tone for `<Play>`.

    Unauthenticated by design: these are five fixed sine beeps with no information in them, and
    TextGrid's media fetcher is not the signed webhook client. Cacheable so a morning start isn't
    waiting on a download.
    """
    audio = v.TONES.get(name)
    if audio is None:
        return Response(status_code=404, content="no such tone")
    return Response(content=audio, media_type="audio/wav",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.post("/voice")
async def voice_entry(request: Request):
    raw = await request.body()
    form, refusal = _voice_gate(request, raw, "")
    if refusal:
        return _xml(refusal)
    logger.info("inbound call from %s (CallSid %s)", form.get("From", "")[-4:],
                form.get("CallSid", "")[-6:])
    return _xml(_voice_prompt())


@app.post("/voice/code")
async def voice_code(request: Request):
    raw = await request.body()
    form, refusal = _voice_gate(request, raw, "/code")
    if refusal:
        return _xml(refusal)

    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    digits = form.get("Digits", "")
    command = v.lookup(VOICE_CODES, digits)

    if command is None:
        # Never reveal how it was wrong, or which codes exist — one flat buzz, then another try.
        logger.warning("voice: wrong %d-digit code from %s", len(digits), caller[-4:] or "?")
        locked = _voice_throttle.record_failure(caller)
        if locked:
            return _xml(v.say(_lockout_line(locked)))
        return _xml(v.gather(v.tone_frag("reject", VOICE_PUBLIC_URL), _voice_url("/code"),
                             num_digits=v.code_length(VOICE_CODES),
                             timeout=VOICE_GATHER_TIMEOUT,
                             after=v.say_frag("Goodbye.")))

    _voice_throttle.record_success(caller)

    # NO second "press 1 to confirm" step, deliberately, including for unlock — and `unlock` stays
    # in `tc.CONFIRM_REQUIRED` because on SMS that word still earns its keep.
    #
    # On the phone it did not. The gesture this path exists for is a speed dial, so the confirming
    # keypress would be stored in the same contact as the code (`…,,<code>,,1`) and autosent by the
    # same single press: it cannot distinguish a deliberate unlock from an accidental one, because
    # both arrive as one burst from one keypress. It guarded nothing.
    #
    # What makes an unlock deliberate here is the code itself. Each command has its OWN secret, so
    # keying the unlock code is already an unambiguous choice — unlike SMS, where "UNLOCK" is a
    # word that a fat finger or an autocomplete can produce by accident and where there is no
    # per-command secret at all. Dropping the step also removes a gather that could time out and
    # leave the owner standing at a locked car.
    return _xml(_voice_dispatch(call_sid, caller, command))


@app.post("/voice/wait")
async def voice_wait(request: Request):
    """The hold loop: sound the verdict as soon as there is one, otherwise pause and come back.

    This is intentionally NOT gated on the allowlist/lockout re-checks that `_voice_gate` runs for
    a *command* — by here the command has already gone to the car, and refusing the loop would only
    withhold the answer from the owner. The CallSid is the capability: it is TextGrid's own opaque
    id for this call, and we only ever answer with a verdict we created for it.
    """
    raw = await request.body()
    problem = _signature_problem(request, raw, _voice_url("/wait"),
                                 require=VOICE_REQUIRE_SIGNATURE)
    if problem:
        return _xml(v.say("Goodbye."))

    form = _voice_form(raw)
    call_sid = form.get("CallSid", "")
    job = _voice_jobs.get(call_sid)
    if job is None:
        # Either the call already heard its verdict, or it was never dispatched. Nothing to add.
        return _xml(v.say("Goodbye."))

    if job["outcome"]:
        _voice_jobs.drop(call_sid)
        return _tone(job["outcome"])

    # An auto-lock-and-retry turns one command into three, so the call has to be told why it is
    # suddenly taking a minute — otherwise the only honest thing left is the `partial` tone, on a
    # start that is very probably about to succeed.
    if job["stage"] == "retrying" and not job["stage_played"]:
        _voice_jobs.mark_stage_played(call_sid)
        return _xml(v.hold(v.tone_frag("retrying", VOICE_PUBLIC_URL), _voice_url("/wait"),
                           pause=VOICE_LOOP_PAUSE))

    limit = VOICE_RETRY_HOLD_SECONDS if job["stage"] == "retrying" else VOICE_HOLD_SECONDS
    if time.time() - job["created"] > limit:
        _voice_jobs.drop(call_sid)
        # The command itself is NOT abandoned — the worker is still running and its outcome still
        # reaches the log; only the readback gives up, so this is `partial`, never `failed`.
        logger.info("voice: hold loop gave up on %s for call %s after %ds (stage=%r)",
                    job["command"], call_sid[-6:] or "?", limit, job["stage"])
        return _tone("partial")

    return _xml(v.hold(None, _voice_url("/wait"), pause=VOICE_LOOP_PAUSE))
