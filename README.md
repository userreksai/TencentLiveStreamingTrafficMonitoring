# 腾讯云国际站直播流量监控

版本：**1.1.1**。面向腾讯云 CSS 标准直播／快直播，默认每 **5 分钟**采集一次，每个**北京时间整点小时**发送汇总；计算后的安全可用量低于 **50% / 10%** 时提醒重点关注并购买流量包。正常通知和所有异常通知分别发送到两个 Lark／飞书机器人。

## 能得到什么

- 按直播类型、计费地区读取五分钟流量并换算套餐抵扣量，保存本地估算台账。
- 同一时间段重复获取时替换修订值，避免重复扣量。
- 首次校准后自动识别新购流量包，按包 ID 去重并按购买时间加入总额度，无需每次购包修改 `anchor`。
- 已知额度归属的套餐按到期顺序扣量，到期／退款只失效其剩余部分，已耗尽旧包不再拉低新包的可用百分比。
- 正常整点汇总采用精简格式，显示统计窗口、账号、官方结算余量对照、流量包名称／ID／状态、估算余量、一小时原始播放流量与折算扣量、数据完整度及用量截至时间。
- 整点数据未齐时发送暂报，数据齐全后自动补发该窗口完整统计；回查期限内已完整窗口的用量发生修订时发送更正。
- 余量不足 50%、10% 时向正常通知机器人发送购买资源提醒。
- 获取数据失败、密钥／权限错误、数据过旧、基准未校准、通知失败等异常走异常机器人；持续异常按配置间隔提醒，恢复后通知。
- 采集周期、汇总周期、时区、阈值、机器人地址和账号均在 JSON 配置中修改，无需修改 Python 源码。
- 使用标准库，无第三方 Python 运行依赖。支持 Linux systemd 常驻部署，配置与状态独立于代码目录，更新代码时保留。

## 余量口径：必须先读

**输出是基于直播用量计算的估算余量，不是腾讯云提供的实时套餐余额。** 腾讯云套餐剩余量随次日账单结算更新，缩短查询套餐接口的间隔无法获得实时余额。

本项目以已核对的余额及用量起点建立基准，再按五分钟流量扣减。海外流量、标准直播和快直播有不同抵扣比例，不能用“套餐额度减全球播放流量”直接计算。

```text
估算剩余量 = 基准额度与新购额度按时间顺序加入，再扣除折算用量和失效的未用额度
安全可用量 = 估算剩余量 - 数据延迟及处置预留 - 历史未覆盖用量预留
```

首次部署请从套餐抵扣明细确认基准已经覆盖的用量范围。**基准用量起点不是购包时间，也不是读取余额的当前时间。** 未配置基准时仍保留近期用量并按整点汇总已取得的折算用量，但明确显示“未校准”，并发送异常提醒，不提供可靠余量、不触发假设健康的判断。

五分钟是采集周期和计费用量粒度，**不代表腾讯云保证五分钟内更新**。程序会显示数据截至时间并监测数据陈旧；是否满足实际五至十分钟的可见性目标，应在真实账号上验证。某个地区／业务类型没有返回数据时，不会自动补成零流量，而会标记缺失；只有接口明确返回的零值才算零消耗。所有异常均应结合日志检查，机器人本身不可用时，通知无法凭空送达。

**首次确认一次基准，后续普通新购套餐会自动加入，不再逐次要求修改 `anchor`。** 程序按购买时间、包 ID 和总额度登记，定时查询、重启、迟发现的新包及用量修订都不会重复加额度；每日变化的官方 `Left` 不会再次扣入本地台账。正常通道会发送新购包识别通知。

新购额度从购买后的完整五分钟段开始参与计算，购包前未覆盖的用量保留独立记录，不能据此认定新买的包已经追溯抵扣。安全余量会为这部分不确定扣减保留预留。

当前有明确额度归属的套餐按最早到期优先扣量，到期或退款只移除未使用部分；退款／冻结没有公开生效时间时，采用上一次正常观察时间作为保守边界。初始合并基准无法准确分配到多个旧包时，仍可自动加入新购包；但旧基准包发生到期／退款、已知包 ID 的购买时间／总量／有效期发生不明变化、包暂时消失或核对差异超限时，会发异常要求核查，避免猜测余额。一般的新购包无需人工重校准。

程序只识别已购买的套餐并进行计算，**不会自动购买套餐、充值、修改计费方式或中断播放**。

## 如何与官网小时流量核对

报告中的两个用量字段使用不同口径：

| 字段 | 含义 | 核对方式 |
| --- | --- | --- |
| 原始播放流量（MB／GB） | 已收到的 LVB、LEB 播放计费用量之和，尚未乘套餐抵扣倍率 | 与官网同一统计范围的 `Flux` 对照 |
| 流量包折算扣量 | 各业务类型、计费地区的原始流量分别乘抵扣倍率后相加 | 用于本地套餐余量估算，通常大于原始播放流量 |

程序仍按所配置的业务类型、计费地区分别采集并折算，精简通知不展开地区范围和各维度明细，不影响实际查询范围。如果配置启用了 `PUSH`，用量行另列原始推流（上行）用量，不会把它混入原始播放流量；折算总量包含所配置的上行统计范围，其实际是否计费仍受腾讯云上行计费条件约束。

正常通知保持以下结构，用量字段放在同一行。时区仍由 `reports.timezone` 控制，默认北京时间；通知不重复显示时区后缀、生成时间、正常状态、安全预留或预计可用时长。异常或未校准时另加一行状态说明，详细异常发送到异常机器人。

```text
【腾讯云直播流量整点汇总】
统计窗口：2026-10-30 13:00:00 ～ 2026-10-30 14:00:00
账号：直播主账号
官方结算余量：159.100 TB（数据滞后，仅供对照）
云直播流量包 200.000 TB（ID 321018）使用中
估算剩余：132.449 TB（66.22%）
一小时内用量：原始播放流量：57450.943180 MB（57.450943 GB）；流量包折算扣量：101.642 GB（101.642209 GB）；完整统计，12/12 段
用量截至：2026-10-30 14:00:00
说明：未到达的用量段不计为零；数据可能回补修订。
```

以上数字仅演示字段。完整统计补报与用量修订保留同一统计窗口，并提醒替换上一份用量、不要重复累加；其中余量是补报生成时的当前值。

腾讯云接口中 `Flux` 的单位是 **MB（字节）**，`Bandwidth` 是 **Mbps（比特每秒）**；不要因表头写成 `Mb` 就把 `Flux` 再除以 8。流量采用十进制换算，`1 GB = 1000 MB`。例如官网显示 `57,450.94318 MB`，原始流量就是 `57.45094318 GB`；**只有这些流量全部属于 LVB 亚太一区时**，才有 `57.45094318 × 1.7692 = 101.642208674056 GB` 的套餐折算扣量。其他地区或快直播应使用各自倍率。

`暂报（数据不完整），11/12 段` 表示一小时的 12 个五分钟段中，只有 11 段取得了**全部配置的业务类型 × 地区**数据。剩下一段可能完全没到，也可能只到了一部分维度；“已到达用量”会包含已返回的部分维度，因此不能把该值直接说成完整一小时或恰好 55 分钟的用量。存在部分到达段时会显示其段数；缺失数据不会当成零。

整点时数据尚未齐全的报告标为**暂报**。该窗口所有段到齐后，会自动补发一次**完整统计补报**；此前部分数据继续到达而仍未完整时，不逐次刷屏。已经完整的窗口若发生原始用量修订，会在跟踪期限内自动发送修订通知。完整统计仍可能被腾讯云后续修订，不等于最终账单。

同一窗口采用左闭右开范围，例如 `13:00 ～ 14:00` 汇总 `13:00` 至 `13:55` 开始的 12 段，不将 `14:00` 开始的下一段加进来。补报中的窗口用量属于该历史小时，**估算余量是报告生成时的当前值**，不是历史整点余额。安全预留和预计可用时长仍用于计算与风险告警，仅从正常汇总中省略。

与官网核对时，应保持相同时间窗口、时区、业务类型、计费地区和域名范围，并等双方数据更新。本项目按配置的全部域名采集，不能直接与官网某个域名的筛选结果比较；不同时间读取的接口数据也可能因回补而变化，不能仅凭两个汇总数承诺完全一致。

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

启动后立即采集一次，然后按采集周期对齐时钟边界。全新安装的第一份定时汇总在下一个汇总边界发送；保留状态后重启，会补发最近一个尚未发送的汇总时段，不会把停机期间所有小时逐条刷屏。

每个已报告窗口在结束后的 `max(lookback_minutes × 60, interval_seconds)` 秒内跟踪迟到数据和修订，默认两小时。整点暂报后的完整统计补报、完整窗口的修订通知均走正常机器人，发送记录持久化保存，重启不会无故重复补报。超出跟踪期限的窗口不继续补报；这不代表缺失用量已经变成零。改变账号统计范围、校准基准、汇总周期或时区后，旧窗口不会混入新范围继续补报。更新代码时请保留状态文件。

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
- `anchor` 只描述起点时已经存在的额度，不能包含该起点之后才购买的包；后续包由程序自动登记。先前如果把新包手动加进了旧起点的余额，请核对一次，避免重复计入。
- 首次确认或特殊情况重新校准时同时修改余额、起点以及 `anchor.revision`，如 `settled-20261030-1`。普通新购包无需修改它；程序会自动登记并重新进入相应的告警周期。
- 百分比按当前仍可用且尚未耗尽的包总额度计算，已耗尽旧包不长期累积在分母中。`anchor.total_tb` 是初始额度，不再手动累加后续购包。

从 `1.0.2` 更新时保留现有配置和状态文件。程序会补齐购包登记，能明确识别为基准之后的新购包会自动迁移并继续计算；无法还原的历史额度调整仍会提示核查。不要删除状态文件，否则会丢失已识别的退款、历史通知等记录。
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
- [直播数据结构：Flux 单位为 MB，Bandwidth 单位为 Mbps](https://www.tencentcloud.com/document/api/267/30767)
- [播放监控数据：一分钟粒度及约四分钟延迟](https://www.tencentcloud.com/document/product/267/37297)
- [预付费套餐：海外倍率、抵扣顺序与次日结算](https://www.tencentcloud.com/document/product/267/52220)
- [直播计费说明：套餐用量更新滞后与超量按量计费](https://www.tencentcloud.com/document/product/267/32479)
- [Lark 自定义机器人](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)
