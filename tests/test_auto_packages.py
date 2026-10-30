"""Offline purchase lifecycle regressions for the actual Monitor integration."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_app import ANCHOR, CaptureSender, FakeClient, buckets, raw_config
from traffic_monitor.app import Monitor
from traffic_monitor.__main__ import main
from traffic_monitor.config import validate_config
from traffic_monitor.ledger import new_ledger
from traffic_monitor.storage import StateStore
from traffic_monitor.tencent import TencentError


MB = 1_000_000


def api_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def plan(identifier="321018", total=100 * MB, left=40 * MB, status=1,
         bought=ANCHOR - 86400, expires=ANCHOR + 365 * 86400):
    result = {"Id": identifier, "Type": 0, "Status": status, "Total": total,
              "Used": total-left, "Left": left, "ExpireTime": api_time(expires)}
    if bought is not None:
        result["BuyTime"] = api_time(bought)
    return result


class AutoPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        raw = raw_config(self.path)
        raw["accounts"][0]["anchor"].update(remaining_tb="0.00004", total_tb="0.0001")
        self.config = validate_config(raw)
        self.account = self.config["accounts"][0]
        self.store = StateStore(self.path)
        self.sender = CaptureSender()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        self.amounts = {}
        self.plans = [plan()]
        self.packages = patch("traffic_monitor.app.fetch_packages", side_effect=lambda _client: self.plans).start()
        self.usage = patch("traffic_monitor.app.fetch_billable_buckets", side_effect=lambda _client, start, end, _services, _regions:
                           buckets(start, end, self.amounts)).start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real cloud requests are forbidden")).start()
        self.addCleanup(patch.stopall)

    def record(self):
        return self.store.state["accounts"][self.account["_id"]]

    def sample(self, offset):
        success = self.monitor.sample(ANCHOR + offset)
        self.monitor.queue.drain(ANCHOR + offset)
        return success

    def restart(self):
        self.store = StateStore(self.path)
        self.store.load()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

    def add_new(self, **changes):
        values = {"identifier": "new-plan", "total": 100 * MB, "left": 100 * MB,
                  "bought": ANCHOR + 600, "expires": ANCHOR + 366 * 86400}
        values.update(changes)
        self.plans.append(plan(**values))

    def test_new_purchase_uses_total_not_already_deducted_official_left(self):
        self.amounts = {ANCHOR: 10 * MB}
        self.assertTrue(self.sample(300))
        self.add_new(left=80 * MB)
        self.amounts.update({ANCHOR + 300: 5 * MB, ANCHOR + 600: 20 * MB})
        self.assertTrue(self.sample(900))
        self.assertEqual(str(105 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertEqual(str(200 * MB), self.record()["result"]["active_capacity_bytes"])
        self.assertEqual(str(40 * MB), self.record()["ledger"]["balance_bytes"])
        self.assertEqual(self.account["_anchor_id"], self.record()["anchor_id"])
        self.plans[1].update(Left=70 * MB, Used=30 * MB)
        self.assertTrue(self.sample(1200))
        self.assertEqual(str(105 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.amounts[ANCHOR] = 8 * MB
        self.assertTrue(self.sample(1500))
        self.assertEqual(str(107 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_same_purchase_is_not_added_twice_and_survives_restart(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.assertTrue(self.sample(900))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertTrue(self.sample(1200))
        self.restart()
        self.assertTrue(self.sample(1500))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_post_anchor_purchase_is_detected_even_in_first_snapshot(self):
        self.add_new()
        self.assertTrue(self.sample(900))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_newly_surfaced_pre_anchor_active_package_is_not_new_credit(self):
        self.assertTrue(self.sample(300))
        self.add_new(identifier="older-plan", bought=ANCHOR - 3600)
        self.assertFalse(self.sample(900))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertFalse(self.record()["quality"])

    def test_new_package_without_purchase_time_cannot_inflate_balance(self):
        self.assertTrue(self.sample(300))
        self.add_new(bought=None)
        self.assertFalse(self.sample(900))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertFalse(self.record()["quality"])

    def test_historical_exhausted_packages_do_not_create_credit_or_alerts(self):
        self.plans.extend(plan(identifier="historic-%s" % index, left=0, status=4,
                               bought=ANCHOR - 366 * 86400, expires=ANCHOR - 86400)
                          for index in range(60))
        self.assertTrue(self.sample(300))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        # The baseline is below 50%, so one ordinary business alert is expected;
        # none of the historical package IDs should appear in any notification.
        self.assertFalse(any("historic-" in text for _route, text in self.sender.messages))

    def test_new_purchase_first_seen_exhausted_still_funds_its_recorded_usage(self):
        self.assertTrue(self.sample(300))
        self.add_new(left=0, status=4)
        self.amounts = {ANCHOR + 600: 140 * MB}
        self.assertTrue(self.sample(900))
        result = self.record()["result"]
        self.assertEqual("0", result["estimated_remaining_bytes"])
        self.assertEqual("0", result["overrun_bytes"])

    def test_failed_inventory_query_does_not_register_a_purchase(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.packages.side_effect = TencentError("InternalError", "offline package failure")
        self.assertFalse(self.sample(900))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.packages.side_effect = lambda _client: self.plans
        self.assertTrue(self.sample(1200))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_registered_purchase_survives_usage_query_failure_and_retry(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.usage.side_effect = TencentError("InternalError", "offline usage failure")
        self.assertFalse(self.sample(900))
        self.assertFalse(self.record()["quality"])
        self.restart()
        self.usage.side_effect = lambda _client, start, end, _services, _regions: buckets(start, end, self.amounts)
        self.assertTrue(self.sample(1200))
        self.assertTrue(self.sample(1500))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_new_lot_expiration_removes_only_its_unused_balance(self):
        self.assertTrue(self.sample(300))
        self.add_new(expires=ANCHOR + 1200)
        self.assertTrue(self.sample(900))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertTrue(self.sample(1500))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.plans = self.plans[:1]
        self.assertTrue(self.sample(1800))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.restart()
        self.assertTrue(self.sample(1800))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_refunded_new_lot_is_removed_once_without_recalibration(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.assertTrue(self.sample(900))
        self.plans[1]["Status"] = 5
        self.assertTrue(self.sample(1200))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertTrue(self.sample(1500))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.plans = self.plans[:1]
        self.assertTrue(self.sample(1800))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def legacy_state(self, include_new=False, inventory_changed=False):
        known = {}
        plans = self.plans if include_new else self.plans[:1]
        for item in plans:
            known[item["Id"]] = {"total": str(item["Total"]), "left": str(item["Left"]),
                                  "status": item["Status"],
                                  "expires": int(datetime.strptime(item["ExpireTime"], "%Y-%m-%d %H:%M:%S")
                                                 .replace(tzinfo=timezone(timedelta(hours=8))).timestamp())}
        self.store.state["accounts"][self.account["_id"]] = {
            "anchor_id": self.account["_anchor_id"], "name": self.account["name"],
            "ledger": new_ledger(ANCHOR, 40 * MB, 100 * MB), "quality": True,
            "inventory_changed": inventory_changed,
            "official": {"checked_at": ANCHOR + 300, "known": known,
                         "packages": [], "remaining_bytes": str(sum(item["Left"] for item in plans)),
                         "total_bytes": str(sum(item["Total"] for item in plans))},
        }
        self.store.save()

    def test_legacy_state_without_purchase_metadata_can_detect_new_purchase(self):
        self.legacy_state()
        self.add_new()
        self.restart()
        self.assertTrue(self.sample(900))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_legacy_new_purchase_latch_can_clear_after_metadata_proves_it(self):
        self.add_new()
        self.legacy_state(include_new=True, inventory_changed=True)
        self.restart()
        self.assertTrue(self.sample(900))
        self.assertTrue(self.record()["quality"])
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_legacy_baseline_id_cannot_be_credited_again_when_late_metadata_contradicts_it(self):
        self.plans[0].pop("BuyTime")
        self.legacy_state()
        self.restart()
        self.assertTrue(self.sample(300))
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.plans[0]["BuyTime"] = api_time(ANCHOR + 600)
        self.assertFalse(self.sample(900))
        self.assertFalse(self.record()["quality"])
        self.assertEqual(str(40 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.assertEqual(str(100 * MB), self.record()["result"]["active_capacity_bytes"])

    def test_registered_purchase_identity_change_does_not_add_a_second_total(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.assertTrue(self.sample(900))
        self.plans[1]["BuyTime"] = api_time(ANCHOR + 1200)
        self.assertFalse(self.sample(1500))
        self.assertFalse(self.record()["quality"])
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])
        self.restart()
        self.assertFalse(self.sample(1800))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_unknown_provider_status_is_an_error_not_a_healthy_credit(self):
        self.assertTrue(self.sample(300))
        self.add_new()
        self.assertTrue(self.sample(900))
        self.sender.messages.clear()
        self.plans[1]["Status"] = 7
        self.assertFalse(self.sample(1200))
        self.assertFalse(self.record()["quality"])
        self.assertTrue(self.sender.route("error"))
        self.assertEqual([], self.sender.route("normal"))
        self.assertEqual(str(140 * MB), self.record()["result"]["estimated_remaining_bytes"])

    def test_official_balance_can_reflect_debt_already_reserved_locally(self):
        self.amounts = {ANCHOR: 60 * MB}
        self.assertTrue(self.sample(300))
        self.add_new(left=80 * MB)
        self.plans[0].update(Status=4, Left=0, Used=100 * MB)
        self.assertTrue(self.sample(900))
        result = self.record()["result"]
        self.assertTrue(self.record()["quality"])
        self.assertEqual(str(100 * MB), result["estimated_remaining_bytes"])
        self.assertEqual(str(20 * MB), result["overrun_bytes"])
        self.assertEqual(str(20 * MB), result["uncovered_reserve_bytes"])
        self.assertEqual(str(80 * MB), result["safe_remaining_bytes"])
        self.assertFalse(any("官方结算余量低于本地估算" in issue for issue in self.record()["issues"]))
        self.restart()
        self.assertTrue(self.sample(1200))
        self.assertEqual(str(20 * MB), self.record()["result"]["overrun_bytes"])
        self.assertEqual(str(80 * MB), self.record()["result"]["safe_remaining_bytes"])

    def test_historical_unfunded_usage_is_not_silently_paid_by_new_purchase(self):
        self.amounts = {ANCHOR: 60 * MB}
        self.sample(300)
        self.add_new()
        self.sample(900)
        result = self.record()["result"]
        self.assertGreaterEqual(Decimal(result["overrun_bytes"]), 20 * MB)
        self.assertLessEqual(Decimal(result["safe_remaining_bytes"]),
                             max(Decimal(0), Decimal(result["estimated_remaining_bytes"]) - 20 * MB))

    def test_automatic_purchase_rearms_threshold_alerts_for_the_new_pool(self):
        self.amounts = {ANCHOR: 35 * MB}
        self.assertTrue(self.sample(300))
        self.assertTrue(any("不足 10%" in message for message in self.sender.route("normal")))
        self.add_new()
        self.assertTrue(self.sample(900))
        self.amounts[ANCHOR + 900] = 60 * MB
        self.assertTrue(self.sample(1200))
        self.assertTrue(any("不足 50%" in message for message in self.sender.route("normal")))

    def test_detected_new_funding_cancels_queued_old_low_balance_alert(self):
        self.amounts = {ANCHOR: 35 * MB}
        self.assertTrue(self.monitor.sample(ANCHOR + 300))
        prefix = "normal/threshold/%s/" % self.account["_id"]
        self.assertTrue(any(item["id"].startswith(prefix) for item in self.store.state["outbox"]))
        self.add_new()
        self.assertTrue(self.monitor.sample(ANCHOR + 900))
        self.assertFalse(any(item["id"].startswith(prefix) for item in self.store.state["outbox"]))
        self.monitor.queue.drain(ANCHOR + 900)
        self.assertFalse(any("不足 10%" in message for message in self.sender.route("normal")))
        self.assertTrue(any("新购流量包已识别" in message for message in self.sender.route("normal")))

    def test_cli_restart_samples_purchase_before_retrying_old_low_quota_message(self):
        self.amounts = {ANCHOR: 35 * MB}
        self.assertTrue(self.monitor.sample(ANCHOR + 300))
        with patch.object(self.sender, "send", side_effect=RuntimeError("offline normal delivery failure")):
            self.assertFalse(self.monitor.queue.drain(ANCHOR + 300))
        prefix = "normal/threshold/%s/" % self.account["_id"]
        self.assertTrue(any(item["id"].startswith(prefix) for item in self.store.state["outbox"]))
        self.add_new()
        raw = raw_config(self.path)
        raw["accounts"][0]["anchor"].update(remaining_tb="0.00004", total_tb="0.0001")
        config_path = self.path.with_name("config.json")
        config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.sender.messages.clear()
        with patch("traffic_monitor.__main__.time.time", return_value=ANCHOR + 900), \
                patch("traffic_monitor.app.TencentClient", FakeClient), \
                patch("traffic_monitor.notifications.LarkSender.send", side_effect=self.sender.send):
            code = main(["--config", str(config_path), "--once", "--report-now"])
        self.assertEqual(0, code)
        normal = self.sender.route("normal")
        self.assertFalse(any("不足 10%" in message for message in normal))
        self.assertTrue(any("新购流量包已识别" in message for message in normal))
        self.assertTrue(any("整点汇总" in message and "105.000 MB" in message for message in normal))
        restored = StateStore(self.path)
        restored.load()
        record = restored.state["accounts"][self.account["_id"]]
        self.assertEqual(str(105 * MB), record["result"]["estimated_remaining_bytes"])


if __name__ == "__main__":
    unittest.main()
