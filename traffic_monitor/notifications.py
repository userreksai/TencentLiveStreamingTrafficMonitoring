"""Separate normal, balance and error Lark routes with durable delivery."""

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import format_time, redact

LOG = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LarkSender:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())

    def send(self, route, text):
        lark = self.config["lark"]
        if route == "balance" and not lark.get("balance_webhook"):
            raise NotificationError("尚未配置 lark.balance_webhook，无法发送余量告警")
        payload = {"msg_type": "text", "content": {"text": text}}
        secret = lark.get(route + "_sign_secret", "")
        if secret:
            timestamp = str(int(time.time()))
            key = (timestamp + "\n" + secret).encode("utf-8")
            payload.update(timestamp=timestamp, sign=base64.b64encode(
                hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii"))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(lark[route + "_webhook"], data=body,
                          headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with self.opener.open(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise NotificationError("Lark HTTP %s" % exc.code) from None
        except (URLError, OSError, ValueError, UnicodeError) as exc:
            raise NotificationError("Lark 请求失败（%s）" % type(exc).__name__) from None
        if not isinstance(result, dict):
            raise NotificationError("Lark 返回值不是 JSON 对象")
        codes = [result[k] for k in ("code", "StatusCode") if k in result]
        if not codes or any(str(code) != "0" for code in codes):
            detail = result.get("msg", result.get("StatusMessage", "缺少成功状态码"))
            raise NotificationError(redact("Lark 拒绝通知：%s" % detail, self.config))


def split_message(text, max_bytes=18000):
    """Preserve UTF-8 characters, with room below the webhook body size limit."""
    parts, current, length = [], [], 0
    for character in text:
        size = len(character.encode("utf-8"))
        if length + size > max_bytes:
            parts.append("".join(current))
            current, length = [], 0
        current.append(character)
        length += size
    if current:
        parts.append("".join(current))
    return parts or [""]


class NotificationQueue:
    def __init__(self, store, config, sender=None, dry_run=False):
        self.store = store
        self.config = config
        self.sender = sender or LarkSender(config)
        self.dry_run = dry_run

    def enqueue(self, route, key, text, now):
        text = redact(text, self.config)
        if self.dry_run:
            LOG.info("通知预览 route=%s key=%s\n%s", route, key, redact(text, self.config))
            return
        existing = {item["id"] for item in self.store.state["outbox"]}
        parts = split_message(text)
        for index, part in enumerate(parts):
            delivery_id = "%s/%s/%s" % (route, key, index)
            if delivery_id in existing:
                continue
            if len(parts) > 1:
                part = "（%s/%s）\n%s" % (index + 1, len(parts), part)
            self.store.state["outbox"].append({"id": delivery_id, "route": route,
                                             "text": part, "created_at": now,
                                             "next_attempt": now, "attempts": 0})
        self.store.save()

    def incident(self, key, message, now, fingerprint=None):
        message = redact(message, self.config)
        record = self.store.state["errors"].get(key, {})
        marker = fingerprint or key
        changed = not record.get("active") or record.get("fingerprint") != marker
        due = now - record.get("last_notified", 0) >= self.config["alerts"]["error_repeat_seconds"]
        if changed or due:
            text = "【腾讯云直播监控异常】\n时间：%s\n异常项：%s\n%s" % (
                format_time(now, self.config["_timezone_offset"]), key, message)
            self.enqueue("error", "incident/%s/%s" % (key, now), text, now)
            record["last_notified"] = now
        record.update(active=True, fingerprint=marker, message=message, last_seen=now)
        self.store.state["errors"][key] = record
        self.store.save()

    def resolve(self, key, now):
        record = self.store.state["errors"].get(key)
        if record and record.get("active"):
            self.enqueue("error", "recovery/%s/%s" % (key, now),
                         "【腾讯云直播监控恢复】\n时间：%s\n恢复项：%s" % (
                             format_time(now, self.config["_timezone_offset"]), key), now)
            record["active"] = False
            self.store.save()

    def drain(self, now):
        if self.dry_run:
            return True
        success = True
        blocked_routes = set()
        items = sorted(list(self.store.state["outbox"]), key=lambda x: x["route"] != "error")
        for item in items:
            route = item["route"]
            if item["next_attempt"] > now or route in blocked_routes:
                continue
            if route == "balance" and not self.config["lark"].get("balance_webhook"):
                success = False
                blocked_routes.add(route)
                self.incident("lark/balance", "余量告警群尚未配置，请填写 lark.balance_webhook；待发送余量告警已保留。",
                              now, fingerprint="MissingConfiguration")
                continue
            try:
                self.sender.send(route, item["text"])
            except Exception as exc:
                success = False
                blocked_routes.add(route)
                item["attempts"] += 1
                item["next_attempt"] = now + min(300, 15 * 2 ** min(item["attempts"], 5))
                LOG.error("%s 通知投递失败：%s", route, redact(exc, self.config))
                if route in ("normal", "balance"):
                    label = "正常通知" if route == "normal" else "余量告警"
                    self.incident("lark/" + route, "%s通道投递失败：%s" % (label, redact(exc, self.config)), now)
                self.store.save()
                continue
            self.store.state["outbox"].remove(item)
            self.store.save()
            if route in ("normal", "balance"):
                self.resolve("lark/" + route, now)
        return success
