from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from traffic_monitor.tencent import (
    REGIONS, TencentClient, TencentError, fetch_billable_buckets, fetch_packages, fetch_today_downstream,
)


def utc(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TencentClientTests(unittest.TestCase):
    def setUp(self):
        self.client = TencentClient("offline-id", "offline-secret", "offline-token")
        self.transport = patch("traffic_monitor.tencent._open_request").start()
        patch("traffic_monitor.tencent.time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_signs_the_exact_body_and_uses_utc_signature_date(self):
        self.transport.return_value = Response({"Response": {"RequestId": "request-1"}})
        with patch("traffic_monitor.tencent.time.time", return_value=utc("2026-10-30T00:30:00Z")):
            self.assertEqual("request-1", self.client.call("DescribeLivePackageInfo", {"PackageType": 0})["RequestId"])
        request, timeout = self.transport.call_args.args
        self.assertEqual(b'{"PackageType":0}', request.data)
        self.assertIn("/2026-10-30/live/tc3_request", request.get_header("Authorization"))
        self.assertIn("SignedHeaders=content-type;host", request.get_header("Authorization"))
        self.assertEqual("offline-token", request.get_header("X-tc-token"))
        self.assertEqual("2018-08-01", request.get_header("X-tc-version"))
        self.assertEqual(30, timeout)

    def test_authentication_error_is_classified_and_redacted_without_retry(self):
        self.transport.return_value = Response({"Response": {
            "RequestId": "request-2", "Error": {
                "Code": "AuthFailure.SecretIdNotFound",
                "Message": "offline-id / offline-secret / offline-token invalid",
            },
        }})
        with self.assertRaises(TencentError) as caught:
            self.client.call("DescribeLivePackageInfo", {})
        self.assertEqual("AuthFailure.SecretIdNotFound", caught.exception.code)
        self.assertEqual("request-2", caught.exception.request_id)
        for value in ("offline-id", "offline-secret", "offline-token"):
            self.assertNotIn(value, str(caught.exception))
        self.transport.assert_called_once()

    def test_transient_network_and_rate_limit_are_retried(self):
        self.transport.side_effect = [
            URLError("contains-secret-url"),
            Response({"Response": {"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}}}),
            Response({"Response": {"RequestId": "ok"}}),
        ]
        self.assertEqual({"RequestId": "ok"}, self.client.call("DescribeLivePackageInfo", {}))
        self.assertEqual(3, self.transport.call_count)

    def test_http_403_is_not_retried_and_does_not_include_url(self):
        self.transport.side_effect = HTTPError("https://contains-secret-url", 403, "forbidden", {}, None)
        with self.assertRaises(TencentError) as caught:
            self.client.call("DescribeLivePackageInfo", {})
        self.assertEqual("HTTP403", caught.exception.code)
        self.assertNotIn("contains-secret", str(caught.exception))
        self.transport.assert_called_once()

    def test_invalid_envelope_is_an_error(self):
        for result in ([], {"Response": []}, {"Other": {}}, {"Response": {"Error": "bad"}}):
            with self.subTest(result=result):
                self.transport.return_value = Response(result)
                with self.assertRaises(TencentError):
                    self.client.call("DescribeLivePackageInfo", {})

    def test_custom_endpoint_or_missing_credentials_are_rejected(self):
        for kwargs in ({"endpoint": "evil.example"}, {"timeout": 0}):
            with self.assertRaises(TencentError):
                TencentClient("id", "key", **kwargs)
        with self.assertRaises(TencentError):
            TencentClient("", "key")


class UsageBucketTests(unittest.TestCase):
    def setUp(self):
        self.start = utc("2026-10-28T00:00:00Z")
        self.end = self.start + 600
        self.client = Mock()
        self.client.call.return_value = {"DataInfoList": []}

    def fetch(self, services=None, regions=None):
        return fetch_billable_buckets(self.client, self.start, self.end,
                                      services or ["LVB"], regions or ["Asia Pacific I"])

    def test_decimal_mb_and_naive_beijing_time_are_normalized(self):
        self.client.call.return_value = {"DataInfoList": [
            {"Time": "2026-10-28 08:00:00", "Flux": "0.0000001"},
            {"Time": "2026-10-28T00:05:00Z", "Flux": Decimal("22.123456789")},
        ]}
        buckets = self.fetch()
        self.assertEqual([self.start, self.start + 300], [b["time"] for b in buckets])
        self.assertEqual([1, 22123457], [b["bytes"] for b in buckets])

    def test_zero_is_recorded_but_missing_data_is_not_fabricated(self):
        self.assertEqual([], self.fetch())
        self.client.call.return_value = {"DataInfoList": [{"Time": "2026-10-28T00:00:00Z", "Flux": 0}]}
        self.assertEqual(0, self.fetch()[0]["bytes"])
        for response in ({}, {"DataInfoList": None}, {"DataInfoList": [
            {"Time": "2026-10-28T00:00:00Z", "Flux": None}]}):
            self.client.call.return_value = response
            with self.assertRaises(TencentError):
                self.fetch()

    def test_queries_each_scope_separately_and_deduplicates_selectors(self):
        self.fetch(["LVB", "LEB", "LVB"], ["Asia Pacific I", "Europe", "Europe"])
        self.assertEqual(4, self.client.call.call_count)
        scopes = {(c.args[1]["ServiceName"], tuple(c.args[1]["RegionNames"]))
                  for c in self.client.call.call_args_list}
        self.assertEqual({("LVB", ("Asia Pacific I",)), ("LVB", ("Europe",)),
                          ("LEB", ("Asia Pacific I",)), ("LEB", ("Europe",))}, scopes)

    def test_upstream_is_explicit_optional_usage(self):
        self.fetch(["PUSH"])
        action, params = self.client.call.call_args.args
        self.assertEqual("DescribePushBandwidthAndFluxList", action)
        self.assertNotIn("ServiceName", params)

    def test_splits_at_utc_midnight_and_ignores_inclusive_end_points(self):
        self.start = utc("2026-10-28T23:55:00Z")
        self.end = self.start + 900
        self.client.call.side_effect = [
            {"DataInfoList": [
                {"Time": "2026-10-28T23:55:00Z", "Flux": 1},
                {"Time": "2026-10-29T00:00:00Z", "Flux": 2},
            ]},
            {"DataInfoList": [
                {"Time": "2026-10-29T00:00:00Z", "Flux": 2},
                {"Time": "2026-10-29T00:05:00Z", "Flux": 3},
                {"Time": "2026-10-29T00:10:00Z", "Flux": 100},
            ]},
        ]
        buckets = self.fetch()
        self.assertEqual([1000000, 2000000, 3000000], [b["bytes"] for b in buckets])
        self.assertEqual(2, self.client.call.call_count)
        for call in self.client.call.call_args_list:
            self.assertLessEqual(utc(call.args[1]["EndTime"]) - utc(call.args[1]["StartTime"]), 86400)

    def test_conflicting_duplicate_buckets_are_rejected(self):
        self.client.call.return_value = {"DataInfoList": [
            {"Time": "2026-10-28T00:00:00Z", "Flux": 1},
            {"Time": "2026-10-28T00:00:00Z", "Flux": 2},
        ]}
        with self.assertRaises(TencentError):
            self.fetch()

    def test_invalid_flux_and_unaligned_time_do_not_silently_pass(self):
        for value in (-1, "NaN", "Infinity", True, "no-number"):
            with self.subTest(value=value):
                self.client.call.return_value = {"DataInfoList": [{"Time": "2026-10-28T00:00:00Z", "Flux": value}]}
                with self.assertRaises(TencentError):
                    self.fetch()
        self.client.call.return_value = {"DataInfoList": [{"Time": "2026-10-28T00:01:00Z", "Flux": 1}]}
        with self.assertRaises(TencentError):
            self.fetch()

    def test_unknown_or_empty_scopes_are_rejected(self):
        for services, regions in (([], ["Europe"]), (["LVB"], []), (["LVB"], ["Global"]), (["other"], ["Europe"])):
            with self.subTest(services=services, regions=regions):
                with self.assertRaises(TencentError):
                    fetch_billable_buckets(self.client, self.start, self.end, services, regions)


class TodayDownstreamTests(unittest.TestCase):
    def setUp(self):
        self.now = utc("2026-10-30T07:55:21Z")
        self.client = Mock()
        self.client.call.return_value = {
            "SumFlux": Decimal("4470000.1234567"),
            "DataInfoList": [{"Time": "2026-10-30 15:50:00", "Flux": "12.3456789"}],
        }

    def test_reads_official_sumflux_once_for_all_playback_scopes(self):
        result = fetch_today_downstream(self.client, self.now)
        self.client.call.assert_called_once_with("DescribeBillBandwidthAndFluxList", {
            "StartTime": "2026-10-29T16:00:00Z", "EndTime": "2026-10-30T07:55:21Z", "Granularity": 5,
        })
        # SumFlux is an official aggregate, not a local sum of Flux rows or a
        # region/service package conversion. Fractional bytes round upward.
        self.assertEqual("4470000123457", result["bytes"])
        self.assertEqual(utc("2026-10-29T16:00:00Z"), result["day_start"])
        self.assertEqual(self.now, result["checked_at"])
        self.assertEqual(self.now, result["through"])
        self.assertEqual(utc("2026-10-30T07:55:00Z"), result["latest_data_time"])
        for excluded in ("PlayDomains", "ServiceName", "RegionNames", "MainlandOrOversea"):
            self.assertNotIn(excluded, self.client.call.call_args.args[1])

    def test_day_boundary_uses_configured_timezone_instead_of_utc(self):
        for offset, start in ((0, "2026-10-30T00:00:00Z"),
                              (7 * 3600, "2026-10-29T17:00:00Z"),
                              (-5 * 3600, "2026-10-30T05:00:00Z")):
            with self.subTest(offset=offset):
                result = fetch_today_downstream(self.client, self.now, offset)
                self.assertEqual(utc(start), result["day_start"])
                self.assertEqual(start, self.client.call.call_args.args[1]["StartTime"])

    def test_midnight_returns_new_day_zero_without_querying_or_using_yesterday(self):
        now = utc("2026-10-29T16:00:00Z")
        result = fetch_today_downstream(self.client, now)
        self.assertEqual({"day_start": now, "checked_at": now, "through": now,
                          "bytes": "0", "latest_data_time": None}, result)
        self.client.call.assert_not_called()

    def test_inclusive_current_bucket_does_not_extrapolate_past_query_time(self):
        self.client.call.return_value = {"SumFlux": 1, "DataInfoList": [
            {"Time": "2026-10-30T07:55:00Z", "Flux": 1},
        ]}
        self.assertEqual(self.now, fetch_today_downstream(self.client, self.now)["latest_data_time"])
        exact = utc("2026-10-30T07:55:00Z")
        self.assertEqual(exact, fetch_today_downstream(self.client, exact)["latest_data_time"])

    def test_explicit_zero_is_allowed_but_missing_or_invalid_total_is_not_zero(self):
        for items in ([], [{"Time": "2026-10-30 15:50:00", "Flux": 0}]):
            with self.subTest(items=items):
                self.client.call.return_value = {"SumFlux": 0, "DataInfoList": items}
                self.assertEqual("0", fetch_today_downstream(self.client, self.now)["bytes"])
        for value in (None, -1, True, "NaN", "Infinity", "not-a-number"):
            with self.subTest(value=value):
                self.client.call.return_value = {"SumFlux": value, "DataInfoList": []}
                with self.assertRaises(TencentError):
                    fetch_today_downstream(self.client, self.now)
        self.client.call.return_value = {"DataInfoList": []}
        with self.assertRaises(TencentError):
            fetch_today_downstream(self.client, self.now)

    def test_missing_malformed_or_out_of_range_records_are_rejected(self):
        for items in (None, {}, [None], [{}],
                      [{"Time": "2026-10-29 23:55:00", "Flux": 1}],
                      [{"Time": "2026-10-30 16:00:00", "Flux": 1}],
                      [{"Time": "2026-10-30 15:50:00", "Flux": -1}],
                      [{"Time": "2026-10-30 15:50:01", "Flux": 1}], []):
            with self.subTest(items=items):
                self.client.call.return_value = {"SumFlux": 1, "DataInfoList": items}
                with self.assertRaises(TencentError):
                    fetch_today_downstream(self.client, self.now)

    def test_rejects_invalid_time_and_timezone_types(self):
        for now, offset in ((True, 0), (-1, 0), (self.now, True),
                            (self.now, 15 * 3600), (self.now, "UTC+08:00")):
            with self.subTest(now=now, offset=offset), self.assertRaises(TencentError):
                fetch_today_downstream(self.client, now, offset)
        self.client.call.assert_not_called()


class PackagePaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.endpoint = "live.intl.tencentcloudapi.com"

    def test_reads_all_package_pages(self):
        self.client.call.side_effect = [
            {"LivePackageInfoList": [{"Id": str(i)} for i in range(100)], "TotalNum": 101, "TotalPage": 2},
            {"LivePackageInfoList": [{"Id": "100"}], "TotalNum": 101, "TotalPage": 2},
        ]
        self.assertEqual(101, len(fetch_packages(self.client)))
        self.assertEqual(2, self.client.call.call_count)

    def test_incomplete_package_response_is_not_returned_as_complete(self):
        self.client.call.return_value = {"LivePackageInfoList": [{"Id": "1"}], "TotalNum": 2, "TotalPage": 1}
        with self.assertRaises(TencentError):
            fetch_packages(self.client)

    def test_only_unsupported_package_action_switches_endpoint(self):
        self.client.call.side_effect = TencentError("InvalidAction", "unsupported")
        general = Mock()
        general.call.return_value = {"LivePackageInfoList": [], "TotalNum": 0, "TotalPage": 0}
        self.client.at_endpoint.return_value = general
        self.assertEqual([], fetch_packages(self.client))
        self.client.at_endpoint.assert_called_once_with("live.tencentcloudapi.com")

    def test_key_errors_keep_their_classification_and_never_fallback(self):
        self.client.call.side_effect = TencentError("AuthFailure.SecretIdNotFound", "invalid key")
        with self.assertRaises(TencentError):
            fetch_packages(self.client)
        self.client.at_endpoint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
