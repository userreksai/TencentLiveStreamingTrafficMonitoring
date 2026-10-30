"""Offline CLI smoke tests for deployment and single-run modes."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from traffic_monitor.__main__ import main
from traffic_monitor.tencent import TencentError
from test_app import ANCHOR, FakeClient, buckets, package, raw_config


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(raw_config(self.state)), encoding="utf-8")
        self.args = ["--config", str(self.config_path)]
        patch("traffic_monitor.__main__.time.time", return_value=ANCHOR + 300).start()
        patch("traffic_monitor.app.TencentClient", FakeClient).start()
        self.packages = patch("traffic_monitor.app.fetch_packages", return_value=[package()]).start()
        self.usage = patch("traffic_monitor.app.fetch_billable_buckets",
                           side_effect=lambda client, start, end, services, regions: buckets(start, end)).start()
        self.send = patch("traffic_monitor.notifications.LarkSender.send").start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real cloud requests are forbidden")).start()
        self.addCleanup(patch.stopall)

    def test_config_check_is_offline_and_does_not_create_state(self):
        self.assertEqual(0, main(self.args + ["--check-config"]))
        self.packages.assert_not_called()
        self.usage.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_once_collects_and_exits_without_a_startup_hourly_message(self):
        self.assertEqual(0, main(self.args + ["--once"]))
        self.packages.assert_called_once()
        self.usage.assert_called_once()
        self.send.assert_not_called()
        self.assertTrue(self.state.exists())

    def test_report_now_can_be_combined_with_once(self):
        self.assertEqual(0, main(self.args + ["--once", "--report-now"]))
        self.send.assert_called_once()
        route, text = self.send.call_args.args
        self.assertEqual("normal", route)
        self.assertIn("整点汇总", text)

    def test_dry_run_never_sends_or_writes_state(self):
        self.assertEqual(0, main(self.args + ["--dry-run", "--report-now"]))
        self.send.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_bad_key_single_run_fails_but_routes_notifications_to_error(self):
        self.packages.side_effect = TencentError("AuthFailure.SecretIdNotFound", "invalid key")
        self.usage.side_effect = TencentError("AuthFailure.SecretIdNotFound", "invalid key")
        self.assertEqual(1, main(self.args + ["--once"]))
        self.assertTrue(self.send.called)
        self.assertTrue(all(call.args[0] == "error" for call in self.send.call_args_list))

    def test_notification_test_uses_only_the_selected_channel(self):
        self.assertEqual(0, main(self.args + ["--test-notify", "error"]))
        self.send.assert_called_once()
        self.assertEqual("error", self.send.call_args.args[0])
        self.packages.assert_not_called()
        self.usage.assert_not_called()
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
