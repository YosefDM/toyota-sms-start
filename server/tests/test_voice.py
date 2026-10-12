"""Tests for the voice (phone-call) control path — code table, TwiML, tones, limiter, jobs.

Offline and device-free, like the rest of the suite: everything here lives in `voice.py` precisely
so it can be tested without a phone call, an emulator, or FastAPI installed.
"""
import io
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import toyota_control as tc  # noqa: E402
import voice as v  # noqa: E402

GOOD = "4821:start,7390:lock,5566:unlock"


class CodeTable(unittest.TestCase):
    def test_parses_and_normalises(self):
        self.assertEqual(v.parse_codes(GOOD),
                         {"4821": "start", "7390": "lock", "5566": "unlock"})

    def test_whitespace_and_trailing_comma_are_tolerated(self):
        self.assertEqual(v.parse_codes(" 4821 : start , 7390:LOCK ,"),
                         {"4821": "start", "7390": "lock"})

    def test_empty_is_an_empty_table_not_an_error(self):
        # An unset VOICE_CODES means "the phone path isn't set up", which app.py reports as such.
        self.assertEqual(v.parse_codes(""), {})
        self.assertEqual(v.code_length({}), 0)

    def test_non_digit_code_rejected(self):
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("48a1:start")

    def test_short_code_rejected(self):
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("482:start")

    def test_command_outside_the_phone_set_rejected(self):
        # The whole point of VOICE_ALLOWED_COMMANDS: a keypad code must not reach the trunk.
        for entry in ("4821:unlock_trunk", "4821:horn", "4821:status", "4821:nonsense"):
            with self.assertRaises(v.CodeTableError, msg=entry):
                v.parse_codes(entry)

    def test_duplicate_code_rejected(self):
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("4821:start,4821:lock")

    def test_mixed_lengths_rejected(self):
        # <Gather> ends on numDigits alone, so one table cannot hold two lengths.
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("4821:start,73901:lock")

    def test_malformed_entry_rejected(self):
        for entry in ("4821", "4821:", ":start"):
            with self.assertRaises(v.CodeTableError, msg=entry):
                v.parse_codes(entry)

    def test_code_length_is_the_shared_length(self):
        self.assertEqual(v.code_length(v.parse_codes(GOOD)), 4)


class Lookup(unittest.TestCase):
    def setUp(self):
        self.table = v.parse_codes(GOOD)

    def test_hit(self):
        self.assertEqual(v.lookup(self.table, "7390"), "lock")

    def test_miss(self):
        self.assertIsNone(v.lookup(self.table, "0000"))

    def test_empty_and_none_miss(self):
        self.assertIsNone(v.lookup(self.table, ""))
        self.assertIsNone(v.lookup(self.table, None))

    def test_surrounding_whitespace_still_matches(self):
        self.assertEqual(v.lookup(self.table, " 4821 "), "start")

    def test_prefix_and_extra_digits_do_not_match(self):
        # A handset that sends an extra tone must not actuate anything.
        self.assertIsNone(v.lookup(self.table, "482"))
        self.assertIsNone(v.lookup(self.table, "48211"))

    def test_empty_table_matches_nothing(self):
        self.assertIsNone(v.lookup({}, "4821"))


class PhonePathInvariants(unittest.TestCase):
    """Cross-module guards: the phone path's safety rests on these two facts holding."""

    def test_every_phone_command_is_a_real_command(self):
        for command in v.VOICE_ALLOWED_COMMANDS:
            self.assertIn(command, tc.COMMAND_IDS, command)

    def test_unlock_still_needs_its_confirmation_word_on_sms(self):
        # The VOICE path deliberately has no confirm keypress (a speed dial would autosend it, so
        # it distinguished nothing). That decision must not bleed into SMS, where "UNLOCK" is a
        # word a fat finger can produce and there is no per-command secret to make it deliberate.
        self.assertIn("unlock", tc.CONFIRM_REQUIRED)
        self.assertIn("unlock_trunk", tc.CONFIRM_REQUIRED)

    def test_no_phone_command_lacks_a_success_code(self):
        # The voice path answers in tones, so it must be able to DECIDE done vs failed for
        # everything it can be asked to do (see tc.result_is_success returning None).
        for command in v.VOICE_ALLOWED_COMMANDS:
            self.assertIsNotNone(tc.result_is_success(command, ""), command)


class SuccessVerdict(unittest.TestCase):
    def test_own_success_code_is_success(self):
        self.assertIs(tc.result_is_success("lock", "The vehicle is now locked. [DL1]"), True)
        self.assertIs(tc.result_is_success(
            "start", "Vehicle was started and will automatically shut off in 10 minutes. [RES1]"),
            True)

    def test_a_refusal_push_is_not_success(self):
        # Toyota sends failures down the same channel; arrival is not an outcome.
        self.assertIs(tc.result_is_success("start", "Remote start failed because a keyfob was "
                                                    "detected in the vehicle. [70]"), False)

    def test_unknown_command_is_undecidable(self):
        self.assertIsNone(tc.result_is_success("horn", "anything"))
        self.assertIsNone(tc.result_is_success(None, "anything"))


class TwiML(unittest.TestCase):
    BASE = "https://car.example.com/voice"

    def test_gather_puts_the_prompt_inside_so_autosent_digits_are_heard(self):
        xml = v.gather(v.say_frag("Enter your 4 digit code."), self.BASE + "/code", 4)
        self.assertIn('numDigits="4"', xml)
        self.assertIn(f'action="{self.BASE}/code"', xml)
        self.assertIn('method="POST"', xml)
        # The <Say> must sit between <Gather> and </Gather>, not before it.
        self.assertRegex(xml, r"<Gather[^>]*><Say[^>]*>Enter your 4 digit code\.</Say></Gather>")

    def test_gather_after_fragment_follows_the_gather(self):
        xml = v.gather(v.say_frag("prompt"), self.BASE + "/code", 4,
                       after=v.say_frag("no input"))
        self.assertLess(xml.index("</Gather>"), xml.index("no input"))

    def test_no_hangup_verb_is_ever_emitted(self):
        # TextGrid's verb set has no <Hangup>; running out of document is the hangup.
        for xml in (v.say("bye"), v.tone("done", self.BASE),
                    v.gather(v.say_frag("p"), self.BASE + "/code", 4),
                    v.hold(None, self.BASE + "/wait", 5)):
            self.assertNotIn("Hangup", xml)

    def test_only_supported_verbs_are_emitted(self):
        supported = {"Response", "Say", "Gather", "Play", "Pause", "Redirect"}
        import re
        for xml in (v.say("bye"), v.tone("reject", self.BASE),
                    v.gather(v.tone_frag("reject", self.BASE), self.BASE + "/code", 4,
                             after=v.say_frag("x")),
                    v.hold(v.tone_frag("accepted", self.BASE), self.BASE + "/wait", 8)):
            for verb in re.findall(r"</?([A-Za-z]+)", xml.split("?>", 1)[1]):
                self.assertIn(verb, supported, xml)

    def test_hold_without_a_fragment_is_silent(self):
        xml = v.hold(None, self.BASE + "/wait", 5)
        self.assertNotIn("<Say", xml)
        self.assertNotIn("<Play", xml)
        self.assertIn('<Pause length="5"/>', xml)
        self.assertIn(f"<Redirect>{self.BASE}/wait</Redirect>", xml)

    def test_tone_url_and_fragment(self):
        self.assertEqual(v.tone_url("done", self.BASE), self.BASE + "/tone/done.wav")
        self.assertEqual(v.tone_url("done", self.BASE + "/"), self.BASE + "/tone/done.wav")
        self.assertIn(f"<Play>{self.BASE}/tone/accepted.wav</Play>",
                      v.tone_frag("accepted", self.BASE))

    def test_unknown_tone_raises_rather_than_playing_silence(self):
        with self.assertRaises(KeyError):
            v.tone_frag("nope", self.BASE)

    def test_text_and_urls_are_escaped(self):
        xml = v.gather(v.say_frag('5 < 6 & "quoted"'),
                       'https://h/voice/code?a=1&b="2"', 4)
        self.assertNotIn("<Say>5 < 6", xml)
        self.assertIn("&amp;", xml)
        self.assertIn("&lt;", xml)
        # The action is an attribute — its quotes must not end the attribute.
        self.assertNotIn('b="2""', xml)

    def test_every_response_is_one_well_formed_document(self):
        from xml.etree import ElementTree
        for xml in (v.say("bye"), v.tone("partial", self.BASE),
                    v.gather(v.say_frag("p"), self.BASE + "/code", 4, after=v.say_frag("a")),
                    v.hold(v.tone_frag("accepted", self.BASE), self.BASE + "/wait", 8)):
            root = ElementTree.fromstring(xml)
            self.assertEqual(root.tag, "Response")


class Tones(unittest.TestCase):
    def test_all_five_outcomes_have_a_tone(self):
        self.assertEqual(set(v.TONES), {"accepted", "done", "partial", "failed", "reject"})

    def test_each_is_a_playable_8k_mono_wav(self):
        for name, data in v.TONES.items():
            with wave.open(io.BytesIO(data)) as w:
                self.assertEqual(w.getnchannels(), 1, name)
                self.assertEqual(w.getsampwidth(), 2, name)
                self.assertEqual(w.getframerate(), v.SAMPLE_RATE, name)
                # Long enough to register on a phone speaker, short enough not to delay a hang-up.
                seconds = w.getnframes() / w.getframerate()
                self.assertGreater(seconds, 0.15, name)
                self.assertLess(seconds, 2.0, name)

    def test_the_two_tones_that_matter_are_not_the_same_sound(self):
        # "your code worked" and "the car did it" mean different things; they must not be
        # confusable by ear, so at minimum they must not be identical audio.
        self.assertNotEqual(v.TONES["accepted"], v.TONES["done"])
        self.assertEqual(len(set(v.TONES.values())), len(v.TONES))

    def test_samples_stay_inside_16_bit_range(self):
        # A clipped sine rasps over a phone line; the envelope exists to keep headroom.
        with wave.open(io.BytesIO(v.TONES["done"])) as w:
            frames = w.readframes(w.getnframes())
        peak = max(abs(int.from_bytes(frames[i:i + 2], "little", signed=True))
                   for i in range(0, len(frames), 2))
        self.assertLess(peak, 32767)
        self.assertGreater(peak, 8000)  # ...but still audible


class ThrottleTests(unittest.TestCase):
    def test_locks_out_after_the_limit_and_recovers(self):
        th = v.Throttle(max_failures=3, window=600, lockout=300)
        self.assertEqual(th.record_failure("+1555", now=100), 0)
        self.assertEqual(th.record_failure("+1555", now=101), 0)
        self.assertEqual(th.record_failure("+1555", now=102), 300)
        self.assertEqual(th.locked_for("+1555", now=102), 300)
        self.assertEqual(th.locked_for("+1555", now=250), 152)
        self.assertEqual(th.locked_for("+1555", now=500), 0)

    def test_failures_are_counted_across_calls_not_within_one(self):
        # Guessing by redialling must be as expensive as guessing in one call.
        th = v.Throttle(max_failures=2, window=600, lockout=300)
        th.record_failure("+1555", now=100)
        self.assertEqual(th.record_failure("+1555", now=400), 300)

    def test_failures_outside_the_window_are_forgotten(self):
        th = v.Throttle(max_failures=2, window=60, lockout=300)
        th.record_failure("+1555", now=100)
        self.assertEqual(th.record_failure("+1555", now=200), 0)

    def test_callers_are_tracked_separately(self):
        th = v.Throttle(max_failures=2, window=600, lockout=300)
        th.record_failure("+1555", now=100)
        self.assertEqual(th.record_failure("+1666", now=101), 0)

    def test_success_clears_the_record(self):
        th = v.Throttle(max_failures=2, window=600, lockout=300)
        th.record_failure("+1555", now=100)
        th.record_success("+1555")
        self.assertEqual(th.record_failure("+1555", now=101), 0)

    def test_not_locked_by_default(self):
        self.assertEqual(v.Throttle().locked_for("+1555"), 0)


class JobsTests(unittest.TestCase):
    def test_lifecycle(self):
        jobs = v.Jobs()
        jobs.start("CA1", "start")
        job = jobs.get("CA1")
        self.assertEqual(job["command"], "start")
        self.assertIsNone(job["outcome"])
        jobs.finish("CA1", "done", "The vehicle is now locked. [DL1]")
        self.assertEqual(jobs.get("CA1")["outcome"], "done")
        jobs.drop("CA1")
        self.assertIsNone(jobs.get("CA1"))

    def test_get_returns_a_copy(self):
        jobs = v.Jobs()
        jobs.start("CA1", "start")
        jobs.get("CA1")["outcome"] = "done"
        self.assertIsNone(jobs.get("CA1")["outcome"])

    def test_finishing_an_unknown_call_is_a_no_op(self):
        # The owner can hang up before the worker finishes; the TTL may already have swept it.
        v.Jobs().finish("gone", "done", "text")  # must not raise

    def test_expired_jobs_are_swept_on_write(self):
        jobs = v.Jobs(ttl=0)
        jobs.start("CA1", "start")
        jobs.start("CA2", "lock")  # the sweep runs here
        self.assertIsNone(jobs.get("CA1"))
        self.assertIsNotNone(jobs.get("CA2"))


if __name__ == "__main__":
    unittest.main()
