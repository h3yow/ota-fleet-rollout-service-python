# OTA 固件分阶段发布与设备回滚服务

基于 FastAPI + 本地 JSON 存储的灰度发布服务，模拟设备、固件版本与健康上报，
支持**设备主动拉取指令**、**指令有效期与无人值守自动收尾回退**、
**按批次在途规模控制**与**批次放行准入**，
保证灰度推进、状态流转与回滚在并发与重试下结论一致、原因可区分。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务（127.0.0.1:8000，含后台自动收尾线程）
uv run pytest -q -o log_cli=true --log-cli-level=INFO   # 运行测试并查看状态流转日志
```

## 发布状态机

设备状态（`ota.models.DeviceState`）：

```
IDLE --(发布创建, 分配批次)--> PENDING --(批次放行且有名额, 设备拉取/补发泵)--> DELIVERED
DELIVERED --(健康上报达标)--> REPORTED_OK
DELIVERED --(健康检查失败/超时未上报)--> FAILED / ROLLED_BACK(超时自动收尾一步到位)
FAILED --(手动回滚)--> ROLLED_BACK
```

- `PENDING`：已分配批次但**还没取走指令**（批次未放行，或批次在途已满正在排队）。
- `DELIVERED`：已取走指令、在途（尚未有定论）。指令带 `expires_at` 有效期。
- `REPORTED_OK`：设备升级到目标版本，该版本成为新的 `last_good_version`。
- `ROLLED_BACK`：设备回退到 `last_good_version`（最近可用版本）。
- `PENDING` 设备（从未取走指令/状态未知）不允许回滚，也不会被强制推进。

发布（Campaign）状态：`active → done`。当前批次样本数 ≥ `min_samples`
且健康比例 ≥ `min_health_ratio` 时推进到下一批次，否则拒绝推进并记录原因
（样本不足 / 健康率不足）。所有批次完成后发布置为 `done`。
推进前会先执行一次超时自动收尾。

## 设备主动拉取指令（pull 模型）

设备随时可调用 `GET /campaigns/{campaign_id}/devices/{device_id}/command`
查询自己当前应执行的指令，结果状态（`ota.models.FetchStatus`）明确可区分：

| status | 含义 | 是否带指令 |
|---|---|---|
| `issued` | 本次新下发（设备此前未持有指令且占用到在途名额） | 是 |
| `already_delivered` | 已有指令，反复拉取始终返回**同一条**，不重发、不续期 | 是 |
| `queued` | 批次已放行，但本批次在途已达上限，排队等待补发 | 否 |
| `batch_not_released` | 还没轮到该设备的批次（`device_batch > current_batch`） | 否 |
| `not_in_campaign` | 设备未注册进该发布（型号不兼容/降级被排除） | 否 |
| `unknown_device` | 设备未注册 | 否 |

- **同一台设备反复拉取永远是同一条指令**：指令 ID 由
  `sha256("cmd:{campaign}:{device}:{version}")` 稳定派生，
  `issued_at/expires_at` 随指令持久化，不因重复拉取而改变。
- 设备已有定论（成功/失败/已回滚）后再拉取，仍返回当初那条指令，保证幂等。
- 首批在创建发布时即按在途上限预投递，因此首批设备首次拉取通常直接得到
  `already_delivered`；这属于"投递可预发、拉取始终幂等"，不是重复下发。

## 指令有效期与自动收尾

- 创建发布时用 `command_ttl_seconds` 约定指令有效期（默认 3600 秒），
  设备取走指令时写入 `issued_at = 取走时刻`、`expires_at = issued_at + ttl`。
- 设备在 `expires_at` 前未给出**有效健康上报**即判超时：
  `DELIVERED -> ROLLED_BACK`，当前版本直接回退到各自的 `last_good_version`
  （失败与回退一步完成，无需人工先判失败再回滚）。
- 自动收尾由三种方式触发，规则完全相同、可反复触发：
  1. 后台守护线程（服务启动时自动拉起，默认每 5 秒扫描全部 active 发布）；
  2. 设备拉取/上报时顺带触发；
  3. 手动 `POST /campaigns/{id}/sweep`。
- **幂等且重启一致**：判定只依据设备上持久化的 `expires_at` 与当前状态；
  已正常上报（`REPORTED_OK`）、已失败/已回滚的设备不受影响；
  重复收尾不会对同一设备二次回滚。服务重启后重新加载状态即按同一规则继续。
- 收尾空出的在途名额会**立即按稳定顺序补发给同批次排队设备**（新指令有新的
  有效期）；因此时间持续推进时，排队设备可能逐台超时——这是级联补发，
  每台设备仍只回退一次。

## 在途规模控制（每批次上限，任何情况下不超发）

- 创建发布时用 `max_in_flight_per_batch` 设定每个批次同一时间的在途上限
  （在途 = 已取走指令、状态为 `DELIVERED` 的设备）。默认值极大（不限制），
  以兼容不关心该参数的旧调用方式。
- 批次放行（发布创建/推进）时通过"补发泵"投递：只给 `PENDING` 设备下发，
  按 `device_id` 稳定排序，数量 = `上限 - 当前在途`，超出的保持排队。
- 名额在设备**有定论**（健康上报成功/失败、超时回滚、手动回滚）后立即归还，
  补发泵随即补发给排队最前者。设备主动拉取时也会尝试占用名额，
  名额不足返回 `queued`。所有判定在同一把全局锁内完成，
  并发拉取/上报/收尾下在途数恒不超过上限。

## 上报准入规则（收紧后）

只有**批次已放行、设备已取走指令（DELIVERED）**后提交的有效上报才算数。
准入按下列顺序短路（前置的结构性门槛拒绝时不落任何状态，
因此不消耗幂等键、不推进 `seq`，不影响设备后面正式那一轮上报）：

1. `unknown_device`：设备未注册；
2. `not_in_campaign`：发布不存在或设备不在该发布中；
3. `batch_not_released`：设备批次 `> current_batch`，提前上报，明确拒绝；
4. `duplicate`：幂等键已处理（重启后仍生效）；
5. `stale_report`：`seq <= last_report_seq`，乱序/过期；
6. `command_not_delivered`：批次已放行但设备还在排队、未取走指令；
7. `terminal_state`：设备已有定论（成功/失败/已回滚），上报不再生效。

通过全部准入后才接受上报：健康达标转 `REPORTED_OK` 并更新当前版本与
`last_good_version`；不健康转 `FAILED`。

## 幂等键与批次划分规则

- **幂等键**：每条上报携带 `idempotency_key`，已处理的键持久化在存储中，
  重复上报返回 `duplicate`，不二次改变设备状态；重启后仍然有效。
- **断点续传**：设备记录已接受的最大上报序号 `last_report_seq`，
  `seq <= last_report_seq` 的乱序/过期上报返回 `stale_report`。
- **批次划分**：对 `sha256(campaign_id:device_id)` 取哈希值映射到 [0,1)，
  按各批次累计比例落桶。同一设备在同一发布中无论何时计算都归属同一批次，
  并发上报不会导致重复计数、跳批或误推进（所有状态变更在全局锁内完成并原子落盘）。

## 上报拒绝原因（可区分）

| 原因 | 含义 |
|---|---|
| `duplicate` | 幂等键重复 |
| `stale_report` | 序号过期/乱序 |
| `unknown_device` | 设备未注册 |
| `not_in_campaign` | 设备不在该发布中 |
| `downgrade` | 目标版本不高于当前版本，拒绝降级（建发布时排除） |
| `incompatible` | 设备型号不在固件兼容列表中（建发布时排除） |
| `batch_not_released` | 批次尚未放行就提前上报（或拉取） |
| `command_not_delivered` | 批次已放行但设备仍在排队、未取走指令 |
| `terminal_state` | 设备已有定论，上报不再生效 |

## 回滚条件与验证方法

- **自动回滚**：`DELIVERED` 设备超过指令有效期无有效上报，由收尾逻辑
  直接回退到 `last_good_version`（无需人工干预，可反复/重启后触发，不重复回退）。
- **手动回滚**：`POST /devices/{id}/rollback` 仍支持对 `FAILED`
  （健康检查失败）或 `DELIVERED`（人工提前判定超时）设备回退；
  其他状态拒绝回滚并记录日志。两种回滚都会归还在途名额并触发补发。
- **本地验证**：
  - `tests/test_rollout.py` 覆盖稳定批次、推进门槛（样本/健康率）、
    幂等重复、乱序拒绝、重启后状态保留、16 线程并发重复上报只生效一次、
    8 线程并发推进不跳批、失败设备回滚到最近可用版本、
    未上报设备不被强制推进、降级与不兼容拒绝（历史用例，保持无回归）。
  - `tests/test_pull.py` 覆盖新场景：反复拉取始终同一条指令、
    拉取结果四类状态可区分、在途上限（拉取/批次推进/上报后补发均不超发）、
    提前上报被拒且不影响后续正式上报、未取指令上报被拒、定论后上报被拒、
    到期自动回退、重复收尾不重复回退、正常上报设备不受收尾影响、
    收尾后补发排队设备、重启后规则继续生效、后台守护线程无人值守自动回退、
    以及并发拉取/并发在途竞争/上报与收尾混合/并发提前上报的一致性。
  - `tests/test_api.py` 通过 HTTP 入口做端到端冒烟，覆盖拉取与手动收尾接口。
  - 测试日志（`ota.service` logger）打印设备标识、批次判定依据
    （`device_batch` vs `current_batch`）、在途计数与状态流转。
  - 时钟可注入：`RolloutService(store, clock=callable)`，
    测试中用伪时钟精确控制有效期；生产默认使用 `time.time()`。

## 兼容范围

- 存储向后兼容：旧 JSON 状态文件缺少 `command_*`、`command_ttl_seconds`、
  `max_in_flight_per_batch` 字段时按默认值加载，不影响已有设备/发布。
- 接口向后兼容：`POST /campaigns` 的 `command_ttl_seconds`、
  `max_in_flight_per_batch` 均为可选（默认不限在途、TTL 3600s）；
  不传新参数时，批次放行会给该批次全部 `PENDING` 设备投递指令，
  行为与旧版本基本一致（区别仅在于指令改为带有效期的持久化实体）。
- 行为收紧点（需调用方配合）：批次未放行或设备未取走指令时的上报会被拒绝；
  设备应先通过拉取接口取得指令再升级、上报。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 注册设备 |
| POST | `/firmwares` | 注册固件（版本 + 兼容型号） |
| POST | `/campaigns` | 创建发布（可带 ttl、每批次在途上限），返回被拒绝设备及原因 |
| GET | `/campaigns/{id}/devices/{device_id}/command` | 设备主动拉取指令（幂等，状态可区分） |
| POST | `/reports` | 健康上报（幂等 + 批次/在途准入） |
| POST | `/campaigns/{id}/advance` | 尝试推进到下一批次 |
| POST | `/campaigns/{id}/sweep` | 手动触发一次超时自动收尾 |
| POST | `/devices/{id}/rollback` | 回滚设备 |
| GET | `/devices/{id}` | 查询设备状态（含指令与有效期信息） |
