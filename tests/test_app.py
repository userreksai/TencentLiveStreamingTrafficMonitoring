"""Offline end-to-end accounting and routing tests, with no cloud requests."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traffic_monitor.app import Monitor, hourly_usage, slot_for
from traffic_monitor.config import validate_config
from traffic_monitor.reporting import window_usage
from traffic_monitor.storage import StateError, StateStore
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

    def test_known_baseline_expiry_is_automatic_even_if_provider_status_is_still_active(self):
        self.packages.return_value = [package(expires="2026-10-30 08:07:00")]
        self.assertTrue(self.sample(ANCHOR + 300))
        self.assertTrue(self.sample(ANCHOR + 600))
        self.assertFalse(self.record()["inventory_changed"])
        self.assertEqual([], self.record()["official"]["packages"])
        self.assertTrue(self.record()["quality"])
        self.assertEqual("0", self.record()["result"]["estimated_remaining_bytes"])
        self.assertEqual("100", self.record()["result"]["expired_bytes"])

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
        self.assertIn("2026-10-30 08:00:00", self.sender.route("normal")[0])
        self.assertIn("2026-10-30 09:00:00", self.sender.route("normal")[0])
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

    def use_uncalibrated_reporting(self, names=("账号 A",), regions=("China Mainland",)):
        raw = raw_config(self.path, names=names, calibrated=False)
        for account in raw["accounts"]:
            account["regions"] = list(regions)
        self.config = validate_config(raw)
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

    def report_usage(self, full_until, missing_region=None, revised=None):
        """Provide explicit zero rows too: absent dimensions must stay unknown."""
        revised = revised or {}

        def fetch(client, start, end, services, regions):
            result = []
            for service in services:
                for region in regions:
                    for timestamp in range(start, end, 300):
                        if timestamp >= full_until and region == missing_region:
                            continue
                        if timestamp >= full_until and missing_region is None:
                            continue
                        amount = revised.get((timestamp, service, region), 1_000_000)
                        result.append({"time": timestamp, "service": service,
                                       "region": region, "bytes": amount})
            return result

        self.usage.side_effect = fetch

    def test_hourly_usage_filters_half_open_window_alignment_and_scope(self):
        account = {"services": ["LVB"], "regions": ["China Mainland", "Europe"]}
        rows = [
            {"time": ANCHOR - 300, "service": "LVB", "region": "China Mainland", "bytes": 999},
            {"time": ANCHOR, "service": "LVB", "region": "China Mainland", "bytes": 10},
            {"time": ANCHOR, "service": "LVB", "region": "Europe", "bytes": 20},
            {"time": ANCHOR + 300, "service": "LVB", "region": "China Mainland", "bytes": 30},
            {"time": ANCHOR + 1, "service": "LVB", "region": "China Mainland", "bytes": 999},
            {"time": ANCHOR, "service": "LEB", "region": "Europe", "bytes": 999},
            {"time": ANCHOR + 600, "service": "LVB", "region": "China Mainland", "bytes": 999},
        ]
        amount, complete = hourly_usage({"buckets": dict(enumerate(rows))}, ANCHOR, ANCHOR + 600, account)
        self.assertEqual(Decimal("73.8460"), amount)
        self.assertEqual(1, complete)  # The second timestamp lacks Europe, not zero traffic.

    def test_hourly_report_separates_raw_mb_from_weighted_package_deduction(self):
        self.use_uncalibrated_reporting(regions=("China Mainland", "Europe"))
        self.report_usage(ANCHOR + 3600, revised={
            (timestamp, "LVB", "Europe"): 2_000_000
            for timestamp in range(ANCHOR, ANCHOR + 3600, 300)
        })
        self.sample(ANCHOR + 3600)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("原始播放流量：36.000000 MB（0.036000 GB）", text)
        self.assertIn("流量包折算扣量", text)
        self.assertIn("52.615 MB", text)  # 12 * (1 + 2 * 1.6923), in MB.
        self.assertIn("12/12", text)
        self.assertIn("完整统计", text)

    def test_healthy_report_follows_compact_notification_template(self):
        self.sample(ANCHOR + 3600)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertTrue(text.startswith("【腾讯云直播流量整点汇总】\n统计窗口："))
        self.assertIn("账号：账号 A", text)
        self.assertIn("官方结算余量：100.000 B（数据滞后，仅供对照）", text)
        self.assertIn("直播主流量包（ID 321018）使用中", text)
        self.assertIn("估算剩余：100.000 B（100.00%）", text)
        self.assertIn("一小时内用量：原始播放流量：", text)
        self.assertIn("用量截至：2026-10-30 09:00:00", text)
        self.assertTrue(text.endswith("说明：未到达的用量段不计为零；数据可能回补修订。"))
        self.assertEqual(9, len(text.splitlines()))
        for removed in ("生成时间", "统计范围", "安全可用", "预留", "预计安全可用时长",
                        "当前有效可用包总额度", "折算明细", "+0800"):
            self.assertNotIn(removed, text)

    def test_partial_timestamp_is_in_amount_but_not_complete_count(self):
        self.use_uncalibrated_reporting(regions=("China Mainland", "Europe"))
        self.report_usage(ANCHOR + 3300, missing_region="Europe")
        self.sample(ANCHOR + 3600)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("原始播放流量：23.000000 MB（0.023000 GB）", text)
        self.assertIn("11/12", text)
        self.assertIn("暂报", text)
        self.assertIn("不完整", text)

    def test_incomplete_hour_is_supplemented_once_when_all_data_arrives(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.assertTrue(self.monitor.reports(ANCHOR + 3600))
        self.monitor.queue.drain(ANCHOR + 3600)
        first = self.sender.route("normal")[-1]
        self.assertIn("暂报", first)
        self.assertIn("11/12", first)
        self.assertIn("11.000000 MB", first)

        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        reports = self.sender.route("normal")
        self.assertEqual(2, len(reports))
        self.assertIn("补报", reports[-1])
        self.assertIn("12/12", reports[-1])
        self.assertIn("12.000000 MB", reports[-1])
        self.assertIn("2026-10-30 08:00:00", reports[-1])
        self.assertIn("2026-10-30 09:00:00", reports[-1])
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.monitor.queue.drain(ANCHOR + 3901)
        self.assertEqual(2, len(self.sender.route("normal")))

    def test_partial_updates_wait_for_completion_before_sending_supplement(self):
        self.use_uncalibrated_reporting(regions=("China Mainland", "Europe"))
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300, missing_region="Europe")
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.report_usage(ANCHOR + 3300, missing_region="Europe", revised={
            (ANCHOR, "LVB", "China Mainland"): 2_000_000,
        })
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_completed_hour_revisions_are_reported_and_deduplicated(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.report_usage(ANCHOR + 3900, revised={
            (ANCHOR + 3300, "LVB", "China Mainland"): 2_000_000,
        })
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("修订", self.sender.route("normal")[-1])
        self.assertIn("13.000000 MB", self.sender.route("normal")[-1])
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.monitor.queue.drain(ANCHOR + 3901)
        self.assertEqual(2, len(self.sender.route("normal")))

    def test_completion_waits_for_all_accounts_in_the_report(self):
        self.use_uncalibrated_reporting(names=("账号 A", "账号 B"))
        self.monitor.reports(ANCHOR + 1800)

        def initial_usage(client, start, end, services, regions):
            cutoff = ANCHOR + (3300 if "账号 B" in client.secret_id else 3600)
            return buckets(start, min(end, cutoff), {
                timestamp: 1_000_000 for timestamp in range(start, min(end, cutoff), 300)
            })

        self.usage.side_effect = initial_usage
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.assertIn("12/12", self.sender.route("normal")[-1])
        self.assertIn("11/12", self.sender.route("normal")[-1])
        self.assertFalse(self.monitor.reports(ANCHOR + 3601))
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("补报", self.sender.route("normal")[-1])
        self.assertNotIn("11/12", self.sender.route("normal")[-1])

    def test_pending_completion_notification_survives_restart_without_duplicates(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.restart()  # The supplement is queued but has not been sent yet.
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.monitor.queue.drain(ANCHOR + 3901)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("补报", self.sender.route("normal")[-1])

    def test_completion_replaces_unsent_provisional_report_with_latest_data(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
        self.assertEqual(1, len(pending))
        self.assertIn("暂报", pending[0]["text"])
        pending[0]["attempts"] = 3
        pending[0]["next_attempt"] = ANCHOR + 7200

        self.report_usage(ANCHOR + 3900)
        self.monitor.sample(ANCHOR + 3900)  # Leave the old report undelivered.
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
        self.assertEqual(1, len(pending))
        self.assertIn("补报", pending[0]["text"])
        self.assertIn("12.000000 MB", pending[0]["text"])
        self.assertNotIn("暂报", pending[0]["text"])
        self.assertEqual(0, pending[0]["attempts"])
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("补报", self.sender.route("normal")[0])

    def test_queued_revisions_returning_to_same_value_keep_only_latest_text(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        # A -> B -> A reuses A's content signature. The latest A must replace B.
        for now, amount, total_mb in ((3900, 2_000_000, "13.000000"),
                                      (4200, 3_000_000, "14.000000"),
                                      (4500, 2_000_000, "13.000000")):
            self.report_usage(ANCHOR + now, revised={
                (ANCHOR + 3300, "LVB", "China Mainland"): amount,
            })
            self.monitor.sample(ANCHOR + now)
            self.assertTrue(self.monitor.reports(ANCHOR + now))
            pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
            self.assertEqual(1, len(pending))
            self.assertIn("修订", pending[0]["text"])
            self.assertIn("原始播放流量：%s MB" % total_mb, pending[0]["text"])
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 4501))
        self.monitor.queue.drain(ANCHOR + 4501)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("原始播放流量：13.000000 MB", self.sender.route("normal")[-1])

    def test_scope_change_discards_queued_reports_but_keeps_business_and_error_messages(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.enqueue("normal", "threshold/account/10/test", "business alert", ANCHOR + 3600)
        self.monitor.queue.incident("offline/unavailable", "error alert", ANCHOR + 3600)
        retained = {item["id"] for item in self.store.state["outbox"]
                    if not item["id"].startswith(("normal/report/", "normal/report-update/"))}
        self.assertEqual(2, len(retained))
        raw = raw_config(self.path, calibrated=False)
        raw["accounts"][0]["regions"] = ["China Mainland", "Europe"]
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.assertEqual(retained, {item["id"] for item in self.store.state["outbox"]})
        self.restart()
        self.assertEqual(retained, {item["id"] for item in self.store.state["outbox"]})
        self.monitor.queue.drain(ANCHOR + 3601)
        self.assertEqual(["business alert"], self.sender.route("normal"))
        self.assertTrue(any("error alert" in text for text in self.sender.route("error")))

    def test_schedule_change_discards_queued_report_before_next_collection(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.assertTrue(any(item["route"] == "normal" for item in self.store.state["outbox"]))
        raw = raw_config(self.path, calibrated=False)
        raw["reports"]["interval_minutes"] = 30
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.assertFalse(any(item["route"] == "normal" for item in self.store.state["outbox"]))

    def test_unchanged_five_second_scheduler_ticks_do_not_write_state(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            for offset in range(3605, 3660, 5):
                self.assertFalse(self.monitor.reports(ANCHOR + offset))
            save.assert_not_called()

    def test_upgrade_can_complete_last_reported_hour_without_resending_provisional(self):
        self.use_uncalibrated_reporting()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        # Simulate a 1.1.0 state: an hourly report was sent, without the new tracker.
        self.store.state["reports"]["UTC+08:00/60"] = ANCHOR + 3600
        self.store.save()
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3601))
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("补报", self.sender.route("normal")[-1])

    def test_current_hour_new_data_does_not_revise_previous_hour(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.report_usage(ANCHOR + 3900, revised={
            (ANCHOR + 3600, "LVB", "China Mainland"): 99_000_000,
        })
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_revisions_that_preserve_raw_total_still_update_folded_deduction(self):
        self.use_uncalibrated_reporting(regions=("China Mainland", "Europe"))
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        self.report_usage(ANCHOR + 3900, revised={
            (ANCHOR + 3300, "LVB", "China Mainland"): 0,
            (ANCHOR + 3300, "LVB", "Europe"): 2_000_000,
        })
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("修订", self.sender.route("normal")[-1])
        self.assertIn("原始播放流量：24.000000 MB", self.sender.route("normal")[-1])
        self.assertIn("33.000 MB", self.sender.route("normal")[-1])

    def test_explicit_zero_rows_complete_the_hour(self):
        self.use_uncalibrated_reporting()
        self.usage.side_effect = lambda client, start, end, services, regions: buckets(start, end)
        self.sample(ANCHOR + 3600)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("原始播放流量：0.000000 MB（0.000000 GB）", text)
        self.assertIn("流量包折算扣量：0.000 B", text)
        self.assertIn("完整统计", text)
        self.assertIn("12/12", text)
        self.assertNotIn("暂报", text)

    def test_push_traffic_is_separate_from_raw_playback(self):
        raw = raw_config(self.path, calibrated=False)
        raw["accounts"][0]["services"] = ["LVB", "PUSH"]
        self.config = validate_config(raw)
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        usage = window_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(Decimal(12_000_000), usage["raw"])
        self.assertEqual(Decimal(12_000_000), usage["push"])
        self.assertEqual(Decimal(24_000_000), usage["folded"])
        self.assertEqual(12, usage["complete"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("原始播放流量：12.000000 MB", text)
        self.assertIn("原始推流流量：12.000000 MB", text)

    def test_continuous_cutoff_stops_at_first_gap_even_with_later_complete_data(self):
        self.use_uncalibrated_reporting()
        self.usage.side_effect = lambda client, start, end, services, regions: [
            row for row in buckets(start, end) if row["time"] != ANCHOR + 600
        ]
        self.sample(ANCHOR + 3600)
        usage = window_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(11, usage["complete"])
        self.assertEqual(0, usage["partial"])
        self.assertEqual(ANCHOR + 600, usage["cutoff"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3600)
        self.assertIn("暂报", text)
        self.assertIn("用量截至：2026-10-30 08:10:00", text)

    def test_expired_correction_window_does_not_send_historical_revision(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        raw = raw_config(self.path, calibrated=False)
        raw["sampling"]["lookback_minutes"] = 5
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        key = "%s|LVB|China Mainland" % ANCHOR
        self.record()["usage"]["buckets"][key]["bytes"] = 2_000_000
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3901)
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.monitor.queue.drain(ANCHOR + 3901)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_corrupt_report_tracker_is_rejected_without_overwriting_state(self):
        window = {str(ANCHOR + 3600): {"signature": "abc", "complete": True}}
        malformed = [
            [],
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": []},
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": {
                "bad-time": {"signature": "abc", "complete": True}}},
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": {
                str(ANCHOR): {"signature": 1, "complete": True}}},
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": {
                str(ANCHOR): {"signature": "abc", "complete": 1}}},
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": window,
             "observation": [["account", 1, "not-a-time"]]},
            {"schedule": "UTC+08:00/60", "scope": "abc", "windows": window,
             "observation": [["account", True, 1]]},
        ]
        for updates in malformed:
            with self.subTest(updates=updates):
                state = {"version": 1, "accounts": {}, "outbox": [], "errors": {}, "reports": {},
                         "report_updates": updates}
                original = json.dumps(state)
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(StateError):
                    StateStore(self.path).load()
                self.assertEqual(original, self.path.read_text(encoding="utf-8"))

    def test_schedule_change_starts_at_next_boundary_without_old_supplement(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        changed = raw_config(self.path, calibrated=False)
        changed["reports"]["interval_minutes"] = 30
        self.config = validate_config(changed)
        self.monitor.reconfigure(self.config)
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.monitor.reports(ANCHOR + 3900))
        self.assertTrue(self.monitor.reports(ANCHOR + 5400))
        self.monitor.queue.drain(ANCHOR + 5400)
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("2026-10-30 09:30:00", self.sender.route("normal")[-1])

    def test_scope_change_does_not_compare_new_scope_with_old_hour(self):
        self.use_uncalibrated_reporting()
        self.monitor.reports(ANCHOR + 1800)
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3600)
        self.monitor.reports(ANCHOR + 3600)
        self.monitor.queue.drain(ANCHOR + 3600)
        raw = raw_config(self.path, calibrated=False)
        raw["accounts"][0]["regions"] = ["China Mainland", "Europe"]
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.monitor.reports(ANCHOR + 3900))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertEqual(1, len(self.sender.route("normal")))

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
