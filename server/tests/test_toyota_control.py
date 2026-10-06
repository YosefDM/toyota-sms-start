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
              "doors": "Locked", "windows": "Closed", "trunk": "Closed",
              "updated": "Updated 22 minutes ago"}
        out = tc.format_status(st)
        self.assertIn("Doors: Locked", out)
        self.assertIn("Range: 200 mi", out)
        self.assertIn("Trunk: Closed", out)

    def test_format_status_empty(self):
        self.assertIn("Couldn't read", tc.format_status({}))


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
        for cmd, (rid, label) in tc.COMMAND_IDS.items():
            self.assertIsNotNone(tc._find(self.nodes, rid, label), f"{cmd} not found")


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


if __name__ == "__main__":
    unittest.main()
