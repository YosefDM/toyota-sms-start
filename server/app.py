"""
Twilio inbound-SMS webhook → tap the Toyota app's button. UI-only (see toyota_control).

Security:
  - verifies Twilio's request signature (X-Twilio-Signature)
  - allowlists sender numbers (ALLOWED_NUMBERS)
  - requires a confirmation word for car-opening commands (unlock / unlock trunk)
Run behind HTTPS (Twilio requires it); expose via a reverse proxy or Cloudflare Tunnel.

Env (see .env.example):
  TWILIO_AUTH_TOKEN   — to validate request signatures
  PUBLIC_URL          — the exact public https URL Twilio calls (for signature validation)
  ALLOWED_NUMBERS     — comma-separated E.164 numbers permitted to command the car
  ADB_PATH, ADB_SERIAL, HOLD_MS — passed through to toyota_control
"""
from __future__ import annotations
import os
import time

from fastapi import FastAPI, Form, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

import toyota_control as tc

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # e.g. https://car.example.com/sms
ALLOWED_NUMBERS = {n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()}
RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT_SECONDS", "8"))

app = FastAPI(title="Toyota SMS Start")
_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None
_last_cmd_at: dict[str, float] = {}


def _reply(text: str) -> Response:
    tw = MessagingResponse()
    tw.message(text)
    return Response(content=str(tw), media_type="application/xml")


@app.get("/health")
def health():
    return {"ok": True, "device_online": tc.device_online()}


@app.post("/sms")
async def sms(request: Request, From: str = Form(""), Body: str = Form("")):
    # 1) verify this is really Twilio calling the URL we expect
    if _validator is not None:
        form = dict((await request.form()).items())
        sig = request.headers.get("X-Twilio-Signature", "")
        if not PUBLIC_URL or not _validator.validate(PUBLIC_URL, form, sig):
            return Response(status_code=403, content="bad signature")

    # 2) allowlist the sender
    if ALLOWED_NUMBERS and From not in ALLOWED_NUMBERS:
        return _reply("Sorry, this number isn't authorized.")

    # 3) parse
    command, confirmed = tc.parse_command(Body)
    if not command:
        return _reply("Unknown command. Try: START, LOCK, UNLOCK, LIGHTS, HAZARDS, HORN, TRUNK.")

    # 4) confirmation gate for car-opening actions
    if command in tc.CONFIRM_REQUIRED and not confirmed:
        pretty = command.replace("_", " ")
        return _reply(f"Reply 'TOYOTA {pretty.upper()} YES' to confirm {pretty}.")

    # 5) simple per-sender rate limit
    now = time.time()
    if now - _last_cmd_at.get(From, 0) < RATE_LIMIT_SECONDS:
        return _reply("One moment — still processing your last command.")
    _last_cmd_at[From] = now

    # 6) do it (UI tap)
    pretty = command.replace("_", " ")
    try:
        since_ms = tc.execute(command)
    except tc.NotLoggedIn:
        return _reply(
            "The car app is signed out and needs to be re-authenticated before I can send commands. "
            "The operator has been alerted."
        )
    except tc.ControlError as e:
        return _reply(f"Couldn't send {pretty}: {e}")

    # 7) report the REAL result from Toyota's push notification (authoritative), not the UI spinner
    result = tc.await_result(since_ms)
    if result and result.get("text"):
        return _reply(result["text"])
    return _reply(
        f"Sent '{pretty}' to the car. No confirmation came back within "
        f"{tc.RESULT_TIMEOUT}s — the command may still be completing."
    )
