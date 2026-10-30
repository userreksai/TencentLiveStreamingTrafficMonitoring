"""Collection, calibrated accounting, business alerts and clock-aligned reports."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging

from .config import format_time, redact
from .ledger import DEFAULT_RATIOS, calculate, new_ledger, upsert_buckets
from .notifications import NotificationQueue
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
        if not total.is_finite() or not left.is_finite() or total <= 0 or left < 0 or left > total:
            raise ValueError("腾讯云返回了无效的套餐额度")
        expires = _expiry(item.get("ExpireTime"))
        status = int(item["Status"])
        known[package_id] = {"total": str(total), "expires": expires, "status": status, "left": str(left)}
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
    amount = Decimal(0)
    timestamps = {}
    for item in ledger["buckets"].values():
        if start <= item["time"] < end:
            amount += Decimal(item["bytes"]) * Decimal(DEFAULT_RATIOS[item["service"]][item["region"]])
            timestamps.setdefault(item["time"], set()).add((item["service"], item["region"]))
    expected = {(d["service"], d["region"]) for d in dimensions(account)}
    complete = sum(expected <= found for found in timestamps.values())
    return amount, complete


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
        self.config = config
        self.queue.config = config
        if hasattr(self.queue.sender, "config"):
            self.queue.sender.config = config

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
            name = account["name"]
            client = self.client_factory(account["secret_id"], account["secret_key"],
                                         token=account["session_token"], endpoint=account["endpoint"],
                                         timeout=self.config["sampling"]["request_timeout_seconds"])
            try:
                snapshot = package_snapshot(fetch_packages(client), account, now)
                previous = record.get("official", {}).get("known", {})
                if previous:
                    for package_id, old in previous.items():
                        if old["status"] in (0, 1) and Decimal(old["left"]) > 0:
                            current = snapshot["known"].get(package_id)
                            if (current is None or (old["expires"] is not None and old["expires"] <= now)
                                    or current["expires"] != old["expires"]):
                                record["inventory_changed"] = True
                    for package_id, current in snapshot["known"].items():
                        old = previous.get(package_id)
                        if old is None or old["total"] != current["total"]:
                            record["inventory_changed"] = True
                        if (old and old["status"] in (0, 1) and Decimal(old["left"]) > 0
                                and current["status"] in (2, 3, 5)):
                            record["inventory_changed"] = True
                record["official"] = snapshot
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
                        if Decimal(official["remaining_bytes"]) + tolerance < Decimal(result["estimated_remaining_bytes"]):
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
                faults.append("套餐发生新增、额度改变、过期或冻结，需要重新校准")
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

    def report_text(self, slot, now):
        cfg = self.config
        period = cfg["reports"]["interval_minutes"] * 60
        start = slot - period
        offset = cfg["_timezone_offset"]
        lines = ["【腾讯云直播流量整点汇总】",
                 "统计窗口：%s ～ %s" % (format_time(start, offset), format_time(slot, offset)),
                 "生成时间：%s" % format_time(now, offset)]
        for account in cfg["accounts"]:
            record = self.store.state["accounts"].get(account["_id"], {})
            lines.extend(["", "账号：%s" % account["name"],
                          "统计范围：%s；%s" % (", ".join(account["services"]), ", ".join(account["regions"]))])
            if record.get("anchor_id") != account["_anchor_id"]:
                lines.append("状态：配置基准已更改，等待本轮采集完成，当前不提供可靠余量判断。")
                continue
            official = record.get("official")
            if official:
                lines.append("官方结算余量：%s（可能滞后，仅供对照）" % size(official["remaining_bytes"]))
                for package in official["packages"]:
                    lines.append("  %s（ID %s）：%s，%s" % (package["name"], package["id"],
                                 size(package["left_bytes"]), "使用中" if package["status"] == 1 else "备用未使用"))
            if not record.get("ledger"):
                lines.append("状态：未校准，无法可靠计算剩余量；请配置 anchor，异常信息已进入异常通知通道。")
                usage = record.get("usage", {"buckets": {}})
                amount, count = hourly_usage(usage, start, slot, account)
                lines.append("窗口内已到达的折算用量：%s；完整五分钟段：%s/%s" % (size(amount), count, period // 300))
                latest = max((item["time"] + 300 for item in usage["buckets"].values()), default=None)
                if latest is not None:
                    lines.append("最近取得用量截至：%s" % format_time(latest, offset))
                continue
            result = calculate(record["ledger"], now,
                               safety_seconds=cfg["alerts"]["safety_seconds"], safety_factor=cfg["alerts"]["safety_factor"],
                               expected_dimensions=dimensions(account))
            issues = list(record.get("issues", [])) + self.quality_faults(result)
            if not record.get("quality") or issues:
                lines.append("状态：数据异常，当前剩余量不能作为可靠判断依据（详见异常通道）。")
                lines.append("问题：%s" % "；".join(dict.fromkeys(issues)))
            else:
                lines.append("状态：已校准，用量数据在允许延迟内")
                lines.append("估算剩余：%s（%.2f%%）" % (size(result["estimated_remaining_bytes"]), Decimal(result["remaining_percent"])))
                lines.append("安全可用：%s（%.2f%%）；预留：%s" % (
                    size(result["safe_remaining_bytes"]), Decimal(result["safe_percent"]), size(result["reserve_bytes"])))
                runway = result["safe_runway_seconds"]
                lines.append("预计安全可用时长：%s" % ("暂无消耗速率，无法预测" if runway is None else "%s 分钟" % (runway // 60)))
            amount, count = hourly_usage(record["ledger"], start, slot, account)
            lines.append("窗口内已到达的折算扣量：%s；完整五分钟段：%s/%s" % (size(amount), count, period // 300))
            if start < record["ledger"]["anchor_time"]:
                lines.append("本窗口早于校准基准的部分不在台账中，统计不完整。")
            if result["latest_data_time"] is not None:
                lines.append("用量截至：%s；落后：%s 秒" % (format_time(result["latest_data_time"], offset), result["data_lag_seconds"]))
        lines.extend(["", "说明：未到达的用量段不计为零；数据可能回补修订。预留和可用时长属于保守预测。",
                      "请在流量不足时提前购包；本报告不承诺已发生用量一定可追溯抵扣。"])
        return "\n".join(lines)

    def reports(self, now, force=False):
        cfg = self.config
        slot = slot_for(now, cfg["reports"]["interval_minutes"], cfg["_timezone_offset"])
        key = "%s/%s" % (cfg["reports"]["timezone"], cfg["reports"]["interval_minutes"])
        previous = self.store.state["reports"].get(key)
        if previous is None and not force:
            # First installation begins at the NEXT clock boundary, not one hour after startup.
            self.store.state["reports"][key] = slot
            self.store.save()
            return False
        if not force and slot <= previous:
            return False
        self.queue.enqueue("normal", "report/%s/%s%s" % (key, slot, "/manual/%s" % now if force else ""),
                           self.report_text(slot, now), now)
        self.store.state["reports"][key] = slot
        self.store.save()
        return True
