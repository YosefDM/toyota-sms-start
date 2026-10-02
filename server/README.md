# SMS bridge

Inbound SMS → verify sender → tap the Toyota app's button (UI only) → reply.

## Run

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env    # fill in TWILIO_AUTH_TOKEN, PUBLIC_URL, ALLOWED_NUMBERS
uvicorn app:app --host 0.0.0.0 --port 8000
```

Expose it over HTTPS (Twilio requires https) via a reverse proxy or a Cloudflare/ngrok tunnel, then set that
public URL as the **"A message comes in"** webhook (HTTP POST) on your Twilio number, and put the same URL in
`PUBLIC_URL`.

## Commands

Text your Twilio number (a leading `TOYOTA` is optional):

| Text | Action |
|---|---|
| `START` | remote start |
| `LOCK` | lock doors |
| `UNLOCK YES` | unlock doors (confirmation word required) |
| `LIGHTS` | flash lights |
| `HAZARDS` | hazards |
| `HORN` | horn |
| `TRUNK YES` | unlock trunk (confirmation required) |

Car-opening actions (`UNLOCK`, `TRUNK`) require a confirmation word (`YES`/`CONFIRM`) to avoid accidents.
Senders not in `ALLOWED_NUMBERS` are rejected. Requests are signature-verified against `TWILIO_AUTH_TOKEN`.

## Health

`GET /health` → `{"ok": true, "device_online": true}` — handy for the watchdog.

## Notes

- `toyota_control.py` holds the keyword→command map and the button coordinates (pixel_7 / 1080px / software
  GPU). Re-dump and update if the app layout changes.
- The emulator + app + Frida session must already be up and logged in (see the repo root `docs/`). This
  service only performs the taps; it does not boot the emulator or log in.
- Hard rule: taps only, never a direct API call to the car.
