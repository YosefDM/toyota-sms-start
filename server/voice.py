"""Voice (phone-call) control path: press one speed-dial key, the car starts.

The owner carries a basic phone, so this is the second control channel alongside SMS. The intended
gesture is a *speed dial*: a contact stored as ``+1845XXXXXXX,,,,,4821`` — the handset dials, waits
out the comma pauses, autosends the code as DTMF, and the engine starts. No typing, one keypress.
The taps themselves still go through `toyota_control` — UI only, never an API call to the car.

## The call says nothing out loud about the car

Confirmations are **tones, not speech**. A phone held to the ear in a driveway, or a voicemail, or
anyone standing nearby, should not hear "the vehicle is now unlocked" — the spoken result is a
state disclosure the owner explicitly doesn't want. So:

  - `accepted` — your code was recognised and the command is on its way to the car
  - `done`     — Toyota confirmed the car did it
  - `partial`  — the app accepted it but no confirmation came back (e.g. FCM down) — unknown
  - `failed`   — the car or the app refused it
  - `reject`   — that code isn't recognised

The owner only has to learn the first two. Speech survives only for things that say nothing about
the car: "not authorized", "not configured", a lockout. Every tone is synthesised here at import
(stdlib `wave`, no audio files in the repo) and served from memory by app.py's `/voice/tone/*`.

**Nothing depends on the second tone.** The command runs on a background thread, so it completes
whether or not the call is still up: hanging up right after `accepted` is a supported, normal way
to use this — the `done`/`partial`/`failed` tone is a courtesy, not the mechanism.

## What TextGrid's voice API actually supports (from their "Breeze Voice API" doc)

Twilio-compatible in shape but a strict SUBSET, and these limits drive the design:

- **Only `<Say>`, `<Dial>`, `<Gather>`, `<Play>`, `<Pause>`, `<Redirect>`.** There is **no
  `<Hangup>`** — a call ends by letting the TwiML document run out, which is why `terminal()`
  simply emits its fragments and stops.
- **`<Gather>` is documented with `action`, `numDigits` and `timeout` only** — no `finishOnKey`.
  So there is no "#" to end the digits and every code must be the SAME length, closing the gather
  on `numDigits` alone. `parse_codes` enforces that, and it is also what makes a speed-dial string
  work: the handset just blurts the digits and the gather closes itself.
- The prompt goes INSIDE the `<Gather>` (as in their own example) so an autosent code keyed over
  the greeting is still captured — barge-in is the whole point here.
- **Voices** are `man`, `female` or `Polly.*`; `alice` appears only in their example and not in
  their supported list, so the default is `Polly.Joanna`.
- The doc specifies the *outbound* call flow; inbound (a number's voiceUrl) is undocumented but
  requests the same TwiML with the same Twilio-style params (`CallSid`, `From`, `Digits`, ...).
- It says **nothing about signing inbound voice webhooks**, so unlike `/sms` the signature may
  never arrive — see `VOICE_REQUIRE_SIGNATURE` in app.py.
"""
from __future__ import annotations
import hmac
import io
import logging
import math
import os
import re
import struct
import threading
import time
import wave
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger("toyota_voice")

# Commands reachable BY PHONE — deliberately only the three core controls, not all nine.
# A code keyed on a phone keypad is a short secret that can be shoulder-surfed or guessed, and on a
# speed dial it is stored in the handset's contact list in clear text, so the phone path exposes
# the smallest useful set; hazards, horn, buzzer, lights and the trunk stay SMS-only. `unlock` is
# here but still passes through `toyota_control.CONFIRM_REQUIRED`, so it costs a second, separate
# keypress (see app.py).
VOICE_ALLOWED_COMMANDS = ("start", "lock", "unlock")

# A code shorter than this is not a secret. Four digits is 10k guesses; the attempt limiter below
# is what actually makes that unprofitable, so keep both.
MIN_CODE_LENGTH = 4

SAY_VOICE = os.environ.get("VOICE_TTS_VOICE", "Polly.Joanna")
SAY_LANGUAGE = os.environ.get("VOICE_TTS_LANGUAGE", "en-US")


class CodeTableError(Exception):
    """VOICE_CODES is malformed — refuse to serve the voice path rather than guess."""


def parse_codes(raw: str) -> dict[str, str]:
    """Parse ``VOICE_CODES`` (``"4821:start,7390:lock"``) into {digits: command}.

    Raises `CodeTableError` on anything ambiguous or unsafe, because the failure mode of a
    silently-dropped entry is a code that does nothing, and of a silently-kept bad one is a code
    that opens a car. All codes must share one length — see the module docstring on `numDigits`.
    """
    table: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        code, sep, command = entry.partition(":")
        code, command = code.strip(), command.strip().lower().replace(" ", "_")
        if not sep or not code or not command:
            raise CodeTableError(f"entry {entry!r} is not 'digits:command'")
        if not re.fullmatch(r"\d+", code):
            raise CodeTableError(f"code {code!r} must be digits only — it is keyed on a phone keypad")
        if len(code) < MIN_CODE_LENGTH:
            raise CodeTableError(f"code for {command!r} is only {len(code)} digits; "
                                 f"use at least {MIN_CODE_LENGTH}")
        if command not in VOICE_ALLOWED_COMMANDS:
            raise CodeTableError(
                f"{command!r} is not reachable by phone (allowed: "
                f"{', '.join(VOICE_ALLOWED_COMMANDS)})")
        if code in table:
            raise CodeTableError(f"code {code!r} is assigned to both {table[code]!r} and {command!r}")
        table[code] = command
    if table and len({len(c) for c in table}) > 1:
        raise CodeTableError(
            "every code must have the SAME number of digits (TextGrid's <Gather> can only end on "
            "numDigits, so one fixed length has to be announced up front) — got lengths " +
            ", ".join(str(n) for n in sorted({len(c) for c in table})))
    return table


def code_length(table: dict[str, str]) -> int:
    """The single digit-length of every code in the table (0 when the table is empty)."""
    return len(next(iter(table))) if table else 0


def lookup(table: dict[str, str], digits: str) -> str | None:
    """The command for `digits`, or None.

    Compares against EVERY entry with `compare_digest` and keeps going after a hit: a plain dict
    lookup leaks through timing, and short-circuiting on the first match would make a correct code
    measurably faster than a wrong one — a side channel on a 4-digit secret is worth closing.
    """
    digits = (digits or "").strip()
    found: str | None = None
    for code, command in table.items():
        if hmac.compare_digest(code, digits):
            found = command
    return found


# --------------------------------------------------------------------------- tones

SAMPLE_RATE = 8000  # the telephone network's own rate; resampling only adds artefacts
_FADE_MS = 8        # a hard start/stop clicks on a phone speaker

# name -> sequence of (frequency Hz, duration ms). Chosen to be told apart by EAR, by someone
# half-awake in a driveway who never sees a screen: rising = good, flat = unknown, falling = bad.
TONE_SPECS: dict[str, tuple[tuple[int, int], ...]] = {
    # code accepted, command on its way — a quick two-note rise
    "accepted": ((880, 110), (1175, 110)),
    # the car confirmed it — an unmistakable three-note ascent
    "done": ((1047, 110), (1319, 110), (1568, 180)),
    # sent and accepted by the app, but the car never confirmed: two flat, neutral notes
    "partial": ((1047, 160), (1047, 160)),
    # refused — a two-note fall
    "failed": ((440, 180), (294, 260)),
    # that code isn't recognised — one low buzz, nothing cheerful about it
    "reject": ((196, 500),),
}
GAP_MS = 60


def _render_wav(beeps: tuple[tuple[int, int], ...], gap_ms: int = GAP_MS) -> bytes:
    """Synthesise a short mono 16-bit PCM WAV of `beeps` separated by silence.

    Generated at import rather than committed: a handful of sine beeps is a few lines of stdlib,
    and binary assets in a public repo are exactly the kind of thing that rots (and that the
    .gitignore already turns away).
    """
    frames = bytearray()
    for i, (freq, ms) in enumerate(beeps):
        if i:
            frames += b"\x00\x00" * int(SAMPLE_RATE * gap_ms / 1000)
        n = int(SAMPLE_RATE * ms / 1000)
        fade = max(1, int(SAMPLE_RATE * _FADE_MS / 1000))
        for s in range(n):
            # 0.6 of full scale: loud enough through a phone earpiece, with headroom so the
            # network's own compression doesn't clip it into a rasp.
            env = min(1.0, s / fade, (n - s) / fade)
            frames += struct.pack("<h", int(32767 * 0.6 * env *
                                            math.sin(2 * math.pi * freq * s / SAMPLE_RATE)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


TONES: dict[str, bytes] = {name: _render_wav(spec) for name, spec in TONE_SPECS.items()}


# --------------------------------------------------------------------------- TwiML rendering

def _response(*parts: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            + "".join(parts) + "</Response>")


def say_frag(text: str) -> str:
    """A `<Say>` fragment. Only for things that reveal NOTHING about the car."""
    return (f"<Say voice={quoteattr(SAY_VOICE)} language={quoteattr(SAY_LANGUAGE)}>"
            f"{escape(text)}</Say>")


def tone_frag(name: str, base_url: str) -> str:
    """A `<Play>` fragment for one of `TONES`, served by app.py at ``<base>/tone/<name>.wav``."""
    if name not in TONES:
        raise KeyError(f"no such tone: {name!r}")
    return f"<Play>{escape(tone_url(name, base_url))}</Play>"


def tone_url(name: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/tone/{name}.wav"


def terminal(*frags: str) -> str:
    """Emit these fragments and end the call.

    TextGrid has no `<Hangup>` verb, so running out of document IS the hangup.
    """
    return _response(*frags)


def say(text: str) -> str:
    """Speak one line and end the call (see `say_frag` on what may be spoken)."""
    return terminal(say_frag(text))


def tone(name: str, base_url: str) -> str:
    """Play one tone and end the call."""
    return terminal(tone_frag(name, base_url))


def gather(prompt: str, action: str, num_digits: int, timeout: int = 10,
           after: str = "") -> str:
    """Collect `num_digits` DTMF digits and POST them to `action`.

    `prompt` is a pre-rendered fragment (`say_frag` / `tone_frag`) played INSIDE the gather, so a
    speed-dial handset that autosends its digits over the greeting is still heard — see the module
    docstring. `after` is played only when nothing was keyed, and then the call ends.
    """
    return _response(
        f"<Gather action={quoteattr(action)} method=\"POST\" "
        f"numDigits=\"{int(num_digits)}\" timeout=\"{int(timeout)}\">{prompt}</Gather>",
        after,
    )


def hold(frag: str | None, action: str, pause: int) -> str:
    """Optionally play `frag`, wait `pause` seconds, then ask us again at `action`.

    This is how a 15-30s car command fits inside a live phone call: the webhook must answer in
    seconds, so the work runs on a background thread and the call is parked in a play/pause/redirect
    loop until the result lands (see `Jobs`). Later turns pass nothing — a tone repeating every few
    seconds would train the owner to ignore the tone that matters.
    """
    return _response(*([frag] if frag else []),
                     f'<Pause length="{int(pause)}"/>',
                     f"<Redirect>{escape(action)}</Redirect>")


# --------------------------------------------------------------------------- attempt limiting

class Throttle:
    """Counts wrong codes per caller and locks the path after too many.

    A 4-digit code is only as strong as the number of guesses allowed, and a phone call is a cheap,
    automatable guessing channel — so wrong codes are counted across calls (keyed on the caller),
    not just within one, and the lockout is deliberately long enough to make exhaustive guessing
    hopeless while staying short enough to not strand the owner who fat-fingered it.
    """

    def __init__(self, max_failures: int = 5, window: int = 900, lockout: int = 900):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked_for(self, caller: str, now: float | None = None) -> int:
        """Seconds of lockout left for this caller (0 when not locked)."""
        now = time.time() if now is None else now
        with self._lock:
            until = self._locked_until.get(caller, 0.0)
            return max(0, int(round(until - now))) if until > now else 0

    def record_failure(self, caller: str, now: float | None = None) -> int:
        """Note a wrong code. Returns the seconds of lockout now in force (0 if still allowed)."""
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._fails.get(caller, []) if now - t < self.window]
            recent.append(now)
            self._fails[caller] = recent
            if len(recent) >= self.max_failures:
                self._locked_until[caller] = now + self.lockout
                self._fails[caller] = []
                logger.warning("voice: locking out caller %s for %ds after %d wrong codes",
                               caller[-4:] or "?", self.lockout, len(recent))
                return self.lockout
            return 0

    def record_success(self, caller: str) -> None:
        with self._lock:
            self._fails.pop(caller, None)
            self._locked_until.pop(caller, None)


# --------------------------------------------------------------------------- in-call job store

class Jobs:
    """Outcomes of in-flight car commands, keyed by CallSid, for the hold loop to pick up.

    A job outlives the webhook request that created it (the work is on a background thread) and may
    outlive the CALL (the owner hangs up on the `accepted` tone, by design), so entries are swept
    on every write — otherwise a long-running rig accumulates one dead entry per call forever.
    """

    def __init__(self, ttl: int = 600):
        self.ttl = ttl
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        # `>=`, not `>`: with a ttl of 0 (meaning "don't keep these at all") two writes inside one
        # clock tick would otherwise never sweep, since the elapsed time is exactly 0.0.
        for key in [k for k, j in self._jobs.items() if now - j["created"] >= self.ttl]:
            del self._jobs[key]

    def start(self, call_sid: str, command: str) -> None:
        now = time.time()
        with self._lock:
            self._sweep(now)
            self._jobs[call_sid] = {"command": command, "created": now,
                                    "outcome": None, "text": ""}

    def finish(self, call_sid: str, outcome: str, text: str) -> None:
        """Record the verdict — one of the result tone names — plus the text, for the log."""
        with self._lock:
            job = self._jobs.get(call_sid)
            if job is not None:
                job["outcome"], job["text"] = outcome, text

    def get(self, call_sid: str) -> dict | None:
        with self._lock:
            return dict(self._jobs[call_sid]) if call_sid in self._jobs else None

    def drop(self, call_sid: str) -> None:
        with self._lock:
            self._jobs.pop(call_sid, None)
