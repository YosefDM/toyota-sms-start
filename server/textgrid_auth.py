"""Verify TextGrid inbound-webhook signatures.

TextGrid is Twilio-API-compatible for *sending*, but its inbound webhook signature
is a DIFFERENT scheme from Twilio's, so `twilio-python`'s RequestValidator does not
work here (it fails for two independent reasons: wrong header name, and it signs the
sorted params keyed on the auth token). This logic is ported from the proven smspilot
implementation (its `app/services/twilio_validation.py`), which was confirmed against
live TextGrid traffic.

Scheme:
  X-TextGrid-Signature = base64( HMAC-SHA1( webhook_secret, smsUrl + body ) )

where:
  - `webhook_secret` is the account's Webhook Secret (NOT the auth token),
  - `smsUrl` is the exact URL configured on the number (our PUBLIC_URL), and
  - `body` is the raw request body, verbatim and unsorted, with every non-ASCII
    code unit escaped as \\uXXXX (see `encode_non_ascii`).

The raw body must be captured BEFORE the form is parsed — form parsing consumes the
stream. app.py reads `await request.body()` first for exactly this reason.
"""
from __future__ import annotations
import base64
import hashlib
import hmac


def encode_non_ascii(value: str) -> str:
    """Escape every code unit above 127 as a literal ``\\uXXXX``.

    Ports TextGrid's ``EncodeNonAsciiCharacters``. Their reference is C# and iterates
    UTF-16 code units, so an astral char (e.g. an emoji) becomes TWO escapes, one per
    surrogate half — hence we encode to UTF-16-BE first rather than iterating Python's
    str (which would emit a single escape and never match). ASCII-only input (the
    ordinary case for percent-encoded form bodies, and for plain keyword SMS) is
    returned unchanged, making this a no-op on the hot path.
    """
    if value.isascii():
        return value
    out = []
    raw = value.encode("utf-16-be")  # -be suppresses the BOM
    for i in range(0, len(raw), 2):
        unit = (raw[i] << 8) | raw[i + 1]
        out.append(chr(unit) if unit <= 127 else f"\\u{unit:04x}")
    return "".join(out)


def verify_signature(secret: str, signed_url: str, raw_body: bytes, header_sig: str) -> bool:
    """True iff ``header_sig`` is a valid X-TextGrid-Signature for this request.

    - Fails OPEN when ``secret`` is empty: there's no way to validate without it, and an
      unset secret is a deployment concern, not a request to silently drop. (The sender
      allowlist in app.py is the other line of defense; set the secret in production.)
    - Fails CLOSED when a secret is set but the header is missing or wrong.

    ``signed_url`` must equal the smsUrl configured on the TextGrid number exactly
    (scheme, host, path, any query) — that is our PUBLIC_URL.
    """
    if not secret:
        return True
    if not header_sig:
        return False

    try:
        payload = encode_non_ascii(raw_body.decode("utf-8"))
    except UnicodeDecodeError:
        payload = None  # not UTF-8 — sign the bytes as-is rather than mangle them

    string_to_sign = (
        signed_url.encode("utf-8") + raw_body if payload is None
        else (signed_url + payload).encode("utf-8")
    )
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha1).digest()
    )
    # Compare as bytes: a non-ASCII header byte would make compare_digest raise on a
    # str, and Werkzeug/Starlette decode headers as latin-1. A non-ASCII signature is
    # never valid base64 anyway — it just has to compare False, not explode.
    return hmac.compare_digest(expected, header_sig.encode("utf-8", "replace"))
