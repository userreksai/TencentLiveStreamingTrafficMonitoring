"""Offline schedule, persistence and isolation checks for daily reports."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from traffic_monitor.config import validate_config
from traffic_monitor.daily import DailyReporter, DailyReportService, daily_notification_config, daily_state_path
from traffic_monitor.notifications import NotificationError
from traffic_monitor.storage import StateStore
from test_app import epoch, raw_config


NOW = epoch("2026-10-31T10:05:00+08:00")


class DailyReporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        raw = raw_config(self.path)
        raw["daily_report"] = {"enabled": True, "time": "10:05",
                               "webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-daily",
                               "sign_secret": "daily-secret"}
        self.config = validate_config(raw)
        self.sender = Mock()
        self.factory = Mock()
        self.fetch = patch("traffic_monitor.daily.fetch_daily_report", return_value="official daily statistics").start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)
        self.reporter = DailyReporter(self.config, sender=self.sender, client_factory=self.factory)

    def restart(self):
        self.reporter = DailyReporter(self.config, sender=self.sender, client_factory=self.factory)
        self.reporter.store.load()

    def test_due_time_is_beijing_and_before_it_no_cloud_or_notification(self):
        self.config["_timezone_offset"] = -5 * 3600
        self.assertTrue(self.reporter.tick(NOW - 1))
        self.fetch.assert_not_called()
        self.sender.send.assert_not_called()
        self.assertFalse(self.reporter.store.path.exists())
        self.assertTrue(self.reporter.tick(NOW))
        self.fetch.assert_called_once()
        self.assertIn("2026-10-31 10:05:00", self.sender.send.call_args.args[1])

    def test_success_is_once_per_day_even_after_restart_and_force(self):
        self.assertTrue(self.reporter.tick(NOW))
        self.assertTrue(self.reporter.tick(NOW + 5))
        self.restart()
        self.assertTrue(self.reporter.tick(NOW + 1200, force=True))
        self.fetch.assert_called_once()
        self.sender.send.assert_called_once()
        self.assertTrue(self.reporter.tick(NOW + 86400))
        self.assertEqual(2, self.fetch.call_count)

    def test_successful_idle_ticks_do_not_write_state_again(self):
        self.assertTrue(self.reporter.tick(NOW))
        with patch.object(self.reporter.store, "save", wraps=self.reporter.store.save) as save:
            for elapsed in (5, 10, 30, 300, 3600):
                self.assertTrue(self.reporter.tick(NOW + elapsed))
            save.assert_not_called()
        self.fetch.assert_called_once()

    def test_start_after_schedule_only_generates_today(self):
        self.assertTrue(self.reporter.tick(NOW + 3600 * 9))
        self.fetch.assert_called_once()
        self.assertEqual(1, len(self.reporter.store.state["reports"]))

    def test_daily_routes_and_state_never_use_hourly_destinations_or_state(self):
        hourly = StateStore(self.path)
        hourly.state["accounts"]["hourly"] = {"ledger": {"important": "preserved"}}
        hourly.save()
        before = self.path.read_bytes()
        self.reporter.tick(NOW)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(str(self.path) + ".daily.json", str(self.reporter.store.path))
        self.assertEqual(daily_state_path(self.config), str(self.reporter.store.path))
        self.assertEqual({}, self.reporter.store.state["accounts"])
        private = daily_notification_config(self.config)
        for route in ("normal", "error"):
            self.assertEqual(self.config["daily_report"]["webhook"], private["lark"][route + "_webhook"])
            self.assertEqual("daily-secret", private["lark"][route + "_sign_secret"])
        self.assertNotEqual(self.config["lark"], private["lark"])

    def test_failed_collection_retries_after_five_minutes_without_marking_sent(self):
        self.fetch.side_effect = [ValueError("offline failure"), "recovered data"]
        self.assertFalse(self.reporter.tick(NOW))
        self.assertEqual({}, self.reporter.store.state["reports"])
        self.assertFalse(self.reporter.tick(NOW + 299))
        self.assertEqual(1, self.fetch.call_count)
        self.assertTrue(self.reporter.tick(NOW + 300))
        self.assertEqual(2, self.fetch.call_count)
        self.assertTrue(any("recovered data" in call.args[1] for call in self.sender.send.call_args_list))

    def test_failed_delivery_retries_persisted_snapshot_without_new_cloud_calls(self):
        failures = []

        def send(route, text):
            if route == "normal" and not failures:
                failures.append(True)
                raise NotificationError("offline delivery failure")

        self.sender.send.side_effect = send
        self.assertFalse(self.reporter.tick(NOW))
        self.assertTrue(any(item["route"] == "normal" for item in self.reporter.store.state["outbox"]))
        self.restart()
        self.assertTrue(self.reporter.tick(NOW + 60))
        self.fetch.assert_called_once()
        self.assertFalse(any(item["route"] == "normal" for item in self.reporter.store.state["outbox"]))

    def test_expired_delivery_is_not_sent_the_next_day(self):
        self.sender.send.side_effect = NotificationError("offline failure")
        self.assertFalse(self.reporter.tick(NOW))
        self.restart()
        self.sender.send.side_effect = None
        self.sender.send.reset_mock()
        self.assertTrue(self.reporter.tick(NOW + 86400))
        normal = [call for call in self.sender.send.call_args_list if call.args[0] == "normal"]
        self.assertEqual(1, len(normal))
        self.assertIn("2026-11-01 10:05:00", normal[0].args[1])

    def test_disabled_worker_does_not_query_and_explicit_force_does(self):
        changed = deepcopy(self.config)
        changed["daily_report"]["enabled"] = False
        self.reporter.reconfigure(changed)
        self.assertTrue(self.reporter.tick(NOW))
        self.fetch.assert_not_called()
        self.assertTrue(self.reporter.tick(NOW - 3600, force=True))
        self.fetch.assert_called_once()

    def test_reconfigure_discards_pending_payloads_and_respects_new_time(self):
        self.sender.send.side_effect = NotificationError("offline failure")
        self.reporter.tick(NOW)
        changed = deepcopy(self.config)
        changed["daily_report"].update(time="12:00", webhook=self.config["daily_report"]["webhook"] + "-new")
        self.reporter.reconfigure(changed)
        self.reporter.tick(NOW + 5)
        self.assertEqual([], self.reporter.store.state["outbox"])
        self.assertEqual(changed["daily_report"]["webhook"], self.reporter.queue.config["lark"]["normal_webhook"])
        self.fetch.assert_called_once()
        self.assertEqual({}, self.reporter.store.state["reports"])
        self.sender.send.side_effect = None
        self.assertTrue(self.reporter.tick(NOW + 6900))
        self.assertEqual(2, self.fetch.call_count)

    def test_destination_change_while_down_recollects_undelivered_report(self):
        self.fetch.return_value = "old destination snapshot"
        self.sender.send.side_effect = NotificationError("offline failure")
        self.reporter.tick(NOW)
        self.config["daily_report"]["webhook"] += "-new"
        self.restart()
        self.sender.send.side_effect = None
        self.sender.send.reset_mock()
        self.fetch.return_value = "fresh snapshot for new destination"
        self.reporter.tick(NOW + 60)
        self.sender.send.assert_called_once()
        self.assertIn("fresh snapshot for new destination", self.sender.send.call_args.args[1])
        self.assertNotIn("old destination snapshot", self.sender.send.call_args.args[1])
        self.assertEqual(2, self.fetch.call_count)
        self.assertEqual([], self.reporter.store.state["outbox"])

    def test_group_change_retries_only_account_with_undelivered_report(self):
        changed = deepcopy(self.config)
        other = deepcopy(changed["accounts"][0])
        other.update(name="account B", _id="other-account")
        changed["accounts"].append(other)
        self.reporter.reconfigure(changed)

        def send(route, text):
            if route == "normal" and "account B" in text:
                raise NotificationError("second account delivery failure")

        self.sender.send.side_effect = send
        self.assertFalse(self.reporter.tick(NOW))
        self.assertEqual(2, self.fetch.call_count)
        changed = deepcopy(changed)
        changed["daily_report"]["webhook"] += "-new"
        self.reporter.reconfigure(changed)
        self.sender.send.side_effect = None
        self.sender.send.reset_mock()
        self.assertTrue(self.reporter.tick(NOW + 60))
        self.assertEqual(3, self.fetch.call_count)
        self.sender.send.assert_called_once()
        self.assertIn("account B", self.sender.send.call_args.args[1])
        self.assertEqual(2, len(self.reporter.store.state["reports"]))

    def test_reconfigure_during_cloud_read_cancels_old_result(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return "obsolete result"

        self.fetch.side_effect = fetch
        thread = threading.Thread(target=lambda: self.reporter.tick(NOW))
        thread.start()
        self.assertTrue(entered.wait(5))
        changed = deepcopy(self.config)
        changed["daily_report"]["enabled"] = False
        started = time.perf_counter()
        self.reporter.reconfigure(changed)
        self.assertLess(time.perf_counter() - started, 0.2)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.sender.send.assert_not_called()
        self.assertEqual({}, self.reporter.store.state["reports"])

    def test_one_failed_account_does_not_prevent_another_daily_report(self):
        changed = deepcopy(self.config)
        other = deepcopy(changed["accounts"][0])
        other.update(name="account B", _id="other-account")
        changed["accounts"].append(other)
        self.reporter.reconfigure(changed)
        self.fetch.side_effect = [ValueError("first account failed"), "second account healthy"]
        self.assertFalse(self.reporter.tick(NOW))
        self.assertIn("other-account", self.reporter.store.state["reports"])
        self.assertTrue(any("second account healthy" in call.args[1] for call in self.sender.send.call_args_list))


class DailyServiceTests(unittest.TestCase):
    def test_blocked_daily_cloud_read_never_blocks_reconfigure_or_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = raw_config(Path(temp) / "state.json")
            raw["daily_report"] = {"enabled": True, "time": "10:05",
                                   "webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-daily"}
            config = validate_config(raw)
            entered, release = threading.Event(), threading.Event()

            def blocked(*args):
                entered.set()
                release.wait(5)
                return "cancelled data"

            with patch("traffic_monitor.daily.time.time", return_value=NOW), \
                    patch("traffic_monitor.daily.fetch_daily_report", side_effect=blocked), \
                    patch("traffic_monitor.daily.TencentClient", return_value=Mock()), \
                    patch("traffic_monitor.notifications.LarkSender.send") as send:
                service = DailyReportService(config)
                service.__enter__()
                self.assertTrue(entered.wait(5))
                changed = deepcopy(config)
                changed["daily_report"]["enabled"] = False
                started = time.perf_counter()
                service.reconfigure(changed)
                self.assertLess(time.perf_counter() - started, 0.2)
                started = time.perf_counter()
                service.__exit__()
                self.assertLess(time.perf_counter() - started, 0.5)
                release.set()
                service._thread.join(5)
                self.assertFalse(service._thread.is_alive())
                send.assert_not_called()
            self.assertFalse((Path(temp) / "state.json").exists())

    def test_disabled_service_does_not_create_daily_state_or_query(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = raw_config(Path(temp) / "state.json")
            config = validate_config(raw)
            with patch("traffic_monitor.daily.fetch_daily_report") as fetch:
                with DailyReportService(config):
                    pass
                fetch.assert_not_called()
            self.assertEqual([], list(Path(temp).iterdir()))


if __name__ == "__main__":
    unittest.main()
