"""Offline verification of independent official daily reporting."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from traffic_monitor.daily_data import fetch_daily_report
from traffic_monitor.tencent import BEIJING, TencentError


NOW = int(datetime(2026, 10, 31, 10, 5, tzinfo=BEIJING).timestamp())


def day_rows():
    start = datetime(2026, 10, 24, tzinfo=BEIJING)
    return [{"Time": (start + timedelta(days=index)).strftime("%Y-%m-%d %H:%M:%S"),
             "Flux": Decimal("39150000") + index * Decimal("1000000")}
            for index in range(7)]


def package(identifier="old", bought="2026-10-22 05:31:15", status=4):
    return {"Id": identifier, "Type": 0, "Total": 200 * 10**12,
            "Used": 200 * 10**12 if status == 4 else 10**12,
            "Left": 0 if status == 4 else 199 * 10**12,
            "BuyTime": bought, "Status": status}


class DailyDataTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.rows = day_rows()
        self.client.call.return_value = {"DataInfoList": self.rows, "SumFlux": 999999999999999}
        self.packages = patch("traffic_monitor.daily_data.fetch_packages", return_value=[package()]).start()
        patch("traffic_monitor.tencent._open_request", side_effect=AssertionError("Real cloud requests forbidden")).start()
        self.addCleanup(patch.stopall)

    def report(self):
        return fetch_daily_report(self.client, NOW)

    def test_queries_cloud_day_granularity_and_complete_beijing_days(self):
        text = self.report()
        self.client.call.assert_called_once_with("DescribeBillBandwidthAndFluxList", {
            "StartTime": "2026-10-24 00:00:00", "EndTime": "2026-10-31 00:00:00", "Granularity": 1440,
        })
        self.packages.assert_called_once_with(self.client)
        self.assertIn("- 2026-10-24: 39.15 TB", text)
        self.assertIn("- 2026-10-30: 45.15 TB", text)
        self.assertNotIn("2026-10-31:", text)
        self.assertIn("官方已结算用量", text)

    def test_inclusive_end_record_does_not_add_an_eighth_day(self):
        self.rows.append({"Time": "2026-10-31 00:00:00", "Flux": 5000000000})
        text = self.report()
        self.assertEqual(7, text.count("\n- "))
        self.assertNotIn("2026-10-31:", text)

    def test_zero_is_valid_and_missing_date_is_not_fabricated(self):
        self.rows[0]["Flux"] = 0
        self.assertIn("- 2026-10-24: 0.00 TB", self.report())
        self.rows.pop(0)
        with self.assertRaises(TencentError) as error:
            self.report()
        self.assertEqual("IncompleteResponse", error.exception.code)
        self.assertIn("2026-10-24", str(error.exception))
        self.assertEqual(1, self.packages.call_count)

    def test_utc_and_offset_times_are_normalized_to_beijing(self):
        self.rows[0]["Time"] = "2026-10-23T16:00:00Z"
        self.rows[1]["Time"] = "2026-10-25T00:00:00+08:00"
        self.assertIn("- 2026-10-24: 39.15 TB", self.report())

    def test_date_only_day_labels_are_accepted(self):
        self.rows[0]["Time"] = "2026-10-24"
        self.assertIn("- 2026-10-24: 39.15 TB", self.report())

    def test_equal_duplicate_is_deduplicated_but_conflicting_day_is_rejected(self):
        self.rows.append(deepcopy(self.rows[0]))
        self.assertEqual(7, self.report().count("\n- "))
        self.rows[-1]["Flux"] += 1
        with self.assertRaises(TencentError):
            self.report()

    def test_non_daily_invalid_and_out_of_range_times_are_rejected(self):
        original = self.rows[0]["Time"]
        for value in (None, "bad", "2026-10-24 00:05:00", "2026-10-23 00:00:00",
                      "2026-11-01 00:00:00", "2026-10-24 00:00:00.000001"):
            with self.subTest(value=value), self.assertRaises(TencentError):
                self.rows[0]["Time"] = value
                self.report()
        self.rows[0]["Time"] = original

    def test_invalid_flux_and_missing_or_malformed_list_are_rejected(self):
        for value in (None, True, -1, "NaN", "Infinity", "bad"):
            with self.subTest(value=value), self.assertRaises(TencentError):
                self.rows[0]["Flux"] = value
                self.report()
        for response in ({}, {"DataInfoList": None}, {"DataInfoList": [False]}):
            with self.subTest(response=response), self.assertRaises(TencentError):
                self.client.call.return_value = response
                self.report()

    def test_all_package_statuses_including_exhausted_are_displayed(self):
        self.packages.return_value = [package(str(status), status=status) for status in range(6)]
        text = self.report()
        for state in ("未使用", "使用中", "已过期", "已冻结", "已耗尽", "已退款"):
            self.assertIn(state, text)
        self.assertIn("已使用：200.00 TB，总量：200.00 TB", text)
        self.assertIn("2026-10-22 05:31:15", text)

    def test_newest_package_first_and_purchase_time_converted_to_beijing(self):
        self.packages.return_value = [package(), package("new", "2026-10-26T05:31:23+08:00", 1)]
        text = self.report()
        self.assertLess(text.index("2026-10-26 05:31:23"), text.index("2026-10-22 05:31:15"))
        self.packages.return_value = [package("new", "2026-10-25T21:31:23Z")]
        self.assertIn("2026-10-26 05:31:23", self.report())

    def test_used_is_read_directly_rather_than_derived_from_left(self):
        row = package(status=1)
        row["Used"], row["Left"] = 23 * 10**12, 100 * 10**12
        self.packages.return_value = [row]
        self.assertIn("已使用：23.00 TB", self.report())

    def test_empty_packages_have_explicit_message(self):
        self.packages.return_value = []
        self.assertIn("暂无流量包", self.report())

    def test_invalid_package_fields_are_not_presented_as_valid_usage(self):
        cases = [("Total", 0), ("Total", "NaN"), ("Used", -1), ("Used", True),
                 ("Used", "1.1"), ("Used", 201 * 10**12), ("Status", 6),
                 ("Status", True), ("BuyTime", "not-a-time"), ("BuyTime", None)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(TencentError):
                self.packages.return_value = [dict(package(), **{key: value})]
                self.report()

    def test_invalid_now_is_rejected_without_querying(self):
        for now in (True, -1, None, "2026-10-31", 1.5):
            with self.subTest(now=now), self.assertRaises(TencentError):
                fetch_daily_report(self.client, now)
        self.client.call.assert_not_called()

    def test_api_failure_is_propagated_without_publishing_partial_success(self):
        self.client.call.side_effect = TencentError("AuthFailure", "offline failure")
        with self.assertRaises(TencentError) as error:
            self.report()
        self.assertEqual("AuthFailure", error.exception.code)
        self.packages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
