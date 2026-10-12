# SMS bridge

Inbound SMS **or phone call** → verify sender → tap the Toyota app's button (UI only) → reply.

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

## Call the number instead (speed dial)

The point of this path is **one keypress in the morning**. Store the number in your phone with the
code appended after comma pauses, put that contact on a speed dial, and pressing it starts the car:

```
+15550001234,,4821
```

Each `,` is a pause before the digits are autosent as DTMF, and the handset starts counting them
**when the call is answered**, not when it starts dialling — so there's no ringing to wait out, only
the ~1-2s for the `<Gather>` to open. Two commas (4-6s, since a comma is 2s on most handsets and 3s
on some) is the right starting point. If no tone comes back at all, the digits went out before
anything was listening: add one. The prompt is played *inside* the `<Gather>` precisely so an early-but-not-too-early
code keyed over the greeting is still heard. `;` (wait for me) works on some phones if you'd rather
press send yourself.

Unlock is one press too — **no** "press 1 to confirm" step. On SMS that confirmation word earns
its keep, but on a speed dial the confirming keypress would live in the same contact as the code
and be autosent by the same single press, so it could not tell a deliberate unlock from an
accidental one. Here the per-command secret is what makes it deliberate: each command has its own
code, so keying the unlock code is already an unambiguous choice.

### The call answers in tones, not words

Nothing about the car is ever spoken out loud — a phone at your ear in a driveway, or a voicemail,
shouldn't announce "the vehicle is now unlocked". Two tones matter:

| Tone | Sound | Means |
|---|---|---|
| `accepted` | two quick rising beeps | your code was right; the command is on its way |
| `done` | three rising beeps | Toyota confirmed the car did it |
| `partial` | two flat beeps | it went out, but no confirmation came back — **state unknown** |
| `failed` | two falling beeps | the car or the app refused it |
| `reject` | one low buzz | that code isn't recognised |

**You can hang up on the first tone.** The command runs in a background thread, so it completes
whether or not the call is still up; the second tone is a courtesy, never the mechanism. A
`partial` right after a start is the signature of the FCM push outage — the fix is in
`docs/PRODUCTION.md`.

The spoken lines that remain ("this number isn't authorized", "too many incorrect codes") say
nothing about the car, and exist so a misconfigured line is diagnosable by ear.

### Setup

1. Put a code per command in `VOICE_CODES` (`server/.env`). Digits only, 4+, **all the same
   length** — TextGrid's `<Gather>` can only end on `numDigits`, so there's no `#` to finish on.
   Only `start`, `lock` and `unlock` are reachable by phone; the trunk, horn, lights, hazards and
   buzzer stay SMS-only on purpose.
2. Set the number's **voiceUrl** (HTTP POST) to `https://<your-host>/voice` and put that exact URL
   in `VOICE_PUBLIC_URL` — it's the signed URL *and* the base for the `<Gather>`/`<Play>` URLs.
3. Call it. The log line to look for is `inbound call from NNNN`.

The tone audio is five sine beeps synthesised at import (`voice.py`) and served from memory at
`/voice/tone/<name>.wav` — no audio files in the repo, nothing to go stale. That path is
deliberately unauthenticated: it's what TextGrid's media fetcher pulls, and there's no information
in a beep.

### Why a call, given SMS already works

Texting a keyword is five or six keypresses on a numeric keypad plus a wait. A speed dial is one,
and it's the gesture you want while still half asleep. The SMS path remains the one with words:
`STATUS`, the full nine commands, and a result you can read.

## Health

`GET /health` → `{"ok": true, "device_online": true}` — handy for the watchdog.

## Notes

- `toyota_control.py` holds the keyword→command map and finds each button by **resource-id** with the guard
  model (resolution-independent); `textgrid_auth.py` verifies the inbound signature.
- The emulator + app + Frida session must already be up and logged in (see the repo root `docs/`). This
  service only performs the taps; it does not boot the emulator or log in.
- Hard rule: taps only, never a direct API call to the car.
