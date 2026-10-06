# SMS bridge

Inbound SMS → verify sender → tap the Toyota app's button (UI only) → reply.

## Run

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env    # fill in TEXTGRID_WEBHOOK_SECRET, PUBLIC_URL, ALLOWED_NUMBERS
uvicorn app:app --host 127.0.0.1 --port 8080
```

Expose it over HTTPS via a Cloudflare/ngrok tunnel, then set that public URL as the **smsUrl** (HTTP POST)
on your TextGrid number, and put the exact same URL in `PUBLIC_URL` (it's part of the signed payload, so it
must match byte-for-byte).

## Commands

Text your TextGrid number (a leading `TOYOTA` is optional):

| Text | Action |
|---|---|
| `STATUS` | read-only report: range, tire pressure, doors, windows, trunk |
| `HELP` (or `?`) | the command list |
| `START` | remote start |
| `LOCK` | lock doors |
| `UNLOCK YES` | unlock doors (confirmation word required) |
| `LIGHTS` | flash lights |
| `HAZARDS` | hazards |
| `HORN` | horn |
| `BUZZER` | buzzer |
| `TRUNK YES` | unlock trunk (confirmation required) |
| `LOCK TRUNK` | lock trunk |

Car-opening actions (`UNLOCK`, `TRUNK`) require a confirmation word (`YES`/`CONFIRM`) to avoid accidents.
Senders not in `ALLOWED_NUMBERS` are rejected. Requests are signature-verified against
`TEXTGRID_WEBHOOK_SECRET` (TextGrid's `X-TextGrid-Signature` scheme — see `textgrid_auth.py`).

## Health

`GET /health` → `{"ok": true, "device_online": true}` — handy for the watchdog.

## Notes

- `toyota_control.py` holds the keyword→command map and finds each button by **resource-id** with the guard
  model (resolution-independent); `textgrid_auth.py` verifies the inbound signature.
- The emulator + app + Frida session must already be up and logged in (see the repo root `docs/`). This
  service only performs the taps; it does not boot the emulator or log in.
- Hard rule: taps only, never a direct API call to the car.
