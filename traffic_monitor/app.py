"""Collection, calibrated accounting, business alerts and clock-aligned reports."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
import logging

from .config import format_time, redact
from .ledger import calculate, new_ledger, upsert_buckets
from .notifications import NotificationQueue
from .packages import sync_packages
from .reporting import report_scope, window_usage
from .tencent import TencentClient, fetch_billable_buckets, fetch_packages

LOG = logging.getLogger(__name__)


def size(value):
    value = Decimal(str(value))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000 or unit == "PB":
            return "%.3f %s" % (value, unit)
        value /= 1000


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


class Monitor:
    def __init__(self, config, store, sender=None, client_factory=None, dry_run=False):
        self.config = config
        self.store = store
        self.queue = NotificationQueue(store, config, sender=sender, dry_run=dry_run)
        self.client_factory = client_factory or TencentClient
        self.dry_run = dry_run

    def reconfigure(self, config):
        if config["storage"]["state_file"] != self.config["storage"]["state_file"]:
            raise ValueError("运行中更改 state_file 需要重启服务")
        if (report_scope(config["accounts"]) != report_scope(self.config["accounts"])
                or config["reports"] != self.config["reports"]):
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
            except Exception as exc:
                faults.append("直播用量获取或计算失败")
                self.queue.incident(name + "/collection", "直播用量获取或计算失败：%s" % redact(exc, self.config), now,
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
                    prefixes = ("normal/threshold/%s/" % account["_id"], "normal/runway/%s/" % account["_id"])
                    self.store.state["outbox"] = [item for item in self.store.state["outbox"]
                                                   if not item["id"].startswith(prefixes)]
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
        last = record.get("last_threshold_at", 0)
        repeat = self.config["alerts"]["repeat_seconds"]
        if tag is not None:
            more_severe = previous is None or (tag == "exhausted" and previous != tag)
            if tag != "exhausted" and previous not in (None, "exhausted"):
                more_severe = Decimal(tag) < Decimal(previous)
            due = repeat > 0 and now - last >= repeat
            if more_severe or due:
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
                self.queue.enqueue("normal", "threshold/%s/%s/%s" % (account["_id"], tag, now), text, now)
                record.update(last_threshold=tag, last_threshold_at=now)
        runway = result["safe_runway_seconds"]
        limit = self.config["alerts"]["runway_minutes"] * 60
        if (limit and runway is not None and runway < limit
                and (record.get("last_runway_at") is None or (repeat and now - record["last_runway_at"] >= repeat))):
            self.queue.enqueue("normal", "runway/%s/%s" % (account["_id"], now),
                               "【腾讯云直播流量即将不足】\n账号：%s\n按近期较高消耗速率，安全可用量预计支撑约 %s 分钟。\n请及时核查并购买流量包。此时长为预测，不保证后续流量不突增。" % (
                                   account["name"], runway // 60), now)
            record["last_runway_at"] = now
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

    def usage_lines(self, record, account, start, slot):
        usage = window_usage(record.get("ledger") or record.get("usage", {}), start, slot, account)
        full = usage["complete"] == usage["expected"]
        label = "一小时内用量" if slot - start == 3600 else "本统计窗口用量"
        parts = ["%s：原始播放流量：%.6f MB（%.6f GB）" % (
            label, usage["raw"] / 10**6, usage["raw"] / 10**9)]
        if "PUSH" in account["services"]:
            parts.append("原始推流流量：%.6f MB（%.6f GB）" % (
                usage["push"] / 10**6, usage["push"] / 10**9))
        parts.append("流量包折算扣量：%s（%.6f GB）" % (size(usage["folded"]), usage["folded"] / 10**9))
        parts.append("%s，%s/%s 段" % ("完整统计" if full else "暂报（数据不完整）",
                                        usage["complete"], usage["expected"]))
        if usage["partial"]:
            parts.append("另有 %s 段部分到达，已计入已到达用量" % usage["partial"])
        cutoff = format_time(usage["cutoff"], self.config["_timezone_offset"]).rsplit(" ", 1)[0]
        return ["；".join(parts), "用量截至：%s" % cutoff]

    def report_text(self, slot, now, kind="regular"):
        cfg = self.config
        period = cfg["reports"]["interval_minutes"] * 60
        start = slot - period
        offset = cfg["_timezone_offset"]
        def timestamp(value):
            return format_time(value, offset).rsplit(" ", 1)[0]

        title = {"regular": "整点汇总", "completion": "完整统计补报", "revision": "用量修订"}[kind]
        lines = ["【腾讯云直播流量%s】" % title,
                 "统计窗口：%s ～ %s" % (timestamp(start), timestamp(slot))]
        for account in cfg["accounts"]:
            record = self.store.state["accounts"].get(account["_id"], {})
            lines.append("账号：%s" % account["name"])
            if record.get("anchor_id") != account["_anchor_id"]:
                lines.append("状态：配置基准已更改，等待本轮采集完成，当前不提供可靠余量判断。")
                continue
            official = record.get("official")
            if official:
                lines.append("官方结算余量：%s（数据滞后，仅供对照）" % size(official["remaining_bytes"]))
                for package in official["packages"]:
                    lines.append("%s（ID %s）%s" % (package["name"], package["id"],
                                 "使用中" if package["status"] == 1 else "备用未使用"))
            if not record.get("ledger"):
                lines.append("状态：未校准，无法可靠计算剩余量；请配置 anchor，异常信息已进入异常通知通道。")
                lines.extend(self.usage_lines(record, account, start, slot))
                continue
            result = calculate(record["ledger"], now,
                               safety_seconds=cfg["alerts"]["safety_seconds"], safety_factor=cfg["alerts"]["safety_factor"],
                               expected_dimensions=dimensions(account))
            issues = list(record.get("issues", [])) + self.quality_faults(result)
            if not record.get("quality") or issues:
                lines.append("估算剩余：暂不可可靠计算（数据异常，详见异常通知）")
            else:
                lines.append("估算剩余：%s（%.2f%%）" % (size(result["estimated_remaining_bytes"]), Decimal(result["remaining_percent"])))
            lines.extend(self.usage_lines(record, account, start, slot))
            if start < record["ledger"]["anchor_time"]:
                lines.append("本窗口早于校准基准的部分不在台账中，统计不完整。")
        lines.append("说明：未到达的用量段不计为零；数据可能回补修订。")
        if kind != "regular":
            lines.append("本次替换同一窗口先前用量，请勿重复累加；余量为通知生成时估算。")
        return "\n".join(lines)

    def reports(self, now, force=False):
        cfg = self.config
        slot = slot_for(now, cfg["reports"]["interval_minutes"], cfg["_timezone_offset"])
        key = "%s/%s" % (cfg["reports"]["timezone"], cfg["reports"]["interval_minutes"])
        previous = self.store.state["reports"].get(key)
        horizon = max(cfg["sampling"]["lookback_minutes"] * 60, cfg["sampling"]["interval_seconds"])
        scope = report_scope(cfg["accounts"])
        tracker = self.store.state.get("report_updates")
        changed = False
        if not tracker or tracker["schedule"] != key or tracker["scope"] != scope:
            migration = tracker is None
            tracker = {"schedule": key, "scope": scope, "windows": {}}
            self.store.state["report_updates"] = tracker
            changed = True
            if (migration and previous is not None and 0 <= now - previous <= horizon
                    and any(record.get("last_attempt", 0) >= previous
                            for record in self.store.state["accounts"].values())):
                # Older versions saved only a slot, not the delivered coverage.
                # Reconcile at most this recent window once data is complete.
                tracker["windows"][str(previous)] = {"signature": "", "complete": False}
            if not migration:
                # A changed scope/schedule begins afresh at the next boundary.
                self.discard_queued_reports()
                previous = slot
                self.store.state["reports"][key] = slot
        retained = {end: value for end, value in tracker["windows"].items()
                    if 0 <= now - int(end) <= horizon}
        if retained != tracker["windows"]:
            changed = True
            tracker["windows"] = retained
        if previous is None and not force:
            # First installation begins at the NEXT clock boundary, not one hour after startup.
            self.store.state["reports"][key] = slot
            self.store.save()
            return False
        sent = False
        if force or slot > previous:
            status = self.window_status(slot)
            self.queue.enqueue("normal", "report/%s/%s%s" % (key, slot, "/manual/%s" % now if force else ""),
                               self.report_text(slot, now), now)
            tracker["windows"][str(slot)] = status
            self.store.state["reports"][key] = slot
            sent = True
        # Only usage collection can change a window. Avoid scanning historical
        # ledgers on the daemon's five-second scheduling ticks.
        observation = [[account["_id"], self.store.state["accounts"].get(account["_id"], {}).get("last_attempt"),
                        self.store.state["accounts"].get(account["_id"], {}).get("last_success")]
                       for account in cfg["accounts"]]
        if tracker.get("observation") != observation:
            changed = True
            for end, old in sorted(tracker["windows"].items()):
                status = self.window_status(int(end))
                if status["complete"] and (not old["complete"] or status["signature"] != old["signature"]):
                    kind = "revision" if old["complete"] else "completion"
                    # A later version supersedes any unsent text for this
                    # window, including an earlier revision with the same hash.
                    self.discard_queued_reports(key, end)
                    self.queue.enqueue("normal", "report-update/%s/%s/%s/%s" % (
                        key, end, kind, status["signature"]), self.report_text(int(end), now, kind=kind), now)
                    tracker["windows"][end] = status
                    sent = True
            tracker["observation"] = observation
        if sent or changed:
            self.store.save()
        return sent
