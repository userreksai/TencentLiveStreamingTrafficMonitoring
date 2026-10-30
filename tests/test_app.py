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
        "reports": {"timezone": "UTC+08:00", "interval_minutes": 60,
                    "delay_minutes": 0, "revision_updates": True},
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


def today_snapshot(client, now, offset=8*3600):
    start = ((now + offset) // 86400) * 86400 - offset
    return {"day_start": start, "checked_at": now, "through": now,
            "bytes": "4470000000000", "latest_data_time": now // 300 * 300}


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
        self.today = patch("traffic_monitor.app.fetch_today_downstream",
                           side_effect=today_snapshot, create=True).start()
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
        self.assertIn("一小时内流量包用量：0.000000 TB", text)
        self.assertNotIn("（100.00%）", text)

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
        self.assertNotIn("（100.00%）", report)

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

    def use_reporting(self, names=("账号 A",), regions=("China Mainland",), delay=5,
                      revisions=False, calibrated=False, anchor=ANCHOR, defaults=False):
        raw = raw_config(self.path, names=names, calibrated=calibrated, anchor=anchor)
        if defaults:
            raw["reports"].pop("delay_minutes")
            raw["reports"].pop("revision_updates")
        else:
            raw["reports"].update(delay_minutes=delay, revision_updates=revisions)
        for account in raw["accounts"]:
            account["regions"] = list(regions)
        self.config = validate_config(raw)
        self.monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=FakeClient)

    def report_usage(self, full_until, missing_region=None, revised=None):
        revised = revised or {}

        def fetch(client, start, end, services, regions):
            result = []
            for service in services:
                for region in regions:
                    for timestamp in range(start, end, 300):
                        if timestamp >= full_until and (missing_region is None or region == missing_region):
                            continue
                        result.append({"time": timestamp, "service": service, "region": region,
                                       "bytes": revised.get((timestamp, service, region), 1_000_000_000)})
            return result

        self.usage.side_effect = fetch

    def begin_hour(self):
        self.assertFalse(self.monitor.reports(ANCHOR + 1800))

    def send_hour(self, now=ANCHOR + 3900):
        sent = self.monitor.reports(now)
        self.monitor.queue.drain(now)
        return sent

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
        self.assertEqual(1, complete)

    def test_default_hourly_report_waits_until_five_minutes_after_boundary(self):
        self.use_reporting(defaults=True)
        self.assertEqual(5, self.config["reports"]["delay_minutes"])
        self.assertFalse(self.config["reports"]["revision_updates"])
        self.begin_hour()
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.assertFalse(self.send_hour(ANCHOR + 3600))
        self.assertFalse(self.send_hour(ANCHOR + 3899))
        self.assertEqual([], self.sender.route("normal"))
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("2026-10-30 08:00:00 ～ 2026-10-30 09:00:00", self.sender.route("normal")[0])

    def test_delayed_report_requires_a_collection_after_the_delay(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.assertFalse(self.send_hour(ANCHOR + 3900))
        self.report_usage(ANCHOR + 3900, revised={(ANCHOR + 3300, "LVB", "China Mainland"): 2_000_000_000})
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.assertIn("一小时内流量包用量：0.013000 TB", self.sender.route("normal")[-1])

    def test_failed_delayed_collection_does_not_send_preliminary_prior_sample(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.usage.side_effect = TencentError("InternalError", "delayed collection failed")
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        self.assertEqual([], self.sender.route("normal"))
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_partial_hour_waits_without_provisional_until_data_is_complete(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        self.assertEqual([], self.sender.route("normal"))
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("一小时内流量包用量：0.012000 TB", self.sender.route("normal")[-1])
        self.assertNotIn("暂报", self.sender.route("normal")[-1])

    def test_pending_incomplete_window_survives_restart(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        self.restart()
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_manual_partial_report_does_not_advance_automatic_schedule(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900, force=True))
        self.monitor.queue.drain(ANCHOR + 3900)
        self.assertIn("数据不完整", self.sender.route("normal")[-1])
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertNotIn("数据不完整", self.sender.route("normal")[-1])

    def test_default_later_revisions_update_usage_without_second_hourly_message(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.report_usage(ANCHOR + 4200, revised={(ANCHOR + 3300, "LVB", "China Mainland"): 2_000_000_000})
        self.sample(ANCHOR + 4200)
        self.assertFalse(self.send_hour(ANCHOR + 4200))
        amount, complete = hourly_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(Decimal(13_000_000_000), amount)
        self.assertEqual(12, complete)
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_optional_revision_uses_same_compact_template_and_restart_deduplication(self):
        self.use_reporting(revisions=True)
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.report_usage(ANCHOR + 4200, revised={(ANCHOR + 3300, "LVB", "China Mainland"): 2_000_000_000})
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        reports = self.sender.route("normal")
        self.assertEqual(2, len(reports))
        self.assertTrue(all(text.startswith("【腾讯云直播流量整点汇总】\n统计窗口：") for text in reports))
        self.assertIn("一小时内流量包用量：0.013000 TB", reports[-1])
        self.assertNotIn("用量修订", reports[-1])
        self.assertNotIn("重复累加", reports[-1])
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(2, len(self.sender.route("normal")))

    def test_all_report_kinds_follow_exact_six_line_healthy_template(self):
        self.sample(ANCHOR + 3900)
        expected = "\n".join([
            "【腾讯云直播流量整点汇总】",
            "统计窗口：2026-10-30 08:00:00 ～ 2026-10-30 09:00:00",
            "直播主流量包（ID 321018）使用中",
            "流量包估算剩余：0.000 TB（100.00%）",
            "今日下行流量：4.470000 TB",
            "一小时内流量包用量：0.000000 TB",
        ])
        for kind in ("regular", "completion", "revision"):
            with self.subTest(kind=kind):
                text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900, kind=kind)
                self.assertEqual(expected, text)

    def test_compact_hourly_amount_is_folded_usage_not_raw_traffic(self):
        self.use_reporting(regions=("China Mainland", "Europe"))
        self.report_usage(ANCHOR + 3900, revised={
            (timestamp, "LVB", "Europe"): 2_000_000_000
            for timestamp in range(ANCHOR, ANCHOR + 3600, 300)
        })
        self.sample(ANCHOR + 3900)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("一小时内流量包用量：0.052615 TB", text)
        for omitted in ("原始播放流量", "官方结算余量", "账号：", "说明：", "用量截至", " MB", " GB", "+0800"):
            self.assertNotIn(omitted, text)

    def test_daily_downstream_is_queried_independently_from_scope_and_anchor(self):
        self.use_reporting(regions=("Europe",))
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.today.assert_called_once()
        args, kwargs = self.today.call_args
        self.assertEqual(ANCHOR + 3900, args[1])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("今日下行流量：4.470000 TB", text)
        self.assertIn("一小时内流量包用量：0.020308 TB", text)

    def test_today_query_uses_configured_report_timezone(self):
        self.use_reporting()
        raw = raw_config(self.path, calibrated=False)
        raw["reports"].update(timezone="UTC+07:00", delay_minutes=5, revision_updates=False)
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        args, kwargs = self.today.call_args
        offset = args[2] if len(args) > 2 else kwargs.get("offset")
        self.assertEqual(7 * 3600, offset)

    def test_daily_downstream_failure_uses_error_channel_without_resetting_ledger(self):
        self.today.side_effect = TencentError("InternalError", "today statistics unavailable")
        self.assertFalse(self.sample(ANCHOR + 3900))
        self.assertEqual("100", self.record()["ledger"]["balance_bytes"])
        self.assertTrue(self.record()["quality"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("今日下行流量：", text)
        self.assertTrue("获取失败" in text or "待更新" in text)
        self.assertNotIn("今日下行流量：0.000000 TB", text)
        self.assertTrue(any("今日" in text for text in self.sender.route("error")))

    def test_stale_daily_value_is_marked_pending_instead_of_presented_as_current(self):
        self.sample(ANCHOR + 3900)
        self.record()["today_downstream"]["checked_at"] = ANCHOR
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("今日下行流量：待更新", text)
        self.assertNotIn("今日下行流量：4.470000 TB", text)

    def test_stale_daily_source_is_an_independent_incident_and_recovers(self):
        self.today.side_effect = lambda client, now, offset=8*3600: dict(
            today_snapshot(client, now, offset), latest_data_time=now - 1800)
        self.assertFalse(self.sample(ANCHOR + 3900))
        self.assertTrue(self.record()["quality"])
        self.assertEqual("100", self.record()["ledger"]["balance_bytes"])
        self.assertEqual("100", self.record()["result"]["estimated_remaining_bytes"])
        incident_key = self.config["accounts"][0]["name"] + "/today_downstream"
        incident = self.store.state["errors"][incident_key]
        self.assertTrue(incident["active"])
        self.assertEqual("DataStale", incident["fingerprint"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("今日下行流量：数据滞后", text)
        self.assertNotIn("今日下行流量：4.470000 TB", text)
        self.today.side_effect = today_snapshot
        self.assertTrue(self.sample(ANCHOR + 4200))
        self.assertFalse(self.store.state["errors"][incident_key]["active"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 4200)
        self.assertIn("今日下行流量：4.470000 TB", text)
        self.assertTrue(any("监控恢复" in message and "today_downstream" in message
                            for message in self.sender.route("error")))

    def test_explicit_zero_daily_total_without_rows_is_accepted(self):
        self.today.side_effect = lambda client, now, offset=8*3600: dict(
            today_snapshot(client, now, offset), bytes="0", latest_data_time=None)
        self.assertTrue(self.sample(ANCHOR + 3900))
        self.assertTrue(self.record()["quality"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("今日下行流量：0.000000 TB", text)
        self.assertNotIn("今日下行流量：数据滞后", text)
        self.assertNotIn("今日下行流量：获取失败", text)

    def test_low_balance_business_alerts_also_use_tb_units(self):
        self.sample(ANCHOR + 300, {ANCHOR: 91})
        text = self.sender.route("normal")[-1]
        self.assertIn("估算剩余：0.000 TB", text)
        self.assertIn("安全可用：0.000 TB", text)
        self.assertNotRegex(text, r"\d(?:\.\d+)? (?:B|KB|MB|GB|PB)\b")

    def test_prior_day_daily_value_is_not_reused_after_midnight_failure(self):
        midnight = epoch("2026-10-31T00:00:00+08:00")
        self.use_reporting()
        self.report_usage(midnight)
        self.sample(midnight - 300)
        self.today.side_effect = TencentError("InternalError", "new day statistics unavailable")
        self.report_usage(midnight + 300)
        self.sample(midnight + 300)
        text = self.monitor.report_text(midnight, midnight + 300)
        self.assertIn("今日下行流量：", text)
        self.assertNotIn("今日下行流量：4.470000 TB", text)
        self.assertTrue("获取失败" in text or "待更新" in text)

    def test_midnight_report_keeps_previous_hour_window_and_current_day_total(self):
        midnight = epoch("2026-10-31T00:00:00+08:00")
        self.use_reporting()
        self.assertFalse(self.monitor.reports(midnight - 1800))
        self.today.side_effect = lambda client, now, offset=8*3600: dict(today_snapshot(client, now, offset), bytes="500000000")
        self.report_usage(midnight + 300)
        self.sample(midnight + 300)
        self.assertTrue(self.send_hour(midnight + 300))
        text = self.sender.route("normal")[-1]
        self.assertIn("2026-10-30 23:00:00 ～ 2026-10-31 00:00:00", text)
        self.assertIn("今日下行流量：0.000500 TB", text)

    def test_restart_within_delay_still_sends_just_finished_hour_at_due_time(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3600)
        self.sample(ANCHOR + 3600)
        self.assertFalse(self.send_hour(ANCHOR + 3600))
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 3720))
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_reporting_waits_for_all_accounts_to_have_complete_fresh_data(self):
        self.use_reporting(names=("账号 A", "账号 B"))
        self.begin_hour()

        def initial(client, start, end, services, regions):
            cutoff = ANCHOR + (3300 if "账号 B" in client.secret_id else 3900)
            return buckets(start, min(end, cutoff))

        self.usage.side_effect = initial
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        text = self.sender.route("normal")[-1]
        self.assertIn("账号：账号 A", text)
        self.assertIn("账号：账号 B", text)
        self.assertEqual(2, text.count("今日下行流量："))

    def test_incomplete_manual_preview_marks_known_usage_as_incomplete(self):
        self.use_reporting()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("一小时内流量包用量：0.011000 TB", text)
        self.assertIn("数据不完整", text)
        self.assertIn("11/12", text)

    def test_scope_filter_and_partial_dimensions_do_not_fake_completeness(self):
        self.use_reporting(regions=("China Mainland", "Europe"))
        self.report_usage(ANCHOR + 3300, missing_region="Europe")
        self.sample(ANCHOR + 3900)
        usage = window_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(11, usage["complete"])
        self.assertEqual(1, usage["partial"])
        self.assertEqual(Decimal(23_000_000_000), usage["raw"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("数据不完整", text)
        self.assertIn("11/12", text)

    def test_continuous_cutoff_stops_at_first_gap_even_with_later_complete_data(self):
        self.use_reporting()
        self.usage.side_effect = lambda client, start, end, services, regions: [
            row for row in buckets(start, end) if row["time"] != ANCHOR + 600]
        self.sample(ANCHOR + 3900)
        usage = window_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(11, usage["complete"])
        self.assertEqual(ANCHOR + 600, usage["cutoff"])
        self.assertIn("数据不完整", self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900))

    def test_explicit_zero_rows_complete_the_hour(self):
        self.use_reporting()
        self.begin_hour()
        self.usage.side_effect = lambda client, start, end, services, regions: buckets(start, end)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.assertIn("一小时内流量包用量：0.000000 TB", self.sender.route("normal")[-1])
        self.assertNotIn("数据不完整", self.sender.route("normal")[-1])

    def test_optional_push_is_in_package_usage_but_not_today_downstream_total(self):
        self.use_reporting()
        self.config["accounts"][0]["services"] = ["LVB", "PUSH"]
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        usage = window_usage(self.record()["usage"], ANCHOR, ANCHOR + 3600, self.config["accounts"][0])
        self.assertEqual(Decimal(12_000_000_000), usage["raw"])
        self.assertEqual(Decimal(12_000_000_000), usage["push"])
        self.assertEqual(Decimal(24_000_000_000), usage["folded"])
        text = self.monitor.report_text(ANCHOR + 3600, ANCHOR + 3900)
        self.assertIn("一小时内流量包用量：0.024000 TB", text)
        self.assertIn("今日下行流量：4.470000 TB", text)

    def test_current_hour_new_data_does_not_revise_previous_hour(self):
        self.use_reporting(revisions=True)
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        self.report_usage(ANCHOR + 4200, revised={(ANCHOR + 3600, "LVB", "China Mainland"): 99_000_000_000})
        self.sample(ANCHOR + 4200)
        self.assertFalse(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_queued_revisions_returning_to_same_value_keep_only_latest_text(self):
        self.use_reporting(revisions=True)
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        for offset, amount, total in ((4200, 2_000_000_000, "0.013000"),
                                     (4500, 3_000_000_000, "0.014000"),
                                     (4800, 2_000_000_000, "0.013000")):
            self.report_usage(ANCHOR + offset, revised={(ANCHOR + 3300, "LVB", "China Mainland"): amount})
            self.monitor.sample(ANCHOR + offset)
            self.assertTrue(self.monitor.reports(ANCHOR + offset))
            pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
            self.assertEqual(1, len(pending))
            self.assertIn("一小时内流量包用量：%s TB" % total, pending[0]["text"])
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4801))
        self.assertEqual(2, len(self.sender.route("normal")))
        self.assertIn("一小时内流量包用量：0.013000 TB", self.sender.route("normal")[-1])

    def test_pending_delivery_survives_restart_without_duplicate_reports(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.monitor.queue.drain(ANCHOR + 3901)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_failed_unsent_report_refreshes_revised_usage_without_enabling_revision_pushes(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        original_send = self.sender.send

        def fail_normal(route, text):
            if route == "normal":
                raise RuntimeError("offline normal delivery failure")
            original_send(route, text)

        with patch.object(self.sender, "send", side_effect=fail_normal):
            self.assertFalse(self.monitor.queue.drain(ANCHOR + 3900))
        self.assertEqual([], self.sender.route("normal"))
        self.today.side_effect = lambda client, now, offset=8*3600: dict(today_snapshot(client, now, offset), bytes="4800000000000")
        self.report_usage(ANCHOR + 4200, revised={(ANCHOR + 3300, "LVB", "China Mainland"): 2_000_000_000})
        self.monitor.sample(ANCHOR + 4200)
        self.assertTrue(self.monitor.reports(ANCHOR + 4200))
        pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
        self.assertEqual(1, len(pending))
        self.assertIn("一小时内流量包用量：0.013000 TB", pending[0]["text"])
        self.assertIn("今日下行流量：4.800000 TB", pending[0]["text"])
        self.assertTrue(self.monitor.queue.drain(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("一小时内流量包用量：0.013000 TB", self.sender.route("normal")[0])
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_unsent_report_refreshes_today_total_even_when_hourly_signature_is_unchanged(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.monitor.reports(ANCHOR + 3900))
        self.today.side_effect = lambda client, now, offset=8*3600: dict(today_snapshot(client, now, offset), bytes="4800000000000")
        self.report_usage(ANCHOR + 4200)
        self.monitor.sample(ANCHOR + 4200)
        self.assertTrue(self.monitor.reports(ANCHOR + 4200))
        pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
        self.assertEqual(1, len(pending))
        self.assertIn("一小时内流量包用量：0.012000 TB", pending[0]["text"])
        self.assertIn("今日下行流量：4.800000 TB", pending[0]["text"])
        self.monitor.queue.drain(ANCHOR + 4200)
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_scope_change_discards_only_queued_reports(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.monitor.reports(ANCHOR + 3900)
        self.monitor.queue.enqueue("normal", "threshold/account/10/test", "business alert", ANCHOR + 3900)
        self.monitor.queue.incident("offline/unavailable", "error alert", ANCHOR + 3900)
        retained = {item["id"] for item in self.store.state["outbox"]
                    if not item["id"].startswith(("normal/report/", "normal/report-update/"))}
        raw = raw_config(self.path, calibrated=False)
        raw["accounts"][0]["regions"] = ["China Mainland", "Europe"]
        self.monitor.reconfigure(validate_config(raw))
        self.assertEqual(retained, {item["id"] for item in self.store.state["outbox"]})

    def test_changing_delay_preserves_already_sent_window_and_deduplication(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.assertTrue(self.send_hour())
        raw = raw_config(self.path, calibrated=False)
        raw["reports"].update(delay_minutes=10, revision_updates=False)
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertFalse(self.send_hour(ANCHOR + 4200))
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_changing_delay_before_due_moves_pending_window_without_losing_it(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        raw = raw_config(self.path, calibrated=False)
        raw["reports"].update(delay_minutes=10, revision_updates=False)
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.assertFalse(self.send_hour(ANCHOR + 3900))
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_schedule_change_starts_next_new_boundary_without_old_supplement(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        raw = raw_config(self.path, calibrated=False)
        raw["reports"].update(interval_minutes=30, delay_minutes=5, revision_updates=False)
        self.config = validate_config(raw)
        self.monitor.reconfigure(self.config)
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertFalse(self.send_hour(ANCHOR + 4200))
        self.report_usage(ANCHOR + 5700)
        self.sample(ANCHOR + 5700)
        self.assertTrue(self.send_hour(ANCHOR + 5700))
        self.assertEqual(1, len(self.sender.route("normal")))
        self.assertIn("2026-10-30 09:00:00 ～ 2026-10-30 09:30:00", self.sender.route("normal")[-1])

    def test_old_already_sent_report_is_not_repeated_on_upgrade(self):
        self.use_reporting()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.store.state["reports"]["UTC+08:00/60"] = ANCHOR + 3600
        self.store.state.pop("report_updates", None)
        self.store.save()
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 3901))
        self.assertEqual([], self.sender.route("normal"))

    def test_old_unsent_report_is_rebuilt_in_new_tb_format_once_after_upgrade(self):
        self.use_reporting()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.store.state["reports"]["UTC+08:00/60"] = ANCHOR + 3600
        self.store.state.pop("report_updates", None)
        self.monitor.queue.enqueue("normal", "report/UTC+08:00/60/%s" % (ANCHOR + 3600),
                                   "old format 12 GB", ANCHOR + 3600)
        self.store.save()
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.monitor.reports(ANCHOR + 4200))
        pending = [item for item in self.store.state["outbox"] if item["route"] == "normal"]
        self.assertEqual(1, len(pending))
        self.assertIn("/v2/", pending[0]["id"])
        self.assertIn("一小时内流量包用量：0.012000 TB", pending[0]["text"])
        self.assertNotIn("old format", pending[0]["text"])
        self.restart()
        self.assertFalse(self.send_hour(ANCHOR + 4201))
        self.assertEqual(1, len(self.sender.route("normal")))

    def test_loaded_complete_legacy_queue_waits_for_current_runtime_collection(self):
        self.use_reporting()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.store.state["reports"]["UTC+08:00/60"] = ANCHOR + 3600
        self.store.state.pop("report_updates", None)
        self.record()["official"]["packages"][0]["name"] = "云直播流量包 100.000 GB"
        self.monitor.queue.enqueue("normal", "report/UTC+08:00/60/%s" % (ANCHOR + 3600),
                                   "old format 12 GB", ANCHOR + 3600)
        self.store.save()
        self.restart()
        self.assertFalse(self.monitor.reports(ANCHOR + 3901))
        self.assertFalse(any(item["route"] == "normal" for item in self.store.state["outbox"]))
        self.assertEqual([], self.sender.route("normal"))
        self.today.side_effect = lambda client, now, offset=8*3600: dict(today_snapshot(client, now, offset), bytes="4800000000000")
        self.report_usage(ANCHOR + 4200)
        self.sample(ANCHOR + 4200)
        self.assertTrue(self.send_hour(ANCHOR + 4200))
        self.assertEqual(1, len(self.sender.route("normal")))
        text = self.sender.route("normal")[0]
        self.assertIn("今日下行流量：4.800000 TB", text)
        self.assertNotIn("old format", text)
        self.assertNotIn(" GB", text)

    def test_expired_pending_window_does_not_send_historical_report(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3300)
        self.sample(ANCHOR + 3900)
        self.assertFalse(self.send_hour())
        self.report_usage(ANCHOR + 10801)
        self.sample(ANCHOR + 10801)
        self.send_hour(ANCHOR + 10801)
        self.assertFalse(any("08:00:00 ～ 2026-10-30 09:00:00" in text for text in self.sender.route("normal")))

    def test_unchanged_five_second_scheduler_ticks_do_not_write_state(self):
        self.use_reporting()
        self.begin_hour()
        self.report_usage(ANCHOR + 3900)
        self.sample(ANCHOR + 3900)
        self.send_hour()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            for offset in range(3905, 3960, 5):
                self.assertFalse(self.monitor.reports(ANCHOR + offset))
            save.assert_not_called()

    def test_corrupt_report_tracker_is_rejected_without_overwriting_state(self):
        malformed = [[], {"schedule": "UTC+08:00/60", "scope": "abc", "windows": []},
                     {"schedule": "UTC+08:00/60", "scope": "abc", "windows": {
                         "bad-time": {"signature": "abc", "complete": True}}},
                     {"schedule": "UTC+08:00/60", "scope": "abc", "windows": {
                         str(ANCHOR): {"signature": 1, "complete": True}}}]
        for updates in malformed:
            with self.subTest(updates=updates):
                state = {"version": 1, "accounts": {}, "outbox": [], "errors": {}, "reports": {},
                         "report_updates": updates}
                original = json.dumps(state)
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(StateError):
                    StateStore(self.path).load()
                self.assertEqual(original, self.path.read_text(encoding="utf-8"))

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
