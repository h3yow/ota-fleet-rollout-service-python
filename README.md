# OTA 固件分阶段发布与设备回滚服务

基于 FastAPI + 本地 JSON 存储的灰度发布服务，模拟设备、固件版本与健康上报，
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
IDLE --(发布创建, 分配批次)--> PENDING --(批次被激活, 指令投递)--> DELIVERED
DELIVERED --(健康上报达标)--> REPORTED_OK
DELIVERED --(健康检查失败/超时)--> FAILED --(回滚)--> ROLLED_BACK
```

- `REPORTED_OK`：设备升级到目标版本，该版本成为新的 `last_good_version`。
- `ROLLED_BACK`：设备回退到 `last_good_version`（最近可用版本）。
- `PENDING` 设备（从未上报/状态未知）不允许回滚，也不会被强制推进。

发布（Campaign）状态：`active → done`。当前批次样本数 ≥ `min_samples`
且健康比例 ≥ `min_health_ratio` 时推进到下一批次，否则拒绝推进并记录原因
（样本不足 / 健康率不足）。所有批次完成后发布置为 `done`。

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
| `downgrade` | 目标版本不高于当前版本，拒绝降级 |
| `incompatible` | 设备型号不在固件兼容列表中 |

## 回滚条件与验证方法

- **回滚条件**：设备处于 `FAILED`（健康检查失败）或 `DELIVERED`（超时未上报，
  由调用方判定后触发）状态时，`POST /devices/{id}/rollback` 将其回退到
  `last_good_version`；其他状态拒绝回滚并记录日志。
- **验证方法**：
  - `tests/test_rollout.py` 覆盖稳定批次、推进门槛（样本/健康率）、
    幂等重复、乱序拒绝、重启后状态保留、16 线程并发重复上报只生效一次、
    8 线程并发推进不跳批、失败设备回滚到最近可用版本、
    未上报设备不被强制推进、降级与不兼容拒绝。
  - `tests/test_api.py` 通过 HTTP 入口做端到端冒烟，验证各入口结论一致。
  - 测试日志（`ota.service` logger）打印设备标识、批次判定依据与状态流转。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 注册设备 |
| POST | `/firmwares` | 注册固件（版本 + 兼容型号） |
| POST | `/campaigns` | 创建发布，返回被拒绝设备及原因 |
| POST | `/reports` | 健康上报（幂等） |
| POST | `/campaigns/{id}/advance` | 尝试推进到下一批次 |
| POST | `/devices/{id}/rollback` | 回滚设备 |
| GET | `/devices/{id}` | 查询设备状态 |
