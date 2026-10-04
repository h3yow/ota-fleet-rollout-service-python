# OTA 固件分阶段发布与设备回滚服务

基于 FastAPI + 本地 JSON 存储的灰度发布服务，模拟设备、固件版本与健康上报，
支持**设备主动拉取升级指令、指令有效期超时自愈、上报准入与每批次在途规模控制**，
保证灰度推进、状态流转与回滚在并发与重试下结论一致、原因可区分。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务（127.0.0.1:8000）
uv run pytest -q -o log_cli=true --log-cli-level=INFO   # 运行测试并查看状态流转日志
```

## 发布状态机

设备状态（`ota.models.DeviceState`）：

```
IDLE --(发布创建, 分配批次)--> PENDING --(批次放行, 在途上限内)--> DELIVERED（在途）
                                   └--> QUEUED（批次已放行，但在途名额已满，排队）
DELIVERED --(健康上报达标)--> REPORTED_OK
DELIVERED --(健康检查失败)--> FAILED --(回滚)--> ROLLED_BACK
DELIVERED --(指令有效期内无有效上报, 自动收尾)--> ROLLED_BACK（直接回退到最近可用版本）
```

- `QUEUED`：设备所在批次已放行，但该批次在途（已放行指令但尚无定论）设备数达到
  `max_in_flight_per_batch`，排队等待；有名额空出（设备定论 / 回滚 / 收尾）或推进时
  按 device_id 顺序补发。
- `REPORTED_OK`：设备升级到目标版本，该版本成为新的 `last_good_version`。
- `ROLLED_BACK`：设备回退到 `last_good_version`（最近可用版本）。手动回滚与超时
  自动收尾都进入该终态，重复触发不会二次回退。
- `PENDING` 设备（批次尚未放行）不允许回滚，也不会被强制推进。

发布（Campaign）状态：`active → done`。当前批次样本数 ≥ `min_samples`
且健康比例 ≥ `min_health_ratio` 时推进到下一批次，否则拒绝推进并记录原因
（样本不足 / 健康率不足）；某批次经哈希分桶后恰好没有成员时直接跳过该批次。
所有批次完成后发布置为 `done`。

## 设备主动取指令

`GET /devices/{device_id}/commands?campaign_id=...`（`campaign_id` 可省略，
默认取设备当前归属的发布），返回 `status` 与 `command`：

| status | 含义 |
|---|---|
| `issued` | 本次首次放行一条指令（随响应体返回指令） |
| `in_flight` | 之前已取走同一条指令，反复询问始终返回它，不重新下发 |
| `queued` | 批次已放行，但在途名额已满，排队等待 |
| `batch_not_released` | 已纳入发布，但自己的批次还没轮到 |
| `not_in_campaign` | 设备未纳入该发布 / 发布不存在 / 设备未注册 / 已有定论 |

- **指令稳定**：`command_id = cmd:{campaign_id}:{device_id}`，同一设备在同一发布中
  无论询问多少次，拿到的指令 ID、目标版本、放行时间、有效期截止时间完全一致。
- 指令体包含 `command_id / campaign_id / device_id / version / issued_at / deadline`。
- 未轮到批次、未纳入发布、排队三种结果彼此可区分，不会和正常下发混淆。

## 指令有效期与自动收尾

- 创建发布时可指定 `command_ttl_seconds`（默认 300 秒）。指令放行时记录
  `issued_at` 与绝对截止时间 `deadline`，并随存储持久化。
- 在途设备在 `deadline` 前没有给出**有效健康上报**，即被判失败并自动回退到
  `last_good_version`（状态直接到 `ROLLED_BACK`）。
- 自动收尾是幂等的可重复操作：
  - 可显式调用 `POST /campaigns/{id}/sweep`；
  - 也会在设备**取指令**与**提交上报**时顺带触发，做到无人盯守也能自愈；
  - 只处理 `DELIVERED` 且 `deadline` 已过的设备，重复触发不重复回退；
  - 已 `REPORTED_OK` 的设备不受影响；
  - 判定基于持久化的 `deadline`，**服务重启后按同一规则继续生效**；
  - 收尾空出的在途名额立即补给同批次排队设备。
- 收尾后迟到的健康上报会被拒绝（`not_awaiting`），不改变回滚结论。

## 上报准入与在途规模控制

只有同时满足下列条件的上报才被接受：

1. 设备已注册且属于该发布（否则 `unknown_device` / `not_in_campaign`）；
2. **设备所在批次已经放行**（`batch_index <= current_batch`），
   批次未到时提前上报返回 `batch_not_released`——拒绝不消费幂等键、不推进序号，
   等批次放行后用同样的 key/seq 重新上报仍然有效，正式轮次结果不受影响；
3. 设备不在批次排队队列中（排队设备上报返回 `queued_for_slot`）；
4. 设备当前持有在途指令（`DELIVERED` 且有 `command_id`），已上报/已失败/已回滚/
   已收尾返回 `not_awaiting`；
5. 幂等键未处理过（`duplicate`）、序号严格递增（`stale_report`）。

在途规模：

- 每批次同时在途（已放行指令但还没定论）的设备数绝不超过
  `max_in_flight_per_batch`（创建发布时指定，默认极大值，等价于不限）。
- 批次放行瞬间只在上限内发指令，其余设备进入 `QUEUED`；
  任何设备定论（成功/失败/回滚/超时收尾）空出名额后，按 device_id 顺序补发。
- 所有判定与状态变更都在同一把全局锁内完成并原子落盘，并发取指令、并发上报、
  并发收尾混用也不会超发、不会重复回退、不会重复计数。

## 幂等键与批次划分规则

- **幂等键**：每条上报携带 `idempotency_key`，已处理的键持久化在存储中，
  重复上报返回 `duplicate`，不二次改变设备状态；重启后仍然有效。
  （准入类拒绝——批次未放行/排队/不在等待中——不消费幂等键。）
- **断点续传**：设备记录已接受的最大上报序号 `last_report_seq`，
  `seq <= last_report_seq` 的乱序/过期上报返回 `stale_report`。
- **批次划分**：对 `sha256(campaign_id:device_id)` 取哈希值映射到 [0,1)，
  按各批次累计比例落桶。同一设备在同一发布中无论何时计算都归属同一批次，
  并发上报不会导致重复计数、跳批或误推进。

## 上报拒绝原因（可区分）

| 原因 | 含义 |
|---|---|
| `duplicate` | 幂等键重复 |
| `stale_report` | 序号过期/乱序 |
| `unknown_device` | 设备未注册 |
| `not_in_campaign` | 设备不在该发布中 |
| `downgrade` | 目标版本不高于当前版本，拒绝降级 |
| `incompatible` | 设备型号不在固件兼容列表中 |
| `batch_not_released` | 批次尚未放行，提前上报 |
| `queued_for_slot` | 批次已放行但在途名额满，设备还没拿到指令 |
| `not_awaiting` | 设备已上报/已失败/已回滚/已收尾，无在途指令 |

## 回滚条件与验证方法

- **手动回滚**：设备处于 `FAILED`（健康检查失败）或 `DELIVERED`（调用方判定超时）
  状态时，`POST /devices/{id}/rollback` 将其回退到 `last_good_version`；
  其他状态拒绝回滚并记录日志。
- **自动回滚**：在途指令超过 `command_ttl_seconds` 未收到有效上报时，
  由 sweep（显式触发或取指令/上报时顺带触发）自动判失败并回退，规则见上。
- **验证方法**：
  - `tests/test_rollout.py` 覆盖稳定批次、推进门槛（样本/健康率）、
    幂等重复、乱序拒绝、重启后状态保留、16 线程并发重复上报只生效一次、
    8 线程并发推进不跳批、失败设备回滚、未放行设备不被强制推进、
    降级与不兼容拒绝（历史用例，无回归）。
  - `tests/test_pull_and_sweep.py` 覆盖本轮新增场景：
    - 反复取指令始终拿到同一条（`in_flight`/稳定 `command_id`）；
    - 未轮到批次 / 未纳入发布 / 在途满排队三类结果可区分；
    - 过时限后自动判失败并回退、重复收尾幂等、不误伤已上报设备、
      重启后继续按同一规则收尾；
    - 提前上报被拒（`batch_not_released`）且不影响批次放行后的正式上报；
    - 每批次在途上限：放行不超发、排队、空名额后按序补发；
    - 32 线程并发取同一条指令只放行一次、并发取指令/上报/收尾混合结论一致、
      在上限压力下多线程操作任意提交点都不超发。
  - `tests/test_api.py` 通过 HTTP 入口做端到端冒烟（含取指令、排队、
    提前上报拒绝、sweep 接口）。
  - 测试使用可注入的可控时钟（`FakeClock`）验证有效期；日志（`ota.service`
    logger）打印设备标识、campaign、批次判定依据、在途计数与状态流转。

## 兼容范围

- 存储向后兼容：旧版 JSON 存档缺少 `campaign_id / command_id /
  command_issued_at / command_deadline / command_ttl_seconds /
  max_in_flight_per_batch` 字段时按默认值加载，老数据可直接重启使用。
- `start_campaign` 与 `POST /campaigns` 新增的 `command_ttl_seconds`、
  `max_in_flight_per_batch` 均有默认值；不传参时行为近似旧版
  （不限在途规模、有效期 300 秒），并额外获得取指令与自动收尾能力。
- 新增 `QUEUED` 设备状态与 `issued / in_flight / queued / batch_not_released /
  not_in_campaign` 取指令结果、`batch_not_released / queued_for_slot /
  not_awaiting` 上报拒绝原因，均为新增枚举值，不改动旧枚举语义。

## 本地验证示例

```bash
uv run pytest -q -o log_cli=true --log-cli-level=INFO   # 全量单测 + 状态流转日志

# HTTP 手工验证
uv run python main.py &
curl -s -X POST localhost:8000/firmwares -H 'Content-Type: application/json' \
  -d '{"version":"2.0.0","models":["model-a"]}'
curl -s -X POST localhost:8000/devices -H 'Content-Type: application/json' \
  -d '{"device_id":"d0","model":"model-a","version":"1.0.0"}'
curl -s -X POST localhost:8000/campaigns -H 'Content-Type: application/json' \
  -d '{"campaign_id":"c1","version":"2.0.0","batch_ratios":[1.0],
       "command_ttl_seconds":60,"max_in_flight_per_batch":100}'
curl -s localhost:8000/devices/d0/commands        # 首次 issued，再次 in_flight
curl -s -X POST localhost:8000/campaigns/c1/sweep # 超时自动收尾，可重复调用
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 注册设备 |
| POST | `/firmwares` | 注册固件（版本 + 兼容型号） |
| POST | `/campaigns` | 创建发布（含 ttl、在途上限），返回被拒绝设备及原因 |
| GET | `/devices/{id}/commands` | 设备主动取当前指令（反复取同一条，结果可区分） |
| POST | `/reports` | 健康上报（幂等 + 批次/在途准入） |
| POST | `/campaigns/{id}/advance` | 尝试推进到下一批次 |
| POST | `/campaigns/{id}/sweep` | 触发超时自动收尾，返回本次回退设备列表 |
| POST | `/devices/{id}/rollback` | 手动回滚设备 |
| GET | `/devices/{id}` | 查询设备状态（含批次、指令与有效期） |
