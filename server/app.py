"""TextGrid inbound-SMS webhook → tap the Toyota app's button. UI-only (see toyota_control).

Security:
  - verifies TextGrid's request signature (X-TextGrid-Signature) against the raw body
  - allowlists sender numbers (ALLOWED_NUMBERS)
  - requires a confirmation word for car-opening commands (unlock / unlock trunk)
Run behind HTTPS (point the TextGrid number's smsUrl at PUBLIC_URL); expose via a tunnel
(e.g. Cloudflare Tunnel) in front of uvicorn on 127.0.0.1.

TIMING: tapping the app takes 15-30s, which is longer than TextGrid's inbound-webhook
timeout, so a synchronous TwiML reply gets discarded. Instead we answer the webhook
INSTANTLY with an EMPTY reply (no up-front ack), run the slow work in a background thread,
and send the ONE real result as a SEPARATE outbound SMS via the TextGrid send API
(Twilio-compatible). HELP, the confirmation prompt, and errors are instant, so they reply inline.

Env (see .env.example):
  TEXTGRID_WEBHOOK_SECRET — the number's Webhook Secret, to validate inbound signatures
  PUBLIC_URL              — the exact https smsUrl configured on the TextGrid number
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

app = FastAPI(title="Toyota SMS Remote")
_last_cmd_at: dict[str, float] = {}
_pending_confirm: dict[str, tuple[str, float]] = {}
_ui_lock = threading.Lock()
_ui_busy_since: float | None = None


def _claim_ui() -> bool:
    """Take the exclusive right to drive the app's UI. False if another worker still holds it."""
    global _ui_busy_since
    with _ui_lock:
        now = time.time()
        if _ui_busy_since is not None and now - _ui_busy_since < UI_CLAIM_TIMEOUT:
            return False
        if _ui_busy_since is not None:
            logger.warning("stealing a UI claim held %.0fs — previous worker looks wedged",
                           now - _ui_busy_since)
        _ui_busy_since = now
        return True


def _release_ui() -> None:
    global _ui_busy_since
    with _ui_lock:
        _ui_busy_since = None


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
            payload = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        sid = payload.get("sid")
        # Log the BODY, not just the status code: without it, diagnosing which error text the owner
        # actually received meant going to TextGrid's API, because the journal never recorded it.
        logger.info("outbound SMS to %s sid=%s: %s", to[-4:], sid, body.replace("\n", " | "))
        return sid
    except Exception as e:
        logger.error("outbound SMS to %s failed: %r | body=%s", to[-4:], e, body.replace("\n", " | "))
        return None


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
    was never told anything. The same wording delivered fine before and after, so the trigger is
    duplicate content — the resend carries a timestamp to make it unique.
    """
    sid = send_sms(to, body)
    if not sid:
        return
    for wait in (6, 10):
        time.sleep(wait)
        status, err = _message_status(sid)
        if status in ("delivered", "sent"):
            return
        if status in ("failed", "undelivered"):
            logger.error("reply sid=%s was not delivered (status=%s err=%s) — resending", sid, status, err)
            send_sms(to, f"{body} ({time.strftime('%H:%M')})")
            return


def _run_bg(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _do_status(to: str) -> None:
    try:
        reply = tc.format_status(tc.read_status())
    except tc.NotLoggedIn:
        reply = "The car app is signed out and needs re-authentication. The operator has been alerted."
    except tc.ControlError as e:
        reply = f"Couldn't read status: {e}"
    except Exception as e:  # never let the thread die silently
        logger.exception("status failed")
        reply = f"Status failed: {e}"
    finally:
        _release_ui()
    deliver(to, reply)


def _do_command(to: str, command: str) -> None:
    """Drive the car, then report. Works out the whole reply BEFORE releasing the UI claim, and
    only sends afterwards — `deliver` spends up to ~16s confirming the SMS went out, which must
    not keep the next command waiting on the screen."""
    pretty = command.replace("_", " ")
    try:
        try:
            since_ms = tc.execute(command)
        except tc.NotLoggedIn:
            reply = ("The car app is signed out and needs re-authentication before I can send "
                     "commands. The operator has been alerted.")
        except tc.CommandNotApplicable as e:
            reply = str(e)  # already a complete sentence for the owner
        except tc.ControlError as e:
            reply = f"Couldn't send {pretty}: {e}"
        except Exception as e:
            logger.exception("command failed")
            reply = f"{pretty} failed: {e}"
        else:
            try:
                result = tc.await_result(since_ms, command=command)
            except Exception:
                logger.exception("reading the result notification failed")
                result = None
            if result and result.get("text"):
                reply = result["text"]
            elif tc.app_reported_success(command, since_ms):
                # Toyota's push didn't arrive, but the app logged the command as accepted — report
                # what we actually know rather than an unqualified "no confirmation".
                reply = (f"{pretty.capitalize()} went through — the Toyota app confirmed it. "
                         f"(The car's own confirmation message didn't arrive.)")
            else:
                reply = (f"Sent '{pretty}' to the car. No confirmation came back within "
                         f"{tc.RESULT_TIMEOUT}s — it may still be completing.")
    finally:
        _release_ui()
    deliver(to, reply)


@app.get("/health")
def health():
    return {"ok": True, "device_online": tc.device_online()}


@app.post("/sms")
async def sms(request: Request):
    # Read the RAW body first — TextGrid signs it verbatim, and parsing the form would
    # consume the stream before we could verify.
    raw = await request.body()

    # 1) verify the request really came from TextGrid for the URL we expect. Fail CLOSED with no secret.
    if TEXTGRID_WEBHOOK_SECRET:
        sig = request.headers.get("X-TextGrid-Signature", "")
        if not tg.verify_signature(TEXTGRID_WEBHOOK_SECRET, PUBLIC_URL, raw, sig):
            return Response(status_code=403, content="bad signature")
    elif ALLOW_UNSIGNED:
        logger.warning("ALLOW_UNSIGNED is set — skipping signature verification. DEV ONLY; never in production.")
    else:
        logger.error("Refusing request: TEXTGRID_WEBHOOK_SECRET unset (set it, or ALLOW_UNSIGNED=1 for local dev only).")
        return Response(status_code=503, content="server not configured: TEXTGRID_WEBHOOK_SECRET is required")

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
        pending = _pending_confirm.pop(sender, None)
        if pending and time.time() - pending[1] <= CONFIRM_WINDOW_SECONDS:
            command = pending[0]
        else:
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
        if not _claim_ui():
            return _twiml("One moment — still working on your last command.")
        _run_bg(_do_status, sender)
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
    if not _claim_ui():
        return _twiml("One moment — still working on your last command.")
    _last_cmd_at[sender] = now

    _run_bg(_do_command, sender, command)
    return _empty_twiml()
