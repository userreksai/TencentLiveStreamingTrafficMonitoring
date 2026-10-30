# 腾讯云国际站直播流量监控

版本：**1.0.2**。面向腾讯云 CSS 标准直播／快直播，默认每 **5 分钟**采集一次，每个**北京时间整点小时**发送汇总；计算后的安全可用量低于 **50% / 10%** 时提醒重点关注并购买流量包。正常通知和所有异常通知分别发送到两个 Lark／飞书机器人。

## 能得到什么

- 按直播类型、计费地区读取五分钟流量并换算套餐抵扣量，保存本地估算台账。
- 同一时间段重复获取时替换修订值，避免重复扣量。
- 正常整点汇总包含账号、数据截至时间、已采集扣量、估算／安全余量及风险状态。
- 余量不足 50%、10% 时向正常通知机器人发送购买资源提醒。
- 获取数据失败、密钥／权限错误、数据过旧、基准未校准、通知失败等异常走异常机器人；持续异常按配置间隔提醒，恢复后通知。
- 采集周期、汇总周期、时区、阈值、机器人地址和账号均在 JSON 配置中修改，无需修改 Python 源码。
- 使用标准库，无第三方 Python 运行依赖。支持 Linux systemd 常驻部署，配置与状态独立于代码目录，更新代码时保留。

## 余量口径：必须先读

**输出是基于直播用量计算的估算余量，不是腾讯云提供的实时套餐余额。** 腾讯云套餐剩余量随次日账单结算更新，缩短查询套餐接口的间隔无法获得实时余额。

本项目以已核对的余额及用量起点建立基准，再按五分钟流量扣减。海外流量、标准直播和快直播有不同抵扣比例，不能用“套餐额度减全球播放流量”直接计算。

```text
估算剩余量 = 基准剩余量 - 基准之后按地区／直播类型折算的用量
安全可用量 = 估算剩余量 - 数据延迟及处置时间的安全预留
```

首次部署请从套餐抵扣明细确认基准已经覆盖的用量范围。**基准用量起点不是购包时间，也不是读取余额的当前时间。** 未配置基准时仍保留近期用量并按整点汇总已取得的折算用量，但明确显示“未校准”，并发送异常提醒，不提供可靠余量、不触发假设健康的判断。

五分钟是采集周期和计费用量粒度，**不代表腾讯云保证五分钟内更新**。程序会显示数据截至时间并监测数据陈旧；是否满足实际五至十分钟的可见性目标，应在真实账号上验证。某个地区／业务类型没有返回数据时，不会自动补成零流量，而会标记缺失；只有接口明确返回的零值才算零消耗。所有异常均应结合日志检查，机器人本身不可用时，通知无法凭空送达。

首版采用手工核对基准：新增套餐、套餐到期、调整计费模式，或按日结算对账发现偏差时，应重新确认余额和用量起点，修改 `anchor.revision` 后保存配置。发现套餐新增、额度变化或到期失效时，会将当前估算标记为需要重新校准。程序不会因为官方滞后余额改变就自动清零台账，避免重复扣减或漏扣。本程序只发送购买提醒，**不会自动购买套餐、充值、修改计费方式或中断播放**。

## 首次部署

建议 Ubuntu 22.04+、Debian 11+ 或 Rocky Linux 9+，需要 systemd、Python 3.9+、Git、CA 证书和可访问腾讯云／Lark 的网络。

先下载部署脚本，再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/userreksai/TencentLiveStreamingTrafficMonitoring/main/install.sh -o /tmp/tencent-traffic-install.sh
sudo bash /tmp/tencent-traffic-install.sh
```

脚本会安装缺少的依赖、获取 `main` 分支最新代码、创建独立 Python 环境和专用服务用户，并生成：

| 内容 | 路径 |
| --- | --- |
| 程序与虚拟环境 | `/usr/local/tencenttraffic` |
| 配置文件 | `/etc/tencent-traffic-monitor/config.json` |
| 持久化状态 | `/var/lib/tencent-traffic-monitor/state.json` |
| systemd 服务 | `tencent-traffic-monitor.service` |

首次生成的模板没有密钥和机器人地址，不会自动启动。填写配置后检查、启动：

```bash
sudo nano /etc/tencent-traffic-monitor/config.json
sudo bash /usr/local/tencenttraffic/install.sh --check
sudo bash /usr/local/tencenttraffic/install.sh --start
```

`--start` 会同时设置开机自启。配置目录权限为 `0750`，配置文件为 `root:tencent-traffic-monitor 0640`；服务以独立用户运行，代码只读，状态目录可写。

如果此前使用旧版默认目录部署，请重新下载上面的最新安装脚本并执行 `--install`。脚本会在 `/usr/local/tencenttraffic` 获取代码并新建虚拟环境，复用 `/etc/tencent-traffic-monitor` 的配置和 `/var/lib/tencent-traffic-monitor` 的状态，重新写入 systemd 路径；原来已运行或启用的服务会在检查通过后重启。旧代码目录不会自动移动或删除。

## 配置说明

完整模板见 [`config.example.json`](config.example.json)。部署时填写本机的 `/etc/tencent-traffic-monitor/config.json`，不要把真实配置提交到公开仓库。

### 采集与整点通知

```json
{
  "sampling": {
    "interval_seconds": 300,
    "lookback_minutes": 120,
    "max_data_age_seconds": 600,
    "max_backfill_days": 31,
    "request_timeout_seconds": 30
  },
  "reports": {
    "timezone": "UTC+08:00",
    "interval_minutes": 60
  }
}
```

- `interval_seconds=300`：每五分钟采集；改为 `600` 即十分钟。应使用 300 秒的整数倍。
- `lookback_minutes`：每次重新拉取近期窗口，回补窗口内的迟到／修订数据，按时间段去重。超出窗口的历史修订需提高回查范围或重新核对基准。
- `max_data_age_seconds=600`：最新用量数据落后超过十分钟时发送异常提醒，不能解释为零消耗。每次采集时检查，不构成腾讯云数据时延保证。
- `max_backfill_days`：首次或变更基准时允许回溯的天数上限。
- `interval_minutes=60`：北京时间每个整点汇总，按时钟对齐；不是“进程启动后累计一小时”。
- `timezone` 可改为 `UTC+07:00` 等固定时差。汇总周期至少五分钟、为五分钟的整数倍，且应能整除一天的 1440 分钟。

程序会重新读取配置，通常数秒内生效。修改后可先执行 `--check` 检查 JSON 和参数；也可通过 `--restart` 重启后生效。

启动后立即采集一次，然后按采集周期对齐时钟边界。全新安装的第一份定时汇总在下一个汇总边界发送；保留状态后重启，会补发最近一个尚未发送的汇总时段，不会把停机期间所有小时逐条刷屏。整点时尚未到达的末尾用量不会假计为零，汇总会标明当前完整的五分钟段数；回查窗口内的迟到数据会在后续采集中修订。

### 两个机器人地址

```json
"lark": {
  "normal_webhook": "",
  "error_webhook": "",
  "normal_sign_secret": "",
  "error_sign_secret": ""
}
```

- `normal_webhook`：整点小时汇总、计算后余量不足及购买流量包提醒。
- `error_webhook`：账号密钥／权限异常、API 查询失败、数据延迟、配置及其他运行异常。
- 两个地址均由你部署时填写。机器人启用签名校验时再填写对应的 `*_sign_secret`。
- 若启用关键词限制，请允许“腾讯云”；若启用 IP 白名单，需允许服务器的实际出口 IP。
- 正常机器人投递失败会尝试通过异常机器人报告。异常机器人也不可用时只可写日志，需结合 `journalctl` 发现。

### 告警阈值与重复间隔

```json
"alerts": {
  "remaining_percent": [50, 10],
  "repeat_seconds": 3600,
  "error_repeat_seconds": 1800,
  "safety_seconds": 900,
  "safety_factor": "1.10",
    "reconciliation_tolerance_percent": "0.1",
  "runway_minutes": 60
}
```

阈值比较的是**安全可用量占基准总量的比例**，为严格“小于”。低于 50% 提醒提前准备，低于 10% 重点提醒购买流量包。`repeat_seconds` 控制持续低余量的提醒间隔，`error_repeat_seconds` 控制持续异常的提醒间隔；重启后会保留通知记录。

`safety_seconds` 和 `safety_factor` 控制风险预留，避免等估算余量降到零才处置。它们是可配置的保守预警参数，**不是官方抵扣倍率**。`runway_minutes` 控制预计可用时长的预警。

`reconciliation_tolerance_percent` 是官方结算余量与本地估算的差异容忍度，默认 `0.1` 表示基准总量的 **0.1%**。官方结算余量明显低于本地估算、且差额超过此阈值时，会进入异常通道要求核对并重新校准；程序不会直接用滞后的官方数值重置台账。

### 账号与基准

每个账号独立配置、独立估算；`accounts` 可包含多个账号。

```json
{
  "name": "直播主账号",
  "secret_id": "",
  "secret_key": "",
  "session_token": "",
  "endpoint": "live.intl.tencentcloudapi.com",
  "services": ["LVB", "LEB"],
  "regions": ["Asia Pacific I"],
  "anchor": {
    "revision": "initial",
    "usage_start": "",
    "remaining_tb": null,
    "total_tb": null
  },
  "package_names": {}
}
```

- `secret_id`、`secret_key` 填入具有对应直播查询权限的账号密钥；临时凭证再填写 `session_token`。
- `services` 指定标准直播 `LVB`、快直播 `LEB`。按实际业务选择，不要在未核实的情况下漏掉消耗来源。
- 也支持可选的 `PUSH` 上行用量。上行是否计费受腾讯云日级计费条件影响，只有确认需计入或决定采用保守统计时才启用；不能将所有推流用量默认视为实际套餐扣量。
- `regions` 使用官方计费大区名称，完整默认列表见模板。只选择实际需统计的大区前，须确认不会漏掉其他地区用量。
- 模板默认包含全部大区及 LVB／LEB，以避免默默漏计。若某些维度接口一直返回空列表，程序会报缺数据；应先核实实际业务范围和接口零流量行为，再调整 `services`／`regions`，不要仅为了消除告警而删掉可能产生用量的地区。
- `anchor.usage_start` 是**尚未包含在基准余额中的用量起点**，采用带时区 ISO 时间，例如 `2026-10-30T00:00:00+08:00`。示例日期只解释格式，不能直接当作你的实际结算起点。
- `anchor.remaining_tb` 是该基准的剩余套餐额度；`anchor.total_tb` 用于计算剩余百分比，均为十进制 TB（1 TB = 1,000,000,000,000 字节）。
- 首次确认或重新校准时同时修改余额、起点以及 `anchor.revision`，如 `settled-20261030-1`；相同基准不会反复重置台账。
- 官方套餐查询未提供原名称字段。`package_names` 可按包 ID 设置展示名称，例如 `{"321018": "直播主流量包"}`；没有设置时显示容量生成的名称及包 ID。

主体查询使用 `DescribeBillBandwidthAndFluxList`。子账号需具备对应查询权限，且账号处于可正常使用的状态。不同账号／合同、跨区和快直播的实际返回应先验证；权限或接口不支持将走异常通道。

`storage.state_file` 默认位于 `/var/lib/tencent-traffic-monitor`。部署服务只允许写入此目录；如修改状态文件路径，应仍放在该目录内，或同时调整 systemd 的写入授权并重启。

## 更新代码与日常管理

```bash
# 拉取最新 main，离线检查与配置校验通过后重启原有服务
sudo bash /usr/local/tencenttraffic/install.sh --update

# 只检查配置（不联网、不发通知）
sudo bash /usr/local/tencenttraffic/install.sh --check

# 查看状态和实时日志
sudo bash /usr/local/tencenttraffic/install.sh --status
sudo bash /usr/local/tencenttraffic/install.sh --logs

# 重启 / 停止
sudo bash /usr/local/tencenttraffic/install.sh --restart
sudo bash /usr/local/tencenttraffic/install.sh --stop
```

更新采用 Git 快进合并，拒绝覆盖本地修改、不同仓库、不同分支或未推送提交。配置、凭证和状态在 Git 目录之外，不会被更新覆盖。若测试或配置检查失败，不会重启服务，应处理错误后重试。

自定义源码仓库或分支：

```bash
sudo env REPO_URL=https://github.com/userreksai/TencentLiveStreamingTrafficMonitoring.git REPO_BRANCH=main bash /tmp/tencent-traffic-install.sh
```

不要同时配置 cron 和 systemd 常驻任务。旧的单文件脚本及其状态格式不作为本版精确余量基准，升级时请先停止旧进程，完成一次基准核对。

## 手动验证

进入代码目录，使用虚拟环境。下面 `--dry-run` 不发送消息：

```bash
cd /usr/local/tencenttraffic
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --once --dry-run --report-now
```

`--report-now` 会立即生成当前汇总窗口的预览。去掉 `--dry-run` 后，这条手动命令会实际采集、写入状态并向正常机器人发送汇总；日常运行无需添加 `--report-now`。正在运行服务时请通过服务日志查看，避免同时启动第二个进程争用同一状态文件。

测试通知会实际向所选机器人发送消息：

```bash
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --test-notify normal
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --test-notify error
```

离线测试与脚本语法检查：

```bash
python3 -m unittest discover -s tests -p 'test*.py' -v
bash -n install.sh
```

仓库的 GitHub Actions 会执行离线测试和 Bash 语法检查；不会使用真实密钥或真实机器人。离线验证不能代替真实账号的数据延迟、地区抵扣口径及跨出账周期校准验证。

## 官方参考

- [直播计费用量查询：五分钟粒度、业务类型与计费地区](https://www.tencentcloud.com/document/product/267/36098)
- [播放监控数据：一分钟粒度及约四分钟延迟](https://www.tencentcloud.com/document/product/267/37297)
- [预付费套餐：海外倍率、抵扣顺序与次日结算](https://www.tencentcloud.com/document/product/267/52220)
- [直播计费说明：套餐用量更新滞后与超量按量计费](https://www.tencentcloud.com/document/product/267/32479)
- [Lark 自定义机器人](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)
