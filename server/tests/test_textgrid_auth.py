"""Tests for TextGrid inbound-webhook signature verification."""
import base64
import hashlib
import hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import textgrid_auth as tg  # noqa: E402

SECRET = "whsec_example"
URL = "https://car.example.com/sms"
BODY = b"From=%2B15551234567&Body=TOYOTA+UNLOCK+YES&To=%2B15559998888"


def _reference_sig(secret: str, url: str, body: bytes) -> str:
    """Independent re-computation of the expected signature (ASCII body)."""
    string_to_sign = (url + body.decode("utf-8")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class Signature(unittest.TestCase):
    def test_valid_signature_passes(self):
        sig = _reference_sig(SECRET, URL, BODY)
        self.assertTrue(tg.verify_signature(SECRET, URL, BODY, sig))

    def test_tampered_body_fails(self):
        sig = _reference_sig(SECRET, URL, BODY)
        tampered = BODY.replace(b"UNLOCK", b"START")
        self.assertFalse(tg.verify_signature(SECRET, URL, tampered, sig))

    def test_wrong_url_fails(self):
        sig = _reference_sig(SECRET, URL, BODY)
        self.assertFalse(tg.verify_signature(SECRET, "https://evil.example/sms", BODY, sig))

    def test_wrong_secret_fails(self):
        sig = _reference_sig("other", URL, BODY)
        self.assertFalse(tg.verify_signature(SECRET, URL, BODY, sig))

    def test_missing_header_fails_closed_when_secret_set(self):
        self.assertFalse(tg.verify_signature(SECRET, URL, BODY, ""))

    def test_no_secret_is_a_noop_primitive(self):
        # With no secret there's nothing to verify against, so the primitive returns True.
        # This is NOT "trusted": app.py refuses unsigned requests by default (503) — see app.py.
        self.assertTrue(tg.verify_signature("", URL, BODY, ""))

    def test_non_ascii_header_does_not_explode(self):
        # A non-ASCII signature can never be valid base64; it must compare False, not raise.
        self.assertFalse(tg.verify_signature(SECRET, URL, BODY, "sig-with-é"))


class EncodeNonAscii(unittest.TestCase):
    def test_ascii_unchanged(self):
        self.assertEqual(tg.encode_non_ascii("TOYOTA LOCK"), "TOYOTA LOCK")

    def test_bmp_char_escaped(self):
        self.assertEqual(tg.encode_non_ascii("é"), "\\u00e9")

    def test_astral_char_is_two_surrogate_escapes(self):
        # 😀 U+1F600 → UTF-16 surrogate pair d83d de00 (matches the C# char-iteration).
        self.assertEqual(tg.encode_non_ascii("😀"), "\\ud83d\\ude00")


if __name__ == "__main__":
    unittest.main()
