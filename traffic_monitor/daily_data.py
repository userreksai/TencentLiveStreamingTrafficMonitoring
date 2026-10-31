"""Independent official daily usage and package reports; never update a ledger."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from .tencent import BEIJING, TencentError, _bytes, fetch_packages


PACKAGE_STATES = {
    0: "⚪ 未使用", 1: "🟢 使用中", 2: "⚫ 已过期",
    3: "🟠 已冻结", 4: "🔴 已耗尽", 5: "⚫ 已退款",
}


def _timestamp(value, allow_date=False):
    if (not isinstance(value, str) or not value.strip()
            or (not allow_date and " " not in value and "T" not in value)):
        raise TencentError("InvalidResponse", "官方日报返回了无效的时间")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING)
    except (ValueError, OverflowError, OSError):
        raise TencentError("InvalidResponse", "官方日报返回了无效的时间") from None


def _daily_usage(client, now):
    if type(now) is not int or now < 0:
        raise TencentError("ConfigurationError", "日报采集时间必须为有效的时间戳")
    try:
        end = datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
    except (ValueError, OverflowError, OSError):
        raise TencentError("ConfigurationError", "日报采集时间超出有效范围") from None
    # Naive API times explicitly mean Beijing time. Query through today's
    # midnight so yesterday is complete; an inclusive endpoint record for
    # today is validated and excluded below rather than counted as an eighth day.
    response = client.call("DescribeBillBandwidthAndFluxList", {
        "StartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
        "Granularity": 1440,
    })
    items = response.get("DataInfoList")
    if not isinstance(items, list):
        raise TencentError("InvalidResponse", "官方日报缺少日流量列表", response.get("RequestId", ""))
    days = {}
    for item in items:
        if not isinstance(item, dict):
            raise TencentError("InvalidResponse", "官方日报的日流量记录格式无效")
        timestamp = _timestamp(item.get("Time"), allow_date=True)
        if (timestamp.hour or timestamp.minute or timestamp.second or timestamp.microsecond
                or not start <= timestamp <= end):
            raise TencentError("InvalidResponse", "官方日报返回了非日粒度或查询范围外的记录")
        amount = _bytes(item.get("Flux"))
        if timestamp == end:
            continue
        day = timestamp.strftime("%Y-%m-%d")
        if day in days and days[day] != amount:
            raise TencentError("InvalidResponse", "官方日报的同一天存在冲突流量记录")
        days[day] = amount
    expected = [(start + timedelta(days=index)).strftime("%Y-%m-%d") for index in range(7)]
    missing = [day for day in expected if day not in days]
    if missing:
        raise TencentError("IncompleteResponse", "官方日报缺少以下日期的流量数据：%s" % "、".join(missing))
    return [(day, days[day]) for day in expected]


def _integer(value, label, minimum=0):
    if value is None or isinstance(value, bool):
        raise TencentError("InvalidResponse", "官方流量包的%s无效" % label)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < minimum or amount != amount.to_integral_value():
            raise ValueError()
        return int(amount)
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        raise TencentError("InvalidResponse", "官方流量包的%s无效" % label) from None


def _package_rows(client):
    rows = []
    for item in fetch_packages(client):
        if _integer(item.get("Type", 0), "类型") != 0:
            continue
        total = _integer(item.get("Total"), "总量", minimum=1)
        used = _integer(item.get("Used"), "已用量")
        status = _integer(item.get("Status"), "状态")
        if used > total or status not in PACKAGE_STATES:
            raise TencentError("InvalidResponse", "官方流量包的已用量或状态无效")
        bought = _timestamp(item.get("BuyTime"))
        rows.append((bought, str(item["Id"]), used, total, status))
    return sorted(rows, key=lambda row: (row[0], row[1]), reverse=True)


def _tb(amount):
    return format(Decimal(amount) / Decimal(10**12), ".2f") + " TB"


def fetch_daily_report(client, now):
    """Fetch seven complete Beijing days and all official traffic package rows.

    Daily Flux is MB, package Used/Total are bytes. Neither source is derived
    from local sampling, hourly reports, package multipliers or accounting.
    Missing/invalid data raises an error so callers can retry without reporting
    a fabricated zero. In particular, SumFlux is not a per-day value.
    """
    days = _daily_usage(client, now)
    packages = _package_rows(client)
    lines = ["📊 最近7天流量使用情况"]
    lines.extend("- %s: %s" % (day, _tb(amount)) for day, amount in days)
    lines.extend(["", "📦 流量包使用情况"])
    if packages:
        lines.extend(["| 使用情况 | 购买时间（北京时间） | 状态 |", "| --- | --- | --- |"])
        for bought, _identifier, used, total, status in packages:
            lines.append("| 已使用：%s，总量：%s | %s | %s |" % (
                _tb(used), _tb(total), bought.strftime("%Y-%m-%d %H:%M:%S"), PACKAGE_STATES[status]))
    else:
        lines.append("暂无流量包。")
    lines.extend(["", "每日流量为官方下行用量，统计截至昨天的7个完整北京时间自然日。",
                  "流量包为官方已结算用量，随次日计费账单更新，并非实时余额。"])
    return "\n".join(lines)
