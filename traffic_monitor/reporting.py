"""Comparable raw usage and package deductions for explicit five-minute windows."""

from decimal import Decimal
import hashlib
import json

from .ledger import DEFAULT_RATIOS


def window_usage(ledger, start, end, account):
    expected = {(service, region) for service in account["services"] for region in account["regions"]}
    times = range(start, end, 300)
    rows = {}
    for item in ledger.get("buckets", {}).values():
        dimension = (item["service"], item["region"])
        timestamp = item["time"]
        if start <= timestamp < end and (timestamp - start) % 300 == 0 and dimension in expected:
            rows[(timestamp, *dimension)] = Decimal(str(item["bytes"]))
    complete = 0
    partial = 0
    cutoff = start
    raw = Decimal(0)
    push = Decimal(0)
    folded = Decimal(0)
    complete_folded = Decimal(0)
    details = {}
    for timestamp in times:
        present = {(service, region) for t, service, region in rows if t == timestamp}
        full = expected <= present
        complete += int(full)
        partial += int(bool(present) and not full)
        if full and cutoff == timestamp:
            cutoff += 300
        for service, region in present:
            amount = rows[(timestamp, service, region)]
            deduction = amount * Decimal(DEFAULT_RATIOS[service][region])
            if service == "PUSH":
                push += amount
            else:
                raw += amount
            folded += deduction
            if full:
                complete_folded += deduction
            details[(service, region)] = details.get((service, region), Decimal(0)) + amount
    canonical = [(timestamp, service, region, format(amount, "f"))
                 for (timestamp, service, region), amount in sorted(rows.items())]
    signature = hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()
    return {"raw": raw, "push": push, "folded": folded, "complete_folded": complete_folded,
            "complete": complete, "partial": partial, "expected": len(times), "cutoff": cutoff,
            "details": details, "signature": signature}


def report_scope(accounts):
    scope = sorted((account["_id"], account["_anchor_id"],
                    sorted(account["services"]), sorted(account["regions"])) for account in accounts)
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
