"""Offline configuration, secret handling and durable delivery regression tests."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

from traffic_monitor.config import ConfigError, load_config, redact, validate_config
from traffic_monitor.notifications import (
    LarkSender, NotificationError, NotificationQueue, split_message,
)
from traffic_monitor.storage import StateError, StateStore


def raw_config():
    return {
        "version": 1,
        "sampling": {}, "reports": {}, "alerts": {},
        "lark": {
            "normal_webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-normal",
            "error_webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/offline-error",
        },
        "storage": {"state_file": "state.json"},
        "accounts": [{"name": "test-account", "secret_id": "offline-id",
                      "secret_key": "offline-key", "services": ["LVB"],
                      "regions": ["Asia Pacific I"]}],
    }


class ConfigTests(unittest.TestCase):
    def test_defaults_are_five_minutes_hourly_and_beijing_clock(self):
        cfg = validate_config(raw_config())
        self.assertEqual(300, cfg["sampling"]["interval_seconds"])
        self.assertEqual(600, cfg["sampling"]["max_data_age_seconds"])
        self.assertEqual(60, cfg["reports"]["interval_minutes"])
        self.assertEqual(5, cfg["reports"]["delay_minutes"])
        self.assertIs(False, cfg["reports"]["revision_updates"])
        self.assertEqual(8 * 3600, cfg["_timezone_offset"])
        self.assertEqual(["50", "10"], cfg["alerts"]["remaining_percent"])
        self.assertIsNone(cfg["accounts"][0]["_anchor"])

    def test_sampling_period_must_be_closed_five_minute_multiple(self):
        for interval in (0, 60, 301, 450, 600.5):
            with self.subTest(interval=interval):
                raw = raw_config()
                raw["sampling"]["interval_seconds"] = interval
                with self.assertRaises(ConfigError):
                    validate_config(raw)
        raw = raw_config()
        raw["sampling"]["interval_seconds"] = 600
        self.assertEqual(600, validate_config(raw)["sampling"]["interval_seconds"])

    def test_report_period_respects_clock_boundaries(self):
        raw = raw_config()
        raw["reports"] = {"interval_minutes": 120, "timezone": "UTC+07:00"}
        cfg = validate_config(raw)
        self.assertEqual(120, cfg["reports"]["interval_minutes"])
        self.assertEqual(7 * 3600, cfg["_timezone_offset"])
        raw["reports"]["interval_minutes"] = 61
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_report_delay_accepts_zero_and_configurable_integer_minutes(self):
        for delay in (0, 5, 10, 1440):
            with self.subTest(delay=delay):
                raw = raw_config()
                raw["reports"]["delay_minutes"] = delay
                self.assertEqual(delay, validate_config(raw)["reports"]["delay_minutes"])
        for delay in (-1, 0.5, "NaN", "Infinity", "bad", 1441, True, False, None):
            with self.subTest(delay=delay):
                raw = raw_config()
                raw["reports"]["delay_minutes"] = delay
                with self.assertRaises(ConfigError):
                    validate_config(raw)

    def test_report_revision_updates_requires_a_json_boolean(self):
        for value in (True, False):
            with self.subTest(value=value):
                raw = raw_config()
                raw["reports"]["revision_updates"] = value
                self.assertIs(value, validate_config(raw)["reports"]["revision_updates"])
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value):
                raw = raw_config()
                raw["reports"]["revision_updates"] = value
                with self.assertRaises(ConfigError):
                    validate_config(raw)

    def test_report_delivery_preferences_do_not_change_accounting_anchor(self):
        raw = raw_config()
        first = validate_config(raw)
        self.assertEqual({}, raw["reports"])
        raw["reports"].update(delay_minutes=10, revision_updates=True)
        changed = validate_config(raw)
        self.assertEqual(first["accounts"][0]["_anchor_id"], changed["accounts"][0]["_anchor_id"])
        self.assertEqual(5, first["reports"]["delay_minutes"])
        self.assertIs(False, first["reports"]["revision_updates"])

    def test_secret_environment_references_resolve_without_mutating_raw(self):
        raw = raw_config()
        raw["accounts"][0]["secret_id"] = "env:MONITOR_TEST_ID"
        raw["accounts"][0]["secret_key"] = "env:MONITOR_TEST_KEY"
        raw["lark"]["normal_webhook"] = "env:MONITOR_TEST_HOOK"
        environ = {
            "MONITOR_TEST_ID": "resolved-offline-id",
            "MONITOR_TEST_KEY": "resolved-offline-key",
            "MONITOR_TEST_HOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/offline-test",
        }
        with patch.dict(os.environ, environ):
            cfg = validate_config(raw)
        self.assertEqual("resolved-offline-key", cfg["accounts"][0]["secret_key"])
        self.assertEqual(environ["MONITOR_TEST_HOOK"], cfg["lark"]["normal_webhook"])
        self.assertEqual("env:MONITOR_TEST_KEY", raw["accounts"][0]["secret_key"])
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError) as error:
            validate_config(raw)
        self.assertNotIn("resolved-offline", str(error.exception))

    def test_both_notification_routes_are_required(self):
        for route in ("normal", "error"):
            raw = raw_config()
            raw["lark"][route + "_webhook"] = ""
            with self.subTest(route=route), self.assertRaises(ConfigError):
                validate_config(raw)

    def test_untrusted_webhook_origins_and_credentials_are_rejected(self):
        for webhook in (
            "http://open.larksuite.com/open-apis/bot/v2/hook/offline",
            "https://example.com/open-apis/bot/v2/hook/offline",
            "https://user:password@open.larksuite.com/open-apis/bot/v2/hook/offline",
            "https://open.larksuite.com/open-apis/bot/v2/hook/",
        ):
            raw = raw_config()
            raw["lark"]["normal_webhook"] = webhook
            with self.subTest(webhook=webhook), self.assertRaises(ConfigError):
                validate_config(raw)

    def test_malformed_webhook_port_is_a_safe_configuration_error(self):
        raw = raw_config()
        raw["lark"]["normal_webhook"] = "https://open.larksuite.com:not-a-port/open-apis/bot/v2/hook/offline-private"
        with self.assertRaises(ConfigError) as error:
            validate_config(raw)
        self.assertNotIn("offline-private", str(error.exception))
        self.assertNotIn("https://", str(error.exception))

    def test_partial_anchor_is_rejected_and_missing_anchor_is_explicit(self):
        raw = raw_config()
        self.assertIsNone(validate_config(raw)["accounts"][0]["_anchor"])
        raw["accounts"][0]["anchor"] = {"usage_start": "2026-10-30T00:00:00+08:00"}
        with self.assertRaises(ConfigError):
            validate_config(raw)
        raw["accounts"][0]["anchor"].update(remaining_tb="100", total_tb="200")
        cfg = validate_config(raw)
        self.assertEqual(100 * 10**12, cfg["accounts"][0]["_anchor"]["remaining_bytes"])
        raw["accounts"][0]["anchor"]["usage_start"] = "2026-10-30T00:00:01+08:00"
        with self.assertRaises(ConfigError):
            validate_config(raw)
        raw["accounts"][0]["anchor"]["usage_start"] = "2026-10-30T00:00:00"
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_anchor_revision_or_dimensions_change_calibration_identity(self):
        raw = raw_config()
        raw["accounts"][0]["anchor"] = {
            "usage_start": "2026-10-30T00:00:00+08:00", "remaining_tb": "100", "total_tb": "200",
            "revision": "first",
        }
        first = validate_config(raw)["accounts"][0]["_anchor_id"]
        raw["accounts"][0]["anchor"]["revision"] = "second"
        self.assertNotEqual(first, validate_config(raw)["accounts"][0]["_anchor_id"])

    def test_redaction_covers_credentials_hooks_and_signing_secrets(self):
        raw = raw_config()
        raw["lark"]["error_sign_secret"] = "offline-sign-secret"
        cfg = validate_config(raw)
        text = " | ".join([
            cfg["accounts"][0]["secret_id"], cfg["accounts"][0]["secret_key"],
            cfg["lark"]["normal_webhook"], cfg["lark"]["error_sign_secret"],
        ])
        output = redact(text, cfg)
        for value in text.split(" | "):
            self.assertNotIn(value, output)
        self.assertIn("已隐藏", output)

    def test_relative_state_file_is_resolved_next_to_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "config.json"
            config_path.write_text(json.dumps(raw_config()), encoding="utf-8")
            cfg = load_config(config_path)
            self.assertEqual(str((Path(folder) / "state.json").resolve()), cfg["storage"]["state_file"])


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.config = validate_config(raw_config())
        self.store = StateStore(self.path)
        self.store.load()
        self.sender = Mock()
        self.queue = NotificationQueue(self.store, self.config, sender=self.sender)

    def sender_with_result(self, result):
        sender = LarkSender(self.config)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(result).encode("utf-8")
        sender.opener = opener
        return sender, opener

    def test_each_route_uses_its_own_webhook(self):
        for route in ("normal", "error"):
            sender, opener = self.sender_with_result({"code": 0})
            sender.send(route, "offline test")
            request = opener.open.call_args.args[0]
            self.assertEqual(self.config["lark"][route + "_webhook"], request.full_url)
            self.assertEqual("offline test", json.loads(request.data)["content"]["text"])

    def test_signed_webhook_uses_correct_timestamp_protocol(self):
        self.config["lark"]["normal_sign_secret"] = "test-sign-secret"
        sender, opener = self.sender_with_result({"StatusCode": 0})
        with patch("traffic_monitor.notifications.time.time", return_value=1700000000):
            sender.send("normal", "offline test")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual("1700000000", payload["timestamp"])
        self.assertEqual("x/O/feHDb8/s8GmShwXLUDX46rbchpfgJAXphHDU3Tk=", payload["sign"])

    def test_webhook_requires_explicit_consistent_success_codes(self):
        for result in ({"code": 0}, {"StatusCode": 0}, {"code": 0, "StatusCode": 0}):
            with self.subTest(result=result):
                self.sender_with_result(result)[0].send("normal", "test")
        for result in ({}, {"code": 1}, {"StatusCode": 1}, {"code": 0, "StatusCode": 1}, []):
            with self.subTest(result=result), self.assertRaises(NotificationError):
                self.sender_with_result(result)[0].send("normal", "test")

    def test_webhook_error_messages_do_not_expose_secrets_or_hook(self):
        sender, _ = self.sender_with_result({"code": 1, "msg": "bad offline-key " + self.config["lark"]["normal_webhook"]})
        with self.assertRaises(NotificationError) as error:
            sender.send("normal", "test")
        self.assertNotIn("offline-key", str(error.exception))
        self.assertNotIn("https://", str(error.exception))
        for exc in (
            HTTPError(self.config["lark"]["normal_webhook"], 429, "failure", None, None),
            URLError("failure containing offline-key"),
        ):
            sender.opener.open.side_effect = exc
            with self.assertRaises(NotificationError) as error:
                sender.send("normal", "test")
            self.assertNotIn("offline-key", str(error.exception))
            self.assertNotIn("https://", str(error.exception))

    def test_queue_survives_restart_and_deduplicates_pending_delivery(self):
        self.queue.enqueue("normal", "hour/100", "hourly report", 100)
        self.queue.enqueue("normal", "hour/100", "hourly report", 100)
        restored = StateStore(self.path)
        restored.load()
        self.assertEqual(1, len(restored.state["outbox"]))
        self.assertTrue(NotificationQueue(restored, self.config, sender=self.sender).drain(100))
        self.sender.send.assert_called_once_with("normal", "hourly report")
        reread = StateStore(self.path)
        self.assertEqual([], reread.load()["outbox"])

    def test_normal_queue_redacts_at_ingress_before_persistence_or_delivery(self):
        secret = self.config["accounts"][0]["secret_key"]
        webhook = self.config["lark"]["normal_webhook"]
        self.queue.enqueue("normal", "unexpected-payload", "unexpected %s %s" % (secret, webhook), 100)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, persisted)
        self.assertNotIn(webhook, persisted)
        self.queue.drain(100)
        delivered = self.sender.send.call_args.args[1]
        self.assertNotIn(secret, delivered)
        self.assertNotIn(webhook, delivered)
        self.assertIn("已隐藏", delivered)

    def test_failed_normal_delivery_is_retained_and_reported_to_error_route(self):
        self.queue.enqueue("normal", "report", "hourly", 100)

        def fail_normal(route, text):
            if route == "normal":
                raise NotificationError("offline failure")

        self.sender.send.side_effect = fail_normal
        self.assertFalse(self.queue.drain(100))
        state = StateStore(self.path).load()
        normal = next(item for item in state["outbox"] if item["route"] == "normal")
        self.assertEqual(1, normal["attempts"])
        self.assertGreater(normal["next_attempt"], 100)
        self.assertTrue(state["errors"]["lark/normal"]["active"])
        self.queue.drain(100)
        self.assertTrue(any(args.args[0] == "error" and "正常通知通道投递失败" in args.args[1]
                            for args in self.sender.send.call_args_list))

    def test_normal_recovery_is_reported_to_error_channel(self):
        self.queue.enqueue("normal", "report", "hourly", 100)
        self.sender.send.side_effect = NotificationError("failure")
        self.queue.drain(100)
        self.sender.send.side_effect = None
        self.queue.drain(200)
        self.assertFalse(self.store.state["errors"]["lark/normal"]["active"])
        self.queue.drain(200)
        self.assertTrue(any(args.args[0] == "error" and "恢复" in args.args[1]
                            for args in self.sender.send.call_args_list))

    def test_error_route_failure_does_not_recursively_enqueue_errors(self):
        self.queue.incident("account/key", "invalid offline-key", 100)
        self.sender.send.side_effect = NotificationError("offline error route failure")
        self.assertFalse(self.queue.drain(100))
        self.assertEqual(1, len(self.store.state["outbox"]))
        self.assertEqual(1, len(self.store.state["errors"]))
        self.assertNotIn("offline-key", self.store.state["outbox"][0]["text"])

    def test_unchanged_incidents_repeat_only_after_configured_interval(self):
        self.queue.incident("account/auth", "invalid key", 100)
        self.queue.incident("account/auth", "invalid key", 101)
        self.assertEqual(1, len(self.store.state["outbox"]))
        self.queue.incident("account/auth", "invalid key", 100 + self.config["alerts"]["error_repeat_seconds"])
        self.assertEqual(2, len(self.store.state["outbox"]))

    def test_dry_run_enqueue_does_not_write_or_deliver(self):
        queue = NotificationQueue(self.store, self.config, sender=self.sender, dry_run=True)
        queue.enqueue("normal", "preview", "offline preview", 100)
        self.assertTrue(queue.drain(100))
        self.sender.send.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual([], self.store.state["outbox"])

    def test_utf8_split_preserves_text_without_exceeding_limit(self):
        original = "腾讯云😀" * 100
        parts = split_message(original, max_bytes=31)
        self.assertEqual(original, "".join(parts))
        self.assertTrue(all(len(part.encode("utf-8")) <= 31 for part in parts))

    def test_corrupt_state_is_preserved_and_read_only_save_is_non_mutating(self):
        self.path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(StateError):
            StateStore(self.path).load()
        self.assertEqual("{not-json", self.path.read_text(encoding="utf-8"))
        other = Path(self.temp.name) / "read-only.json"
        readonly = StateStore(other, read_only=True)
        readonly.save()
        self.assertFalse(other.exists())

    def test_malformed_state_entries_are_rejected_before_queue_processing(self):
        valid_item = {"id": "normal/hour/0", "route": "normal", "text": "report",
                      "created_at": 100, "next_attempt": 100, "attempts": 0}
        cases = [
            {"outbox": ["not-an-object"]},
            {"outbox": [dict(valid_item, route="untrusted")]},
            {"outbox": [dict(valid_item, next_attempt="100")]},
            {"outbox": [dict(valid_item, attempts=-1)]},
            {"errors": {"account/auth": "not-an-object"}},
            {"accounts": {"account-id": []}},
            {"reports": {"UTC+08:00/60": "not-a-timestamp"}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                malformed = deepcopy(self.store.state)
                malformed.update(changes)
                original = json.dumps(malformed)
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(StateError):
                    StateStore(self.path).load()
                self.assertEqual(original, self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
