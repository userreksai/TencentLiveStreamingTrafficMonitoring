"""Replay dated package funding against revision-safe, weighted usage.

The caller classifies package inventory and persists the funding registry.  This
module is pure: it does not read cloud balances, mutate the registry, or erase
usage that was unfunded before a later purchase.  Byte arithmetic uses Decimal.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


BUCKET_SECONDS = 300


def _decimal(value, label, integral=False, minimum=Decimal(0)):
    if isinstance(value, (bool, float)):
        raise ValueError("%s must be an integer or decimal string" % label)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("%s is not a valid decimal" % label) from None
    if not result.is_finite() or result < minimum:
        raise ValueError("%s is outside the valid range" % label)
    if integral and result != result.to_integral_value():
        raise ValueError("%s must be an integer" % label)
    return result


def _integer(value, label, minimum=0):
    return int(_decimal(value, label, integral=True, minimum=Decimal(minimum)))


def _text(value):
    return format(value, "f")


def _floor(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _cutoff(expires, retired_at):
    """An expiry inside a bucket cannot safely fund that entire bucket."""
    candidates = []
    if expires is not None:
        candidates.append((expires // BUCKET_SECONDS * BUCKET_SECONDS, "expired"))
    if retired_at is not None:
        candidates.append((retired_at // BUCKET_SECONDS * BUCKET_SECONDS, "retired"))
    return min(candidates, default=(None, None), key=lambda item: (item[0], item[1]))


def _make_lot(key, identifier, capacity, total, start, expires, retired_at, baseline):
    cutoff, reason = _cutoff(expires, retired_at)
    return {"key": key, "id": identifier, "capacity": capacity, "total": total,
            "start": start, "expires": expires, "retired_at": retired_at,
            "cutoff": cutoff, "reason": reason, "baseline": baseline,
            "remaining": Decimal(0), "consumed": Decimal(0),
            "expired": Decimal(0), "retired": Decimal(0), "credited": False}


def replay_funding(anchor_time, anchor_balance, anchor_total, usage, lots, now,
                   baseline_lots=None):
    """Replay package credits and usage chronologically, without side effects.

    ``usage`` has one aggregate weighted record per closed five-minute bucket:
    ``{"time": utc_bucket_start, "bytes": decimal_string_or_int}``. Identical
    duplicates coalesce; conflicting duplicates are invalid (the caller should
    replace source revisions before replay).

    ``lots`` contains new purchases only, not packages already included in the
    anchor. Each item is ``{id,buy_time,expires,capacity,retired_at?}``. Capacity
    is the original package Total, NOT the cloud's already-deducted Left.
    Duplicate ID/BuyTime entries coalesce only if their metadata agree.

    Optional ``baseline_lots`` has ``{id,capacity,total,expires,retired_at?}``.
    Capacity represents each package's remaining allocation at the anchor;
    total is its nominal denominator. Their sums must match the anchor. All
    dated lots are spent in expiry order. With no allocation information, the
    opaque anchor pool is spent first; its expiry remains unknown to this
    module and must be treated accordingly by the inventory layer.

    Purchases begin at the NEXT bucket boundary if they occur inside a bucket.
    Expiry/refund/freeze takes effect at the PREVIOUS boundary, conservatively
    reserving the partial bucket. A later purchase never covers earlier
    unfunded usage in this model; such usage is returned separately.
    """
    anchor_time = _integer(anchor_time, "anchor_time")
    now = _integer(now, "now")
    if anchor_time % BUCKET_SECONDS or now < anchor_time:
        raise ValueError("anchor must be an aligned boundary not after now")
    anchor_balance = _decimal(anchor_balance, "anchor_balance", integral=True)
    anchor_total = _decimal(anchor_total, "anchor_total", integral=True, minimum=Decimal(1))
    if anchor_balance > anchor_total:
        raise ValueError("anchor balance cannot exceed total")
    if not isinstance(usage, (list, tuple)) or not isinstance(lots, (list, tuple)):
        raise ValueError("usage and lots must be lists")

    normalized = []
    baseline_ids = set()
    if baseline_lots is None:
        normalized.append(_make_lot("anchor", "anchor", anchor_balance, anchor_total,
                                    anchor_time, None, None, True))
        opaque_baseline = True
    else:
        if not isinstance(baseline_lots, (list, tuple)) or not baseline_lots:
            raise ValueError("baseline_lots must be a non-empty list")
        for item in baseline_lots:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("baseline lot has no id")
            if item["id"] in baseline_ids:
                raise ValueError("duplicate baseline lot id")
            baseline_ids.add(item["id"])
            capacity = _decimal(item.get("capacity"), "baseline capacity", integral=True)
            total = _decimal(item.get("total", capacity), "baseline total", integral=True)
            if capacity > total:
                raise ValueError("baseline capacity exceeds nominal total")
            expires = None if item.get("expires") is None else _integer(item["expires"], "baseline expires")
            retired = None if item.get("retired_at") is None else _integer(item["retired_at"], "baseline retired_at")
            normalized.append(_make_lot("baseline/" + item["id"], item["id"], capacity,
                                        total, anchor_time, expires, retired, True))
        if (sum((item["capacity"] for item in normalized), Decimal(0)) != anchor_balance
                or sum((item["total"] for item in normalized), Decimal(0)) != anchor_total):
            raise ValueError("baseline lot allocations do not match the anchor")
        opaque_baseline = False

    purchases = {}
    for item in lots:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("purchase lot has no id")
        if item["id"] in baseline_ids:
            raise ValueError("purchase package is already included in the baseline")
        buy_time = _integer(item.get("buy_time"), "buy_time")
        if buy_time < anchor_time:
            raise ValueError("purchase predates the anchor and may already be included")
        capacity = _decimal(item.get("capacity"), "purchase capacity", integral=True, minimum=Decimal(1))
        expires = None if item.get("expires") is None else _integer(item["expires"], "expires")
        if expires is not None and expires <= buy_time:
            raise ValueError("purchase expiration must be after purchase")
        retired = None if item.get("retired_at") is None else _integer(item["retired_at"], "retired_at")
        start = (buy_time + BUCKET_SECONDS - 1) // BUCKET_SECONDS * BUCKET_SECONDS
        key = "purchase/%s/%s" % (item["id"], buy_time)
        fingerprint = (capacity, expires, retired)
        if key in purchases:
            if purchases[key] != fingerprint:
                raise ValueError("conflicting duplicate package purchase")
            continue
        purchases[key] = fingerprint
        lot = _make_lot(key, item["id"], capacity, capacity, start, expires, retired, False)
        lot["buy_time"] = buy_time
        normalized.append(lot)

    totals = {}
    for item in usage:
        if not isinstance(item, dict):
            raise ValueError("usage record must be an object")
        timestamp = _integer(item.get("time"), "usage time")
        amount = _decimal(item.get("bytes"), "usage bytes")
        if timestamp % BUCKET_SECONDS:
            raise ValueError("usage time must be aligned to five minutes")
        if timestamp < anchor_time:
            continue
        if timestamp + BUCKET_SECONDS > now:
            raise ValueError("usage interval is not closed")
        if timestamp in totals and totals[timestamp] != amount:
            raise ValueError("conflicting duplicate usage interval")
        totals[timestamp] = amount

    events = []
    for index, lot in enumerate(normalized):
        # A package bought and retired inside the same bucket has no full
        # covered interval; it must not create phantom available capacity.
        if lot["cutoff"] is None or lot["start"] < lot["cutoff"]:
            events.append((lot["start"], 1, index))
        elif lot["baseline"]:
            # An expired baseline allocation is immediately removed rather
            # than silently remaining in the opaque starting balance.
            events.append((anchor_time, 1, index))
        if lot["cutoff"] is not None:
            events.append((max(anchor_time, lot["cutoff"]), 2, index))
    events.extend((timestamp, 3, amount) for timestamp, amount in totals.items())
    events.sort(key=lambda event: (event[0], event[1]))

    uncovered = Decimal(0)
    uncovered_intervals = []
    consumed = Decimal(0)
    for timestamp, kind, payload in events:
        if timestamp > now:
            break
        if kind == 1:
            lot = normalized[payload]
            lot["remaining"] = lot["capacity"]
            lot["credited"] = True
        elif kind == 2:
            lot = normalized[payload]
            lot[lot["reason"]] += lot["remaining"]
            lot["remaining"] = Decimal(0)
        else:
            amount = payload
            consumed += amount
            eligible = [lot for lot in normalized if lot["credited"] and lot["remaining"] > 0
                        and lot["start"] <= timestamp
                        and (lot["cutoff"] is None or timestamp < lot["cutoff"])]

            def priority(lot):
                if opaque_baseline and lot["baseline"]:
                    return (0, 0, lot["start"], lot["key"])
                return (1, lot["cutoff"] if lot["cutoff"] is not None else float("inf"),
                        lot["start"], lot["key"])

            for lot in sorted(eligible, key=priority):
                deducted = min(amount, lot["remaining"])
                lot["remaining"] -= deducted
                lot["consumed"] += deducted
                amount -= deducted
                if amount == 0:
                    break
            if amount:
                uncovered += amount
                uncovered_intervals.append({"time": timestamp, "bytes": _text(amount)})

    remaining = sum((lot["remaining"] for lot in normalized), Decimal(0))
    baseline_remaining = sum((lot["remaining"] for lot in normalized if lot["baseline"]), Decimal(0))
    active = [lot for lot in normalized if lot["credited"] and lot["remaining"] > 0
              and (lot["cutoff"] is None or now < lot["cutoff"])]
    active_capacity = sum((lot["total"] for lot in active), Decimal(0))
    gross_capacity = sum((lot["total"] for lot in normalized if lot["credited"]), Decimal(0))
    per_lot = []
    for lot in normalized:
        per_lot.append({
            "id": lot["id"], "key": lot["key"], "baseline": lot["baseline"],
            "capacity_bytes": _text(lot["capacity"]), "nominal_total_bytes": _text(lot["total"]),
            "effective_time": lot["start"], "expires": lot["expires"], "retired_at": lot["retired_at"],
            "cutoff_time": lot["cutoff"], "credited": lot["credited"],
            "remaining_bytes": _text(lot["remaining"]), "consumed_bytes": _text(lot["consumed"]),
            "expired_bytes": _text(lot["expired"]), "retired_bytes": _text(lot["retired"]),
            "active": lot in active,
        })
    return {
        "remaining_bytes": str(_floor(remaining)),
        "active_capacity_bytes": str(_floor(active_capacity)),
        "gross_capacity_bytes": str(_floor(gross_capacity)),
        "consumed_bytes": str(_ceil(consumed)),
        "uncovered_usage_bytes": str(_ceil(uncovered)),
        "expired_bytes": str(_ceil(sum((lot["expired"] for lot in normalized), Decimal(0)))),
        "retired_bytes": str(_ceil(sum((lot["retired"] for lot in normalized), Decimal(0)))),
        "baseline_remaining_bytes": str(_floor(baseline_remaining)),
        "added_remaining_bytes": str(_floor(remaining - baseline_remaining)),
        "lots": per_lot,
        "uncovered_intervals": uncovered_intervals,
        "opaque_baseline": opaque_baseline,
    }
