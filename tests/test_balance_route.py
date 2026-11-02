"""Offline checks for the dedicated low-balance notification destination."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from traffic_monitor.__main__ import main
from traffic_monitor.app import Monitor
from traffic_monitor.config import ConfigError, redact, validate_config
from traffic_monitor.notifications import LarkSender, NotificationError, NotificationQueue
from traffic_monitor.storage import StateStore
from test_config_notifications import raw_config


BALANCE_HOOK = "https://open.larksuite.com/open-apis/bot/v2/hook/offline-balance"


class BalanceConfigTests(unittest.TestCase):
    def test_legacy_configuration_remains_valid_without_fallback(self):
        cfg = validate_config(raw_config())
        self.assertEqual("", cfg["lark"]["balance_webhook"])
        self.assertEqual("", cfg["lark"]["balance_sign_secret"])
        self.assertEqual(0, cfg["alerts"]["repeat_seconds"])
        self.assertIn("offline-normal", cfg["lark"]["normal_webhook"])
        legacy = raw_config()
        legacy["alerts"]["repeat_seconds"] = 3600
        self.assertEqual(3600, validate_config(legacy)["alerts"]["repeat_seconds"])

    def test_balance_settings_do_not_change_other_routes_or_accounting(self):
        raw = raw_config()
        original = validate_config(raw)
        raw["lark"].update(balance_webhook=BALANCE_HOOK, balance_sign_secret="balance-secret")
        cfg = validate_config(raw)
        for field in ("reports", "sampling", "accounts", "daily_report", "_timezone_offset"):
            self.assertEqual(original[field], cfg[field])
        for key, value in original["lark"].items():
            if not key.startswith("balance_"):
                self.assertEqual(value, cfg["lark"][key])

    def test_optional_webhook_is_validated_and_balance_secrets_are_redacted(self):
        for value in ("http://open.larksuite.com/open-apis/bot/v2/hook/test",
                      "https://example.com/open-apis/bot/v2/hook/test", "not-a-url", 1):
            raw = raw_config()
            raw["lark"]["balance_webhook"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config(raw)
        raw = raw_config()
        raw["lark"].update(balance_webhook="env:BALANCE_TEST_HOOK", balance_sign_secret="env:BALANCE_TEST_SECRET")
        with patch.dict(os.environ, {"BALANCE_TEST_HOOK": BALANCE_HOOK, "BALANCE_TEST_SECRET": "balance-secret"}):
            cfg = validate_config(raw)
        text = redact(BALANCE_HOOK + " balance-secret", cfg)
        self.assertNotIn(BALANCE_HOOK, text)
        self.assertNotIn("balance-secret", text)


class BalanceRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        raw = raw_config()
        raw["storage"]["state_file"] = str(self.path)
        raw["lark"].update(balance_webhook=BALANCE_HOOK, balance_sign_secret="balance-secret")
        self.config = validate_config(raw)
        self.store = StateStore(self.path)
        self.sender = Mock()
        self.queue = NotificationQueue(self.store, self.config, sender=self.sender)

    def test_sender_uses_only_balance_webhook_and_its_signature(self):
        sender = LarkSender(self.config)
        sender.opener = Mock()
        sender.opener.open.return_value = io.BytesIO(b'{"code":0}')
        with patch("traffic_monitor.notifications.time.time", return_value=1700000000):
            sender.send("balance", "low balance")
        request = sender.opener.open.call_args.args[0]
        self.assertEqual(BALANCE_HOOK, request.full_url)
        payload = json.loads(request.data)
        self.assertEqual("low balance", payload["content"]["text"])
        self.assertEqual("1700000000", payload["timestamp"])
        self.assertIn("sign", payload)
        self.assertNotEqual(self.config["lark"]["normal_webhook"], request.full_url)

    def test_unconfigured_sender_errors_without_network_or_normal_fallback(self):
        self.config["lark"]["balance_webhook"] = ""
        sender = LarkSender(self.config)
        sender.opener = Mock()
        with self.assertRaisesRegex(NotificationError, "balance_webhook"):
            sender.send("balance", "low balance")
        sender.opener.open.assert_not_called()

    def test_balance_queue_survives_restart(self):
        self.queue.enqueue("balance", "threshold/account/50", "low balance", 100)
        restored = StateStore(self.path)
        restored.load()
        self.assertEqual("balance", restored.state["outbox"][0]["route"])
        queue = NotificationQueue(restored, self.config, sender=self.sender)
        self.assertTrue(queue.drain(100))
        self.sender.send.assert_called_once_with("balance", "low balance")
        self.assertEqual([], StateStore(self.path).load()["outbox"])

    def test_migration_discards_delayed_fifty_after_ten_was_already_recorded(self):
        key = self.config["accounts"][0]["_id"]
        self.store.state["accounts"][key] = {"last_threshold": "10", "last_threshold_at": 200}
        self.queue.enqueue("normal", "threshold/%s/50/100" % key, "obsolete fifty", 100)
        self.queue.enqueue("normal", "report/hour/200", "hourly report", 200)
        monitor = Monitor(self.config, self.store, sender=self.sender, client_factory=Mock())
        self.assertFalse(any("/threshold/" in item["id"] for item in self.store.state["outbox"]))
        self.assertEqual("10", self.store.state["accounts"][key]["last_threshold"])
        self.assertTrue(monitor.queue.drain(200))
        self.sender.send.assert_called_once_with("normal", "hourly report")

    def test_missing_balance_destination_preserves_pending_and_allows_hourly_delivery(self):
        self.config["lark"]["balance_webhook"] = ""
        self.queue.enqueue("balance", "threshold/account/50", "low balance", 100)
        self.queue.enqueue("normal", "hour/account", "hourly report", 100)
        self.assertFalse(self.queue.drain(100))
        self.assertIn(("normal", "hourly report"), [call.args for call in self.sender.send.call_args_list])
        self.assertFalse(any(call.args[0] == "balance" for call in self.sender.send.call_args_list))
        pending = next(item for item in self.store.state["outbox"] if item["route"] == "balance")
        self.assertEqual(0, pending["attempts"])
        self.assertEqual("MissingConfiguration", self.store.state["errors"]["lark/balance"]["fingerprint"])
        self.queue.drain(101)
        self.assertEqual(1, sum(call.args[0] == "error" for call in self.sender.send.call_args_list))
        self.queue.drain(102)
        self.assertEqual(1, sum(call.args[0] == "error" for call in self.sender.send.call_args_list))
        self.config["lark"]["balance_webhook"] = BALANCE_HOOK
        self.assertTrue(self.queue.drain(103))
        self.assertEqual(1, sum(call.args[0] == "balance" for call in self.sender.send.call_args_list))
        self.assertFalse(self.store.state["errors"]["lark/balance"]["active"])

    def test_failed_balance_route_retries_and_reports_recovery_without_blocking_hourly(self):
        self.queue.enqueue("balance", "threshold/account/50", "low balance", 100)
        self.queue.enqueue("normal", "hour/account", "hourly report", 100)

        def send(route, text):
            if route == "balance":
                raise NotificationError("offline balance delivery failure")

        self.sender.send.side_effect = send
        self.assertFalse(self.queue.drain(100))
        self.assertIn(("normal", "hourly report"), [call.args for call in self.sender.send.call_args_list])
        self.assertTrue(self.store.state["errors"]["lark/balance"]["active"])
        self.assertTrue(any(item["route"] == "balance" and item["attempts"] == 1 for item in self.store.state["outbox"]))
        self.queue.drain(101)
        self.assertTrue(any(call.args[0] == "error" and "余量告警通道投递失败" in call.args[1]
                            for call in self.sender.send.call_args_list))
        self.sender.send.side_effect = None
        self.assertTrue(self.queue.drain(200))
        self.assertFalse(self.store.state["errors"]["lark/balance"]["active"])
        self.queue.drain(201)
        self.assertTrue(any(call.args[0] == "error" and "恢复项：lark/balance" in call.args[1]
                            for call in self.sender.send.call_args_list))


class BalanceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.raw = raw_config()
        self.raw["storage"]["state_file"] = str(self.root / "state.json")
        self.raw["lark"]["balance_webhook"] = BALANCE_HOOK
        self.write_config()
        self.args = ["--config", str(self.path)]

    def write_config(self):
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")

    def test_balance_test_notifies_only_the_selected_route(self):
        with patch("traffic_monitor.__main__.LarkSender.send") as send, \
                patch("traffic_monitor.app.Monitor.sample") as sample, \
                patch("traffic_monitor.daily.fetch_daily_report") as daily:
            self.assertEqual(0, main(self.args + ["--test-notify", "balance"]))
        send.assert_called_once()
        self.assertEqual("balance", send.call_args.args[0])
        sample.assert_not_called()
        daily.assert_not_called()
        self.assertFalse((self.root / "state.json").exists())

    def test_missing_balance_hook_fails_without_sending_to_other_groups(self):
        self.raw["lark"].pop("balance_webhook")
        self.write_config()
        with patch("traffic_monitor.__main__.LarkSender.send") as send:
            self.assertEqual(1, main(self.args + ["--test-notify", "balance"]))
        send.assert_not_called()

    def test_check_reports_balance_destination_and_does_not_send(self):
        with patch("traffic_monitor.__main__.LarkSender.send") as send, \
                self.assertLogs("traffic-monitor", level="INFO") as logs:
            self.assertEqual(0, main(self.args + ["--check-config"]))
        send.assert_not_called()
        self.assertTrue(any("余量告警群已配置" in text for text in logs.output))


if __name__ == "__main__":
    unittest.main()
