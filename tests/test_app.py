"""Offline end-to-end accounting and routing tests, with no cloud requests."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traffic_monitor.app import Monitor, slot_for
from traffic_monitor.config import validate_config
from traffic_monitor.storage import StateStore
from traffic_monitor.tencent import TencentError


def epoch(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


ANCHOR = epoch("2026-10-30T00:00:00Z")


def raw_config(path, names=("账号 A",), calibrated=True, anchor=ANCHOR):
    start = datetime.fromtimestamp(anchor, timezone.utc).isoformat()
    return {
        "version": 1,
        "sampling": {"interval_seconds": 300, "lookback_minutes": 120,
                     "max_data_age_seconds": 900, "max_backfill_days": 31,
                     "request_timeout_seconds": 30},
        "reports": {"timezone": "UTC+08:00", "interval_minutes": 60},
        "alerts": {"remaining_percent": [50, 10], "repeat_seconds": 0,
                   "error_repeat_seconds": 1800, "safety_seconds": 0,
                   "safety_factor": "1", "runway_minutes": 0},
        "lark": {"normal_webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-normal",
                 "error_webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-error"},
        "storage": {"state_file": str(path)},
        "accounts": [{"name": name, "secret_id": "id-" + name, "secret_key": "secret-" + name,
                      "services": ["LVB"], "regions": ["China Mainland"],
                      "package_names": {"321018": "直播主流量包"},
                      "anchor": {"usage_start": start,
                                 "remaining_tb": "0.000000000100", "total_tb": "0.000000000100"}
                      if calibrated else {}}
                     for name in names],
    }


def package(left=100, total=100, status=1, identifier="321018", expires="2030-10-30 00:00:00"):
    return {"Id": identifier, "Type": 0, "Status": status, "Total": total,
            "Left": left, "Used": total - left,
            "BuyTime": "2026-10-01 00:00:00", "ExpireTime": expires}


def buckets(start, end, amounts=None, service="LVB", region="China Mainland"):
    amounts = amounts or {}
    return [{"time": timestamp, "service": service, "region": region,
             "bytes": amounts.get(timestamp, 0)} for timestamp in range(start, end, 300)]


class FakeClient:
    def __init__(self, secret_id, secret_key, **kwargs):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.endpoint = kwargs["endpoint"]


class CaptureSender:
    def __init__(self):
        self.messages = []

    def send(self, route, text):
        self.messages.append((route, text))

    def route(self, route):
        return [text for target, text in self.messages if target == route]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.config = validate_config(raw_config(self.path))
        self.store = StateStore(self.path)
        self.sender = CaptureSender()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        self.packages = patch("traffic_monitor.app.fetch_packages", return_value=[package()]).start()
        self.usage = patch("traffic_monitor.app.fetch_billable_buckets",
                           side_effect=lambda client, start, end, services, regions: buckets(start, end)).start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real cloud requests are forbidden")).start()
        self.addCleanup(patch.stopall)

    def record(self, index=0):
        return self.store.state["accounts"][self.config["accounts"][index]["_id"]]

    def sample(self, now, amounts=None):
        if amounts is not None:
            self.usage.side_effect = lambda client, start, end, services, regions: buckets(start, end, amounts)
        success = self.monitor.sample(now)
        self.monitor.queue.drain(now)
        return success

    def restart(self):
        self.store = StateStore(self.path)
        self.store.load()
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

    def test_healthy_five_minute_samples_do_not_send_normal_messages(self):
        for offset in (300, 600, 900):
            self.assertTrue(self.sample(ANCHOR + offset))
        self.assertTrue(self.record()["quality"])
        self.assertEqual([], self.sender.messages)

    def test_uncalibrated_account_keeps_hourly_usage_without_claiming_balance(self):
        config = validate_config(raw_config(self.path, calibrated=False))
        monitor = Monitor(config, self.store, sender=self.sender, client_factory=FakeClient)
        self.usage.side_effect = lambda client, start, end, services, regions: buckets(
            start, end, {t: 1 for t in range(ANCHOR, ANCHOR + 3600, 300)})
        self.assertFalse(monitor.sample(ANCHOR + 3600))
        text = monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("未校准", text)
        self.assertIn("12.000 B", text)
        self.assertIn("12/12", text)
        self.assertNotIn("估算剩余：", text)

    def test_strict_fifty_and_ten_percent_thresholds_only_when_crossed(self):
        history = {ANCHOR: 50}
        self.assertTrue(self.sample(ANCHOR + 300, history))  # Exactly 50%.
        self.assertEqual([], self.sender.route("normal"))
        history[ANCHOR + 300] = 1
        self.assertTrue(self.sample(ANCHOR + 600, history))
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("不足 50%", self.sender.route("normal")[-1])
        history[ANCHOR + 600] = 39
        self.assertTrue(self.sample(ANCHOR + 900, history))  # Exactly 10%.
        self.assertEqual(1, len(self.sender.route("normal")))
        history[ANCHOR + 900] = 1
        self.assertTrue(self.sample(ANCHOR + 1200, history))
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("不足 10%", self.sender.route("normal")[-1])
        self.assertIn("购买流量包", self.sender.route("normal")[-1])

    def test_deeper_threshold_deduplication_survives_restart(self):
        amounts = {ANCHOR: 91}
        self.sample(ANCHOR + 300, amounts)
        self.assertEqual(1, len(self.sender.route("normal")))
        self.restart()
        self.sample(ANCHOR + 600, amounts)
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertEqual("10", self.record()["last_threshold"])

    def test_same_scope_revisions_replace_previous_deduction(self):
        self.sample(ANCHOR + 300, {ANCHOR: 20})
        self.sample(ANCHOR + 600, {ANCHOR: 15, ANCHOR + 300: 10})
        self.assertEqual("25", self.record()["result"]["consumed_bytes"])
        self.assertEqual("75", self.record()["result"]["estimated_remaining_bytes"])

    def test_official_discrepancy_is_an_error_and_never_rebases_the_ledger(self):
        self.sample(ANCHOR + 300, {ANCHOR: 10})
        self.packages.return_value = [package(left=5)]
        self.assertFalse(self.sample(ANCHOR + 600, {ANCHOR: 10}))
        self.assertEqual("100", self.record()["ledger"]["balance_bytes"])
        self.assertEqual("90", self.record()["result"]["estimated_remaining_bytes"])
        self.assertEqual("5", self.record()["official"]["remaining_bytes"])
        self.assertFalse(self.record()["quality"])
        self.assertTrue(any("官方结算余量低于本地估算" in text for text in self.sender.route("error")))

    def test_consistent_official_remaining_refresh_keeps_the_same_ledger(self):
        self.sample(ANCHOR + 300, {ANCHOR: 10})
        self.packages.return_value = [package(left=90)]
        self.assertTrue(self.sample(ANCHOR + 600, {ANCHOR: 10}))
        self.assertEqual("100", self.record()["ledger"]["balance_bytes"])
        self.assertEqual("90", self.record()["result"]["estimated_remaining_bytes"])

    def test_new_package_requires_recalibration_instead_of_automatic_credit(self):
        self.sample(ANCHOR + 300)
        self.packages.return_value = [package(), package(identifier="new-plan")]
        self.assertFalse(self.sample(ANCHOR + 600))
        self.assertFalse(self.record()["quality"])
        self.assertTrue(self.record()["inventory_changed"])
        self.assertEqual("100", self.record()["ledger"]["balance_bytes"])
        self.assertTrue(self.sender.route("error"))

    def test_missing_previously_active_package_requires_recalibration(self):
        self.sample(ANCHOR + 300)
        self.packages.return_value = []
        self.assertFalse(self.sample(ANCHOR + 600))
        self.assertTrue(self.record()["inventory_changed"])
        self.assertFalse(self.record()["quality"])
        self.assertEqual([], self.sender.route("normal"))

    def test_wall_clock_expiry_is_detected_even_if_provider_status_is_still_active(self):
        self.packages.return_value = [package(expires="2026-10-30 08:07:00")]
        self.assertTrue(self.sample(ANCHOR + 300))
        self.assertFalse(self.sample(ANCHOR + 600))
        self.assertTrue(self.record()["inventory_changed"])
        self.assertEqual([], self.record()["official"]["packages"])
        self.assertFalse(self.record()["quality"])

    def test_changed_expiry_requires_recalibration(self):
        self.sample(ANCHOR + 300)
        self.packages.return_value = [package(expires="2031-10-30 00:00:00")]
        self.assertFalse(self.sample(ANCHOR + 600))
        self.assertTrue(self.record()["inventory_changed"])
        self.assertFalse(self.record()["quality"])

    def test_bad_key_errors_use_error_route_and_other_accounts_continue(self):
        self.config = validate_config(raw_config(self.path, names=("坏账号", "好账号")))
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

        def packages_for(client):
            if "坏账号" in client.secret_id:
                raise TencentError("AuthFailure.SecretIdNotFound", "invalid key")
            return [package()]

        def usage_for(client, start, end, services, regions):
            if "坏账号" in client.secret_id:
                raise TencentError("AuthFailure.SecretIdNotFound", "invalid key")
            return buckets(start, end)

        self.packages.side_effect = packages_for
        self.usage.side_effect = usage_for
        self.assertFalse(self.sample(ANCHOR + 300))
        self.assertFalse(self.record(0)["quality"])
        self.assertTrue(self.record(1)["quality"])
        self.assertEqual(ANCHOR + 300, self.record(1)["last_success"])
        self.assertTrue(any("AuthFailure.SecretIdNotFound" in text for text in self.sender.route("error")))
        self.assertEqual([], self.sender.route("normal"))

    def test_provider_error_secrets_are_redacted_from_error_messages(self):
        account = self.config["accounts"][0]
        self.packages.side_effect = RuntimeError("bad %s %s" % (account["secret_id"], account["secret_key"]))
        self.assertFalse(self.sample(ANCHOR + 300))
        for text in self.sender.route("error"):
            self.assertNotIn(account["secret_id"], text)
            self.assertNotIn(account["secret_key"], text)

    def test_uncalibrated_account_is_reported_as_unknown_not_healthy(self):
        self.config = validate_config(raw_config(self.path, calibrated=False))
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        self.assertFalse(self.sample(ANCHOR + 300))
        self.assertIsNone(self.record()["result"])
        self.assertFalse(self.record()["quality"])
        self.assertTrue(self.sender.route("error"))
        report = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("未校准", report)
        self.assertNotIn("状态：已校准", report)
        self.assertNotIn("估算剩余：", report)

    def test_old_data_does_not_generate_a_healthy_or_business_alert(self):
        self.usage.side_effect = None
        self.usage.return_value = buckets(ANCHOR, ANCHOR + 300, {ANCHOR: 91})
        self.assertFalse(self.sample(ANCHOR + 1800))
        self.assertFalse(self.record()["quality"])
        self.assertTrue(any("落后" in text for text in self.sender.route("error")))
        self.assertEqual([], self.sender.route("normal"))

    def test_missing_region_or_internal_interval_is_an_error_not_zero_usage(self):
        self.config["accounts"][0]["regions"] = ["China Mainland", "Europe"]
        self.assertFalse(self.sample(ANCHOR + 300))
        self.assertIn("LVB/Europe", self.record()["result"]["missing_dimensions"])
        self.assertFalse(self.record()["quality"])
        self.config["accounts"][0]["regions"] = ["China Mainland"]
        self.usage.side_effect = None
        self.usage.return_value = [buckets(ANCHOR, ANCHOR + 300)[0],
                                   buckets(ANCHOR + 600, ANCHOR + 900)[0]]
        self.assertFalse(self.sample(ANCHOR + 900))
        self.assertGreater(self.record()["result"]["internal_missing_intervals"], 0)
        self.assertEqual([], self.sender.route("normal"))

    def test_hourly_report_is_clock_aligned_and_deduplicated_after_restart(self):
        start = ANCHOR + 17 * 60  # 08:17 Beijing time.
        self.assertFalse(self.monitor.reports(start))
        self.assertFalse(self.monitor.reports(ANCHOR + 59 * 60))
        self.assertTrue(self.monitor.reports(ANCHOR + 3600))  # 09:00, not 09:17.
        self.monitor.queue.drain(ANCHOR + 3600)
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("2026-10-30 08:00:00 +0800", self.sender.route("normal")[0])
        self.assertIn("2026-10-30 09:00:00 +0800", self.sender.route("normal")[0])
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3601))
        self.monitor.queue.drain(ANCHOR + 3601)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_first_partial_hour_states_coverage_and_data_timestamp(self):
        anchor = ANCHOR + 1800
        self.config = validate_config(raw_config(self.path, anchor=anchor))
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        self.assertTrue(self.sample(anchor + 300))
        self.monitor.reports(anchor + 300)
        self.assertTrue(self.monitor.reports(ANCHOR + 3600))
        self.monitor.queue.drain(ANCHOR + 3600)
        report = self.sender.route("normal")[-1]
        self.assertIn("本窗口早于校准基准", report)
        self.assertIn("统计不完整", report)
        self.assertIn("用量截至", report)
        self.assertIn("1/12", report)

    def test_dry_run_previews_without_state_or_messages(self):
        self.store = StateStore(self.path, read_only=True)
        self.monitor = Monitor(self.config, self.store, sender=self.sender,
                               client_factory=FakeClient, dry_run=True)
        self.assertTrue(self.sample(ANCHOR + 300, {ANCHOR: 91}))
        self.monitor.reports(ANCHOR + 300, force=True)
        self.monitor.queue.drain(ANCHOR + 300)
        self.assertFalse(self.path.exists())
        self.assertEqual([], self.sender.messages)


if __name__ == "__main__":
    unittest.main()
