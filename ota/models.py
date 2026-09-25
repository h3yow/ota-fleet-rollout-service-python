"""类型定义：设备、固件、发布计划、健康上报与状态机。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DeviceState(enum.Enum):
    """设备在发布状态机中的状态。"""

    IDLE = "idle"                # 未纳入任何发布
    PENDING = "pending"          # 已分配批次，等待指令
    DELIVERED = "delivered"      # 升级指令已投递
    REPORTED_OK = "reported_ok"  # 健康上报达标
    FAILED = "failed"            # 健康检查失败或超时
    ROLLED_BACK = "rolled_back"  # 已回退到最近可用版本


class RejectReason(enum.Enum):
    """上报被拒绝时的可区分原因。"""

    DUPLICATE = "duplicate"              # 幂等键重复
    STALE_REPORT = "stale_report"        # 过期/乱序上报
    UNKNOWN_DEVICE = "unknown_device"    # 设备未注册
    NOT_IN_CAMPAIGN = "not_in_campaign"  # 设备不在当前发布中
    DOWNGRADE = "downgrade"              # 版本降级被拒绝
    INCOMPATIBLE = "incompatible"        # 固件与设备型号不兼容


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


@dataclass
class HealthReport:
    device_id: str
    campaign_id: str
    idempotency_key: str
    seq: int
    healthy: bool
