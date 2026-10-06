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

app = FastAPI(title="Toyota SMS Remote")
_last_cmd_at: dict[str, float] = {}


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


def send_sms(to: str, body: str) -> None:
    """Send an outbound SMS via TextGrid's /2010-04-01 Twilio-compatible endpoint (HTTP Basic)."""
    if not (TEXTGRID_ACCOUNT_SID and TEXTGRID_AUTH_TOKEN and TEXTGRID_FROM):
        logger.error("Outbound SMS not configured (TEXTGRID_ACCOUNT_SID/AUTH_TOKEN/PHONE_NUMBER) — can't deliver: %s", body[:60])
        return
    url = f"{TEXTGRID_BASE_URL}/2010-04-01/Accounts/{TEXTGRID_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": TEXTGRID_FROM, "Body": body}).encode()
    auth = base64.b64encode(f"{TEXTGRID_ACCOUNT_SID}:{TEXTGRID_AUTH_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            logger.info("outbound SMS to %s: HTTP %s", to[-4:], r.status)
    except Exception as e:
        logger.error("outbound SMS to %s failed: %r", to[-4:], e)


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
    send_sms(to, reply)


def _do_command(to: str, command: str) -> None:
    pretty = command.replace("_", " ")
    try:
        since_ms = tc.execute(command)
    except tc.NotLoggedIn:
        send_sms(to, "The car app is signed out and needs re-authentication before I can send commands. The operator has been alerted.")
        return
    except tc.ControlError as e:
        send_sms(to, f"Couldn't send {pretty}: {e}")
        return
    except Exception as e:
        logger.exception("command failed")
        send_sms(to, f"{pretty} failed: {e}")
        return
    result = tc.await_result(since_ms)
    if result and result.get("text"):
        send_sms(to, result["text"])
    else:
        send_sms(to, f"Sent '{pretty}' to the car. No confirmation came back within {tc.RESULT_TIMEOUT}s — it may still be completing.")


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
    command, confirmed = tc.parse_command(body)
    if not command:
        return _twiml("Unknown command. Reply HELP for the list. (STATUS, START, LOCK, UNLOCK, ...)")

    # HELP is instant — reply synchronously.
    if command == "help":
        return _twiml(tc.HELP_TEXT)

    # STATUS and car commands take 15-30s (longer than TextGrid's webhook timeout), so the webhook
    # returns an EMPTY reply (no up-front ack) and the background worker delivers the one real result
    # as a separate outbound SMS.
    if command == "status":
        _run_bg(_do_status, sender)
        return _empty_twiml()

    # confirmation gate for car-opening actions (instant reply)
    pretty = command.replace("_", " ")
    if command in tc.CONFIRM_REQUIRED and not confirmed:
        return _twiml(f"Reply 'TOYOTA {pretty.upper()} YES' to confirm {pretty}.")

    # per-sender rate limit (instant reply)
    now = time.time()
    if now - _last_cmd_at.get(sender, 0) < RATE_LIMIT_SECONDS:
        return _twiml("One moment — still processing your last command.")
    _last_cmd_at[sender] = now

    _run_bg(_do_command, sender, command)
    return _empty_twiml()
