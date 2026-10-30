"""Pure package-credit replay tests; no cloud calls or mutation."""

import copy
import json
import unittest

from traffic_monitor.funding import replay_funding


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.anchor = 1800000000

    def lot(self, identifier="new", buy=300, capacity=200, expires=7200, **changes):
        item = {"id": identifier, "buy_time": self.anchor + buy,
                "capacity": str(capacity), "expires": None if expires is None else self.anchor + expires}
        item.update(changes)
        return item

    def usage(self, offset, amount):
        return {"time": self.anchor + offset, "bytes": str(amount)}

    def replay(self, usage=(), lots=(), offset=3600, balance=100, total=200, baseline_lots=None):
        return replay_funding(self.anchor, balance, total, list(usage), list(lots),
                              self.anchor + offset, baseline_lots=baseline_lots)

    def test_new_purchase_adds_full_capacity_once(self):
        lot = self.lot()
        result = self.replay([self.usage(0, 50), self.usage(300, 20)], [lot, copy.deepcopy(lot)])
        self.assertEqual("230", result["remaining_bytes"])
        self.assertEqual("400", result["active_capacity_bytes"])
        self.assertEqual(2, len(result["lots"]))

    def test_historical_unfunded_usage_is_not_erased_by_purchase(self):
        result = self.replay([self.usage(0, 120), self.usage(300, 10)], [self.lot()])
        self.assertEqual("190", result["remaining_bytes"])
        self.assertEqual("20", result["uncovered_usage_bytes"])
        self.assertEqual("200", result["active_capacity_bytes"])

    def test_purchase_inside_bucket_starts_at_next_boundary(self):
        result = self.replay([self.usage(300, 20), self.usage(600, 10)],
                             [self.lot(buy=301)], balance=0)
        self.assertEqual("20", result["uncovered_usage_bytes"])
        self.assertEqual("190", result["remaining_bytes"])
        self.assertEqual(self.anchor + 600, result["lots"][1]["effective_time"])

    def test_new_purchase_is_not_available_before_effective_time(self):
        result = self.replay([], [self.lot(buy=601)], offset=850)
        self.assertEqual("100", result["remaining_bytes"])
        self.assertEqual("200", result["active_capacity_bytes"])
        self.assertFalse(result["lots"][1]["credited"])

    def test_expiry_removes_only_unconsumed_capacity(self):
        result = self.replay([self.usage(300, 40)], [self.lot(expires=900)], balance=0)
        self.assertEqual("0", result["remaining_bytes"])
        self.assertEqual("160", result["expired_bytes"])
        self.assertEqual("40", result["consumed_bytes"])

    def test_fully_consumed_lot_expiry_does_not_subtract_again(self):
        result = self.replay([self.usage(300, 200)], [self.lot(expires=900)], balance=0)
        self.assertEqual("0", result["remaining_bytes"])
        self.assertEqual("0", result["expired_bytes"])
        self.assertEqual("0", result["uncovered_usage_bytes"])

    def test_refund_removes_only_remaining_credit(self):
        lot = self.lot(retired_at=self.anchor + 900)
        result = self.replay([self.usage(300, 30)], [lot], balance=0)
        self.assertEqual("170", result["retired_bytes"])
        self.assertEqual("0", result["remaining_bytes"])

    def test_retirement_inside_bucket_does_not_cover_whole_interval(self):
        lot = self.lot(retired_at=self.anchor + 850)
        result = self.replay([self.usage(300, 20), self.usage(600, 10)], [lot], balance=0)
        self.assertEqual("180", result["retired_bytes"])
        self.assertEqual("10", result["uncovered_usage_bytes"])

    def test_purchase_and_refund_inside_same_bucket_never_create_funds(self):
        lot = self.lot(buy=301, retired_at=self.anchor + 550)
        result = self.replay([self.usage(300, 20), self.usage(600, 10)], [lot], balance=0)
        self.assertEqual("0", result["remaining_bytes"])
        self.assertEqual("30", result["uncovered_usage_bytes"])
        self.assertFalse(result["lots"][1]["credited"])

    def test_known_baseline_lots_are_allocated_by_expiry(self):
        baseline = [{"id": "old", "capacity": 100, "total": 200, "expires": self.anchor + 1800}]
        # New lot expires first and therefore takes the usage before the old lot.
        result = self.replay([self.usage(300, 50)], [self.lot(expires=900)],
                             offset=1200, baseline_lots=baseline)
        self.assertEqual("100", result["baseline_remaining_bytes"])
        self.assertEqual("150", result["expired_bytes"])
        self.assertEqual("200", result["active_capacity_bytes"])

    def test_baseline_expiry_is_processed_automatically(self):
        baseline = [{"id": "old", "capacity": 100, "total": 200, "expires": self.anchor + 900}]
        result = self.replay([self.usage(0, 30)], offset=1200, baseline_lots=baseline)
        self.assertEqual("70", result["expired_bytes"])
        self.assertEqual("0", result["remaining_bytes"])

    def test_expired_baseline_at_anchor_is_not_usable(self):
        baseline = [{"id": "old", "capacity": 100, "total": 200, "expires": self.anchor}]
        result = self.replay([self.usage(0, 10)], baseline_lots=baseline)
        self.assertEqual("100", result["expired_bytes"])
        self.assertEqual("10", result["uncovered_usage_bytes"])

    def test_consumed_old_capacity_does_not_dilute_new_package_percentage(self):
        result = self.replay([self.usage(0, 100)], [self.lot()])
        self.assertEqual("200", result["remaining_bytes"])
        self.assertEqual("200", result["active_capacity_bytes"])
        self.assertEqual("400", result["gross_capacity_bytes"])

    def test_replay_revised_usage_replaces_old_effects(self):
        lots = [self.lot()]
        first = self.replay([self.usage(0, 60)], lots)
        revised = self.replay([self.usage(0, 20)], lots)
        self.assertEqual("240", first["remaining_bytes"])
        self.assertEqual("280", revised["remaining_bytes"])

    def test_decimal_deductions_do_not_round_each_bucket(self):
        result = self.replay([self.usage(0, "0.4"), self.usage(300, "0.4")],
                             balance=1, total=1)
        self.assertEqual("0", result["remaining_bytes"])
        self.assertEqual("1", result["consumed_bytes"])
        self.assertEqual("0", result["uncovered_usage_bytes"])

    def test_identical_usage_duplicates_do_not_double_charge(self):
        item = self.usage(0, 30)
        result = self.replay([item, dict(item)])
        self.assertEqual("70", result["remaining_bytes"])

    def test_conflicting_duplicate_or_ambiguous_inputs_are_rejected(self):
        cases = [
            ([self.usage(0, 1), self.usage(0, 2)], []),
            ([], [self.lot(), self.lot(capacity=300)]),
            ([self.usage(1, 1)], []),
            ([self.usage(3600, 1)], []),
            ([], [self.lot(buy=-1)]),
            ([], [self.lot(expires=0)]),
        ]
        for usage, lots in cases:
            with self.subTest(usage=usage, lots=lots), self.assertRaises(ValueError):
                self.replay(usage, lots)

    def test_baseline_allocations_must_match_anchor(self):
        bad = [{"id": "old", "capacity": 99, "total": 200, "expires": None}]
        with self.assertRaises(ValueError):
            self.replay(baseline_lots=bad)

    def test_baseline_package_cannot_be_credited_again_as_new(self):
        baseline = [{"id": "old", "capacity": 100, "total": 200, "expires": None}]
        with self.assertRaisesRegex(ValueError, "already included"):
            self.replay(lots=[self.lot(identifier="old")], baseline_lots=baseline)

    def test_results_are_json_serializable_and_inputs_unchanged(self):
        usage, lots = [self.usage(0, "0.4")], [self.lot()]
        before = copy.deepcopy((usage, lots))
        result = self.replay(usage, lots)
        self.assertEqual(before, (usage, lots))
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
