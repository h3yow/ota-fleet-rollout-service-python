"""类型定义：设备、固件、发布计划、健康上报与状态机。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DeviceState(enum.Enum):
    """设备在发布状态机中的状态。"""

    IDLE = "idle"                # 未纳入任何发布
    PENDING = "pending"          # 已分配批次，批次尚未放行
    QUEUED = "queued"            # 批次已放行，但在途名额已满，排队等待
    DELIVERED = "delivered"      # 升级指令已取走/下发，等待健康结果（在途）
    REPORTED_OK = "reported_ok"  # 健康上报达标
    FAILED = "failed"            # 健康检查失败
    ROLLED_BACK = "rolled_back"  # 已回退到最近可用版本（含超时自动收尾）


class RejectReason(enum.Enum):
    """上报被拒绝时的可区分原因。"""

    DUPLICATE = "duplicate"                  # 幂等键重复
    STALE_REPORT = "stale_report"            # 过期/乱序上报
    UNKNOWN_DEVICE = "unknown_device"        # 设备未注册
    NOT_IN_CAMPAIGN = "not_in_campaign"      # 设备不在当前发布中
    DOWNGRADE = "downgrade"                  # 版本降级被拒绝
    INCOMPATIBLE = "incompatible"            # 固件与设备型号不兼容
    BATCH_NOT_RELEASED = "batch_not_released"  # 批次尚未放行，提前上报
    QUEUED_FOR_SLOT = "queued_for_slot"      # 批次已放行但在途名额已满，还没拿到指令
    NOT_AWAITING = "not_awaiting"            # 设备当前不在等待健康结果的状态


class PullStatus(enum.Enum):
    """设备主动取指令时的可区分结果。"""

    ISSUED = "issued"            # 本次首次放行一条指令
    IN_FLIGHT = "in_flight"      # 之前已取走同一条指令，反复询问仍返回它
    QUEUED = "queued"            # 批次已放行，但在途名额已满，排队等待
    BATCH_NOT_RELEASED = "batch_not_released"  # 已纳入发布，但批次还没轮到
    NOT_IN_CAMPAIGN = "not_in_campaign"        # 设备不在该发布中（或无进行中的发布）


@dataclass(frozen=True)
class Firmware:
    version: str
    compatible_models: tuple[str, ...]


@dataclass
class Device:
    device_id: str
    model: str
    current_version: str
    state: DeviceState = DeviceState.IDLE
    batch_index: int = -1          # 稳定批次归属，-1 表示未分配
    last_report_seq: int = 0       # 已接受的最大上报序号（断点续传）
    last_good_version: str = ""    # 最近可用版本（回滚目标）
    campaign_id: str = ""          # 当前归属的发布
    command_id: str = ""           # 已放行指令的稳定标识，空表示尚未放行
    command_issued_at: float = 0.0  # 指令放行时刻（epoch 秒）
    command_deadline: float = 0.0   # 指令有效期截止时刻（epoch 秒）


@dataclass(frozen=True)
class UpgradeCommand:
    """放行给设备的升级指令；同一设备在同一发布中反复取到的始终是同一条。"""

    command_id: str
    campaign_id: str
    device_id: str
    version: str
    issued_at: float
    deadline: float


@dataclass(frozen=True)
class PullResult:
    """设备取指令的结果，状态可区分，非取到指令时 command 为 None。"""

    status: PullStatus
    command: UpgradeCommand | None = None


@dataclass
class Campaign:
    """一次灰度发布计划。"""

    campaign_id: str
    target: Firmware
    batch_ratios: list[float]      # 各批次目标比例，如 [0.1, 0.4, 1.0]
    min_samples: int = 3           # 每批次推进所需最小健康样本数
    min_health_ratio: float = 0.9  # 推进所需健康比例
    current_batch: int = 0
    status: str = "active"         # active / paused / done
    command_ttl_seconds: float = 300.0       # 单条指令的有效期
    max_in_flight_per_batch: int = 2_000_000_000  # 每批次同时在途设备上限


@dataclass
class HealthReport:
    device_id: str
    campaign_id: str
    idempotency_key: str
    seq: int
    healthy: bool
