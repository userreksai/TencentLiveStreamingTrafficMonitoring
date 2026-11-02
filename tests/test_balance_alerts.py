"""Business shortage alerts are one-shot per funding cycle and use their own group."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traffic_monitor.app import Monitor
from traffic_monitor.config import validate_config
from traffic_monitor.storage import StateStore
from test_app import ANCHOR, CaptureSender, FakeClient, raw_config


class BalanceAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        raw = raw_config(self.path)
        raw["alerts"]["repeat_seconds"] = 3600  # Existing installations keep this value.
        self.config = validate_config(raw)
        self.account = self.config["accounts"][0]
        self.store = StateStore(self.path)
        self.sender = CaptureSender()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real network calls forbidden")).start()
        self.addCleanup(patch.stopall)

    def record(self):
        return self.store.state["accounts"][self.account["_id"]]

    def emit(self, percent, now=ANCHOR + 300, runway=None, estimate=None, drain=True):
        record = self.monitor.record_for(self.account, now)
        record["result"] = {
            "safe_percent": str(percent), "remaining_percent": str(percent if estimate is None else estimate),
            "estimated_remaining_bytes": str(percent if estimate is None else estimate),
            "safe_remaining_bytes": str(percent), "reserve_bytes": "0",
            "latest_data_time": now, "safe_runway_seconds": runway,
        }
        self.monitor.business_alerts(self.account, record, now)
        return self.monitor.queue.drain(now) if drain else True

    def restart(self):
        self.store = StateStore(self.path)
        self.store.load()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

    def test_old_hourly_repeat_setting_never_repeats_same_threshold(self):
        for elapsed in (300, 600, 3900, 7500, 11100, 86700):
            self.assertTrue(self.emit(46, ANCHOR + elapsed))
        self.assertEqual(1, len(self.sender.route("balance")))
        self.assertIn("不足 50%", self.sender.route("balance")[0])
        self.assertEqual([], self.sender.route("normal"))

    def test_escalation_is_strict_and_each_level_is_sent_once(self):
        for index, percent in enumerate((50, 49, 10, 9, 9, 0, 0)):
            self.assertTrue(self.emit(percent, ANCHOR + 300 * (index + 1)))
        alerts = self.sender.route("balance")
        self.assertEqual(3, len(alerts))
        self.assertIn("不足 50%", alerts[0])
        self.assertIn("不足 10%", alerts[1])
        self.assertIn("估算已耗尽", alerts[2])
        self.assertEqual([], self.sender.route("normal"))

    def test_first_observation_below_ten_only_sends_the_deeper_level(self):
        self.assertTrue(self.emit(9))
        self.assertEqual(1, len(self.sender.route("balance")))
        self.assertIn("不足 10%", self.sender.route("balance")[0])
        self.assertNotIn("不足 50%", self.sender.route("balance")[0])

    def test_source_is_safe_percentage_rather_than_estimated_percentage(self):
        self.assertTrue(self.emit(49, estimate=75))
        self.assertIn("不足 50%", self.sender.route("balance")[0])

    def test_revisions_above_and_below_threshold_do_not_rearm_sent_level(self):
        for index, percent in enumerate((49, 70, 49, 9, 25, 9)):
            self.assertTrue(self.emit(percent, ANCHOR + 300 + 3600 * index))
        self.assertEqual(2, len(self.sender.route("balance")))
        self.assertEqual("10", self.record()["last_threshold"])

    def test_sent_level_is_durable_across_restart_and_multiple_hours(self):
        self.emit(49)
        self.restart()
        self.emit(49, ANCHOR + 3900)
        self.restart()
        self.emit(49, ANCHOR + 7500)
        self.assertEqual(1, len(self.sender.route("balance")))

    def test_legacy_sent_marker_is_preserved_when_upgrading(self):
        record = self.monitor.record_for(self.account, ANCHOR + 300)
        record.update(last_threshold="50", last_threshold_at=ANCHOR + 300)
        self.store.save()
        self.restart()
        self.emit(46, ANCHOR + 7500)
        self.assertEqual([], self.sender.route("balance"))
        self.emit(9, ANCHOR + 7800)
        self.assertEqual(1, len(self.sender.route("balance")))
        self.assertIn("不足 10%", self.sender.route("balance")[0])

    def test_runway_shortage_is_once_even_if_it_recovers_then_drops_again(self):
        self.config["alerts"]["runway_minutes"] = 60
        self.emit(80, runway=3600)
        self.assertEqual([], self.sender.route("balance"))
        self.emit(80, ANCHOR + 600, runway=3599)
        self.emit(80, ANCHOR + 4200, runway=1800)
        self.emit(80, ANCHOR + 7800, runway=7200)
        self.restart()
        self.emit(80, ANCHOR + 11400, runway=600)
        self.assertEqual(1, len(self.sender.route("balance")))
        self.assertIn("预计支撑", self.sender.route("balance")[0])
        self.assertEqual([], self.sender.route("normal"))

    def test_missing_group_keeps_threshold_eligible_and_hourly_messages_independent(self):
        self.config["lark"]["balance_webhook"] = ""
        self.monitor.queue.enqueue("normal", "report/offline", "hourly report", ANCHOR + 300)
        self.emit(46)
        self.assertEqual([], self.sender.route("balance"))
        self.assertEqual(["hourly report"], self.sender.route("normal"))
        self.assertNotIn("last_threshold", self.record())
        self.assertTrue(self.sender.route("error"))
        self.assertTrue(self.store.state["errors"]["lark/balance"]["active"])
        config = deepcopy(self.config)
        config["lark"]["balance_webhook"] = "https://open.larksuite.com/open-apis/bot/v2/hook/offline-balance"
        self.monitor.reconfigure(config)
        self.config = config
        self.emit(46, ANCHOR + 600)
        self.assertEqual(1, len(self.sender.route("balance")))

    def test_missing_group_does_not_consume_runway_marker(self):
        self.config["lark"]["balance_webhook"] = ""
        self.config["alerts"]["runway_minutes"] = 60
        self.emit(80, runway=600)
        self.assertNotIn("last_runway_at", self.record())
        self.assertEqual([], self.sender.route("balance"))

    def test_changing_group_does_not_rearm_an_already_delivered_threshold(self):
        self.emit(46)
        config = deepcopy(self.config)
        config["lark"]["balance_webhook"] += "-new"
        self.monitor.reconfigure(config)
        self.config = config
        self.emit(46, ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("balance")))

    def test_failed_delivery_retries_same_queue_item_without_hourly_duplicates(self):
        original = self.sender.send

        def fail_balance(route, text):
            if route == "balance":
                raise RuntimeError("offline balance delivery failure")
            return original(route, text)

        with patch.object(self.sender, "send", side_effect=fail_balance):
            self.assertFalse(self.emit(46))
            pending = [item for item in self.store.state["outbox"] if item["route"] == "balance"]
            self.assertEqual(1, len(pending))
            delivery_id = pending[0]["id"]
            self.assertFalse(self.emit(45, ANCHOR + 3900))
            pending = [item for item in self.store.state["outbox"] if item["route"] == "balance"]
            self.assertEqual([delivery_id], [item["id"] for item in pending])
        self.assertTrue(self.monitor.queue.drain(ANCHOR + 7500))
        self.assertEqual(1, len(self.sender.route("balance")))
        self.assertEqual([], self.sender.route("normal"))
        self.assertFalse(any(item["route"] == "balance" for item in self.store.state["outbox"]))

    def test_recalibration_starts_a_new_reminder_cycle(self):
        self.emit(46)
        raw = raw_config(self.path)
        raw["accounts"][0]["anchor"]["revision"] = "new-calibration"
        config = validate_config(raw)
        self.monitor.reconfigure(config)
        self.config = config
        self.account = config["accounts"][0]
        self.emit(46, ANCHOR + 3900)
        self.assertEqual(2, len(self.sender.route("balance")))

    def test_recalibration_to_healthy_balance_cancels_pending_old_shortage(self):
        self.emit(46, drain=False)
        self.assertTrue(any(item["route"] == "balance" for item in self.store.state["outbox"]))
        raw = raw_config(self.path)
        raw["accounts"][0]["anchor"]["revision"] = "healthy-recalibration"
        config = validate_config(raw)
        self.monitor.reconfigure(config)
        self.config = config
        self.account = config["accounts"][0]
        self.emit(80, ANCHOR + 3900)
        self.assertEqual([], self.sender.route("balance"))
        self.assertFalse(any(item["route"] == "balance" for item in self.store.state["outbox"]))

    def test_legacy_queued_repeated_alerts_move_to_balance_and_keep_only_severest(self):
        record = self.monitor.record_for(self.account, ANCHOR + 300)
        # The old repeating policy could overwrite a deeper latch after a revision.
        record.update(last_threshold="50", last_threshold_at=ANCHOR + 7500)
        key = self.account["_id"]
        self.monitor.queue.enqueue("normal", "threshold/%s/50/%s" % (key, ANCHOR + 300), "old fifty", ANCHOR + 300)
        self.monitor.queue.enqueue("normal", "threshold/%s/50/%s" % (key, ANCHOR + 7500), "old repeated fifty", ANCHOR + 7500)
        self.monitor.queue.enqueue("normal", "threshold/%s/10/%s" % (key, ANCHOR + 4200), "old ten", ANCHOR + 4200)
        self.monitor.queue.enqueue("normal", "report/keep", "keep hourly", ANCHOR + 4200)
        self.restart()
        pending = [item for item in self.store.state["outbox"] if item["route"] == "balance"]
        self.assertEqual(["old ten"], [item["text"] for item in pending])
        self.assertEqual("10", self.record()["last_threshold"])
        delivery_id = pending[0]["id"]
        self.emit(9, ANCHOR + 7800, drain=False)
        pending = [item for item in self.store.state["outbox"] if item["route"] == "balance"]
        self.assertEqual([delivery_id], [item["id"] for item in pending])
        self.assertFalse(any(item["id"].startswith("normal/threshold/") for item in self.store.state["outbox"]))
        self.monitor.queue.drain(ANCHOR + 7800)
        self.assertEqual(["old ten"], self.sender.route("balance"))
        self.assertEqual(["keep hourly"], self.sender.route("normal"))


if __name__ == "__main__":
    unittest.main()
