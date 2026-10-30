"""Tencent Cloud CSS API access using the standard-library TC3 signer.

The API's five-minute granularity is not a data-freshness guarantee. Missing
records deliberately remain missing; callers must check coverage and age.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import hashlib
import hmac
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_VERSION = "2018-08-01"
ALLOWED_ENDPOINTS = ("live.intl.tencentcloudapi.com", "live.tencentcloudapi.com")
SERVICES = ("LVB", "LEB", "PUSH")
REGIONS = (
    "China Mainland", "Asia Pacific I", "Asia Pacific II", "Asia Pacific III",
    "Europe", "North America", "South America", "Middle East", "Africa",
)
BEIJING = timezone(timedelta(hours=8))


class TencentError(Exception):
    """A classified error safe to route to an operational notification channel."""

    def __init__(self, code, message, request_id="", retryable=False):
        self.code = str(code)
        self.message = str(message)
        self.request_id = str(request_id or "")
        self.retryable = bool(retryable)
        detail = "[%s] %s" % (self.code, self.message)
        if self.request_id:
            detail += " (RequestId=%s)" % self.request_id
        super().__init__(detail)


class _NoRedirect(HTTPRedirectHandler):
    # An authentication header must never be forwarded to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class TencentClient:
    def __init__(self, secret_id, secret_key, token="",
                 endpoint="live.intl.tencentcloudapi.com", timeout=30):
        if not isinstance(secret_id, str) or not secret_id.strip():
            raise TencentError("ConfigurationError", "Tencent SecretId is missing")
        if not isinstance(secret_key, str) or not secret_key.strip():
            raise TencentError("ConfigurationError", "Tencent SecretKey is missing")
        if not isinstance(token, str):
            raise TencentError("ConfigurationError", "Tencent session token must be a string")
        if endpoint not in ALLOWED_ENDPOINTS:
            raise TencentError("ConfigurationError", "Only official Tencent CSS API endpoints are supported")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise TencentError("ConfigurationError", "Request timeout must be between 0 and 120 seconds")
        self._secret_id = secret_id.strip()
        self._secret_key = secret_key.strip()
        self._token = token.strip()
        self.endpoint = endpoint
        self.timeout = timeout

    def _redact(self, value):
        value = str(value)
        for sensitive in (self._secret_id, self._secret_key, self._token):
            if sensitive:
                value = value.replace(sensitive, "[REDACTED]")
        return value[:1500]

    def at_endpoint(self, endpoint):
        return TencentClient(self._secret_id, self._secret_key, self._token,
                             endpoint=endpoint, timeout=self.timeout)

    def _request(self, action, params, timestamp):
        try:
            body = json.dumps(params, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise TencentError("ConfigurationError", "API parameters must be valid JSON") from None
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        content_type = "application/json; charset=utf-8"
        signed_headers = "content-type;host"
        canonical_headers = "content-type:%s\nhost:%s\n" % (content_type, self.endpoint)
        canonical = "\n".join([
            "POST", "/", "", canonical_headers, signed_headers,
            hashlib.sha256(body).hexdigest(),
        ])
        scope = "%s/live/tc3_request" % date
        to_sign = "\n".join([
            "TC3-HMAC-SHA256", str(timestamp), scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ])

        def digest(key, text):
            return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()

        date_key = digest(("TC3" + self._secret_key).encode("utf-8"), date)
        service_key = digest(date_key, "live")
        signing_key = digest(service_key, "tc3_request")
        signature = hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": content_type,
            "Host": self.endpoint,
            "X-TC-Action": action,
            "X-TC-Version": API_VERSION,
            "X-TC-Timestamp": str(timestamp),
            "Authorization": "TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                             % (self._secret_id, scope, signed_headers, signature),
        }
        if self._token:
            headers["X-TC-Token"] = self._token
        return Request("https://%s/" % self.endpoint, data=body, headers=headers, method="POST")

    def _post(self, request):
        try:
            with _open_request(request, self.timeout) as response:
                # Decimal preserves fractional MB until integer-byte rounding.
                result = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        except HTTPError as exc:
            raise TencentError("HTTP%s" % exc.code, "Tencent API HTTP request failed",
                               retryable=exc.code == 429 or exc.code >= 500) from None
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise TencentError("NetworkError", "Tencent API network failure (%s)" % type(exc).__name__,
                               retryable=True) from None
        except (ValueError, UnicodeError):
            raise TencentError("InvalidResponse", "Tencent API returned invalid JSON") from None
        if not isinstance(result, dict) or not isinstance(result.get("Response"), dict):
            raise TencentError("InvalidResponse", "Tencent API response has no Response object")
        response = result["Response"]
        error = response.get("Error")
        if error is not None:
            if not isinstance(error, dict):
                raise TencentError("InvalidResponse", "Tencent API Error is not an object")
            code = self._redact(error.get("Code", "UnknownError"))
            message = self._redact(error.get("Message", "Tencent API operation failed"))
            request_id = self._redact(response.get("RequestId", ""))
            raise TencentError(code, message, request_id,
                               retryable=code.startswith(("InternalError", "RequestLimitExceeded")))
        return response

    def call(self, action, params):
        if not isinstance(action, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", action):
            raise TencentError("ConfigurationError", "Invalid Tencent API action")
        if not isinstance(params, dict):
            raise TencentError("ConfigurationError", "Tencent API parameters must be an object")
        for attempt in range(3):
            request = self._request(action, params, int(time.time()))
            try:
                return self._post(request)
            except TencentError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                time.sleep(2 ** attempt)


def _epoch(value):
    if not isinstance(value, str) or not value.strip() or " " not in value and "T" not in value:
        raise TencentError("InvalidResponse", "A usage record has no valid Time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        if parsed.microsecond:
            raise ValueError("fractional bucket timestamp")
        epoch = int(parsed.timestamp())
        if epoch % 300:
            raise ValueError("unaligned bucket timestamp")
        return epoch
    except (ValueError, OverflowError, OSError):
        raise TencentError("InvalidResponse", "A usage record Time is not a five-minute boundary") from None


def _bytes(value):
    if value is None or isinstance(value, bool):
        raise TencentError("InvalidResponse", "A usage record has no valid Flux")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("negative or non-finite traffic")
        with localcontext() as context:
            context.prec = max(40, len(amount.as_tuple().digits) + 10)
            return int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        raise TencentError("InvalidResponse", "A usage record Flux must be finite and nonnegative") from None


def _selectors(values, allowed, name):
    if not isinstance(values, (list, tuple)) or not values:
        raise TencentError("ConfigurationError", "%s must be an explicit nonempty list" % name)
    if any(not isinstance(value, str) or value not in allowed for value in values):
        raise TencentError("ConfigurationError", "Unknown %s value; use the documented Tencent CSS labels" % name)
    return list(dict.fromkeys(values))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_today_downstream(client, now, offset=8 * 3600):
    """Read the official cumulative playback total for the configured local day.

    This unweighted counter covers all playback domains, regions, LVB and LEB.
    It is for the daily overview display only, never a resource-package debit.
    The API may include its EndTime point or a partially populated last bucket;
    SumFlux is retained as returned rather than recomputed from bucket records.
    """
    if (isinstance(now, bool) or not isinstance(now, int) or now < 0
            or isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 14 * 3600):
        raise TencentError("ConfigurationError", "Daily usage time and timezone offset are invalid")
    start = ((now + offset) // 86400) * 86400 - offset
    result = {"day_start": start, "checked_at": now, "through": now,
              "bytes": "0", "latest_data_time": None}
    if start == now:
        # A new local day has no elapsed query range yet. Do not send an
        # invalid StartTime == EndTime request, or reuse yesterday's total.
        return result
    response = client.call("DescribeBillBandwidthAndFluxList", {
        "StartTime": _iso(start), "EndTime": _iso(now), "Granularity": 5,
    })
    amount = _bytes(response.get("SumFlux"))
    items = response.get("DataInfoList")
    if not isinstance(items, list):
        raise TencentError("InvalidResponse", "Tencent daily usage response has no DataInfoList",
                           response.get("RequestId", ""))
    latest = None
    for item in items:
        if not isinstance(item, dict):
            raise TencentError("InvalidResponse", "A Tencent daily usage record is not an object")
        timestamp = _epoch(item.get("Time"))
        if not start <= timestamp <= now:
            raise TencentError("InvalidResponse", "Tencent daily usage contains records outside the requested day range")
        _bytes(item.get("Flux"))
        latest = max(latest or timestamp, timestamp)
    if amount and latest is None:
        raise TencentError("InvalidResponse", "Tencent daily total has no underlying usage records")
    result["bytes"] = str(amount)
    result["latest_data_time"] = min(latest + 300, now) if latest is not None else None
    return result


def fetch_billable_buckets(client, start, end, services, regions):
    """Collect explicit per-service/per-region five-minute usage in [start,end).

    PUSH is optional upstream *usage*. Whether it is billable depends on daily
    global push/play conditions; callers must opt in to that conservative scope.
    A missing or empty data list never creates synthetic zero-usage buckets.
    """
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
            or not isinstance(end, int) or start < 0 or end <= start or start % 300 or end % 300):
        raise TencentError("ConfigurationError", "Usage range must be increasing UTC five-minute boundaries")
    services = _selectors(services, SERVICES, "services")
    regions = _selectors(regions, REGIONS, "regions")
    collected = {}
    chunk_start = start
    while chunk_start < end:
        next_midnight = ((chunk_start // 86400) + 1) * 86400
        chunk_end = min(next_midnight, end)
        for service in services:
            action = "DescribePushBandwidthAndFluxList" if service == "PUSH" else "DescribeBillBandwidthAndFluxList"
            for region in regions:
                params = {"StartTime": _iso(chunk_start), "EndTime": _iso(chunk_end),
                          "Granularity": 5, "RegionNames": [region]}
                if service != "PUSH":
                    params["ServiceName"] = service
                response = client.call(action, params)
                items = response.get("DataInfoList")
                if not isinstance(items, list):
                    raise TencentError("InvalidResponse", "Tencent usage response has no DataInfoList",
                                       response.get("RequestId", ""))
                for item in items:
                    if not isinstance(item, dict):
                        raise TencentError("InvalidResponse", "A Tencent usage record is not an object")
                    timestamp = _epoch(item.get("Time"))
                    # The API can include its EndTime point. The next chunk owns
                    # that interval, so it cannot be charged twice here.
                    if not chunk_start <= timestamp < chunk_end:
                        continue
                    amount = _bytes(item.get("Flux"))
                    key = (timestamp, service, region)
                    if key in collected and collected[key]["bytes"] != amount:
                        raise TencentError("InvalidResponse", "Conflicting duplicate usage buckets in one response")
                    collected[key] = {"time": timestamp, "service": service, "region": region, "bytes": amount}
        chunk_start = chunk_end
    return [collected[key] for key in sorted(collected)]


def fetch_packages(client):
    """Get raw CSS package records for display; never use them as a live ledger.

    Only an explicitly unsupported package action triggers the general endpoint
    fallback. Authentication errors retain their original classification.
    """
    selected = client
    params = {"PackageType": 0, "PageNum": 1, "PageSize": 100, "OrderBy": "BuyTimeDesc"}
    try:
        first = selected.call("DescribeLivePackageInfo", params)
    except TencentError as exc:
        if (client.endpoint != "live.intl.tencentcloudapi.com"
                or not ("InvalidAction" in exc.code or "UnsupportedOperation" in exc.code)):
            raise
        selected = client.at_endpoint("live.tencentcloudapi.com")
        first = selected.call("DescribeLivePackageInfo", params)
    packages = {}
    for page in range(1, 10001):
        response = first if page == 1 else selected.call(
            "DescribeLivePackageInfo", dict(params, PageNum=page))
        items = response.get("LivePackageInfoList")
        if not isinstance(items, list):
            raise TencentError("InvalidResponse", "Package response has no LivePackageInfoList")
        new_ids = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("Id"):
                raise TencentError("InvalidResponse", "A package record has no Id")
            identifier = str(item["Id"])
            new_ids += identifier not in packages
            packages[identifier] = item
        try:
            total_page = response.get("TotalPage")
            total_num = response.get("TotalNum")
            if total_page is not None:
                total_page = int(total_page)
            if total_num is not None:
                total_num = int(total_num)
            if ((total_page is not None and total_page < 0)
                    or (total_num is not None and total_num < 0)):
                raise ValueError()
        except (TypeError, ValueError):
            raise TencentError("InvalidResponse", "Package pagination metadata is invalid") from None
        finished = ((total_page is not None and page >= total_page)
                    or (total_num is not None and len(packages) >= total_num)
                    or (total_page is None and total_num is None and len(items) < 100))
        if finished:
            if total_num is not None and len(packages) < total_num:
                raise TencentError("IncompleteResponse", "Package pagination ended before TotalNum was reached")
            if page > 1 and items and not new_ids:
                raise TencentError("IncompleteResponse", "Package pagination repeated a previous page")
            return list(packages.values())
        if not items or not new_ids:
            raise TencentError("IncompleteResponse", "Package pagination stopped before completion")
    raise TencentError("IncompleteResponse", "Package pagination exceeded its safety limit")
