"""Persist package identities and dated funding without rebasing settled Left."""

from copy import deepcopy
from decimal import Decimal


def _baseline_allocations(known, previous, anchor):
    candidates = {}
    for identifier, item in known.items():
        old = previous.get(identifier, {})
        bought = item.get("bought")
        if bought is None and identifier not in previous:
            continue
        if bought is not None and bought > anchor["time"]:
            continue
        if item["expires"] is not None and item["expires"] <= anchor["time"]:
            continue
        if item["status"] in (0, 1) or old.get("status") in (0, 1):
            candidates[identifier] = item
    if len(candidates) == 1:
        identifier, item = next(iter(candidates.items()))
        if Decimal(item["total"]) == anchor["total_bytes"]:
            return [{"id": identifier, "capacity": str(anchor["remaining_bytes"]),
                     "total": str(anchor["total_bytes"]), "expires": item["expires"]}], candidates
    if candidates and (sum(Decimal(i["total"]) for i in candidates.values()) == anchor["total_bytes"]
                       and sum(Decimal(i["left"]) for i in candidates.values()) == anchor["remaining_bytes"]):
        return [{"id": identifier, "capacity": item["left"], "total": item["total"],
                 "expires": item["expires"]} for identifier, item in candidates.items()], candidates
    # An aggregate manual anchor is not sufficient to invent per-package
    # historical balances. Keep it opaque until an actual ambiguity matters.
    return None, candidates


def sync_packages(record, account, snapshot, now):
    """Atomically classify a complete snapshot and retain every registered ID.

    Post-anchor purchases add their original Total once. Current Left is only
    informational, so delayed settlement never makes a second deduction.
    """
    anchor = account["_anchor"]
    if anchor is None:
        return []
    current = snapshot["known"]
    previous_snapshot = record.get("official", {})
    legacy = previous_snapshot.get("known", {})
    registry = deepcopy(record.get("funding"))
    initial = registry is None
    if initial:
        baseline_lots, baseline_known = _baseline_allocations(current, legacy, anchor)
        registry = {"version": 1, "credits": {}, "known": {}, "baseline_lots": baseline_lots,
                    "baseline_ids": sorted(baseline_known), "sticky_issues": {}, "issues": [],
                    "initialized_at": now}
        # A previous broad inventory_changed flag is replaced only by evidence
        # from the current identities, not silently discarded.
        if record.get("inventory_changed"):
            for identifier, old in legacy.items():
                item = current.get(identifier)
                if old.get("status") in (0, 1) and Decimal(old.get("left", "0")) > 0:
                    if item is None or item["total"] != old["total"] or item["expires"] != old["expires"]:
                        registry["sticky_issues"][identifier] = "升级前套餐变更无法完整还原，请核对一次历史基准"
                    elif item["status"] in (3, 5):
                        registry["sticky_issues"][identifier] = "升级前冻结/退款的生效时间不明，请核对一次历史基准"
    issues = list(registry.get("sticky_issues", {}).values())
    known = registry["known"]
    credits = registry["credits"]
    baseline = {item["id"]: item for item in registry["baseline_lots"] or []}
    baseline_ids = set(registry["baseline_ids"])
    for identifier, item in current.items():
        old = known.get(identifier)
        bought = item.get("bought")
        if identifier in baseline_ids and bought is not None and (bought > anchor["time"] or bought > now):
            issues.append("初始基准套餐 %s 的购买时间晚于基准，额度归属需要核对" % identifier)
            continue
        if old:
            identity_changed = (old["total"] != item["total"] or old["expires"] != item["expires"]
                                or (old.get("bought") is not None and old["bought"] != bought))
            if identity_changed:
                issues.append("套餐 %s 的购买时间、总量或到期时间发生不明确变更" % identifier)
                continue
        if identifier in credits:
            credit = credits[identifier]
            if bought is None or bought != credit["buy_time"]:
                issues.append("新购包 %s 的购买时间缺失或与登记不一致" % identifier)
                continue
        elif identifier not in baseline_ids:
            if bought is None:
                if item["status"] in (0, 1):
                    issues.append("套餐 %s 缺少购买时间，不能判断是否已包含在基准中" % identifier)
                continue
            if bought > now:
                issues.append("套餐 %s 的购买时间在未来，请核对腾讯云返回时间" % identifier)
                continue
            if bought > anchor["time"]:
                if item["expires"] is None or item["expires"] <= bought:
                    issues.append("新购包 %s 缺少有效的到期时间" % identifier)
                    continue
                if item["status"] not in (0, 1, 2, 4):
                    issues.append("首次发现的新购包 %s 已冻结或退款，缺少生效变更时间" % identifier)
                    continue
                credit = {"id": identifier, "buy_time": bought, "expires": item["expires"],
                          "capacity": item["total"], "first_seen": now, "announced": False}
                credits[identifier] = credit
            elif not initial and item["status"] in (0, 1):
                issues.append("后来出现的历史套餐 %s 可能已计入初始基准，不能重复入账" % identifier)
                continue
        if identifier in credits or identifier in baseline:
            lot = credits.get(identifier) or baseline[identifier]
            if item["status"] in (3, 5):
                if old and old["status"] in (0, 1, 4):
                    # Earliest possible event boundary is conservative while
                    # the API exposes no actual refund/freeze timestamp.
                    lot.setdefault("retired_at", old["seen_at"] // 300 * 300)
                    lot["retired_reason"] = "冻结" if item["status"] == 3 else "退款"
                elif lot.get("retired_at") is None:
                    issues.append("套餐 %s 的冻结/退款时间不明" % identifier)
            elif lot.get("retired_at") is not None and item["status"] in (0, 1):
                issues.append("套餐 %s 在失效后重新启用，需要核对恢复额度" % identifier)
        elif identifier in baseline_ids:
            if ((item["expires"] is not None and item["expires"] <= now) or item["status"] in (2, 3, 5)):
                issues.append("初始合并基准中的套餐 %s 已失效，历史额度分配不明确" % identifier)
        if item["status"] == 2 and item["expires"] is not None and item["expires"] > now:
            issues.append("套餐 %s 提前显示过期，与到期时间不一致" % identifier)
        known[identifier] = dict(item, seen_at=now)
    for identifier, old in known.items():
        if identifier in current:
            continue
        if identifier in credits or identifier in baseline_ids:
            lot = credits.get(identifier) or baseline.get(identifier)
            if lot is not None and lot.get("retired_at") is not None and lot["retired_at"] <= now:
                continue
            # Keep its identity and original credit across temporary omissions.
            # A reappearance is not a second purchase.
            expiry = old.get("expires")
            if expiry is None or expiry > now:
                issues.append("已登记套餐 %s 本轮未返回，暂时无法确认有效性" % identifier)
            elif identifier in baseline_ids and registry["baseline_lots"] is None:
                issues.append("初始合并基准中的套餐 %s 已到期，历史额度分配不明确" % identifier)
    registry["issues"] = list(dict.fromkeys(issues))
    record["funding"] = registry
    record["inventory_changed"] = bool(registry["issues"])
    record["ledger"]["funding"] = {"lots": list(credits.values()), "baseline_lots": registry["baseline_lots"]}
    return [credit for credit in credits.values() if not credit.get("announced")]
