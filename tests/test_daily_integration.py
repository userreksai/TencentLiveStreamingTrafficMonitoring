"""Daily opt-in configuration and CLI isolation from the hourly monitor."""

from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from traffic_monitor.__main__ import main
from traffic_monitor.config import ConfigError, redact, validate_config
from traffic_monitor.daily import daily_notification_config
from test_app import FakeClient, raw_config
from test_daily_data import NOW


HOOK = "https://open.larksuite.com/open-apis/bot/v2/hook/offline-daily"


class DailyConfigTests(unittest.TestCase):
    def raw(self):
        return raw_config(Path("state.json"))

    def test_legacy_config_remains_disabled_and_schedule_is_beijing(self):
        raw = self.raw()
        raw["reports"]["timezone"] = "UTC+07:00"
        cfg = validate_config(raw)
        self.assertEqual({"enabled": False, "time": "10:05", "webhook": "", "sign_secret": ""}, cfg["daily_report"])
        self.assertEqual(7 * 3600, cfg["_timezone_offset"])
        self.assertNotIn("daily_report", raw)

    def test_daily_settings_do_not_change_hourly_scope_or_accounting(self):
        raw = self.raw()
        legacy = validate_config(raw)
        raw["daily_report"] = {"enabled": True, "time": "11:13", "webhook": HOOK, "sign_secret": "daily-key"}
        changed = validate_config(raw)
        for key in ("reports", "sampling", "alerts", "lark", "accounts", "storage", "_timezone_offset"):
            self.assertEqual(legacy[key], changed[key], key)
        private = daily_notification_config(changed)
        for route in ("normal", "error"):
            self.assertEqual(HOOK, private["lark"][route + "_webhook"])
            self.assertEqual("daily-key", private["lark"][route + "_sign_secret"])
        self.assertEqual(legacy["lark"], changed["lark"])

    def test_time_requires_valid_hh_mm_and_enabled_requires_bool(self):
        for value in ("00:00", "10:05", "23:59"):
            raw = self.raw()
            raw["daily_report"] = {"time": value}
            self.assertEqual(value, validate_config(raw)["daily_report"]["time"])
        for value in (None, 1005, True, "9:05", "24:00", "10:60", "10:05:00", " 10:05"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                raw = self.raw()
                raw["daily_report"] = {"time": value}
                validate_config(raw)
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                raw = self.raw()
                raw["daily_report"] = {"enabled": value}
                validate_config(raw)

    def test_enabled_daily_requires_its_own_valid_webhook(self):
        for block in (None, [], {"enabled": True}, {"enabled": True, "webhook": "https://example.com/hook"}):
            with self.subTest(block=block), self.assertRaises(ConfigError):
                raw = self.raw()
                raw["daily_report"] = block
                validate_config(raw)

    def test_daily_secrets_support_env_and_are_redacted(self):
        raw = self.raw()
        raw["daily_report"] = {"enabled": True, "webhook": "env:DAILY_TEST_HOOK", "sign_secret": "env:DAILY_TEST_KEY"}
        with patch.dict(os.environ, {"DAILY_TEST_HOOK": HOOK, "DAILY_TEST_KEY": "daily-secret"}):
            cfg = validate_config(raw)
        text = redact(HOOK + " daily-secret", cfg)
        self.assertNotIn(HOOK, text)
        self.assertNotIn("daily-secret", text)


class DailyCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.path = self.root / "config.json"
        self.raw = raw_config(self.state)
        self.raw["daily_report"] = {"enabled": True, "webhook": HOOK, "sign_secret": ""}
        self.write_config()
        self.args = ["--config", str(self.path)]
        self.fetch = patch("traffic_monitor.daily.fetch_daily_report", return_value="日报离线数据").start()
        patch("traffic_monitor.daily.TencentClient", FakeClient).start()
        patch("traffic_monitor.__main__.time.time", return_value=NOW).start()
        self.hourly = patch("traffic_monitor.app.Monitor.sample", side_effect=AssertionError("Must not collect hourly data")).start()
        self.send = patch("traffic_monitor.notifications.LarkSender.send").start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real cloud calls forbidden")).start()
        self.addCleanup(patch.stopall)

    def write_config(self):
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")

    def test_preview_only_collects_daily_and_never_writes_state_or_sends(self):
        self.assertEqual(0, main(self.args + ["--daily-report-now", "--dry-run"]))
        self.fetch.assert_called_once()
        self.hourly.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(str(self.state) + ".daily.json").exists())
        self.assertFalse(Path(str(self.state) + ".daily.json.lock").exists())

    def test_daily_command_persists_separately_and_deduplicates(self):
        self.assertEqual(0, main(self.args + ["--daily-report-now"]))
        self.assertEqual(0, main(self.args + ["--daily-report-now"]))
        self.fetch.assert_called_once()
        self.send.assert_called_once()
        self.assertIn("每日流量包统计", self.send.call_args.args[1])
        self.hourly.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertTrue(Path(str(self.state) + ".daily.json").exists())

    def test_daily_test_uses_only_the_dedicated_destination(self):
        captured = []
        class Sender:
            def __init__(self, config):
                captured.append(deepcopy(config))
            def send(self, route, text):
                captured.append(route)
        with patch("traffic_monitor.__main__.LarkSender", Sender):
            self.assertEqual(0, main(self.args + ["--test-notify", "daily"]))
        self.assertEqual(HOOK, captured[0]["lark"]["normal_webhook"])
        self.assertEqual("normal", captured[1])
        self.fetch.assert_not_called()
        self.hourly.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_daily_missing_hook_never_falls_back_to_existing_groups(self):
        self.raw.pop("daily_report")
        self.write_config()
        for flags in (["--test-notify", "daily"], ["--daily-report-now"]):
            with self.subTest(flags=flags):
                self.assertEqual(1, main(self.args + flags))
        self.send.assert_not_called()
        self.fetch.assert_not_called()
        self.hourly.assert_not_called()

    def test_config_check_does_not_fetch_daily(self):
        self.assertEqual(0, main(self.args + ["--check-config"]))
        self.fetch.assert_not_called()
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
