"""Offline unit tests for toyota_control's parsing/matching logic (no device needed).

These cover the pieces most prone to a regex bug: SMS keyword parsing, uiautomator node
parsing, resource-id lookup, the shared-id trunk disambiguation, the `dumpsys notification`
parser, and result-matching (including rejecting a stale older notification). The adb layer is
monkeypatched, so nothing here touches an emulator.

Run:  python -m unittest discover -s server/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import toyota_control as tc  # noqa: E402

UI_XML = """<?xml version='1.0'?><hierarchy>
<node resource-id="com.toyota.oneapp:id/remote_engine_start_button" text="Start" content-desc="Start" bounds="[100,600][300,700]"/>
<node resource-id="com.toyota.oneapp:id/remote_door_lock_button" bounds="[350,600][550,700]"/>
<node resource-id="com.toyota.oneapp:id/remote_door_unlock_button" text="Unlock" content-desc="Unlock" bounds="[600,600][800,700]"/>
<node resource-id="com.toyota.oneapp:id/remote_trunk_lock_button" text="Lock Trunk" content-desc="Lock Trunk" bounds="[100,900][300,1000]"/>
<node resource-id="com.toyota.oneapp:id/remote_trunk_lock_button" text="Unlock Trunk" content-desc="Unlock Trunk" bounds="[400,900][600,1000]"/>
<node resource-id="com.toyota.oneapp:id/remote_lights_button" bounds="[100,1100][300,1200]"/>
<node resource-id="com.toyota.oneapp:id/remote_horn_button" bounds="[350,1100][550,1200]"/>
<node resource-id="com.toyota.oneapp:id/remote_buzzer_button" bounds="[600,1100][800,1200]"/>
<node resource-id="com.toyota.oneapp:id/remote_hazard_button" bounds="[850,1100][1050,1200]"/>
<node resource-id="com.toyota.oneapp:id/remote_progress_text" text="Tap and hold to activate" bounds="[0,500][1080,560]"/>
<node resource-id="android:id/content" content-desc="More Horizontal" bounds="[880,910][924,948]"/>
</hierarchy>"""

# The same panel while a remote start is running: the Start tile is REPLACED by a Stop tile. This is
# the state that used to make every command fail for ~10 minutes after a START.
UI_XML_ENGINE_RUNNING = UI_XML.replace(
    '<node resource-id="com.toyota.oneapp:id/remote_engine_start_button" text="Start" content-desc="Start" bounds="[100,600][300,700]"/>',
    '<node resource-id="com.toyota.oneapp:id/remote_engine_stop_button" text="Stop" content-desc="Stop" bounds="[100,600][300,700]"/>',
)
assert "remote_engine_stop_button" in UI_XML_ENGINE_RUNNING

DUMP = """NotificationManagerService state:
NotificationRecord(0x1a: pkg=com.google.android.gms user=0 key=0|com.google.android.gms|1|null|10100)
      mWhen=1696500000000
      android.title=Google Play services
      android.text=unrelated
NotificationRecord(0x2b: pkg=com.toyota.oneapp user=0 id=7 key=0|com.toyota.oneapp|7|null|12345)
      creationTime=1696599999123
      android.title=2025 Camry Hybrid
      android.text=String (The vehicle is now unlocked. [DL0])
NotificationRecord(0x3c: pkg=com.toyota.oneapp user=0 id=6 key=0|com.toyota.oneapp|6|null|12345)
      mWhen=1696500000000
      android.title=2025 Camry Hybrid
      android.text=The vehicle is now locked. [DL1]
"""


class ParseCommand(unittest.TestCase):
    def test_prefix_and_aliases(self):
        self.assertEqual(tc.parse_command("TOYOTA START"), ("start", False))
        self.assertEqual(tc.parse_command("engine"), ("start", False))
        self.assertEqual(tc.parse_command("open"), ("unlock", False))
        self.assertEqual(tc.parse_command("lock trunk"), ("lock_trunk", False))

    def test_confirmation_word(self):
        self.assertEqual(tc.parse_command("Toyota: Unlock YES"), ("unlock", True))
        self.assertEqual(tc.parse_command("unlock confirm"), ("unlock", True))

    def test_unknown(self):
        self.assertEqual(tc.parse_command("gibberish"), (None, False))

    def test_help_and_status(self):
        self.assertEqual(tc.parse_command("HELP")[0], "help")
        self.assertEqual(tc.parse_command("?")[0], "help")
        self.assertEqual(tc.parse_command("Toyota Status")[0], "status")
        self.assertEqual(tc.parse_command("stat")[0], "status")


STATUS_XML = """<?xml version='1.0'?><hierarchy>
<node resource-id="com.toyota.oneapp:id/dashboard_vehicle_name" text="Yosef's Camry" bounds="[0,100][720,160]"/>
<node resource-id="com.toyota.oneapp:id/fuel_view_range_value_text" text="200" bounds="[40,250][120,290]"/>
<node resource-id="com.toyota.oneapp:id/fuel_view_range_unit_text" text="mi" bounds="[130,250][170,290]"/>
<node text="Doors" bounds="[150,560][300,600]"/>
<node resource-id="com.toyota.oneapp:id/vehicle_status_door_tile_sub_title" text="Locked" bounds="[150,600][320,601]"/>
<node text="Windows" bounds="[150,700][330,740]"/>
<node resource-id="com.toyota.oneapp:id/vehicle_status_window_tile_sub_title" text="Closed" bounds="[150,740][320,741]"/>
<node text="Trunk" bounds="[150,850][280,890]"/>
<node resource-id="com.toyota.oneapp:id/vehicle_status_trunk_tile_sub_title" text="Closed" bounds="[150,890][320,891]"/>
<node text="Tire Pressure" bounds="[150,1000][420,1050]"/>
<node resource-id="com.toyota.oneapp:id/vehicle_status_tire_pressure_tile_sub_title" text="Good" bounds="[150,1055][300,1056]"/>
<node resource-id="com.toyota.oneapp:id/vehicle_status_information_last_updated_time_text" text="Updated 22 minutes ago" bounds="[200,1150][520,1190]"/>
</hierarchy>"""


class StatusParsing(unittest.TestCase):
    def setUp(self):
        self.nodes = tc._nodes(STATUS_XML)

    def test_text_by_id(self):
        self.assertEqual(tc._text_by_id(self.nodes, "fuel_view_range_value_text"), "200")

    def test_tile_sub_title_ids(self):
        self.assertEqual(tc._text_by_id(self.nodes, "vehicle_status_door_tile_sub_title"), "Locked")
        self.assertEqual(tc._text_by_id(self.nodes, "vehicle_status_window_tile_sub_title"), "Closed")
        self.assertEqual(tc._text_by_id(self.nodes, "vehicle_status_trunk_tile_sub_title"), "Closed")
        self.assertEqual(tc._text_by_id(self.nodes, "vehicle_status_tire_pressure_tile_sub_title"), "Good")

    def test_pair_value_tiles(self):
        self.assertEqual(tc._pair_value(self.nodes, "Doors", ("Locked", "Unlocked")), "Locked")
        self.assertEqual(tc._pair_value(self.nodes, "Windows", ("Closed", "Open")), "Closed")
        self.assertEqual(tc._pair_value(self.nodes, "Trunk", ("Closed", "Open")), "Closed")
        self.assertEqual(tc._pair_value(self.nodes, "Tire Pressure", ("Good", "Low")), "Good")

    def test_format_status(self):
        st = {"vehicle": "Yosef's Camry", "range": "200 mi", "tires": "Good",
              "doors": "Locked", "windows": "Closed", "trunk": "Closed"}
        out = tc.format_status(st)
        self.assertIn("Doors: Locked", out)
        self.assertIn("Range: 200 mi", out)
        self.assertIn("Trunk: Closed", out)
        self.assertNotIn("Updated", out)  # the freshness line is intentionally never included

    def test_format_status_empty(self):
        self.assertIn("Couldn't read", tc.format_status({}))


DASH_XML = """<?xml version='1.0'?><hierarchy>
<node resource-id="com.toyota.oneapp:id/ID_TAB_REMOTE" text="Remote" bounds="[21,398][237,482]"/>
<node resource-id="com.toyota.oneapp:id/ID_TAB_STATUS" text="Status" bounds="[251,398][467,482]"/>
<node resource-id="com.toyota.oneapp:id/ID_TAB_HEALTH" text="Health" bounds="[481,398][697,482]"/>
</hierarchy>"""


class DashboardNav(unittest.TestCase):
    def test_on_dashboard_detects_tab_bar(self):
        self.assertTrue(tc._on_dashboard(tc._nodes(DASH_XML)))
        # The Advanced Remote modal (UI_XML) covers the tab bar → not "on dashboard".
        self.assertFalse(tc._on_dashboard(tc._nodes(UI_XML)))

    def test_ensure_dashboard_backs_out_of_a_covering_modal(self):
        dumps = [UI_XML, DASH_XML]   # modal first; after a BACK the tabs appear
        keys: list[str] = []
        orig_adb, orig_dump = tc._adb, tc._ui_dump

        def fake_adb(*a, **k):
            if a[:3] == ("shell", "input", "keyevent"):
                keys.append(a[3])
            return ""

        tc._adb = fake_adb
        tc._ui_dump = lambda: dumps.pop(0) if len(dumps) > 1 else dumps[0]
        try:
            nodes = tc._ensure_dashboard()
        finally:
            tc._adb, tc._ui_dump = orig_adb, orig_dump
        self.assertIn("KEYCODE_BACK", keys)
        self.assertTrue(tc._on_dashboard(nodes))

    def test_ensure_dashboard_noop_when_tabs_already_visible(self):
        keys: list[str] = []
        orig_adb, orig_dump = tc._adb, tc._ui_dump

        def fake_adb(*a, **k):
            if a[:3] == ("shell", "input", "keyevent"):
                keys.append(a[3])
            return ""

        tc._adb = fake_adb
        tc._ui_dump = lambda: DASH_XML
        try:
            tc._ensure_dashboard()
        finally:
            tc._adb, tc._ui_dump = orig_adb, orig_dump
        self.assertEqual(keys, [])  # already on the dashboard → never press BACK


class UIParsing(unittest.TestCase):
    def setUp(self):
        self.nodes = tc._nodes(UI_XML)

    def test_anchor_set_present(self):
        self.assertTrue(tc.ANCHOR_IDS <= tc._present_ids(self.nodes))

    def test_find_by_id_center(self):
        self.assertEqual(tc._find(self.nodes, "remote_engine_start_button"), (200, 650))

    def test_trunk_shared_id_disambiguated_by_label(self):
        self.assertEqual(tc._find(self.nodes, "remote_trunk_lock_button", "Lock Trunk"), (200, 950))
        self.assertEqual(tc._find(self.nodes, "remote_trunk_lock_button", "Unlock Trunk"), (500, 950))

    def test_find_more_horizontal_by_desc(self):
        self.assertEqual(tc._find_desc(self.nodes, "More Horizontal"), (902, 929))

    def test_every_command_id_resolves(self):
        # 'shutoff' is deliberately absent while the engine is off — it's the same tile as 'start'.
        for cmd, (rid, label) in tc.COMMAND_IDS.items():
            if cmd == "shutoff":
                continue
            self.assertIsNotNone(tc._find(self.nodes, rid, label), f"{cmd} not found")

    def test_shutoff_resolves_only_while_the_engine_runs(self):
        running = tc._nodes(UI_XML_ENGINE_RUNNING)
        self.assertIsNotNone(tc._find(running, tc.ENGINE_STOP_ID))
        self.assertIsNone(tc._find(running, tc.ENGINE_START_ID))
        self.assertIsNone(tc._find(self.nodes, tc.ENGINE_STOP_ID))


class PanelConfirmation(unittest.TestCase):
    """The Advanced Remote guard must accept EITHER engine button.

    Requiring `remote_engine_start_button` outright made every command fail for the ~10 minutes a
    remote start was running (5 live SMS failures, 2026-10-07..11) with
    "Advanced Remote panel not confirmed ... (missing: remote_engine_start_button)".
    """

    def test_panel_confirmed_with_start_button(self):
        ids = tc._present_ids(tc._nodes(UI_XML))
        self.assertTrue(tc._panel_confirmed(ids))

    def test_panel_confirmed_with_stop_button(self):
        ids = tc._present_ids(tc._nodes(UI_XML_ENGINE_RUNNING))
        self.assertTrue(tc._panel_confirmed(ids))

    def test_panel_rejected_without_any_engine_button(self):
        ids = tc._present_ids(tc._nodes(UI_XML)) - {tc.ENGINE_START_ID}
        self.assertFalse(tc._panel_confirmed(ids))

    def test_panel_rejected_when_a_fixed_anchor_is_missing(self):
        ids = tc._present_ids(tc._nodes(UI_XML)) - {"remote_horn_button"}
        self.assertFalse(tc._panel_confirmed(ids))

    def test_start_button_is_not_a_fixed_anchor(self):
        self.assertNotIn(tc.ENGINE_START_ID, tc.ANCHOR_IDS)
        self.assertEqual(tc.ENGINE_ANCHOR_IDS, {tc.ENGINE_START_ID, tc.ENGINE_STOP_ID})


class ResultScoping(unittest.TestCase):
    """A push carrying another command's result code must not be reported as this command's result.

    On 2026-10-08 a START was answered "The vehicle is now locked. [DL1]" — the previous LOCK's
    push, matched on timestamp alone.
    """

    def test_rejects_another_commands_code(self):
        self.assertTrue(tc._is_other_commands_result("The vehicle is now locked. [DL1]", "start"))
        self.assertTrue(tc._is_other_commands_result("The vehicle is now unlocked. [DL0]", "lock"))

    def test_accepts_its_own_code(self):
        self.assertFalse(tc._is_other_commands_result(
            "Vehicle was started and will automatically shut off in 10 minutes. [RES1]", "start"))
        self.assertFalse(tc._is_other_commands_result("The vehicle is now locked. [DL1]", "lock"))

    def test_accepts_shared_failure_pushes(self):
        # A failure push carries no per-command code and IS the answer to whatever we sent.
        for text in ("Remote command failed. Please ensure the vehicle is not in use [D1]",
                     "Your request could not be completed because a keyfob was detected. [70]"):
            for cmd in ("start", "lock", "lights", "shutoff"):
                self.assertFalse(tc._is_other_commands_result(text, cmd), f"{cmd}: {text}")

    def test_accepts_anything_for_an_uncatalogued_command(self):
        # LIGHTS has no known code; an unrecognised push is still its legitimate result...
        self.assertFalse(tc._is_other_commands_result("The lights were flashed. [LT1]", "lights"))
        # ...but a LOCK confirmation plainly is not.
        self.assertTrue(tc._is_other_commands_result("The vehicle is now locked. [DL1]", "lights"))

    def test_no_command_means_no_filtering(self):
        self.assertFalse(tc._is_other_commands_result("The vehicle is now locked. [DL1]", None))


CAUTION_XML = """<?xml version='1.0'?><hierarchy>
<node text="Caution" bounds="[29,170][576,220]"/>
<node text="Please make sure the vehicle is in a safe, ventilated area before using remote start and climate features." bounds="[31,262][689,892]"/>
<node content-desc="Do not show this message again" bounds="[14,928][98,1012]"/>
<node text="Don't tell me again." bounds="[102,951][348,989]"/>
<node text="Cancel" bounds="[176,1032][544,1116]"/>
<node text="Continue" bounds="[176,1142][544,1226]"/>
</hierarchy>"""


class CautionDialog(unittest.TestCase):
    def test_detected_on_caution_sheet(self):
        self.assertTrue(tc._caution_present(tc._nodes(CAUTION_XML)))

    def test_not_detected_on_remote_panel(self):
        # The normal Advanced Remote panel is not a Caution sheet.
        self.assertFalse(tc._caution_present(tc._nodes(UI_XML)))

    def test_find_text_center(self):
        nodes = tc._nodes(CAUTION_XML)
        self.assertEqual(tc._find_text(nodes, "Continue"), (360, 1184))
        self.assertEqual(tc._find_text(nodes, "Cancel"), (360, 1074))

    def test_checkbox_located_by_desc(self):
        nodes = tc._nodes(CAUTION_XML)
        self.assertEqual(tc._find_desc(nodes, "Do not show this message again"), (56, 970))

    def test_dismiss_taps_checkbox_then_continue(self):
        taps: list[tuple[int, int]] = []
        orig_adb, orig_dump = tc._adb, tc._ui_dump

        def fake_adb(*a, **k):
            if a[:2] == ("shell", "input") and a[2] == "tap":
                taps.append((int(a[3]), int(a[4])))
            return ""

        tc._adb = fake_adb
        tc._ui_dump = lambda: CAUTION_XML
        try:
            handled = tc._dismiss_caution_dialog(max_wait=0)
        finally:
            tc._adb, tc._ui_dump = orig_adb, orig_dump
        self.assertTrue(handled)
        self.assertIn((56, 970), taps)      # ticked "do not show again"
        self.assertEqual(taps[-1], (360, 1184))  # Continue tapped last

    def test_dismiss_noop_when_absent(self):
        orig_adb, orig_dump = tc._adb, tc._ui_dump
        tc._adb = lambda *a, **k: ""
        tc._ui_dump = lambda: UI_XML
        try:
            self.assertFalse(tc._dismiss_caution_dialog(max_wait=0))
        finally:
            tc._adb, tc._ui_dump = orig_adb, orig_dump


class PanelRetry(unittest.TestCase):
    """The whole Advanced Remote navigation retries with a settle-wait, so a command that lands while
    the app is mid-transition (right after START) recovers instead of failing to the user."""

    def setUp(self):
        self._orig = (tc._try_reach_advanced_remote, tc._PANEL_RETRY_WAIT, tc._PANEL_ATTEMPTS, tc._ui_dump)
        tc._PANEL_RETRY_WAIT = 0  # no real waiting in tests

    def tearDown(self):
        (tc._try_reach_advanced_remote, tc._PANEL_RETRY_WAIT,
         tc._PANEL_ATTEMPTS, tc._ui_dump) = self._orig

    def test_recovers_on_a_later_attempt(self):
        good = tc._nodes(UI_XML)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return good if calls["n"] >= 2 else None  # first pass fails, second succeeds

        tc._try_reach_advanced_remote = flaky
        tc._PANEL_ATTEMPTS = 3
        nodes = tc.ensure_on_advanced_remote()
        self.assertEqual(calls["n"], 2)
        self.assertTrue(tc.ANCHOR_IDS <= tc._present_ids(nodes))

    def test_raises_only_after_all_attempts(self):
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            return None

        tc._try_reach_advanced_remote = always_fail
        tc._PANEL_ATTEMPTS = 3
        tc._ui_dump = lambda: "<hierarchy></hierarchy>"
        with self.assertRaises(tc.UINavigationError):
            tc.ensure_on_advanced_remote()
        self.assertEqual(calls["n"], 3)  # exhausted every attempt before giving up

    def test_not_logged_in_propagates_immediately(self):
        def signed_out():
            raise tc.NotLoggedIn("signed out")

        tc._try_reach_advanced_remote = signed_out
        tc._PANEL_ATTEMPTS = 3
        with self.assertRaises(tc.NotLoggedIn):
            tc.ensure_on_advanced_remote()  # must NOT be retried/swallowed


class NotificationResult(unittest.TestCase):
    def setUp(self):
        self._orig_adb = tc._adb
        tc._adb = lambda *a, **k: DUMP

    def tearDown(self):
        tc._adb = self._orig_adb

    def test_parses_only_toyota_records(self):
        recs = tc._toyota_notifications()
        self.assertEqual(len(recs), 2)
        self.assertTrue(all("Camry" in r["title"] for r in recs))

    def test_returns_newest_at_or_after_command(self):
        res = tc.await_result(since_ms=1696599999000, timeout=0, poll=0)
        self.assertIsNotNone(res)
        self.assertIn("unlocked", res["text"])
        # the dumpsys "String (...)" wrapper must be unwrapped for a clean SMS reply
        self.assertFalse(res["text"].startswith("String ("))
        self.assertEqual(res["text"], "The vehicle is now unlocked. [DL0]")

    def test_rejects_stale_older_notification(self):
        # A command issued later than any existing notification must not match an old one.
        self.assertIsNone(tc.await_result(since_ms=1696700000000, timeout=0, poll=0))




class DumpRetry(unittest.TestCase):
    """A transient `uiautomator dump` failure must not become the owner's answer.

    It produced the 2026-10-09 SMS "Couldn't send lights: adb shell uiautomator dump
    /sdcard/ui.xml failed:" — note the empty reason.
    """

    def setUp(self):
        self._orig_adb, self._orig_sleep = tc._adb, tc.time.sleep
        tc.time.sleep = lambda *_: None

    def tearDown(self):
        tc._adb, tc.time.sleep = self._orig_adb, self._orig_sleep

    def test_retries_then_succeeds(self):
        calls = []

        def fake_adb(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("shell", "uiautomator") and len(calls) <= 2:
                raise tc.ControlError("adb shell uiautomator dump /sdcard/ui.xml failed: ")
            return UI_XML if args[:2] == ("shell", "cat") else ""

        tc._adb = fake_adb
        self.assertIn("<node", tc._ui_dump())

    def test_gives_up_with_a_reason_not_an_empty_message(self):
        def always_fails(*args, **kwargs):
            raise tc.ControlError("adb shell uiautomator dump /sdcard/ui.xml failed: ")

        tc._adb = always_fails
        with self.assertRaises(tc.ControlError) as ctx:
            tc._ui_dump()
        self.assertIn("couldn't read the screen", str(ctx.exception))

    def test_empty_dump_is_a_failure(self):
        tc._adb = lambda *a, **k: "" if a[:2] == ("shell", "cat") else ""
        with self.assertRaises(tc.ControlError):
            tc._ui_dump()


class ShutoffKeyword(unittest.TestCase):
    def test_shutoff_aliases(self):
        for text in ("SHUTOFF", "toyota shut off", "ENGINE OFF", "Toyota kill engine"):
            self.assertEqual(tc.parse_command(text)[0], "shutoff", text)

    def test_stop_is_never_an_engine_command(self):
        # The carrier intercepts STOP as the opt-out keyword; it must never mean "stop the engine".
        self.assertNotEqual(tc.parse_command("STOP")[0], "shutoff")

    def test_help_text_advertises_the_question_mark(self):
        # HELP never reaches the webhook (carrier intercept), so the list must not tell them to use it.
        self.assertIn("?", tc.HELP_TEXT)
        self.assertNotIn("HELP - this list", tc.HELP_TEXT)


class BareConfirmation(unittest.TestCase):
    """A bare "YES" must be recognisable as a confirmation with no command of its own, so the
    webhook can apply it to the pending one. A plain "Yes" answered "Unknown command" on
    2026-10-06 because the owner had to retype "TOYOTA UNLOCK YES" in full."""

    def test_bare_yes_is_a_confirmation_without_a_command(self):
        for text in ("YES", "yes", "Yes", "toyota yes", "confirm", "really"):
            cmd, confirmed = tc.parse_command(text)
            self.assertIsNone(cmd, text)
            self.assertTrue(confirmed, text)

    def test_a_full_command_still_carries_its_confirmation(self):
        self.assertEqual(tc.parse_command("TOYOTA UNLOCK YES"), ("unlock", True))
        self.assertEqual(tc.parse_command("TOYOTA UNLOCK"), ("unlock", False))

    def test_an_unrelated_word_is_not_a_confirmation(self):
        cmd, confirmed = tc.parse_command("sure")
        self.assertIsNone(cmd)
        self.assertFalse(confirmed)


if __name__ == "__main__":
    unittest.main()
