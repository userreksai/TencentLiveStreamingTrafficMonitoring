"""CLI entry point and independently aligned collection/report schedules."""

import argparse
import json
import logging
from pathlib import Path
import signal
import sys
import time

from . import __version__
from .app import Monitor
from .config import load_config, redact
from .notifications import LarkSender
from .storage import StateStore

LOG = logging.getLogger("traffic-monitor")


def _best_effort_config_error(path, error):
    """If enough notification settings can be read, report startup failures too."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        from .config import secret_value, validate_webhook
        lark = raw.get("lark", {})
        cfg = {"accounts": [], "lark": {
            "error_webhook": validate_webhook(lark.get("error_webhook", ""), "error_webhook"),
            "error_sign_secret": secret_value(lark.get("error_sign_secret", ""), "error_sign_secret"),
        }}
        LarkSender(cfg).send("error", "【腾讯云直播监控启动异常】\n配置或状态无法加载，请查看服务日志。\n异常类型：%s" % type(error).__name__)
    except Exception:
        LOG.error("无法投递启动异常，请检查异常 webhook 配置及 systemd 日志")


def main(argv=None):
    parser = argparse.ArgumentParser(description="腾讯云直播五分钟流量监控与整点 Lark 汇总")
    parser.add_argument("--config", default="/etc/tencent-traffic-monitor/config.json")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check-config", action="store_true", help="离线校验配置，不联网")
    parser.add_argument("--once", action="store_true", help="采集一次并退出")
    parser.add_argument("--dry-run", action="store_true", help="采集和预览一次，不发消息、不写状态")
    parser.add_argument("--report-now", action="store_true", help="立即生成一份汇总，可与 --once 配合")
    parser.add_argument("--test-notify", choices=("normal", "error"), help="只向指定通道发送测试消息")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = None
    stop = False

    def stopping(_signum, _frame):
        nonlocal stop
        stop = True

    if argv is None:
        signal.signal(signal.SIGTERM, stopping)
        signal.signal(signal.SIGINT, stopping)
    try:
        config = load_config(args.config)
        if args.check_config:
            LOG.info("配置校验通过：%s 个账号，采集周期 %s 秒，%s 每 %s 分钟汇总，延后 %s 分钟，修订通知%s",
                     len(config["accounts"]), config["sampling"]["interval_seconds"],
                     config["reports"]["timezone"], config["reports"]["interval_minutes"],
                     config["reports"]["delay_minutes"], "开启" if config["reports"]["revision_updates"] else "关闭")
            if any(a["_anchor"] is None for a in config["accounts"]):
                LOG.warning("存在未校准账号：运行后可采集并上报异常，但不会声称剩余量正常")
            return 0
        if args.test_notify:
            LarkSender(config).send(args.test_notify,
                                    "【腾讯云直播监控通道测试】\n通道：%s\n这是投递测试，不代表腾讯云数据或计算基准已验证。" % args.test_notify)
            LOG.info("测试通知已发送至 %s 通道", args.test_notify)
            return 0
        store = StateStore(config["storage"]["state_file"], read_only=args.dry_run)
        with store.lock():
            store.load()
            monitor = Monitor(config, store, dry_run=args.dry_run)
            now = int(time.time())
            monitor.reports(now)  # Establish a clock slot, without sending a startup hourly report.
            next_sample = now
            config_stat = Path(args.config).stat().st_mtime_ns
            while not stop:
                now = int(time.time())
                try:
                    modified = Path(args.config).stat().st_mtime_ns
                    if modified != config_stat:
                        try:
                            candidate = load_config(args.config)
                            monitor.reconfigure(candidate)
                            config = candidate
                            config_stat = modified
                            next_sample = now
                            LOG.info("配置已重新加载")
                            monitor.queue.resolve("config/reload", now)
                        except Exception as exc:
                            monitor.queue.incident("config/reload", "配置修改未生效，继续使用上一份有效配置：%s" % redact(exc, config), now)
                    # Reports and retries have their own wall-clock schedule; report failure does not stop collection.
                    # On a collection tick, discover new funding before retrying
                    # normal business alerts from an older, low-quota snapshot.
                    delivered = monitor.queue.drain(now) if now < next_sample else True
                    success = True
                    if now >= next_sample:
                        success = monitor.sample(now)
                        finished = int(time.time())
                        next_sample = (finished // config["sampling"]["interval_seconds"] + 1) * config["sampling"]["interval_seconds"]
                    monitor.reports(int(time.time()))
                    if args.report_now:
                        monitor.reports(int(time.time()), force=True)
                    delivered = monitor.queue.drain(int(time.time())) and delivered
                    success = success and delivered
                    monitor.queue.resolve("runtime", now)
                except Exception as exc:
                    success = False
                    LOG.error("运行异常（%s）：%s", type(exc).__name__, redact(exc, config))
                    monitor.queue.incident("runtime", "运行异常：%s" % redact(exc, config), now,
                                           fingerprint=type(exc).__name__)
                    monitor.queue.drain(int(time.time()))
                    next_sample = now + config["sampling"]["interval_seconds"]
                if args.once or args.dry_run:
                    return 0 if success else 1
                if args.report_now:
                    args.report_now = False
                time.sleep(min(5, max(1, next_sample - int(time.time()))))
        return 0
    except Exception as exc:
        LOG.error("启动失败：%s", redact(exc, config))
        if not args.check_config and not args.dry_run:
            _best_effort_config_error(args.config, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
