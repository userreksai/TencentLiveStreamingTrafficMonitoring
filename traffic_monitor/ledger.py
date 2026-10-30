"""Pure, revision-safe five-minute traffic ledger.

Balances are calibrated resource-package bytes, not a claim of a real-time
Tencent billing balance. All persisted numbers are JSON compatible and monetary
or byte calculations use Decimal, never binary floating point.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from .funding import replay_funding


BUCKET_SECONDS = 300
REGIONS = (
    "China Mainland", "Asia Pacific I", "Asia Pacific II", "Asia Pacific III",
    "North America", "Europe", "Middle East", "Africa", "South America",
)
DEFAULT_RATIOS = {
    "LVB": dict(zip(REGIONS, ("1", "1.7692", "2.9231", "2.6923", "1.6923", "1.6923", "4.6154", "4.6154", "3.9615"))),
    "LEB": dict(zip(REGIONS, ("2", "3.5385", "5.8462", "5.3846", "3.3846", "3.3846", "9.2308", "9.2308", "7.9231"))),
    "PUSH": dict(zip(REGIONS, ("1", "1.7692", "2.9231", "2.6923", "1.6923", "1.6923", "4.6154", "4.6154", "3.9615"))),
}


def _decimal(value, label, minimum=Decimal(0), integral=False):
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
    return int(_decimal(value, label, Decimal(minimum), integral=True))


def normalize_region(region):
    if not isinstance(region, str):
        raise ValueError("region must be a string")
    normalized = " ".join(region.strip().split()).lower()
    aliases = {name.lower(): name for name in REGIONS}
    aliases.update({
        "mainland": "China Mainland", "china mainland": "China Mainland",
        "mainland china": "China Mainland", "chinese mainland": "China Mainland",
        "asia pacific 1": "Asia Pacific I", "asia pacific 2": "Asia Pacific II",
        "asia pacific 3": "Asia Pacific III",
    })
    if normalized not in aliases:
        raise ValueError("unknown billing region: %s" % region)
    return aliases[normalized]


def _dimension(service, region):
    if not isinstance(service, str) or service.upper() not in DEFAULT_RATIOS:
        raise ValueError("unknown traffic service: %s" % service)
    return service.upper(), normalize_region(region)


def _dimension_name(dimension):
    return "%s/%s" % dimension


def _bucket_key(timestamp, service, region):
    return "%s|%s|%s" % (timestamp, service, region)


def _validate_ledger(ledger):
    if not isinstance(ledger, dict) or ledger.get("version") != 1:
        raise ValueError("unsupported ledger state")
    anchor = _integer(ledger.get("anchor_time"), "anchor_time")
    if anchor % BUCKET_SECONDS:
        raise ValueError("anchor_time must be aligned to a five-minute interval")
    balance = _integer(ledger.get("balance_bytes"), "balance_bytes")
    total = _integer(ledger.get("total_bytes"), "total_bytes", 1)
    if balance > total:
        raise ValueError("balance_bytes cannot exceed total_bytes")
    if not isinstance(ledger.get("buckets"), dict):
        raise ValueError("ledger buckets must be an object")
    return anchor, balance, total


def new_ledger(anchor_time, balance_bytes, total_bytes):
    """Create a ledger at a manually confirmed usage-coverage boundary.

    ``anchor_time`` is the UTC START of the first interval not already included
    in ``balance_bytes``. It is not the clock time at which a stale API balance
    was read and is not automatically the package purchase timestamp.
    """
    ledger = {
        "version": 1,
        "anchor_time": _integer(anchor_time, "anchor_time"),
        "balance_bytes": str(_integer(balance_bytes, "balance_bytes")),
        "total_bytes": str(_integer(total_bytes, "total_bytes", 1)),
        "buckets": {},
    }
    _validate_ledger(ledger)
    return ledger


def upsert_buckets(ledger, buckets, now):
    """Validate a complete response batch, then replace its interval records.

    Each bucket contains ``time`` (UTC interval start), ``service``, ``region``
    and raw billable ``bytes``. A later response replaces an earlier value for
    the same key, so overlapping fetches and server revisions never add a second
    deduction. Explicit zero buckets are valid. Missing records are NOT zero.
    """
    anchor, _, _ = _validate_ledger(ledger)
    now = _integer(now, "now")
    if anchor > now:
        raise ValueError("ledger anchor is in the future")
    if not isinstance(buckets, (list, tuple)):
        raise ValueError("buckets must be a list")
    staged = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("bucket must be an object")
        timestamp = _integer(bucket.get("time"), "bucket time")
        if timestamp % BUCKET_SECONDS:
            raise ValueError("bucket time must be aligned to five minutes")
        service, region = _dimension(bucket.get("service"), bucket.get("region"))
        byte_count = _integer(bucket.get("bytes"), "bucket bytes")
        if timestamp < anchor:
            continue
        if timestamp + BUCKET_SECONDS > now:
            raise ValueError("cannot ingest a future or incomplete interval")
        staged[_bucket_key(timestamp, service, region)] = {
            "time": timestamp, "service": service, "region": region,
            "bytes": str(byte_count),
        }
    ledger["buckets"].update(staged)
    ledger["updated_at"] = now


def _ratio_table(overrides):
    result = {service: {region: Decimal(value) for region, value in regions.items()}
              for service, regions in DEFAULT_RATIOS.items()}
    if overrides is None:
        return result
    if not isinstance(overrides, dict):
        raise ValueError("ratio overrides must be an object")
    for service, regions in overrides.items():
        if not isinstance(service, str) or service.upper() not in result or not isinstance(regions, dict):
            raise ValueError("invalid ratio override service")
        for region, value in regions.items():
            ratio = _decimal(value, "deduction ratio")
            if ratio == 0:
                raise ValueError("deduction ratio must be positive")
            result[service.upper()][normalize_region(region)] = ratio
    return result


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _text(value):
    return format(value, "f")


def calculate(ledger, now, safety_seconds=900, safety_factor="1.1", ratios=None,
              expected_dimensions=None):
    """Return conservative estimates plus freshness and coverage evidence.

    ``expected_dimensions`` should be the collector's configured combinations,
    e.g. ``[{"service": "LVB", "region": "Asia Pacific I"}]``. When omitted,
    coverage is UNKNOWN, not assumed healthy. With dimensions, latest_data_time
    is the end of the latest interval available in *every* expected dimension.
    Missing intervals include the unreported tail; internal_missing_intervals
    counts only holes before this common coverage boundary. A caller may allow
    a short delayed tail but must not silently allow internal holes.

    The reserve uses the largest five-minute deduction rate in the latest
    observed hour, multiplied by (handling reserve + unobserved data lag) and a
    configured safety factor. This is a forecast, not an official balance or a
    guarantee against arbitrarily large future traffic spikes.
    """
    anchor, balance, total = _validate_ledger(ledger)
    now = _integer(now, "now")
    if now < anchor:
        raise ValueError("ledger anchor is in the future")
    safety_seconds = _integer(safety_seconds, "safety_seconds")
    factor = _decimal(safety_factor, "safety_factor", Decimal(1))
    ratio_table = _ratio_table(ratios)
    interval_totals = {}
    dimension_times = {}
    consumed = Decimal(0)
    for key, bucket in ledger["buckets"].items():
        if not isinstance(bucket, dict):
            raise ValueError("invalid stored bucket")
        timestamp = _integer(bucket.get("time"), "stored bucket time")
        service, region = _dimension(bucket.get("service"), bucket.get("region"))
        if timestamp % BUCKET_SECONDS or key != _bucket_key(timestamp, service, region):
            raise ValueError("invalid stored bucket key")
        if timestamp < anchor or timestamp + BUCKET_SECONDS > now:
            raise ValueError("stored bucket is outside the valid ledger window")
        amount = _decimal(bucket.get("bytes"), "stored bucket bytes", integral=True)
        deduction = amount * ratio_table[service][region]
        consumed += deduction
        interval_totals[timestamp] = interval_totals.get(timestamp, Decimal(0)) + deduction
        dimension_times.setdefault((service, region), set()).add(timestamp)

    latest_observed = max((timestamp + BUCKET_SECONDS for timestamp in interval_totals), default=None)
    latest_data = latest_observed
    coverage_complete = None
    missing_intervals = None
    internal_missing_intervals = None
    missing_dimensions = []
    expected_until = now - now % BUCKET_SECONDS
    if expected_dimensions is not None:
        if not isinstance(expected_dimensions, (list, tuple)) or not expected_dimensions:
            raise ValueError("expected_dimensions must be a non-empty list")
        dimensions = set()
        for item in expected_dimensions:
            if not isinstance(item, dict):
                raise ValueError("expected dimension must be an object")
            dimensions.add(_dimension(item.get("service"), item.get("region")))
        expected_count_all = (expected_until - anchor) // BUCKET_SECONDS
        missing_intervals = sum(expected_count_all - sum(timestamp < expected_until for timestamp in dimension_times.get(d, ()))
                                for d in dimensions)
        missing_dimensions = sorted(_dimension_name(d) for d in dimensions if not dimension_times.get(d))
        if missing_dimensions:
            latest_data = None
            coverage_complete = False
        else:
            latest_data = min(max(dimension_times[d]) + BUCKET_SECONDS for d in dimensions)
            expected_count = (latest_data - anchor) // BUCKET_SECONDS
            internal_missing_intervals = sum(expected_count - sum(timestamp < latest_data for timestamp in dimension_times[d])
                                             for d in dimensions)
            coverage_complete = missing_intervals == 0

    # Keep using the latest known burn rate even when upstream has become stale;
    # using a window relative to wall time would incorrectly turn old usage into
    # zero consumption and remove the safety reserve during an outage.
    window_start = max(anchor, (latest_observed or anchor) - 3600)
    recent = [value for timestamp, value in interval_totals.items() if timestamp >= window_start]
    burn_rate = max(recent, default=Decimal(0)) / BUCKET_SECONDS
    observed_duration = max(BUCKET_SECONDS, (latest_observed or anchor) - window_start)
    average_rate = sum(recent, Decimal(0)) / observed_duration
    freshness_time = latest_data if latest_data is not None else latest_observed
    lag = max(0, now - freshness_time) if freshness_time is not None else None
    reserve = _ceil(burn_rate * (safety_seconds + (lag or 0)) * factor)
    used = _ceil(consumed)
    remaining = max(0, balance - used)
    safe_remaining = max(0, remaining - reserve)
    runway = (int((Decimal(remaining) / burn_rate).to_integral_value(rounding=ROUND_FLOOR))
              if burn_rate else (0 if remaining == 0 else None))
    safe_runway = (int((Decimal(safe_remaining) / burn_rate).to_integral_value(rounding=ROUND_FLOOR))
                   if burn_rate else (0 if safe_remaining == 0 else None))
    result = {
        "consumed_bytes": str(used),
        "estimated_remaining_bytes": str(remaining),
        "safe_remaining_bytes": str(safe_remaining),
        "reserve_bytes": str(reserve),
        "overrun_bytes": str(max(0, used - balance)),
        "remaining_percent": _text(Decimal(remaining) * 100 / total),
        "safe_percent": _text(Decimal(safe_remaining) * 100 / total),
        "latest_data_time": latest_data,
        "latest_observed_time": latest_observed,
        "data_lag_seconds": lag,
        "burn_rate_bytes_per_second": _text(burn_rate),
        "average_rate_bytes_per_second": _text(average_rate),
        "runway_seconds": runway,
        "safe_runway_seconds": safe_runway,
        "coverage_complete": coverage_complete,
        "missing_intervals": missing_intervals,
        "internal_missing_intervals": internal_missing_intervals,
        "coverage_until": latest_data,
        "expected_until": expected_until,
        "missing_dimensions": missing_dimensions,
        "bucket_count": len(ledger["buckets"]),
        "anchor_time": anchor,
    }
    funding = ledger.get("funding")
    if funding is not None:
        replay = replay_funding(anchor, balance, total,
                                [{"time": timestamp, "bytes": _text(amount)}
                                 for timestamp, amount in interval_totals.items()],
                                funding["lots"], now, baseline_lots=funding.get("baseline_lots"))
        remaining = int(replay["remaining_bytes"])
        capacity = int(replay["active_capacity_bytes"])
        uncovered = int(replay["uncovered_usage_bytes"])
        # Public package metadata cannot prove that a later purchase paid an
        # earlier uncovered interval. Hold that amount conservatively instead
        # of silently marking either quota or historical charges as settled.
        uncovered_hold = min(remaining, uncovered)
        safe_remaining = max(0, remaining - reserve - uncovered_hold)
        denominator = capacity or 1
        result.update(
            estimated_remaining_bytes=str(remaining), safe_remaining_bytes=str(safe_remaining),
            remaining_percent=_text(Decimal(remaining) * 100 / denominator),
            safe_percent=_text(Decimal(safe_remaining) * 100 / denominator),
            reserve_bytes=str(reserve + uncovered_hold), timing_reserve_bytes=str(reserve),
            uncovered_reserve_bytes=str(uncovered_hold), overrun_bytes=str(uncovered),
            active_capacity_bytes=str(capacity), gross_capacity_bytes=replay["gross_capacity_bytes"],
            expired_bytes=replay["expired_bytes"], retired_bytes=replay["retired_bytes"],
            baseline_remaining_bytes=replay["baseline_remaining_bytes"],
            funding_lots=replay["lots"], opaque_baseline=replay["opaque_baseline"],
            runway_seconds=(int((Decimal(remaining) / burn_rate).to_integral_value(rounding=ROUND_FLOOR))
                            if burn_rate else (0 if remaining == 0 else None)),
            safe_runway_seconds=(int((Decimal(safe_remaining) / burn_rate).to_integral_value(rounding=ROUND_FLOOR))
                                 if burn_rate else (0 if safe_remaining == 0 else None)),
        )
    return result
