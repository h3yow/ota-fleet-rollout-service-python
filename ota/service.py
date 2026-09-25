"""发布推进、上报处理与回滚的核心逻辑。"""
from __future__ import annotations

import hashlib
import logging

from .models import (
    Campaign,
    Device,
    DeviceState,
    Firmware,
    HealthReport,
    RejectReason,
)
from .store import Store

logger = logging.getLogger("ota.service")


def _version_key(version: str) -> tuple[int, ...]:
    """把 '1.2.3' 形式的版本号转成可比较的元组。"""
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


class RolloutService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---------- 注册 ----------

    def register_device(self, device_id: str, model: str, version: str) -> Device:
        with self.store.lock:
            device = self.store.devices.get(device_id)
            if device is None:
                device = Device(
                    device_id=device_id,
                    model=model,
                    current_version=version,
                    last_good_version=version,
                )
                self.store.devices[device_id] = device
                logger.info("注册设备 device=%s model=%s version=%s",
                            device_id, model, version)
            self.store.save()
            return device

    def register_firmware(self, version: str, models: list[str]) -> Firmware:
        with self.store.lock:
            fw = Firmware(version=version, compatible_models=tuple(models))
            self.store.firmwares[version] = fw
            self.store.save()
            return fw

    # ---------- 发布 ----------

    def start_campaign(
        self,
        campaign_id: str,
        version: str,
        batch_ratios: list[float],
        min_samples: int = 3,
        min_health_ratio: float = 0.9,
    ) -> dict[str, str]:
        """创建发布并为设备分配稳定批次，返回 {device_id: 拒绝原因}。"""
        with self.store.lock:
            fw = self.store.firmwares.get(version)
            if fw is None:
                raise ValueError(f"固件不存在: {version}")
            campaign = Campaign(
                campaign_id=campaign_id,
                target=fw,
                batch_ratios=sorted(batch_ratios),
                min_samples=min_samples,
                min_health_ratio=min_health_ratio,
            )
            self.store.campaigns[campaign_id] = campaign
            rejected: dict[str, str] = {}
            for device in self.store.devices.values():
                reason = self._check_eligible(device, fw)
                if reason is not None:
                    rejected[device.device_id] = reason.value
                    logger.info(
                        "拒绝设备 device=%s reason=%s target=%s current=%s",
                        device.device_id, reason.value, version,
                        device.current_version,
                    )
                    continue
                device.batch_index = self.batch_of(device.device_id, campaign_id)
                device.state = DeviceState.PENDING
                logger.info(
                    "批次判定 device=%s campaign=%s batch=%d ratios=%s",
                    device.device_id, campaign_id, device.batch_index,
                    campaign.batch_ratios,
                )
            # 首批次立即投递指令
            for device in self.store.devices.values():
                if device.batch_index == 0 and device.state == DeviceState.PENDING:
                    device.state = DeviceState.DELIVERED
                    logger.info("指令投递 device=%s batch=0", device.device_id)
            self.store.save()
            return rejected

    def _check_eligible(self, device: Device, fw: Firmware) -> RejectReason | None:
        if device.model not in fw.compatible_models:
            return RejectReason.INCOMPATIBLE
        if _version_key(fw.version) <= _version_key(device.current_version):
            return RejectReason.DOWNGRADE
        return None

    def batch_of(self, device_id: str, campaign_id: str) -> int:
        """稳定批次划分：对 (campaign_id, device_id) 做哈希，按累计比例落桶。

        同一设备在同一发布中无论何时计算都归属同一批次。
        """
        campaign = self.store.campaigns[campaign_id]
        digest = hashlib.sha256(f"{campaign_id}:{device_id}".encode()).hexdigest()
        h = int(digest[:8], 16) / 0xFFFFFFFF
        cumulative = 0.0
        for index, ratio in enumerate(campaign.batch_ratios):
            cumulative += ratio
            if h < cumulative:
                return index
        return len(campaign.batch_ratios) - 1

    # ---------- 上报（幂等 + 断点续传） ----------

    def handle_report(self, report: HealthReport) -> tuple[bool, str]:
        """处理健康上报，返回 (是否接受, 原因)。重复/乱序/过期上报不二次生效。"""
        with self.store.lock:
            if report.idempotency_key in self.store.seen_idempotency_keys:
                return False, RejectReason.DUPLICATE.value
            device = self.store.devices.get(report.device_id)
            if device is None:
                return False, RejectReason.UNKNOWN_DEVICE.value
            campaign = self.store.campaigns.get(report.campaign_id)
            if campaign is None or device.batch_index < 0:
                return False, RejectReason.NOT_IN_CAMPAIGN.value
            if report.seq <= device.last_report_seq:
                return False, RejectReason.STALE_REPORT.value

            # 接受：记录幂等键与序号，推进状态机
            self.store.seen_idempotency_keys.add(report.idempotency_key)
            device.last_report_seq = report.seq
            old_state = device.state
            if report.healthy:
                device.state = DeviceState.REPORTED_OK
                device.current_version = campaign.target.version
                device.last_good_version = campaign.target.version
            else:
                device.state = DeviceState.FAILED
            logger.info(
                "状态流转 device=%s %s -> %s seq=%d healthy=%s",
                device.device_id, old_state.value, device.state.value,
                report.seq, report.healthy,
            )
            self.store.save()
            return True, "accepted"

    # ---------- 推进 ----------

    def maybe_advance(self, campaign_id: str) -> bool:
        """当前批次健康达标且样本足够时推进到下一批次。"""
        with self.store.lock:
            campaign = self.store.campaigns[campaign_id]
            if campaign.status != "active":
                return False
            members = [
                d for d in self.store.devices.values()
                if d.batch_index == campaign.current_batch
            ]
            ok = sum(1 for d in members if d.state == DeviceState.REPORTED_OK)
            failed = sum(1 for d in members if d.state in
                         (DeviceState.FAILED, DeviceState.ROLLED_BACK))
            samples = ok + failed
            if samples < campaign.min_samples:
                logger.info("推进被拒 campaign=%s 原因=样本不足 samples=%d 需要=%d",
                            campaign_id, samples, campaign.min_samples)
                return False
            if ok / samples < campaign.min_health_ratio:
                logger.info("推进被拒 campaign=%s 原因=健康率不足 ok=%d/%d",
                            campaign_id, ok, samples)
                return False

            campaign.current_batch += 1
            if campaign.current_batch >= len(campaign.batch_ratios):
                campaign.status = "done"
                logger.info("发布完成 campaign=%s", campaign_id)
            else:
                for d in self.store.devices.values():
                    if (d.batch_index == campaign.current_batch
                            and d.state == DeviceState.PENDING):
                        d.state = DeviceState.DELIVERED
                        logger.info("指令投递 device=%s batch=%d",
                                    d.device_id, d.batch_index)
            self.store.save()
            return True

    # ---------- 回滚 ----------

    def rollback_device(self, device_id: str, reason: str) -> bool:
        """健康检查失败或超时的设备回退到最近可用版本。"""
        with self.store.lock:
            device = self.store.devices.get(device_id)
            if device is None:
                return False
            if device.state not in (DeviceState.FAILED, DeviceState.DELIVERED):
                logger.info("回滚被拒 device=%s 状态=%s 不允许回滚",
                            device_id, device.state.value)
                return False
            old_state = device.state
            device.current_version = device.last_good_version
            device.state = DeviceState.ROLLED_BACK
            logger.info("状态流转 device=%s %s -> %s 回滚到=%s 原因=%s",
                        device_id, old_state.value, device.state.value,
                        device.current_version, reason)
            self.store.save()
            return True
