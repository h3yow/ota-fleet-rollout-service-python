"""类型定义：设备、固件、发布批次与上报的领域模型。

本文件只包含数据结构与枚举，不包含业务逻辑。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any


class DeviceState(str, enum.Enum):
    """设备在发布状态机中的状态。"""

    UNKNOWN = "unknown"            # 从未上报，不得被强制推进
    IDLE = "idle"                  # 已注册，尚未进入发布
    QUEUED = "queued"              # 已落入当前批次，等待拉取指令
    UPDATING = "updating"          # 已投递升级指令
    HEALTHY = "healthy"            # 上报健康，达标
    FAILED = "failed"              # 健康检查失败或超时
    ROLLED_BACK = "rolled_back"    # 已回退到最近可用版本


class RolloutState(str, enum.Enum):
    """发布（rollout）整体状态机。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"              # 健康不达标或样本不足时暂停
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


class RejectReason(str, enum.Enum):
    """上报被拒绝时的可区分原因。"""

    DUPLICATE = "duplicate"                # 幂等键已处理
    STALE_SEQ = "stale_seq"                # 乱序/过期序号
    UNKNOWN_DEVICE = "unknown_device"
    UNKNOWN_ROLLOUT = "unknown_rollout"
    ROLLOUT_CLOSED = "rollout_closed"      # 发布已完成/已回滚
    NOT_IN_BATCH = "not_in_batch"          # 设备尚未落入当前批次
    INCOMPATIBLE = "incompatible"          # 固件与硬件不兼容
    DOWNGRADE = "downgrade"                # 版本降级被拒绝


@dataclass
class Firmware:
    version: str
    compatible_hardware: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Firmware":
        return Firmware(version=d["version"], compatible_hardware=list(d["compatible_hardware"]))


@dataclass
class Device:
    device_id: str
    hardware: str
    current_version: str
    previous_version: str | None = None
    state: DeviceState = DeviceState.IDLE
    last_report_id: str | None = None
    last_seq: int = 0
    last_report_at: float | None = None
    command_at: float | None = None  # 最近一次升级指令投递时间，用于超时判定

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Device":
        return Device(
            device_id=d["device_id"],
            hardware=d["hardware"],
            current_version=d["current_version"],
            previous_version=d.get("previous_version"),
            state=DeviceState(d.get("state", "idle")),
            last_report_id=d.get("last_report_id"),
            last_seq=d.get("last_seq", 0),
            last_report_at=d.get("last_report_at"),
            command_at=d.get("command_at"),
        )


@dataclass
class Rollout:
    rollout_id: str
    target: Firmware
    batch_ratios: list[float]          # 各批次累计比例，如 [0.1, 0.5, 1.0]
    current_batch: int = 0
    state: RolloutState = RolloutState.PENDING
    min_samples: int = 1               # 推进所需最小样本数
    min_health_rate: float = 1.0       # 推进所需最小健康率
    report_timeout_s: float = 300.0    # 设备上报超时，超时判定失败
    processed_report_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollout_id": self.rollout_id,
            "target": self.target.to_dict(),
            "batch_ratios": list(self.batch_ratios),
            "current_batch": self.current_batch,
            "state": self.state.value,
            "min_samples": self.min_samples,
            "min_health_rate": self.min_health_rate,
            "report_timeout_s": self.report_timeout_s,
            "processed_report_ids": sorted(self.processed_report_ids),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Rollout":
        return Rollout(
            rollout_id=d["rollout_id"],
            target=Firmware.from_dict(d["target"]),
            batch_ratios=list(d["batch_ratios"]),
            current_batch=d.get("current_batch", 0),
            state=RolloutState(d.get("state", "pending")),
            min_samples=d.get("min_samples", 1),
            min_health_rate=d.get("min_health_rate", 1.0),
            report_timeout_s=d.get("report_timeout_s", 300.0),
            processed_report_ids=set(d.get("processed_report_ids", [])),
        )


@dataclass
class Report:
    """设备健康/进度上报。report_id 为幂等键，seq 为设备侧单调序号。"""

    report_id: str
    rollout_id: str
    device_id: str
    seq: int
    healthy: bool
    version: str
    ts: float


@dataclass
class ReportResult:
    accepted: bool
    reason: RejectReason | None = None
    device_state: DeviceState | None = None


def parse_version(v: str) -> tuple[int, ...]:
    """将 '1.2.3' 形式的版本解析为可比较的元组。"""
    parts: list[int] = []
    for p in v.split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)
