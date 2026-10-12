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

GOOD = "482100:start,739000:lock,556600:unlock"


class CodeTable(unittest.TestCase):
    def test_parses_and_normalises(self):
        self.assertEqual(v.parse_codes(GOOD),
                         {"482100": "start", "739000": "lock", "556600": "unlock"})

    def test_whitespace_and_trailing_comma_are_tolerated(self):
        self.assertEqual(v.parse_codes(" 482100 : start , 739000:LOCK ,"),
                         {"482100": "start", "739000": "lock"})

    def test_empty_is_an_empty_table_not_an_error(self):
        # An unset VOICE_CODES means "the phone path isn't set up", which app.py reports as such.
        self.assertEqual(v.parse_codes(""), {})
        self.assertEqual(v.code_length({}), 0)

    def test_non_digit_code_rejected(self):
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("48a100:start")

    def test_short_code_rejected(self):
        # 4 and 5 digits are now refused: against the real lockout (~480 guesses/day) a 4-digit
        # code falls in about three weeks, which is an attack someone would actually run.
        for code in ("482", "4821", "48210"):
            with self.assertRaises(v.CodeTableError, msg=code):
                v.parse_codes(f"{code}:start")

    def test_minimum_is_six_digits(self):
        self.assertEqual(v.MIN_CODE_LENGTH, 6)
        self.assertEqual(v.parse_codes("482100:start"), {"482100": "start"})

    def test_command_outside_the_phone_set_rejected(self):
        # The whole point of VOICE_ALLOWED_COMMANDS: a keypad code must not reach the trunk.
        for entry in ("482100:unlock_trunk", "482100:horn", "482100:status",
                      "482100:nonsense"):
            with self.assertRaises(v.CodeTableError, msg=entry):
                v.parse_codes(entry)

    def test_duplicate_code_rejected(self):
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("482100:start,482100:lock")

    def test_mixed_lengths_rejected(self):
        # <Gather> ends on numDigits alone, so one table cannot hold two lengths.
        with self.assertRaises(v.CodeTableError):
            v.parse_codes("482100:start,7390010:lock")

    def test_malformed_entry_rejected(self):
        for entry in ("482100", "482100:", ":start"):
            with self.assertRaises(v.CodeTableError, msg=entry):
                v.parse_codes(entry)

    def test_code_length_is_the_shared_length(self):
        self.assertEqual(v.code_length(v.parse_codes(GOOD)), 6)


class Lookup(unittest.TestCase):
    def setUp(self):
        self.table = v.parse_codes(GOOD)

    def test_hit(self):
        self.assertEqual(v.lookup(self.table, "739000"), "lock")

    def test_miss(self):
        self.assertIsNone(v.lookup(self.table, "000000"))

    def test_empty_and_none_miss(self):
        self.assertIsNone(v.lookup(self.table, ""))
        self.assertIsNone(v.lookup(self.table, None))

    def test_surrounding_whitespace_still_matches(self):
        self.assertEqual(v.lookup(self.table, " 482100 "), "start")

    def test_prefix_and_extra_digits_do_not_match(self):
        # A handset that sends an extra tone must not actuate anything.
        self.assertIsNone(v.lookup(self.table, "48210"))
        self.assertIsNone(v.lookup(self.table, "4821001"))

    def test_empty_table_matches_nothing(self):
        self.assertIsNone(v.lookup({}, "482100"))


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
    def test_every_outcome_has_a_tone(self):
        self.assertEqual(set(v.TONES),
                         {"accepted", "done", "partial", "failed", "reject", "retrying"})

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


class UnsecuredRefusal(unittest.TestCase):
    """The signal the auto-lock-and-retry hangs off (observed live: "…vehicle is secured… [D1]")."""

    def test_matches_the_observed_refusal(self):
        for text in ("Remote command failed - ensure the vehicle is secured. [D1]",
                     "Remote command failed — ensure the vehicle is secured… [D1]",
                     "The vehicle is not secured.",
                     "ENSURE THE VEHICLE IS SECURED"):
            self.assertTrue(tc.refused_as_unsecured(text), text)

    def test_does_not_match_other_refusals(self):
        # Locking the car does nothing about a keyfob left inside, and retrying would just refuse
        # again — so this must stay narrow or the retry fires on failures it cannot fix.
        for text in ("Remote start failed because a keyfob was detected in the vehicle. [70]",
                     "Vehicle was started and will automatically shut off in 10 minutes. [RES1]",
                     "The vehicle is now locked. [DL1]",
                     ""):
            self.assertFalse(tc.refused_as_unsecured(text), text)


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

    def test_rotating_callers_cannot_reset_the_budget_forever(self):
        """The finding this closes: per-caller counting alone is multiplied by the number of
        allowlisted numbers, and the attacker picks which one to present."""
        th = v.Throttle(max_failures=5, window=900, lockout=900, global_max_failures=8)
        locked = 0
        # eight wrong codes spread over eight DIFFERENT callers: no per-caller limit is ever hit
        for i in range(8):
            locked = th.record_failure(f"+1555000{i:04d}", now=100 + i)
        self.assertEqual(locked, 900, "the global cap should have fired")
        # ...and a fresh, never-seen caller is now refused too (the clock started on the 8th
        # failure at now=107, so a few seconds have already elapsed by now=110)
        self.assertGreater(th.locked_for("+15559999999", now=110), 880)

    def test_the_global_cap_sits_above_the_per_caller_one(self):
        # With a single allowlisted number the per-caller limit must always trip first, so the
        # owner never meets the global one.
        th = v.Throttle()
        self.assertGreater(th.global_max_failures, th.max_failures)

    def test_a_correct_code_clears_the_global_counter_too(self):
        # Otherwise a few fumbled calls over a day could accumulate into a global lockout.
        th = v.Throttle(max_failures=5, window=900, lockout=900, global_max_failures=3)
        th.record_failure("+15551234567", now=100)
        th.record_failure("+15559999999", now=101)
        th.record_success("+15551234567")
        self.assertEqual(th.record_failure("+15558888888", now=102), 0)

    def test_global_failures_also_age_out(self):
        th = v.Throttle(max_failures=5, window=60, lockout=900, global_max_failures=2)
        th.record_failure("+15551111111", now=100)
        self.assertEqual(th.record_failure("+15552222222", now=200), 0)

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

    def test_stage_is_recorded_and_sounds_only_once(self):
        jobs = v.Jobs()
        jobs.start("CA1", "start")
        self.assertEqual(jobs.get("CA1")["stage"], "")
        jobs.set_stage("CA1", "retrying")
        self.assertEqual(jobs.get("CA1")["stage"], "retrying")
        self.assertFalse(jobs.get("CA1")["stage_played"])
        jobs.mark_stage_played("CA1")
        self.assertTrue(jobs.get("CA1")["stage_played"])
        # Re-asserting the same stage must not re-arm the tone, or the hold loop would play it on
        # every pass and train the owner to ignore it.
        jobs.set_stage("CA1", "retrying")
        self.assertTrue(jobs.get("CA1")["stage_played"])

    def test_stage_on_an_unknown_call_is_a_no_op(self):
        v.Jobs().set_stage("gone", "retrying")      # must not raise
        v.Jobs().mark_stage_played("gone")

    def test_expired_jobs_are_swept_on_write(self):
        jobs = v.Jobs(ttl=0)
        jobs.start("CA1", "start")
        jobs.start("CA2", "lock")  # the sweep runs here
        self.assertIsNone(jobs.get("CA1"))
        self.assertIsNotNone(jobs.get("CA2"))


if __name__ == "__main__":
    unittest.main()
