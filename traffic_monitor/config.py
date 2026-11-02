"""Load a secret-bearing JSON configuration without printing its contents."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

REGIONS = ["China Mainland", "Asia Pacific I", "Asia Pacific II", "Asia Pacific III",
           "Europe", "North America", "South America", "Middle East", "Africa"]


class ConfigError(ValueError):
    pass


def parse_time(value):
    """Require an explicit timezone for accounting anchors."""
    if not isinstance(value, str):
        raise ConfigError("时间必须是带时区的 ISO8601 字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return int(parsed.timestamp())
    except (ValueError, OverflowError):
        raise ConfigError("时间必须包含时区，例如 2026-10-30T00:00:00+08:00") from None


def timezone_offset(value):
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", str(value))
    if not match:
        raise ConfigError("reports.timezone 使用 UTC+08:00 这样的固定时区格式")
    hours, minutes = int(match[2]), int(match[3])
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        raise ConfigError("reports.timezone 超出有效范围")
    return (hours * 3600 + minutes * 60) * (1 if match[1] == "+" else -1)


def secret_value(value, label):
    if not isinstance(value, str):
        raise ConfigError("%s 必须是字符串" % label)
    if value.startswith("env:"):
        name = value[4:]
        value = os.getenv(name, "")
        if not value:
            raise ConfigError("%s 引用的环境变量未设置" % label)
    return value.strip()


def number(value, label, minimum=0, maximum=None):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or (maximum is not None and result > maximum):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise ConfigError("%s 不是允许范围内的数字" % label) from None


def integer(value, label, minimum=1, maximum=10**9):
    result = number(value, label, minimum, maximum)
    if result != result.to_integral_value():
        raise ConfigError("%s 必须是整数" % label)
    return int(result)


def validate_webhook(value, label):
    value = secret_value(value, label)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ConfigError("%s 的地址格式无效" % label) from None
    if (parsed.scheme != "https" or parsed.hostname not in ("open.larksuite.com", "open.feishu.cn")
            or parsed.username or parsed.password or port not in (None, 443)
            or not parsed.path.startswith("/open-apis/bot/v2/hook/")
            or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")):
        raise ConfigError("%s 请填写有效的 Lark/飞书 HTTPS v2 webhook" % label)
    return value


def validate_config(raw, path=None):
    """Return a normalized copy; no network, file writes, or implicit calibration."""
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ConfigError("配置 version 必须为 1")
    cfg = deepcopy(raw)
    for section in ("sampling", "reports", "alerts", "lark", "storage"):
        if not isinstance(cfg.get(section), dict):
            raise ConfigError("缺少配置对象 %s" % section)
    sampling = cfg["sampling"]
    sampling["interval_seconds"] = integer(sampling.get("interval_seconds", 300), "sampling.interval_seconds", 300, 86400)
    if sampling["interval_seconds"] % 300:
        raise ConfigError("采集周期必须为 300 秒的整数倍（五分钟粒度）")
    for key, default, low, high in (("lookback_minutes", 120, 5, 44640),
                                   ("max_data_age_seconds", 600, 300, 86400),
                                   ("max_backfill_days", 31, 1, 31),
                                   ("request_timeout_seconds", 30, 1, 120)):
        sampling[key] = integer(sampling.get(key, default), "sampling." + key, low, high)
    reports = cfg["reports"]
    reports.setdefault("timezone", "UTC+08:00")
    cfg["_timezone_offset"] = timezone_offset(reports["timezone"])
    reports["interval_minutes"] = integer(reports.get("interval_minutes", 60), "reports.interval_minutes", 5, 1440)
    if reports["interval_minutes"] % 5 or 1440 % reports["interval_minutes"]:
        raise ConfigError("汇总周期必须是五分钟的整数倍，且能整除 1440 分钟")
    reports["delay_minutes"] = integer(reports.get("delay_minutes", 5), "reports.delay_minutes", 0, 1440)
    reports.setdefault("revision_updates", False)
    if type(reports["revision_updates"]) is not bool:
        raise ConfigError("reports.revision_updates 必须为 true 或 false")
    alerts = cfg["alerts"]
    levels = alerts.get("remaining_percent", [50, 10])
    if not isinstance(levels, list) or not levels:
        raise ConfigError("alerts.remaining_percent 必须为非空数组")
    alerts["remaining_percent"] = sorted({str(number(v, "告警百分比", 0, 100)) for v in levels}, key=Decimal, reverse=True)
    for key, default, low in (("repeat_seconds", 0, 0), ("error_repeat_seconds", 1800, 60),
                               ("safety_seconds", 900, 0), ("runway_minutes", 60, 0)):
        alerts[key] = integer(alerts.get(key, default), "alerts." + key, low, 604800)
    alerts["safety_factor"] = str(number(alerts.get("safety_factor", "1.10"), "alerts.safety_factor", 1, 10))
    alerts["reconciliation_tolerance_percent"] = str(number(
        alerts.get("reconciliation_tolerance_percent", "0.1"), "alerts.reconciliation_tolerance_percent", 0, 100))
    lark = cfg["lark"]
    for route in ("normal", "error"):
        lark[route + "_webhook"] = validate_webhook(lark.get(route + "_webhook", ""), "lark." + route + "_webhook")
        lark[route + "_sign_secret"] = secret_value(lark.get(route + "_sign_secret", ""), "lark 签名密钥")
    # Keep older deployments valid while requiring an explicit destination
    # before any low-balance alert can be delivered.
    lark["balance_webhook"] = secret_value(lark.get("balance_webhook", ""), "lark.balance_webhook")
    if lark["balance_webhook"]:
        lark["balance_webhook"] = validate_webhook(lark["balance_webhook"], "lark.balance_webhook")
    lark["balance_sign_secret"] = secret_value(lark.get("balance_sign_secret", ""), "lark.balance_sign_secret")
    # Daily statistics are opt-in and use a separate destination and Beijing clock.
    # Missing this section must preserve existing deployments and account scopes.
    daily = cfg.setdefault("daily_report", {})
    if not isinstance(daily, dict):
        raise ConfigError("daily_report 必须为对象")
    daily.setdefault("enabled", False)
    if type(daily["enabled"]) is not bool:
        raise ConfigError("daily_report.enabled 必须为 true 或 false")
    daily.setdefault("time", "10:05")
    if (not isinstance(daily["time"], str)
            or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily["time"])):
        raise ConfigError("daily_report.time 使用北京时间 HH:MM 格式，例如 10:05")
    daily["webhook"] = secret_value(daily.get("webhook", ""), "daily_report.webhook")
    if daily["enabled"] or daily["webhook"]:
        daily["webhook"] = validate_webhook(daily["webhook"], "daily_report.webhook")
    daily["sign_secret"] = secret_value(daily.get("sign_secret", ""), "daily_report.sign_secret")
    state_path = cfg["storage"].get("state_file", "state.json")
    if not isinstance(state_path, str) or not state_path:
        raise ConfigError("storage.state_file 必须填写路径")
    state_path = Path(state_path).expanduser()
    if not state_path.is_absolute():
        state_path = (Path(path).resolve().parent if path else Path.cwd()) / state_path
    cfg["storage"]["state_file"] = str(state_path.resolve())
    accounts = cfg.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ConfigError("至少配置一个 accounts 账号")
    seen = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ConfigError("accounts 条目必须为对象")
        name = account.get("name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise ConfigError("账号名称必须非空且不能重复")
        seen.add(name)
        for key in ("secret_id", "secret_key", "session_token"):
            account[key] = secret_value(account.get(key, ""), "账号 %s 的 %s" % (name, key))
            if key != "session_token" and not account[key]:
                raise ConfigError("账号 %s 尚未配置 %s" % (name, key))
        account.setdefault("endpoint", "live.intl.tencentcloudapi.com")
        if account["endpoint"] not in ("live.intl.tencentcloudapi.com", "live.tencentcloudapi.com"):
            raise ConfigError("账号 endpoint 必须为腾讯云直播官方域名")
        for key, allowed, default in (("services", ("LVB", "LEB", "PUSH"), ["LVB", "LEB"]),
                                      ("regions", REGIONS, REGIONS)):
            values = account.get(key, default)
            if not isinstance(values, list) or not values or any(v not in allowed for v in values):
                raise ConfigError("账号 %s 的 %s 无效" % (name, key))
            account[key] = list(dict.fromkeys(values))
        names = account.get("package_names", {})
        if not isinstance(names, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in names.items()):
            raise ConfigError("package_names 必须是包 ID 到名称的映射")
        account["package_names"] = names
        anchor = account.get("anchor", {})
        if not isinstance(anchor, dict):
            raise ConfigError("anchor 必须为对象")
        present = [bool(anchor.get("usage_start")), anchor.get("remaining_tb") is not None,
                   anchor.get("total_tb") is not None]
        account["_anchor"] = None
        if any(present):
            if not all(present):
                raise ConfigError("账号 %s 的 anchor 时间、剩余量和总量必须一起填写" % name)
            start = parse_time(anchor["usage_start"])
            if start % 300:
                raise ConfigError("anchor.usage_start 必须对齐五分钟边界")
            remaining = number(anchor["remaining_tb"], "anchor.remaining_tb", 0)
            total = number(anchor["total_tb"], "anchor.total_tb", 0)
            if total <= 0 or remaining > total:
                raise ConfigError("anchor.total_tb 必须大于 0，且不能小于剩余量")
            account["_anchor"] = {"time": start, "remaining_bytes": int(remaining * 10**12),
                                  "total_bytes": int(total * 10**12)}
        anchor_fingerprint = {"anchor": anchor, "regions": account["regions"], "services": account["services"]}
        account["_anchor_id"] = hashlib.sha256(json.dumps(anchor_fingerprint, sort_keys=True).encode()).hexdigest()
        account["_id"] = hashlib.sha256((name + "\n" + account["secret_id"]).encode()).hexdigest()[:24]
    cfg["_path"] = str(Path(path).resolve()) if path else None
    return cfg


def load_config(path):
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            raw = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ConfigError("配置文件读取失败（%s）" % type(exc).__name__) from None
    return validate_config(raw, path)


def redact(text, config=None):
    result = str(text)
    secrets = []
    if config:
        for account in config.get("accounts", []):
            secrets.extend(account.get(k, "") for k in ("secret_id", "secret_key", "session_token"))
        secrets.extend(config.get("lark", {}).values())
        secrets.extend(config.get("daily_report", {}).get(key, "") for key in ("webhook", "sign_secret"))
    for value in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        result = result.replace(value, "[已隐藏]")
    result = re.sub(r"https://open\.(?:larksuite\.com|feishu\.cn)/open-apis/bot/v2/hook/[^\s\"']+", "[Lark 地址已隐藏]", result)
    return result


def format_time(timestamp, offset=8*3600):
    zone = timezone(timedelta(seconds=offset))
    return datetime.fromtimestamp(timestamp, zone).strftime("%Y-%m-%d %H:%M:%S %z")
