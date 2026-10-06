"""TextGrid inbound-SMS webhook → tap the Toyota app's button. UI-only (see toyota_control).

Security:
  - verifies TextGrid's request signature (X-TextGrid-Signature) against the raw body
  - allowlists sender numbers (ALLOWED_NUMBERS)
  - requires a confirmation word for car-opening commands (unlock / unlock trunk)
Run behind HTTPS (point the TextGrid number's smsUrl at PUBLIC_URL); expose via a tunnel
(e.g. Cloudflare Tunnel) in front of uvicorn on 127.0.0.1.

TextGrid is Twilio-compatible enough that a plain TwiML reply is delivered fine, so the
response is still TwiML — but its inbound signature scheme is its own (see textgrid_auth).

Env (see .env.example):
  TEXTGRID_WEBHOOK_SECRET — the number's Webhook Secret, to validate inbound signatures
  PUBLIC_URL              — the exact https smsUrl configured on the TextGrid number
  ALLOWED_NUMBERS         — comma-separated E.164 numbers permitted to command the car
  ADB_PATH, ADB_SERIAL, HOLD_MS, RATE_LIMIT_SECONDS, RESULT_TIMEOUT — passed to toyota_control
"""
from __future__ import annotations
import logging
import os
import time
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

import textgrid_auth as tg
import toyota_control as tc

logger = logging.getLogger("toyota_sms")

TEXTGRID_WEBHOOK_SECRET = os.environ.get("TEXTGRID_WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # the exact smsUrl set on the TextGrid number
ALLOWED_NUMBERS = {n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()}
RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT_SECONDS", "8"))

app = FastAPI(title="Toyota SMS Remote")
_last_cmd_at: dict[str, float] = {}


def _twiml(text: str) -> Response:
    """A minimal TwiML SMS reply (TextGrid executes it like Twilio does)."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(text)}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/health")
def health():
    return {"ok": True, "device_online": tc.device_online()}


@app.post("/sms")
async def sms(request: Request):
    # Read the RAW body first — TextGrid signs it verbatim, and parsing the form would
    # consume the stream before we could verify.
    raw = await request.body()

    # 1) verify the request really came from TextGrid for the URL we expect
    if TEXTGRID_WEBHOOK_SECRET:
        sig = request.headers.get("X-TextGrid-Signature", "")
        if not tg.verify_signature(TEXTGRID_WEBHOOK_SECRET, PUBLIC_URL, raw, sig):
            return Response(status_code=403, content="bad signature")
    else:
        logger.warning("TEXTGRID_WEBHOOK_SECRET unset — skipping signature check (set it in production)")

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
        return _twiml("Unknown command. Try: START, LOCK, UNLOCK, LIGHTS, HAZARDS, HORN, TRUNK.")

    # 5) confirmation gate for car-opening actions
    pretty = command.replace("_", " ")
    if command in tc.CONFIRM_REQUIRED and not confirmed:
        return _twiml(f"Reply 'TOYOTA {pretty.upper()} YES' to confirm {pretty}.")

    # 6) simple per-sender rate limit
    now = time.time()
    if now - _last_cmd_at.get(sender, 0) < RATE_LIMIT_SECONDS:
        return _twiml("One moment — still processing your last command.")
    _last_cmd_at[sender] = now

    # 7) do it (UI tap)
    try:
        since_ms = tc.execute(command)
    except tc.NotLoggedIn:
        return _twiml(
            "The car app is signed out and needs to be re-authenticated before I can send commands. "
            "The operator has been alerted."
        )
    except tc.ControlError as e:
        return _twiml(f"Couldn't send {pretty}: {e}")

    # 8) report the REAL result from Toyota's push notification (authoritative), not the UI spinner
    result = tc.await_result(since_ms)
    if result and result.get("text"):
        return _twiml(result["text"])
    return _twiml(
        f"Sent '{pretty}' to the car. No confirmation came back within "
        f"{tc.RESULT_TIMEOUT}s — the command may still be completing."
    )
