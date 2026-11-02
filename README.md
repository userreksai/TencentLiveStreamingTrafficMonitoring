# 腾讯云国际站直播流量监控

版本：**1.4.0**。面向腾讯云 CSS 标准直播／快直播，默认每 **5 分钟**采集一次，**北京时间整点后 5 分钟**发送上一完整小时的汇总；计算后的安全可用量低于 **50% / 10%** 时，每轮额度各级别最多提醒一次，并发送至余量告警专用群。通知中的流量统一使用 **TB**。小时汇总、余量告警、技术异常和每日报表使用各自的 Lark／飞书机器人配置；每日报表默认通知时间为 **北京时间 10:05**。

## 能得到什么

- 按直播类型、计费地区读取五分钟流量并换算套餐抵扣量，保存本地估算台账。
- 同一时间段重复获取时替换修订值，避免重复扣量。
- 首次校准后自动识别新购流量包，按包 ID 去重并按购买时间加入总额度，无需每次购包修改 `anchor`。
- 已知额度归属的套餐按到期顺序扣量，到期／退款只失效其剩余部分，已耗尽旧包不再拉低新包的可用百分比。
- 正常汇总采用精简格式，显示统计窗口、流量包名称／ID／状态、流量包估算剩余、今日下行流量和一小时内流量包用量。
- 每次从官方接口读取今日累计下行总量，按当前报告时区从今日零点开始查询；该总量不再依赖本地逐段累加，也不重复扣入套餐台账。
- 可独立开启每日报表：到配置时间才查询官方最近七个完整自然日的流量及流量包使用情况，按购买时间倒序展示最新 5 个包，包含已耗尽包，并发送至专用群。
- 默认整点后等待五分钟，且只有小时数据完整后才发送。默认同一窗口发送一次；后续回补仍修正台账，重复修订通知可按需开启。
- 余量不足 50%、10%、估算耗尽及预计可用时长不足时，向余量告警专用机器人发送提醒；同一轮额度内只提示一次，下降至更严重级别时再提示。
- 小时监控的获取数据失败、密钥／权限错误、数据过旧、基准未校准、通知失败等异常走原异常机器人；持续异常按配置间隔提醒，恢复后通知。日报异常使用日报专用群。
- 采集周期、汇总周期、时区、阈值、机器人地址和账号均在 JSON 配置中修改，无需修改 Python 源码。
- 使用标准库，无第三方 Python 运行依赖。支持 Linux systemd 常驻部署，配置与状态独立于代码目录，更新代码时保留。

## 余量口径：必须先读

**输出是基于直播用量计算的估算余量，不是腾讯云提供的实时套餐余额。** 腾讯云套餐剩余量随次日账单结算更新，缩短查询套餐接口的间隔无法获得实时余额。

本项目以已核对的余额及用量起点建立基准，再按五分钟流量扣减。海外流量、标准直播和快直播有不同抵扣比例，不能用“套餐额度减全球播放流量”直接计算。

```text
估算剩余量 = 基准额度与新购额度按时间顺序加入，再扣除折算用量和失效的未用额度
安全可用量 = 估算剩余量 - 数据延迟及处置预留 - 历史未覆盖用量预留
```

首次部署请从套餐抵扣明细确认基准已经覆盖的用量范围。**基准用量起点不是购包时间，也不是读取余额的当前时间。** 未配置基准时仍保留近期用量并定时汇总完整窗口的折算用量，但明确显示“未校准”，并发送异常提醒，不提供可靠余量、不触发假设健康的判断。

五分钟是采集周期和计费用量粒度，**不代表腾讯云保证五分钟内更新**。程序会监测数据陈旧；是否满足实际五至十分钟的可见性目标，应在真实账号上验证。某个地区／业务类型没有返回数据时，不会自动补成零流量，而会标记缺失；只有接口明确返回的零值才算零消耗。所有异常均应结合日志检查，机器人本身不可用时，通知无法凭空送达。

**首次确认一次基准，后续普通新购套餐会自动加入，不再逐次要求修改 `anchor`。** 程序按购买时间、包 ID 和总额度登记，定时查询、重启、迟发现的新包及用量修订都不会重复加额度；每日变化的官方 `Left` 不会再次扣入本地台账。正常通道会发送新购包识别通知。

新购额度从购买后的完整五分钟段开始参与计算，购包前未覆盖的用量保留独立记录，不能据此认定新买的包已经追溯抵扣。安全余量会为这部分不确定扣减保留预留。

当前有明确额度归属的套餐按最早到期优先扣量，到期或退款只移除未使用部分；退款／冻结没有公开生效时间时，采用上一次正常观察时间作为保守边界。初始合并基准无法准确分配到多个旧包时，仍可自动加入新购包；但旧基准包发生到期／退款、已知包 ID 的购买时间／总量／有效期发生不明变化、包暂时消失或核对差异超限时，会发异常要求核查，避免猜测余额。一般的新购包无需人工重校准。

程序只识别已购买的套餐并进行计算，**不会自动购买套餐、充值、修改计费方式或中断播放**。

## 汇总格式与流量口径

整点汇总与可选修订通知使用同一种精简格式。默认单账号时省略账号行；多账号时保留账号名称以区分各自数据。所有流量均为十进制 TB，容量与估算余额显示三位小数，用量显示六位小数。正常通知不展开官方滞后余额、安全预留、数据维度及折算明细；这些数据仍用于内部校验和风险告警。

```text
【腾讯云直播流量整点汇总】
统计窗口：2026-10-30 16:00:00 ～ 2026-10-30 17:00:00
云直播流量包 200.000 TB（ID 321018）使用中
流量包估算剩余：131.915 TB（65.96%）
今日下行流量：4.470000 TB
一小时内流量包用量：0.281864 TB
```

以上数值仅演示格式。时区由 `reports.timezone` 控制，默认北京时间。余额是**通知生成时的当前估算**，不是历史整点余额。若数据异常或基准未校准，余额会明确显示无法可靠计算，并通过异常机器人说明原因。

| 字段 | 统计范围与含义 |
| --- | --- |
| 今日下行流量 | 官方 `DescribeBillBandwidthAndFluxList` 返回的 `SumFlux`；查询报告时区的今天零点至本次采集时间，覆盖账号全部播放域名、全部计费地区、LVB 与 LEB。属于未乘抵扣倍率的累计播放流量，不含 `PUSH` 上行。 |
| 一小时内流量包用量 | 统计窗口内，各个配置的业务类型、计费地区的五分钟原始流量分别乘官方抵扣倍率，再相加。用于套餐台账扣量。若启用了 `PUSH`，该项也包含所配置上行用量的保守折算，其实际是否计费受腾讯云日级条件约束。 |

今日下行流量独立于 `accounts.services`、`accounts.regions` 的套餐监控范围，是与官网全局概览对照的累计值，**不会再次从估算余额中扣减**。例如在 17:05 发送 16:00～17:00 的小时报告时，今日下行流量取本次查询时的今日累计；小时流量包用量仍只统计 16:00～17:00。

接口原始 `SumFlux`、`Flux` 单位为 MB，程序按 `1 TB = 1,000,000 MB` 转换；`Bandwidth` 的 Mbps 是另一个指标。不同地区、标准直播和快直播的抵扣倍率不同，原始今日流量不能直接当作套餐扣量。查询失败时正常报告显示获取失败，详细错误进入异常机器人；成功返回的数据若截至时间落后超过 `max_data_age_seconds`，则显示数据滞后并发送异常，不继续展示旧数值。今日总量异常不会单独使健康的套餐台账失效；失败、缺失或昨日旧值不会当作今日零流量。

默认在整点后五分钟重新采集，等待窗口内所有配置维度均返回完整数据后才发送，避免“整点暂报，再过五分钟补报”。例如 16:00～17:00 的报告最早在 17:05 采集完成后发送；数据仍未齐全时继续等待回补，不提前报成完整小时。

发送还需要在延迟时间点之后完成一次新采集。默认采集周期为五分钟，因此通常在 17:05 处理；若将 `interval_seconds` 改为 `600`，且上一轮在 17:00 采集，报告会等到下一轮 17:10 采集完成后发送。`delay_minutes` 表示最短等待时间，不是强制使用旧数据准时发送的时间。

即使已取得 12/12 个五分钟段，腾讯云也可能继续修订这些段内的数值。延后五分钟不构成数据已最终结算的保证。因此默认 `revision_updates=false`，同一窗口首次完整报告发送后，后续修订只更新台账和下一次余额，不为该窗口重复推送；需要接收更正时可以开启该选项。

同一窗口采用左闭右开范围，例如 `16:00 ～ 17:00` 汇总 `16:00` 至 `16:55` 开始的 12 段，不将 `17:00` 开始的下一段加进来。与官网核对今日累计时，应保持相同账号、时区、域名及地区范围，并等双方数据更新；卡片显示值可能有舍入，且官方未承诺卡片与该公共接口读取时刻完全一致。

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
    "interval_minutes": 60,
    "delay_minutes": 5,
    "revision_updates": false
  }
}
```

- `interval_seconds=300`：每五分钟采集；改为 `600` 即十分钟。应使用 300 秒的整数倍。
- `lookback_minutes`：每次重新拉取近期窗口，回补窗口内的迟到／修订数据，按时间段去重。超出窗口的历史修订需提高回查范围或重新核对基准。
- `max_data_age_seconds=600`：最新用量数据落后超过十分钟时发送异常提醒，不能解释为零消耗。每次采集时检查，不构成腾讯云数据时延保证。
- `max_backfill_days`：首次或变更基准时允许回溯的天数上限。
- `interval_minutes=60`：统计窗口按北京时间整点小时对齐；不是“进程启动后累计一小时”。
- `delay_minutes=5`：每个窗口结束至少五分钟后重新采集并发送，范围为 0～1440 的整数分钟。改为 `10` 即在整点后十分钟处理上一小时。数据缺失时仍等待完整，不因到点而把缺失当成零。
- `revision_updates=false`：同一窗口首次完整报告之后，后续修订不重复推送；台账仍正常回补。设为 `true` 时，在回查期限内发生的修订会使用相同精简格式发送到正常机器人。
- `timezone` 可改为 `UTC+07:00` 等固定时差。汇总周期至少五分钟、为五分钟的整数倍，且应能整除一天的 1440 分钟。

程序会重新读取配置，通常数秒内生效。修改后可先执行 `--check` 检查 JSON 和参数；也可通过 `--restart` 重启后生效。

启动后立即采集一次，然后按采集周期对齐时钟边界。小时统计窗口与发送延迟独立：默认上一完整小时的报告在整点后五分钟采集完成后发送。保留状态后重启会继续处理最近未发送的窗口，不会无故重发已经报告的小时或把停机期间所有小时逐条刷屏。

每个窗口在结束后的 `max(lookback_minutes × 60, delay_minutes × 60 + interval_seconds)` 秒内等待迟到数据并跟踪修订，默认两小时。若到期仍未完整，异常机器人会报告该窗口未能完成汇总；缺失仍为未知，不会变成零。发送记录持久化保存，重启不会无故重复发送。改变账号统计范围、校准基准、汇总周期或时区后，旧窗口不会混入新范围；只修改发送延迟或修订通知开关不会重建套餐计算基准。更新代码时请保留状态文件。

从旧版更新时，配置中没有 `delay_minutes`、`revision_updates` 也会自动采用 `5`、`false`，无需重新填写密钥、机器人或 `anchor`。需要自定义时间时再向本机 `reports` 对象补充这两个字段即可。

### 小时汇总 余量告警和异常机器人地址

```json
"lark": {
  "normal_webhook": "",
  "error_webhook": "",
  "balance_webhook": "",
  "normal_sign_secret": "",
  "error_sign_secret": "",
  "balance_sign_secret": ""
}
```

| 配置 | 通知用途 |
| --- | --- |
| `lark.normal_webhook` | 整点小时汇总、新购流量包识别通知。 |
| `lark.balance_webhook` | 安全余量百分比不足、估算耗尽、预计可用时长不足提醒。 |
| `lark.error_webhook` | 账号密钥／权限异常、API 查询失败、数据延迟、配置及通知投递异常。 |
| `daily_report.webhook` | 独立每日报表及日报获取／投递异常。 |

- 各地址由你部署时填写。机器人启用签名校验时再填写对应的 `*_sign_secret`；余量告警群使用 `balance_sign_secret`，日报签名使用 `daily_report.sign_secret`。
- 旧配置没有 `balance_webhook` 时仍可启动，小时汇总和已启用的日报继续运行。余量业务告警暂不发送，异常群会提示补充配置；未配置期间不会消耗相应级别的提醒机会。余量告警不会回退到正常群。
- 在原有 `lark` 对象中补充 `balance_webhook` 和 `balance_sign_secret` 即可，支持热加载，无需调整账号或 `anchor`。
- 若启用关键词限制，请允许“腾讯云”；若启用 IP 白名单，需允许服务器的实际出口 IP。
- 正常或余量告警机器人投递失败，会保留消息重试，并通过异常机器人报告。余量告警投递失败不会改投正常群。异常机器人也不可用时只可写日志，需结合 `journalctl` 发现。

### 独立每日流量及流量包统计

在配置顶层增加 `daily_report` 对象。下面是默认配置；填写**日报专用群**的机器人地址，并将 `enabled` 改为 `true` 后启用：

```json
"daily_report": {
  "enabled": false,
  "time": "10:05",
  "webhook": "",
  "sign_secret": ""
}
```

- `time` 为 `HH:MM` 格式，固定使用**北京时间（UTC+08:00）**；默认每天 10:05，不受 `reports.timezone`、小时汇总周期和采集周期影响。
- `webhook`、`sign_secret` 仅用于日报群。启用机器人签名校验时填写 `sign_secret`，其余情况留空。日报及其获取／投递异常均不发送到原来的正常群或异常群。
- 旧配置没有 `daily_report` 时保持关闭。新增配置及后续修改支持热加载，原有 `lark` 配置和 `anchor` 无需调整。

日报在配置时间到达后单独请求腾讯云官方接口，内容包括：

| 内容 | 官方数据来源与范围 |
| --- | --- |
| 最近七天每日流量 | `DescribeBillBandwidthAndFluxList`，`Granularity=1440`，直接读取 `DataInfoList` 中的每天 `Time`、`Flux`；统计北京时间截至昨天的七个完整自然日，覆盖账号全部播放域名、全部计费地区、LVB 与 LEB，不含 `PUSH` 上行。缺失的日数据不会当作零。 |
| 流量包使用情况 | `DescribeLivePackageInfo` 的 `Total`、`Used`、`BuyTime`、`Status`；按购买时间倒序显示最新 5 个流量包，不足 5 个时全部显示，包含已耗尽、已过期等状态。表中的购买时间为北京时间。 |

日报流量显示两位小数的十进制 TB。每日下行流量不乘资源包抵扣倍率，也不使用本地小时消耗相加。流量包展示的是**官方已结算用量**，随次日计费账单更新，不能作为实时套餐余额。

每个账号每天生成一份正常日报，独立状态会记录当天进度，保留状态后重启不会重复发送。当日通知时间已过而服务刚恢复时，会补发当天一份；不会补发停机期间多天的旧日报。数据获取失败时每五分钟重试；已成功生成而发送失败时，重试同一份内容，不重新获取和生成。内容较长时可能拆分成多条群消息，仍属于同一份日报。

日报在独立后台线程中获取和投递，使用单独的状态与通知队列，文件名为 `storage.state_file` 后追加 `.daily.json`，例如 `/var/lib/tencent-traffic-monitor/state.json.daily.json`。该文件及相邻锁文件也应保留。日报不会写入小时监控的套餐台账，其请求超时、查询失败或群投递失败不会阻塞小时采集与通知。

可以独立预览日报，不触发小时数据采集，也不发送消息：

```bash
cd /usr/local/tencenttraffic
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --daily-report-now --dry-run
```

`--dry-run` 使用只读模式，不取得日报状态锁，因此可以在服务运行时预览。去掉 `--dry-run` 会实际获取数据并向日报群投递，且仍遵守当天去重。后台服务在整个运行期间持有日报状态锁，实际手动投递前需先停止服务，执行后再启动服务。

### 余量告警阈值和一次性提醒

```json
"alerts": {
  "remaining_percent": [50, 10],
  "repeat_seconds": 0,
  "error_repeat_seconds": 1800,
  "safety_seconds": 900,
  "safety_factor": "1.10",
  "reconciliation_tolerance_percent": "0.1",
  "runway_minutes": 60
}
```

百分比阈值比较的是计算结果中的**安全可用百分比 `safe_percent`**，为严格“小于”。安全可用量已经扣除了数据延迟及处置等预留，因此触发条件与报告中的估算剩余量可能略有差别。

| 默认条件 | 提醒行为 |
| --- | --- |
| 安全可用百分比低于 50% | 本轮额度首次达到时提醒一次；恰好 50% 不触发。 |
| 安全可用百分比低于 10% | 进入更严重级别时再提醒一次；恰好 10% 不触发。 |
| 估算剩余额度小于等于零 | 本轮首次估算耗尽时提醒一次。 |
| 预计安全可用时长低于 `runway_minutes`，默认 60 分钟 | 本轮首次达到时单独提醒一次；这是按近期速率估算的可用时长。 |

例如当前仍可用额度为 200 TB，安全可用量低于 100 TB 时进入 50% 级别，低于 20 TB 时进入 10% 级别。默认没有 40%、30%、20% 这样的中间百分比级别；可通过 `remaining_percent` 调整阈值。

同一轮额度内只升级至更严重的百分比／耗尽级别。如果首次观察时已低于 10%，只发送 10% 提醒，不补发较轻的 50% 提醒。余量因数据回补暂时上升、再次下降，或修改群地址、热加载配置、重启服务，都不会重新触发已经报告的级别。新购可用额度生效或重新校准计算基准后，开始新的提醒周期。预计可用时长不足的提醒也在本轮内最多一次。

`repeat_seconds` 保留为旧配置兼容字段，**不再控制业务余量告警重复发送**；即使旧配置仍为 `3600`，50% 等同一级别也不会每小时重发。`error_repeat_seconds` 仍控制技术异常的持续提醒间隔。一次性规则限制的是告警生成次数；群消息发送失败时，发送队列仍会重试同一条告警。

升级时请保留原状态文件。历史已报告的阈值继续去重，待发送的旧余量业务消息迁入余量专用通道并去重；不会再混入小时汇总的正常群。

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

今日下行总量复用同一查询权限，通过该接口的 `SumFlux` 字段单独读取，不依赖套餐已结算余额。该接口文档未承诺五分钟内更新；程序延后发送只能减少常见回补导致的重复通知，不能提高腾讯云数据源的实际更新速度。

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

从旧版本更新到 1.4.0 后，安装脚本会保留原配置，**不会自动填入新的余量告警群地址**。请在 `/etc/tencent-traffic-monitor/config.json` 的现有 `lark` 对象中加入 `balance_webhook`、`balance_sign_secret`，填写余量专用群的机器人地址，再执行 `sudo bash /usr/local/tencenttraffic/install.sh --check`；服务会自动重新加载有效配置。

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
# 余量告警专用群（需先填写 lark.balance_webhook）
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --test-notify balance
# 日报专用群（需配置有效的独立 webhook，无需开启 enabled）
sudo -u tencent-traffic-monitor .venv/bin/python -m traffic_monitor --config /etc/tencent-traffic-monitor/config.json --test-notify daily
```

离线测试与脚本语法检查：

```bash
python3 -m unittest discover -s tests -p 'test*.py' -v
bash -n install.sh
```

仓库的 GitHub Actions 会执行离线测试和 Bash 语法检查；不会使用真实密钥或真实机器人。离线验证不能代替真实账号的数据延迟、地区抵扣口径及跨出账周期校准验证。

## 官方参考

- [直播计费用量查询：五分钟粒度、业务类型与计费地区](https://www.tencentcloud.com/document/product/267/36098)
- [直播播放流量查询：天粒度 Granularity=1440](https://cloud.tencent.com/document/product/267/35971)
- [直播套餐包查询：总量、已用量、购买时间与状态](https://cloud.tencent.com/document/product/267/40942)
- [直播数据结构：Flux 单位为 MB，Bandwidth 单位为 Mbps](https://www.tencentcloud.com/document/api/267/30767)
- [播放监控数据：一分钟粒度及约四分钟延迟](https://www.tencentcloud.com/document/product/267/37297)
- [预付费套餐：海外倍率、抵扣顺序与次日结算](https://www.tencentcloud.com/document/product/267/52220)
- [直播计费说明：套餐用量更新滞后与超量按量计费](https://www.tencentcloud.com/document/product/267/32479)
- [Lark 自定义机器人](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)
