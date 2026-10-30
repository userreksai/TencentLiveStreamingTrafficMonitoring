import copy
import json
import unittest

from traffic_monitor.ledger import calculate, new_ledger, upsert_buckets


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.anchor = 1800000000  # aligned five-minute UTC boundary
        self.dimensions = [{"service": "LVB", "region": "Asia Pacific I"}]
        self.ledger = new_ledger(self.anchor, 10 ** 12, 2 * 10 ** 12)

    def bucket(self, index=0, amount=1000000000, service="LVB", region="Asia Pacific I"):
        return {"time": self.anchor + 300 * index, "service": service,
                "region": region, "bytes": str(amount)}

    def result(self, offset=600, **kwargs):
        return calculate(self.ledger, self.anchor + offset, expected_dimensions=self.dimensions, **kwargs)

    def test_official_region_ratio_and_decimal_bytes(self):
        upsert_buckets(self.ledger, [self.bucket()], self.anchor + 600)
        result = self.result(safety_seconds=0, safety_factor="1")
        self.assertEqual("1769200000", result["consumed_bytes"])
        self.assertEqual("998230800000", result["estimated_remaining_bytes"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(1, result["missing_intervals"])
        self.assertEqual(0, result["internal_missing_intervals"])

    def test_global_types_and_overrides_are_separate(self):
        upsert_buckets(self.ledger, [self.bucket(amount=100, service="LVB"),
                                    self.bucket(amount=100, service="LEB")], self.anchor + 600)
        self.assertEqual("531", self.result()["consumed_bytes"])
        self.assertEqual("600", self.result(ratios={"LVB": {"Asia Pacific 1": "2.4615"}})["consumed_bytes"])

    def test_overlapping_queries_and_revisions_replace_instead_of_add(self):
        upsert_buckets(self.ledger, [self.bucket(amount=100)], self.anchor + 600)
        upsert_buckets(self.ledger, [self.bucket(amount=100)], self.anchor + 600)
        self.assertEqual("177", self.result()["consumed_bytes"])
        upsert_buckets(self.ledger, [self.bucket(amount=200)], self.anchor + 600)
        self.assertEqual("354", self.result()["consumed_bytes"])
        upsert_buckets(self.ledger, [self.bucket(amount=0)], self.anchor + 600)
        self.assertEqual("0", self.result()["consumed_bytes"])

    def test_pre_anchor_data_excluded_and_anchor_interval_included(self):
        upsert_buckets(self.ledger, [self.bucket(index=-1), self.bucket()], self.anchor + 600)
        self.assertEqual(1, self.result()["bucket_count"])

    def test_bad_batch_is_atomic_and_incomplete_bucket_rejected(self):
        original = copy.deepcopy(self.ledger)
        for invalid in (self.bucket(amount=-1), self.bucket(index=2), self.bucket(region="unknown")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                upsert_buckets(self.ledger, [self.bucket(), invalid], self.anchor + 600)
            self.assertEqual(original, self.ledger)

    def test_closed_zero_interval_is_observed_not_missing(self):
        upsert_buckets(self.ledger, [self.bucket(amount=0)], self.anchor + 300)
        result = self.result(offset=300)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(self.anchor + 300, result["latest_data_time"])
        self.assertEqual("0", result["consumed_bytes"])

    def test_empty_and_unconfigured_coverage_are_not_assumed_healthy(self):
        result = self.result()
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["latest_data_time"])
        self.assertEqual(["LVB/Asia Pacific I"], result["missing_dimensions"])
        self.assertIsNone(calculate(self.ledger, self.anchor + 600)["coverage_complete"])

    def test_internal_missing_interval_detected(self):
        upsert_buckets(self.ledger, [self.bucket(0), self.bucket(2)], self.anchor + 900)
        result = self.result(offset=900)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(1, result["missing_intervals"])
        self.assertEqual(1, result["internal_missing_intervals"])

    def test_slowest_dimension_drives_freshness(self):
        dimensions = self.dimensions + [{"service": "LEB", "region": "Asia Pacific I"}]
        upsert_buckets(self.ledger, [self.bucket(0), self.bucket(1),
                                    self.bucket(0, service="LEB")], self.anchor + 900)
        result = calculate(self.ledger, self.anchor + 900, expected_dimensions=dimensions)
        self.assertEqual(self.anchor + 300, result["latest_data_time"])
        self.assertEqual(600, result["data_lag_seconds"])

    def test_stale_data_preserves_burn_rate_and_increases_reserve(self):
        upsert_buckets(self.ledger, [self.bucket()], self.anchor + 600)
        fresh = self.result(offset=300)
        stale = self.result(offset=7200)
        self.assertEqual(fresh["burn_rate_bytes_per_second"], stale["burn_rate_bytes_per_second"])
        self.assertGreater(int(stale["reserve_bytes"]), int(fresh["reserve_bytes"]))

    def test_exhaustion_and_overrun_never_show_negative_remaining(self):
        self.ledger = new_ledger(self.anchor, 100, 100)
        upsert_buckets(self.ledger, [self.bucket(amount=100)], self.anchor + 300)
        result = self.result(offset=300)
        self.assertEqual("0", result["estimated_remaining_bytes"])
        self.assertEqual("0", result["safe_remaining_bytes"])
        self.assertEqual("77", result["overrun_bytes"])
        self.assertEqual(0, result["runway_seconds"])

    def test_state_and_result_are_json_serializable(self):
        upsert_buckets(self.ledger, [self.bucket()], self.anchor + 300)
        restored = json.loads(json.dumps(self.ledger))
        self.assertEqual(calculate(self.ledger, self.anchor + 600), calculate(restored, self.anchor + 600))
        json.dumps(self.result())

    def test_invalid_calibration_not_accepted(self):
        for args in ((self.anchor + 1, 100, 100), (self.anchor, 101, 100),
                     (self.anchor, 0, 0), (self.anchor, -1, 100)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                new_ledger(*args)


if __name__ == "__main__":
    unittest.main()
