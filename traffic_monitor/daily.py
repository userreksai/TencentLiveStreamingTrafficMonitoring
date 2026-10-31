"""Independent Beijing-clock daily reporting with its own state and worker."""

from copy import deepcopy
import hashlib
import json
import logging
import threading
import time

from .config import format_time, redact
from .daily_data import fetch_daily_report
from .notifications import NotificationQueue
from .storage import StateStore
from .tencent import TencentClient

LOG = logging.getLogger(__name__)
BEIJING_OFFSET = 8 * 3600


def daily_state_path(config):
    return config["storage"]["state_file"] + ".daily.json"


def daily_notification_config(config):
    """Both routes belong exclusively to the daily-report group."""
    private = deepcopy(config)
    daily = config["daily_report"]
    private["_timezone_offset"] = BEIJING_OFFSET
    private["lark"] = {
        route + suffix: daily.get(key, "")
        for route in ("normal", "error")
        for suffix, key in (("_webhook", "webhook"), ("_sign_secret", "sign_secret"))
    }
    return private


class _Cancelled(RuntimeError):
    pass


class _GuardedSender:
    def __init__(self, sender, current):
        self.sender = sender
        self.current = current

    def send(self, route, text):
        if not self.current():
            raise _Cancelled("日报配置已变更，本次投递已取消")
        self.sender.send(route, text)


class DailyReporter:
    """The caller owns this reporter's separate state lock, never the hourly lock."""

    def __init__(self, config, store=None, sender=None, client_factory=None, dry_run=False):
        self.config = config
        self.store = store or StateStore(daily_state_path(config), read_only=dry_run)
        self.client_factory = client_factory or TencentClient
        self.dry_run = dry_run
        self._pending = config
        self._generation = 0
        self._active_generation = 0
        self._external_current = lambda: True
        self.queue = NotificationQueue(self.store, daily_notification_config(config), sender=sender, dry_run=dry_run)
        self._sender = self.queue.sender
        self.queue.sender = _GuardedSender(self._sender, self._current)

    def _current(self):
        return self._generation == self._active_generation and self._external_current()

    def reconfigure(self, config):
        # Publication only: never wait for network requests or touch worker state.
        self._pending = config
        self._generation += 1

    def _discard_pending(self):
        reports = self.store.state["reports"]
        for item in self.store.state["outbox"]:
            parts = item["id"].split("/")
            if len(parts) == 5 and parts[:2] == ["normal", "daily"] and parts[3].isdecimal():
                key, day = parts[2], int(parts[3])
                if reports.get(key) == day:
                    # Only a report with undelivered parts loses its marker;
                    # accounts fully delivered today retain their deduplication.
                    reports.pop(key)
        self.store.state["outbox"] = []
        self.store.state["errors"] = {}
        self.store.state.pop("daily_attempts", None)

    def _apply_config(self):
        if self._active_generation == self._generation:
            return
        old = self.config["daily_report"]
        self.config = self._pending
        self._active_generation = self._generation
        new = self.config["daily_report"]
        self.queue.config = daily_notification_config(self.config)
        if hasattr(self._sender, "config"):
            self._sender.config = self.queue.config
        if old != new:
            # Never transfer queued messages to a newly selected group or keep
            # retries scheduled under an obsolete delivery time.
            self._discard_pending()
            self.store.save()

    def _sync_scope(self):
        scope = {"daily_report": self.config["daily_report"],
                 "accounts": [(account["_id"], account["endpoint"]) for account in self.config["accounts"]]}
        signature = hashlib.sha256(json.dumps(scope, sort_keys=True).encode("utf-8")).hexdigest()
        previous = self.store.state.get("daily_scope")
        if previous != signature:
            # Also apply after restart: never send a previously queued payload
            # to a different destination selected while this process was down.
            self._discard_pending()
            self.store.state["daily_scope"] = signature
        attempts = self.store.state.get("daily_attempts", {})
        if not isinstance(attempts, dict) or any(type(value) is not int or value < 0 for value in attempts.values()):
            raise ValueError("日报重试状态无效，请保留独立日报状态文件排查")
        return previous != signature

    def tick(self, now, force=False):
        self._apply_config()
        config = self.config
        daily = config["daily_report"]
        if not daily["enabled"] and not force:
            return True
        midnight = ((int(now) + BEIJING_OFFSET) // 86400) * 86400 - BEIJING_OFFSET
        hours, minutes = (int(part) for part in daily.get("time", "10:05").split(":"))
        scheduled = midnight + hours * 3600 + minutes * 60
        if not force and now < scheduled:
            return True
        if not self._current():
            return True
        dirty = self._sync_scope()

        # Drop yesterday's pending deliveries instead of sending stale daily
        # statistics after a long outage. Do not touch the hourly outbox.
        pending = self.store.state["outbox"]
        retained = [item for item in pending if item["created_at"] >= midnight]
        if len(retained) != len(pending):
            self.store.state["outbox"] = retained
            dirty = True
        reports = self.store.state["reports"]
        attempts = self.store.state.setdefault("daily_attempts", {})
        success = True
        for account in config["accounts"]:
            key = account["_id"]
            if reports.get(key) == midnight:
                continue
            last_attempt = attempts.get(key)
            if not force and last_attempt is not None and now - last_attempt < 300:
                success = False
                continue
            if not self._current():
                return True
            attempts[key] = int(now)
            dirty = True
            try:
                client = self.client_factory(account["secret_id"], account["secret_key"],
                                             token=account["session_token"], endpoint=account["endpoint"],
                                             timeout=config["sampling"]["request_timeout_seconds"])
                body = fetch_daily_report(client, now)
            except Exception as exc:
                if not self._current():
                    return True
                success = False
                self.queue.incident("daily/" + key,
                                    "每日流量包统计获取失败（%s）：%s" % (account["name"], redact(exc, config)),
                                    now, fingerprint=getattr(exc, "code", type(exc).__name__))
                continue
            if not self._current():
                return True
            text = "【腾讯云直播每日流量包统计】\n账号：%s\n获取时间：%s（北京时间）\n\n%s" % (
                account["name"], format_time(now, BEIJING_OFFSET), body)
            # The daily marker and queue entry are saved together by enqueue.
            # A failed delivery retries the same snapshot without querying again.
            reports[key] = midnight
            self.queue.enqueue("normal", "daily/%s/%s" % (key, midnight), text, now)
            self.queue.resolve("daily/" + key, now)
        if not self._current():
            return True
        if dirty:
            self.store.save()
        return self.queue.drain(now) and success


class DailyReportService:
    """Run cloud reads and webhook retries independently from hourly collection."""

    def __init__(self, config):
        self._config = config
        self._generation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def reconfigure(self, config):
        with self._lock:
            self._config = config
            self._generation += 1
        self._wake.set()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, name="daily-traffic-report", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._wake.set()
        # A slow cloud request must not delay shutdown of the hourly service.
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def _snapshot(self):
        with self._lock:
            return self._config, self._generation

    def _is_current(self, generation):
        with self._lock:
            return generation == self._generation and not self._stop.is_set()

    def _run(self):
        while not self._stop.is_set():
            config, _ = self._snapshot()
            if config["daily_report"]["enabled"]:
                break
            self._wake.wait(5)
            self._wake.clear()
        if self._stop.is_set():
            return
        try:
            reporter = DailyReporter(config)
            with reporter.store.lock():
                reporter.store.load()
                applied = None
                while not self._stop.is_set():
                    config, generation = self._snapshot()
                    if applied != generation:
                        reporter.reconfigure(config)
                        applied = generation
                    reporter._external_current = lambda value=generation: self._is_current(value)
                    try:
                        reporter.tick(int(time.time()))
                    except Exception as exc:
                        LOG.error("独立日报运行异常：%s", redact(exc, config))
                    self._wake.wait(5)
                    self._wake.clear()
        except Exception as exc:
            # A broken/locked daily state is isolated from the hourly monitor.
            LOG.error("独立日报未启动：%s", redact(exc, config))
