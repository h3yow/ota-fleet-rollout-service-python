# OTA 灰度发布与设备回滚服务

本地存储模拟设备、固件版本与健康上报，提供按批次比例推进的灰度发布、
幂等上报与指令投递、失败回滚兜底。HTTP 入口见 `main.py`，核心逻辑在
`ota/service.py`，持久化在 `ota/store.py`（JSON 文件，tmp+rename 原子写）。

## 发布状态机

### 设备状态

```
UNKNOWN ──(从未上报，不得强制推进)
IDLE ──落入当前批次──> QUEUED ──拉取指令──> UPDATING ──健康上报──> HEALTHY
                                              │
                                              ├─健康失败/超时─> FAILED ──> ROLLED_BACK
                                              └─(回退到最近可用版本)
```

- `QUEUED → UPDATING`：设备拉取升级指令时记录 `previous_version` 与投递时间。
- 健康上报失败或超时清扫（`sweep_timeouts`）都会回退到 `previous_version`。

### 发布（rollout）状态

```
PENDING → IN_PROGRESS ──健康达标且样本足够──> (下一批次) … → COMPLETED
                 │                                       
                 ├─样本不足或健康率不达标──> PAUSED（可恢复推进）
                 └─整体回滚──> ROLLED_BACK（不再接受上报）
```

## 幂等键与批次划分规则

- **幂等键**：每条上报的 `report_id` 全局唯一，已处理的键直接拒绝
  （`duplicate`），不二次改变设备状态；`seq` 为设备侧单调序号，
  乱序/过期上报拒绝为 `stale_seq`。
- **指令投递幂等**：`UPDATING` 状态下重复拉取返回同一指令，重启后从
  磁盘恢复继续投递，不丢不重。
- **批次划分**：`bucket = SHA256(device_id)[:8] mod 100`，纯函数无状态，
  同一设备稳定归属同一批次；`batch_ratios` 为严格递增的累计比例
  （如 `[0.1, 0.5, 1.0]`），`bucket < ratio*100` 即落入该批次。
- **推进门控**：当前批次上报样本数 ≥ `min_samples` 且健康率 ≥
  `min_health_rate` 才推进，否则暂停。所有状态变更在同一把锁内完成，
  并发上报/推进串行化，不会重复计数、跳批或误推进。

## 回滚条件

| 触发 | 行为 |
| --- | --- |
| 健康上报 `healthy=false` | 设备立即回退到 `previous_version`，置 `ROLLED_BACK` |
| 上报超时（`sweep_timeouts`） | `UPDATING` 超过 `report_timeout_s` 判失败并回退 |
| 手动单设备回滚 | `POST /devices/{id}/rollback` |
| 整体回滚 | `POST /rollouts/{id}/rollback`，所有已更新设备回退，发布关闭 |
| 版本降级 / 硬件不兼容 | 上报被明确拒绝（`downgrade` / `incompatible`），不静默接受 |
| 从未上报（`UNKNOWN`）设备 | 不投递指令、不强制推进 |

被拒绝的上报通过 HTTP 409 返回可区分原因：`duplicate`、`stale_seq`、
`unknown_device`、`unknown_rollout`、`rollout_closed`、`not_in_batch`、
`incompatible`、`downgrade`。

## 验证方法

```bash
uv run pytest -v          # 17 个用例，含并发推进与重复上报
```

测试日志（`log_cli` 已开启）会打印设备标识、批次判定依据
（bucket/batch）与状态流转，例如：

```
INFO report accepted device=d1 rollout=r1 seq=1 bucket=66 batch=0 healthy=True state=updating->healthy
INFO advance check rollout=r1 batch=0 samples=2/2 health=1.00/1.00
INFO rollback device=d2 reason=health_check_failed version=1.0.0 state=failed->rolled_back
```

关键用例：

- `test_concurrent_advance_never_skips_batch`：16 线程并发推进，批次恰好 +1。
- `test_concurrent_duplicate_reports_counted_once`：32 线程并发同一幂等键，仅接受一次。
- `test_restart_resumes_without_loss_or_duplicate`：重启后重复上报被拒、推进不丢不重。
- `test_timeout_sweep_rolls_back_stale_devices` / `test_unhealthy_report_triggers_rollback`：失败兜底回滚。

## HTTP 接口速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/devices` | 注册设备 |
| POST | `/rollouts` | 创建发布（批次比例、样本与健康率门控） |
| POST | `/rollouts/{rid}/commands/{did}` | 设备拉取升级指令（幂等） |
| POST | `/reports` | 健康上报（幂等键 + 序号） |
| POST | `/rollouts/{rid}/advance` | 尝试推进到下一批次 |
| POST | `/rollouts/{rid}/rollback` | 整体回滚 |
| POST | `/devices/{did}/rollback?rollout_id=` | 单设备回滚 |
| POST | `/rollouts/{rid}/sweep` | 超时清扫 |
| GET | `/rollouts/{rid}` / `/devices/{did}` | 查询状态 |

本地运行：`uv run python main.py`（存储路径由 `OTA_STORE_PATH` 指定，
默认 `ota_store.json`）。
