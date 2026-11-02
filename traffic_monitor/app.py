"""Collection, calibrated accounting, business alerts and clock-aligned reports."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging

from .config import format_time, redact
from .ledger import calculate, new_ledger, upsert_buckets
from .notifications import NotificationQueue
from .packages import sync_packages
from .reporting import report_scope, window_usage
from .tencent import TencentClient, fetch_billable_buckets, fetch_packages, fetch_today_downstream

LOG = logging.getLogger(__name__)


def size(value, places=3):
    return format(Decimal(str(value)) / Decimal(10**12), ".%sf" % places) + " TB"


def slot_for(now, interval_minutes, offset):
    period = interval_minutes * 60
    return ((int(now) + offset) // period) * period - offset


def _expiry(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(parsed.timestamp())


def package_snapshot(raw, account, now):
    """Keep historical package rows out of notifications; never rebase on Left."""
    active = []
    known = {}
    for item in raw:
        package_id = str(item["Id"])
        if int(item.get("Type", 0)) != 0:
            continue
        total = Decimal(str(item["Total"]))
        left = Decimal(str(item["Left"]))
        if (not total.is_finite() or not left.is_finite() or total <= 0 or left < 0 or left > total
                or total != total.to_integral_value() or left != left.to_integral_value()):
            raise ValueError("腾讯云返回了无效的套餐额度")
        expires = _expiry(item.get("ExpireTime"))
        bought = _expiry(item.get("BuyTime"))
        status = int(item["Status"])
        if status not in (0, 1, 2, 3, 4, 5):
            raise ValueError("腾讯云返回未知的套餐状态，不能确认套餐可用性")
        known[package_id] = {"total": str(total), "expires": expires, "status": status,
                             "left": str(left), "bought": bought}
        if status not in (0, 1) or (expires is not None and expires <= now):
            continue
        active.append({"id": package_id,
                       "name": account["package_names"].get(package_id) or "云直播流量包 %s" % size(total),
                       "total_bytes": str(total), "left_bytes": str(left), "status": status,
                       "expires": expires})
    return {"checked_at": now, "packages": active,
            "remaining_bytes": str(sum((Decimal(x["left_bytes"]) for x in active), Decimal(0))),
            "total_bytes": str(sum((Decimal(x["total_bytes"]) for x in active), Decimal(0))),
            "known": known}


def dimensions(account):
    return [{"service": service, "region": region}
            for service in account["services"] for region in account["regions"]]


def hourly_usage(ledger, start, end, account):
    usage = window_usage(ledger, start, end, account)
    return usage["folded"], usage["complete"]


def _more_severe(tag, previous):
    if previous is None:
        return True
    if tag == "exhausted":
        return previous != tag
    return previous != "exhausted" and Decimal(tag) < Decimal(previous)


class Monitor:
    def __init__(self, config, store, sender=None, client_factory=None, dry_run=False):
        self.config = config
        self.store = store
        self.queue = NotificationQueue(store, config, sender=sender, dry_run=dry_run)
        self.client_factory = client_factory or TencentClient
        self.dry_run = dry_run
        self.collected = set()
        self.migrate_balance_alerts()

    def migrate_balance_alerts(self):
        """Move pending legacy business alerts off the hourly route once.

        Keep all parts of the most severe pending threshold and the latest
        runway reminder for each account. Persisted threshold latches survive.
        """
        original = self.store.state["outbox"]
        migrated = []
        chosen = {}
        identities = {}
        obsolete = set()
        for source in original:
            item = source
            if item["id"].startswith(("normal/threshold/", "normal/runway/")):
                item = dict(item, route="balance", id="balance/" + item["id"].split("/", 1)[1])
            migrated.append(item)
            parts = item["id"].split("/")
            if item["route"] != "balance" or len(parts) < 5:
                continue
            kind = parts[1]
            if kind not in ("threshold", "runway"):
                continue
            logical = item["id"].rsplit("/", 1)[0]
            if kind == "threshold":
                if len(parts) != 6:
                    continue
                try:
                    level = Decimal("-Infinity") if parts[3] == "exhausted" else Decimal(parts[3])
                    if parts[3] != "exhausted" and not level.is_finite():
                        continue
                except InvalidOperation:
                    continue
                previous = self.store.state["accounts"].get(parts[2], {}).get("last_threshold")
                if previous is not None and _more_severe(previous, parts[3]):
                    obsolete.add(item["id"])
                    continue
            else:
                if len(parts) != 5:
                    continue
                level = Decimal(0)
            group = (kind, parts[2])
            rank = (level, -item["created_at"], logical)
            identities[item["id"]] = (group, logical)
            if group not in chosen or rank < chosen[group][0]:
                chosen[group] = (rank, logical)
        retained = []
        seen = set()
        for item in migrated:
            if item["id"] in obsolete:
                continue
            identity = identities.get(item["id"])
            if identity and identity[1] != chosen[identity[0]][1]:
                continue
            if item["id"] not in seen:
                retained.append(item)
                seen.add(item["id"])
        if retained != original:
            self.store.state["outbox"] = retained
            self.store.save()
        updated = False
        for (kind, key), (rank, logical) in chosen.items():
            record = self.store.state["accounts"].get(key)
            if record is None:
                continue
            if kind == "runway":
                if record.get("last_runway_at") is None:
                    record["last_runway_at"] = -rank[1]
                    updated = True
                continue
            tag = logical.split("/")[3]
            previous = record.get("last_threshold")
            # Legacy periodic repeats could overwrite a deeper latch after a
            # data revision. An undelivered deeper alert still covers that level.
            if _more_severe(tag, previous):
                record.update(last_threshold=tag, last_threshold_at=-rank[1])
                updated = True
        if updated:
            self.store.save()

    def discard_balance_alerts(self, key, kinds=("threshold", "runway")):
        prefixes = tuple("%s/%s/%s/" % (route, kind, key)
                         for route in ("normal", "balance") for kind in kinds)
        self.store.state["outbox"] = [item for item in self.store.state["outbox"]
                                      if not item["id"].startswith(prefixes)]

    def reconfigure(self, config):
        if config["storage"]["state_file"] != self.config["storage"]["state_file"]:
            raise ValueError("运行中更改 state_file 需要重启服务")
        schedule = lambda cfg: (cfg["reports"]["timezone"], cfg["reports"]["interval_minutes"])
        if (report_scope(config["accounts"]) != report_scope(self.config["accounts"])
                or schedule(config) != schedule(self.config)):
            self.discard_queued_reports()
            self.store.save()
        self.config = config
        self.queue.config = config
        if hasattr(self.queue.sender, "config"):
            self.queue.sender.config = config

    def discard_queued_reports(self, key=None, end=None):
        prefixes = (("normal/report/%s/%s/" % (key, end), "normal/report-update/%s/%s/" % (key, end))
                    if key is not None else ("normal/report/", "normal/report-update/"))
        self.store.state["outbox"] = [item for item in self.store.state["outbox"]
                                      if not item["id"].startswith(prefixes)]

    def record_for(self, account, now):
        record = self.store.state["accounts"].setdefault(account["_id"], {})
        if record.get("anchor_id") != account["_anchor_id"]:
            previous = record.get("anchor_id")
            record.clear()
            record.update(anchor_id=account["_anchor_id"], name=account["name"], quality=False)
            if account["_anchor"]:
                anchor = account["_anchor"]
                record["ledger"] = new_ledger(anchor["time"], anchor["remaining_bytes"], anchor["total_bytes"])
            if previous:
                self.discard_balance_alerts(account["_id"])
                self.queue.incident(account["name"] + "/calibration", "计算基准或统计范围已更改，已按新配置重新建立台账。", now)
        return record

    def sample(self, now):
        success = True
        for account in self.config["accounts"]:
            record = self.record_for(account, now)
            record["last_attempt"] = now
            faults = []
            unannounced = []
            name = account["name"]
            client = self.client_factory(account["secret_id"], account["secret_key"],
                                         token=account["session_token"], endpoint=account["endpoint"],
                                         timeout=self.config["sampling"]["request_timeout_seconds"])
            try:
                snapshot = package_snapshot(fetch_packages(client), account, now)
                unannounced = sync_packages(record, account, snapshot, now)
                record["official"] = snapshot
                self.store.save()  # Funding identity is durable before any notification.
                self.queue.resolve(name + "/packages", now)
            except Exception as exc:
                faults.append("套餐列表获取失败")
                self.queue.incident(name + "/packages", "套餐列表获取失败：%s" % redact(exc, self.config), now,
                                    fingerprint=getattr(exc, "code", type(exc).__name__))

            try:
                anchor = account["_anchor"]
                end = now // 300 * 300
                if anchor:
                    if anchor["time"] > now:
                        raise ValueError("校准基准时间在未来，请核对时区与配置")
                    last_success = record.get("last_success", anchor["time"])
                    start = max(anchor["time"], min(end - self.config["sampling"]["lookback_minutes"] * 60,
                                                    last_success - self.config["sampling"]["lookback_minutes"] * 60))
                    prior = record.get("result", {})
                    if prior.get("internal_missing_intervals") or prior.get("missing_dimensions"):
                        start = anchor["time"]
                else:
                    start = end - self.config["sampling"]["lookback_minutes"] * 60
                start = start // 300 * 300
                if end - start > self.config["sampling"]["max_backfill_days"] * 86400:
                    raise ValueError("需回补的历史超过配置上限，请以近期已确认结算范围重新校准")
                buckets = fetch_billable_buckets(client, start, end, account["services"], account["regions"])
                record["last_success"] = now
                record["last_fetch_end"] = end
                if anchor:
                    upsert_buckets(record["ledger"], buckets, now)
                    result = calculate(record["ledger"], now,
                                       safety_seconds=self.config["alerts"]["safety_seconds"],
                                       safety_factor=self.config["alerts"]["safety_factor"],
                                       expected_dimensions=dimensions(account))
                    record["result"] = result
                    faults.extend(self.quality_faults(result))
                    official = record.get("official")
                    if official and official["checked_at"] == now:
                        # A lower settled balance is already evidence of a discrepancy;
                        # do not reset the ledger or deduct a second time to hide it.
                        tolerance = (Decimal(anchor["total_bytes"])
                                     * Decimal(self.config["alerts"]["reconciliation_tolerance_percent"]) / 100)
                        comparable = (Decimal(result["estimated_remaining_bytes"])
                                      - Decimal(result.get("uncovered_reserve_bytes", "0")))
                        if Decimal(official["remaining_bytes"]) + tolerance < comparable:
                            faults.append("官方结算余量低于本地估算，超过核对容差；请核查统计范围和校准基准")
                    self.queue.resolve(name + "/uncalibrated", now)
                else:
                    # Raw/weighted hourly usage is still useful before a balance
                    # anchor is confirmed; it must not masquerade as a balance.
                    usage = record.setdefault("usage", {"buckets": {}})
                    for bucket in buckets:
                        key = "%s|%s|%s" % (bucket["time"], bucket["service"], bucket["region"])
                        usage["buckets"][key] = bucket
                    cutoff = now - 7 * 86400
                    usage["buckets"] = {k: v for k, v in usage["buckets"].items() if v["time"] >= cutoff}
                    record["result"] = None
                    faults.append("尚未校准计算基准")
                    self.queue.incident(name + "/uncalibrated",
                                        "已采集五分钟用量，但未填写 anchor 校准基准，无法可靠计算剩余量。请填写 usage_start、remaining_tb、total_tb。", now)
                self.queue.resolve(name + "/collection", now)
                self.collected.add(account["_id"])
            except Exception as exc:
                faults.append("直播用量获取或计算失败")
                self.queue.incident(name + "/collection", "直播用量获取或计算失败：%s" % redact(exc, self.config), now,
                                    fingerprint=getattr(exc, "code", type(exc).__name__))
            try:
                record["today_downstream"] = fetch_today_downstream(client, now, self.config["_timezone_offset"])
                record["today_downstream_error"] = False
                latest = record["today_downstream"]["latest_data_time"]
                if latest is not None and now - latest > self.config["sampling"]["max_data_age_seconds"]:
                    success = False
                    self.queue.incident(name + "/today_downstream", "今日下行流量数据已落后 %s 秒，正常通知将标记数据滞后。" % (
                        now - latest), now, fingerprint="DataStale")
                else:
                    self.queue.resolve(name + "/today_downstream", now)
            except Exception as exc:
                # The overview counter is independent of the calibrated ledger;
                # its failure must not invalidate otherwise healthy accounting.
                success = False
                record["today_downstream_error"] = True
                self.queue.incident(name + "/today_downstream", "今日下行流量获取失败：%s" % redact(exc, self.config), now,
                                    fingerprint=getattr(exc, "code", type(exc).__name__))
            if record.get("inventory_changed"):
                faults.extend(record.get("funding", {}).get("issues") or ["套餐历史变更需要核对"])
            if faults:
                success = False
                record["quality"] = False
                record["issues"] = list(dict.fromkeys(faults))
                self.queue.incident(name + "/quality", "；".join(record["issues"]), now)
            else:
                record["quality"] = True
                record["issues"] = []
                self.queue.resolve(name + "/quality", now)
            self.store.save()
            for credit in unannounced:
                title = account["package_names"].get(credit["id"]) or "云直播流量包"
                text = "\n".join([
                    "【腾讯云直播新购流量包已识别】", "账号：%s" % name,
                    "流量包：%s（ID %s）" % (title, credit["id"]),
                    "新增总额度：%s" % size(credit["capacity"]),
                    "购买时间：%s" % format_time(credit["buy_time"], self.config["_timezone_offset"]),
                    "已自动纳入计算，无需为本次购包修改 anchor。",
                    "本轮计算状态：%s" % ("正常" if record["quality"] else "数据待核对，详见异常通知"),
                ])
                self.queue.enqueue("normal", "purchase/%s/%s/%s" % (account["_id"], credit["id"], credit["buy_time"]), text, now)
                credit["announced"] = True
            if record.get("result"):
                active = {lot["id"] for lot in record["result"].get("funding_lots", [])
                          if not lot["baseline"] and lot["active"]}
                armed = set(record.get("armed_purchase_ids", []))
                if active - armed:
                    self.discard_balance_alerts(account["_id"])
                    for field in ("last_threshold", "last_threshold_at", "last_runway_at"):
                        record.pop(field, None)
                    record["armed_purchase_ids"] = sorted(armed | active)
            if record.get("result") and int(record["result"]["overrun_bytes"]) > 0:
                self.queue.incident(name + "/uncovered_usage",
                                    "部分历史用量在发生时没有可用套餐覆盖，不能因后续购包视为已抵扣；已对安全余量保留对应预留，请核查可能的按量费用。", now)
            else:
                self.queue.resolve(name + "/uncovered_usage", now)
            self.store.save()
            if record["quality"]:
                self.business_alerts(account, record, now)
        self.store.save()
        return success

    def quality_faults(self, result):
        issues = []
        if result["missing_dimensions"]:
            issues.append("缺少地区/业务类型数据：%s" % ", ".join(result["missing_dimensions"]))
        if result["internal_missing_intervals"]:
            issues.append("历史用量存在 %s 个五分钟数据缺口" % result["internal_missing_intervals"])
        if result["latest_data_time"] is None:
            issues.append("没有完整的五分钟用量数据")
        elif result["data_lag_seconds"] > self.config["sampling"]["max_data_age_seconds"]:
            issues.append("用量数据已落后 %s 秒" % result["data_lag_seconds"])
        return issues

    def business_alerts(self, account, record, now):
        result = record["result"]
        percent = Decimal(result["safe_percent"])
        levels = [Decimal(level) for level in self.config["alerts"]["remaining_percent"] if percent < Decimal(level)]
        tag = "exhausted" if Decimal(result["estimated_remaining_bytes"]) <= 0 else (str(min(levels)) if levels else None)
        previous = record.get("last_threshold")
        runway = result["safe_runway_seconds"]
        limit = self.config["alerts"]["runway_minutes"] * 60
        runway_low = bool(limit and runway is not None and runway < limit)
        if not self.config["lark"].get("balance_webhook"):
            if tag is not None or runway_low:
                self.queue.incident("lark/balance", "余量告警群未配置：请填写 lark.balance_webhook。余量提醒暂不发送，不会改投小时通知群。",
                                    now, fingerprint="MissingConfiguration")
            return
        incident = self.store.state["errors"].get("lark/balance", {})
        if incident.get("fingerprint") == "MissingConfiguration":
            self.queue.resolve("lark/balance", now)
        if tag is not None:
            if _more_severe(tag, previous):
                text = "\n".join([
                    "【腾讯云直播流量余量告警】", "账号：%s" % account["name"],
                    "级别：%s" % ("估算已耗尽，请立即核查" if tag == "exhausted" else "安全剩余量不足 %s%%" % tag),
                    "估算剩余：%s（%.2f%%）" % (size(result["estimated_remaining_bytes"]), Decimal(result["remaining_percent"])),
                    "安全可用：%s（%.2f%%）" % (size(result["safe_remaining_bytes"]), percent),
                    "安全预留：%s" % size(result["reserve_bytes"]),
                    "用量截至：%s" % format_time(result["latest_data_time"], self.config["_timezone_offset"]),
                    "请重点关注，并及时购买流量包进行抵扣。已经发生的用量能否追溯抵扣，以腾讯云购包与结算规则为准。",
                    "以上为按计费用量折算的估算值，非官方实时套餐余额。",
                ])
                # A queued older level is superseded by the current severity.
                self.discard_balance_alerts(account["_id"], kinds=("threshold",))
                record.update(last_threshold=tag, last_threshold_at=now)
                self.queue.enqueue("balance", "threshold/%s/%s/%s" % (account["_id"], tag, now), text, now)
        if runway_low and record.get("last_runway_at") is None:
            record["last_runway_at"] = now
            self.queue.enqueue("balance", "runway/%s/%s" % (account["_id"], now),
                               "【腾讯云直播流量即将不足】\n账号：%s\n按近期较高消耗速率，安全可用量预计支撑约 %s 分钟。\n请及时核查并购买流量包。此时长为预测，不保证后续流量不突增。" % (
                                   account["name"], runway // 60), now)
        self.store.save()

    def window_status(self, slot):
        start = slot - self.config["reports"]["interval_minutes"] * 60
        signatures = []
        complete = True
        for account in self.config["accounts"]:
            record = self.store.state["accounts"].get(account["_id"], {})
            ledger = record.get("ledger") or record.get("usage", {})
            usage = window_usage(ledger, start, slot, account)
            valid = record.get("anchor_id") == account["_anchor_id"]
            complete = complete and valid and usage["complete"] == usage["expected"]
            signatures.append((account["_id"], valid, usage["signature"]))
        signature = hashlib.sha256(json.dumps(sorted(signatures)).encode()).hexdigest()
        return {"signature": signature, "complete": bool(complete)}

    def today_text(self, record, now):
        daily = record.get("today_downstream")
        if record.get("today_downstream_error"):
            return "获取失败"
        offset = self.config["_timezone_offset"]
        day_start = (now + offset) // 86400 * 86400 - offset
        if (not daily or daily["day_start"] != day_start
                or not 0 <= now - daily["checked_at"] <= self.config["sampling"]["max_data_age_seconds"]):
            return "待更新"
        latest = daily.get("latest_data_time")
        if latest is not None and now - latest > self.config["sampling"]["max_data_age_seconds"]:
            return "数据滞后"
        return size(daily["bytes"], 6)

    def report_text(self, slot, now, kind="regular"):
        cfg = self.config
        period = cfg["reports"]["interval_minutes"] * 60
        start = slot - period
        offset = cfg["_timezone_offset"]

        def timestamp(value):
            return format_time(value, offset).rsplit(" ", 1)[0]

        lines = ["【腾讯云直播流量整点汇总】",
                 "统计窗口：%s ～ %s" % (timestamp(start), timestamp(slot))]
        for account in cfg["accounts"]:
            record = self.store.state["accounts"].get(account["_id"], {})
            if len(cfg["accounts"]) > 1:
                lines.extend(["", "账号：%s" % account["name"]])
            if record.get("anchor_id") != account["_anchor_id"]:
                lines.append("流量包估算剩余：待采集（配置基准已更改）")
                lines.append("今日下行流量：待更新")
                continue
            official = record.get("official")
            if official:
                for package in official["packages"]:
                    lines.append("%s（ID %s）%s" % (package["name"], package["id"],
                                 "使用中" if package["status"] == 1 else "备用未使用"))
            if not record.get("ledger"):
                lines.append("流量包估算剩余：未校准，暂不可计算")
            else:
                result = calculate(record["ledger"], now,
                                   safety_seconds=cfg["alerts"]["safety_seconds"], safety_factor=cfg["alerts"]["safety_factor"],
                                   expected_dimensions=dimensions(account))
                issues = list(record.get("issues", [])) + self.quality_faults(result)
                if not record.get("quality") or issues:
                    lines.append("流量包估算剩余：暂不可可靠计算（详见异常通知）")
                else:
                    lines.append("流量包估算剩余：%s（%.2f%%）" % (
                        size(result["estimated_remaining_bytes"]), Decimal(result["remaining_percent"])))
            lines.append("今日下行流量：%s" % self.today_text(record, now))
            usage = window_usage(record.get("ledger") or record.get("usage", {}), start, slot, account)
            label = "一小时内流量包用量" if period == 3600 else "本统计窗口流量包用量"
            amount = size(usage["folded"], 6)
            if usage["complete"] != usage["expected"]:
                amount += "（数据不完整，%s/%s 段；仅已到达用量）" % (usage["complete"], usage["expected"])
            lines.append("%s：%s" % (label, amount))
        return "\n".join(lines)

    def reports(self, now, force=False):
        cfg = self.config
        delay = cfg["reports"]["delay_minutes"] * 60
        slot = slot_for(now - delay, cfg["reports"]["interval_minutes"], cfg["_timezone_offset"])
        key = "%s/%s" % (cfg["reports"]["timezone"], cfg["reports"]["interval_minutes"])
        previous = self.store.state["reports"].get(key)
        horizon = max(cfg["sampling"]["lookback_minutes"] * 60, delay + cfg["sampling"]["interval_seconds"])
        scope = report_scope(cfg["accounts"])
        tracker = self.store.state.get("report_updates")
        changed = False
        if not tracker or tracker["schedule"] != key or tracker["scope"] != scope:
            migration = tracker is None
            tracker = {"format_version": 2, "schedule": key, "scope": scope, "windows": {}}
            self.store.state["report_updates"] = tracker
            changed = True
            if not migration:
                # A changed scope/schedule begins afresh at the next boundary.
                self.discard_queued_reports()
                previous = slot
                self.store.state["reports"][key] = slot
        if tracker.get("format_version") != 2:
            # Old journal entries describe reports already emitted. Do not
            # resend them merely because their new delivery delay has passed.
            tracker["format_version"] = 2
            for status in tracker["windows"].values():
                status["sent"] = True
            tracker.pop("observation", None)
            changed = True
        # Rebuild old-format pending reports only after fresh collection, while
        # preserving their window identities and all unrelated notifications.
        old_prefixes = ("normal/report/%s/" % key, "normal/report-update/%s/" % key)
        old_pending = [item for item in self.store.state["outbox"]
                       if item["id"].startswith(old_prefixes) and "/v2/" not in item["id"]]
        if old_pending:
            for item in old_pending:
                end = item["id"].split("/")[4]
                if end.isdecimal() and 0 <= now - int(end) <= horizon:
                    tracker["windows"][end] = {"signature": "", "complete": False, "sent": False}
                self.store.state["outbox"].remove(item)
            changed = True
        if previous is None and not force:
            # A fresh install starts at the next delayed wall-clock boundary.
            self.store.state["reports"][key] = slot
            self.store.save()
            return False
        sent = False
        if force:
            manual_slot = slot_for(now, cfg["reports"]["interval_minutes"], cfg["_timezone_offset"])
            self.queue.enqueue("normal", "report/%s/%s/v2/manual/%s" % (key, manual_slot, now),
                               self.report_text(manual_slot, now), now)
            # Explicit previews/reports do not consume the automatic schedule.
            if previous is None:
                self.store.state["reports"][key] = slot
                self.store.save()
            return True
        if slot > previous:
            tracker["windows"][str(slot)] = {"signature": "", "complete": False, "sent": False}
            self.store.state["reports"][key] = slot
            changed = True
        # Only usage collection can change a window. Avoid scanning historical
        # ledgers on the daemon's five-second scheduling ticks.
        observation = [[account["_id"], self.store.state["accounts"].get(account["_id"], {}).get("last_attempt"),
                        self.store.state["accounts"].get(account["_id"], {}).get("last_success")]
                       for account in cfg["accounts"]]
        for end, status in list(tracker["windows"].items()):
            if now - int(end) > horizon:
                if not status.get("sent", True):
                    self.queue.incident("report/incomplete", "统计窗口截至 %s 的用量未在等待期限内完整取得，本窗口未发送正常汇总；请检查采集异常。" % (
                        format_time(int(end), cfg["_timezone_offset"])), now, fingerprint=end)
                del tracker["windows"][end]
                changed = True
        if tracker.get("observation") != observation or changed:
            changed = True
            for end, old in sorted(tracker["windows"].items()):
                deadline = int(end) + delay
                if now < deadline:
                    continue
                fresh = all(account_id in self.collected and last_success is not None
                            and last_success == last_attempt and last_success >= deadline
                            and 0 <= now - last_success <= cfg["sampling"]["max_data_age_seconds"]
                            for account_id, last_attempt, last_success in observation)
                if not fresh:
                    continue
                status = self.window_status(int(end))
                was_sent = old.get("sent", True)
                revised = cfg["reports"]["revision_updates"] and status["signature"] != old["signature"]
                pending_prefix = "normal/report/%s/%s/v2/" % (key, end)
                pending = any(item["id"].startswith(pending_prefix) for item in self.store.state["outbox"])
                if status["complete"] and (not was_sent or revised or pending):
                    kind = "revision" if was_sent and not pending else "regular"
                    self.discard_queued_reports(key, end)
                    self.queue.enqueue("normal", "report/%s/%s/v2/%s/%s" % (
                        key, end, kind, status["signature"]), self.report_text(int(end), now, kind=kind), now)
                    tracker["windows"][end] = dict(status, sent=True)
                    self.queue.resolve("report/incomplete", now)
                    sent = True
            tracker["observation"] = observation
        if sent or changed:
            self.store.save()
        return sent
