"""发布推进、设备取指令、上报准入、在途控制、超时自动收尾与回滚的核心逻辑。"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable

from .models import (
    Campaign,
    Device,
    DeviceState,
    Firmware,
    HealthReport,
    PullResult,
    PullStatus,
    RejectReason,
    UpgradeCommand,
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
    def __init__(
        self,
        store: Store,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        # 默认挂钟；测试可注入可控时钟以验证有效期与自动收尾。
        self._clock: Callable[[], float] = clock or time.time

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
        command_ttl_seconds: float = 300.0,
        max_in_flight_per_batch: int = 2_000_000_000,
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
                command_ttl_seconds=command_ttl_seconds,
                max_in_flight_per_batch=max_in_flight_per_batch,
            )
            self.store.campaigns[campaign_id] = campaign
            rejected: dict[str, str] = {}
            for device in self.store.devices.values():
                reason = self._check_eligible(device, fw)
                if reason is not None:
                    rejected[device.device_id] = reason.value
                    device.batch_index = -1
                    device.campaign_id = ""
                    logger.info(
                        "拒绝设备 device=%s reason=%s target=%s current=%s",
                        device.device_id, reason.value, version,
                        device.current_version,
                    )
                    continue
                device.batch_index = self.batch_of(device.device_id, campaign_id)
                device.campaign_id = campaign_id
                device.command_id = ""
                device.command_issued_at = 0.0
                device.command_deadline = 0.0
                device.last_report_seq = 0
                device.state = DeviceState.PENDING
                logger.info(
                    "批次判定 device=%s campaign=%s batch=%d ratios=%s",
                    device.device_id, campaign_id, device.batch_index,
                    campaign.batch_ratios,
                )
            # 首批次立即放行，受在途上限控制
            self._release_batch(campaign, 0)
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

    # ---------- 指令放行与设备自取 ----------

    def _command_id(self, device: Device) -> str:
        """稳定指令标识：同一设备在同一发布中反复取到的指令 ID 恒定。"""
        return f"cmd:{device.campaign_id}:{device.device_id}"

    def _command_of(self, device: Device) -> UpgradeCommand:
        return UpgradeCommand(
            command_id=device.command_id,
            campaign_id=device.campaign_id,
            device_id=device.device_id,
            version=self.store.campaigns[device.campaign_id].target.version,
            issued_at=device.command_issued_at,
            deadline=device.command_deadline,
        )

    def _issue_command(self, device: Device, campaign: Campaign) -> None:
        """放行一条有有效期的指令。只在设备尚无在途指令时调用。"""
        now = self._clock()
        device.command_id = self._command_id(device)
        device.command_issued_at = now
        device.command_deadline = now + campaign.command_ttl_seconds
        device.state = DeviceState.DELIVERED
        logger.info(
            "指令放行 device=%s campaign=%s batch=%d command=%s 有效期截止=%.3f",
            device.device_id, campaign.campaign_id, device.batch_index,
            device.command_id, device.command_deadline,
        )

    def _in_flight(self, campaign: Campaign, batch_index: int) -> int:
        """统计某批次当前在途（已放行指令但尚无定论）设备数。"""
        return sum(
            1 for d in self.store.devices.values()
            if d.campaign_id == campaign.campaign_id
            and d.batch_index == batch_index
            and d.state == DeviceState.DELIVERED
        )

    def _release_batch(self, campaign: Campaign, batch_index: int) -> int:
        """放行一个批次：在在途上限内放行指令，超出部分排队。返回放行数量。"""
        members = sorted(
            (
                d for d in self.store.devices.values()
                if d.campaign_id == campaign.campaign_id
                and d.batch_index == batch_index
                and d.state == DeviceState.PENDING
            ),
            key=lambda d: d.device_id,
        )
        released = 0
        for device in members:
            if self._in_flight(campaign, batch_index) >= campaign.max_in_flight_per_batch:
                device.state = DeviceState.QUEUED
                logger.info(
                    "排队等待 device=%s campaign=%s batch=%d 在途已达上限=%d",
                    device.device_id, campaign.campaign_id, batch_index,
                    campaign.max_in_flight_per_batch,
                )
                continue
            self._issue_command(device, campaign)
            released += 1
        logger.info(
            "批次放行 campaign=%s batch=%d 放行=%d 排队=%d",
            campaign.campaign_id, batch_index, released,
            sum(1 for d in members if d.state == DeviceState.QUEUED),
        )
        return released

    def _fill_slots(self, campaign: Campaign, batch_index: int) -> None:
        """有名额空出或批次推进后，为仍在排队的同批次设备补发指令。"""
        waiters = sorted(
            (
                d for d in self.store.devices.values()
                if d.campaign_id == campaign.campaign_id
                and d.batch_index == batch_index
                and d.state == DeviceState.QUEUED
            ),
            key=lambda d: d.device_id,
        )
        for device in waiters:
            if self._in_flight(campaign, batch_index) >= campaign.max_in_flight_per_batch:
                return
            self._issue_command(device, campaign)

    def pull_command(
        self,
        device_id: str,
        campaign_id: str | None = None,
    ) -> PullResult:
        """设备主动取自己当前应执行的指令。

        - 反复询问始终返回同一条指令（command_id 稳定，不重复下发）；
        - 批次未放行 / 未纳入发布 / 在途满排队 均返回可区分的结果。
        """
        with self.store.lock:
            # 无人值守自愈：每次取指令顺带做一次超时收尾
            self._sweep_expired_locked()

            device = self.store.devices.get(device_id)
            if device is None:
                logger.info("取指令被拒 device=%s reason=unknown_device", device_id)
                return PullResult(PullStatus.NOT_IN_CAMPAIGN)

            cid = campaign_id or device.campaign_id
            campaign = self.store.campaigns.get(cid) if cid else None
            if (
                campaign is None
                or device.campaign_id != campaign.campaign_id
                or device.batch_index < 0
            ):
                logger.info(
                    "取指令被拒 device=%s campaign=%s reason=not_in_campaign 归属=%s",
                    device_id, campaign_id, device.campaign_id,
                )
                return PullResult(PullStatus.NOT_IN_CAMPAIGN)

            # 已有在途指令：反复询问返回同一条，绝不重复下发
            if device.state == DeviceState.DELIVERED and device.command_id:
                logger.info(
                    "重复取指令 device=%s campaign=%s batch=%d command=%s "
                    "判定=同一指令仍有效",
                    device_id, campaign.campaign_id, device.batch_index,
                    device.command_id,
                )
                return PullResult(PullStatus.IN_FLIGHT, self._command_of(device))

            if device.state in (
                DeviceState.REPORTED_OK,
                DeviceState.FAILED,
                DeviceState.ROLLED_BACK,
                DeviceState.IDLE,
            ):
                logger.info(
                    "取指令被拒 device=%s campaign=%s state=%s reason=已有定论或已收尾",
                    device_id, campaign.campaign_id, device.state.value,
                )
                return PullResult(PullStatus.NOT_IN_CAMPAIGN)

            # 批次尚未放行
            if device.batch_index > campaign.current_batch:
                logger.info(
                    "取指令被拒 device=%s campaign=%s batch=%d 当前放行批次=%d "
                    "reason=batch_not_released",
                    device_id, campaign.campaign_id, device.batch_index,
                    campaign.current_batch,
                )
                return PullResult(PullStatus.BATCH_NOT_RELEASED)

            # 批次已放行：尝试补发（排队设备可能因此拿到指令）
            if device.state == DeviceState.QUEUED:
                self._fill_slots(campaign, device.batch_index)
                if device.state == DeviceState.QUEUED:
                    logger.info(
                        "取指令排队 device=%s campaign=%s batch=%d 在途=%d 上限=%d "
                        "reason=queued",
                        device_id, campaign.campaign_id, device.batch_index,
                        self._in_flight(campaign, device.batch_index),
                        campaign.max_in_flight_per_batch,
                    )
                    return PullResult(PullStatus.QUEUED)
                logger.info(
                    "排队后补发 device=%s campaign=%s batch=%d command=%s",
                    device_id, campaign.campaign_id, device.batch_index,
                    device.command_id,
                )
                self.store.save()
                return PullResult(PullStatus.ISSUED, self._command_of(device))

            # 兜底：PENDING 且批次已放行（理论上放行时已处理）
            if device.state == DeviceState.PENDING:
                self._fill_slots(campaign, device.batch_index)
                if device.state == DeviceState.DELIVERED:
                    self.store.save()
                    return PullResult(PullStatus.ISSUED, self._command_of(device))
                # 名额仍满：显式转入排队，保持设备状态与返回结论一致
                device.state = DeviceState.QUEUED
                self.store.save()
            return PullResult(PullStatus.QUEUED)

    # ---------- 超时自动收尾 ----------

    def _sweep_expired_locked(self, campaign_id: str | None = None) -> list[str]:
        """把超过有效期仍未给出有效健康结果的在途设备判失败并自动回退。

        幂等：只处理 DELIVERED 且 deadline 已过的设备；重复触发不重复回退，
        已上报设备不受影响。所有判定基于持久化的 deadline，重启后规则不变。
        调用方必须已持有 store.lock。
        """
        now = self._clock()
        rolled_back: list[str] = []
        for cid, campaign in self.store.campaigns.items():
            if campaign_id is not None and cid != campaign_id:
                continue
            expired = [
                d for d in self.store.devices.values()
                if d.campaign_id == cid
                and d.state == DeviceState.DELIVERED
                and d.command_deadline > 0
                and now >= d.command_deadline
            ]
            for device in expired:
                old_state = device.state
                # 判定失败并立即回退到最近可用版本（终态，重复触发不会再处理）
                device.current_version = device.last_good_version
                device.state = DeviceState.ROLLED_BACK
                rolled_back.append(device.device_id)
                logger.info(
                    "超时自动收尾 device=%s campaign=%s batch=%d command=%s "
                    "%s -> rolled_back 回滚到=%s 截止=%.3f 现在=%.3f",
                    device.device_id, cid, device.batch_index,
                    device.command_id, old_state.value,
                    device.last_good_version, device.command_deadline, now,
                )
            # 收尾空出名额后给排队设备补发
            if expired:
                batches = {d.batch_index for d in expired}
                for batch_index in batches:
                    self._fill_slots(campaign, batch_index)
        if rolled_back:
            self.store.save()
        return rolled_back

    def sweep_expired(self, campaign_id: str | None = None) -> list[str]:
        """自动收尾的可重复触发入口，返回本次被自动回退的设备 ID 列表。"""
        with self.store.lock:
            return self._sweep_expired_locked(campaign_id)

    # ---------- 上报（幂等 + 断点续传） ----------

    def handle_report(self, report: HealthReport) -> tuple[bool, str]:
        """处理健康上报，返回 (是否接受, 原因)。

        准入规则（前置校验不消费幂等键，被拒后正式轮次可重新上报）：
        只有设备所在批次已放行、且设备持有在途指令（DELIVERED）时上报才有效；
        批次未放行 -> batch_not_released；批次内排队未取到指令 -> queued_for_slot；
        设备已有定论/已收尾/无在途指令 -> not_awaiting。
        其余重复/乱序/过期上报不二次生效。
        """
        with self.store.lock:
            # 取指令之外的自愈入口：上报前先做超时收尾，保证并发下结论一致
            self._sweep_expired_locked()

            device = self.store.devices.get(report.device_id)
            if device is None:
                return False, RejectReason.UNKNOWN_DEVICE.value
            campaign = self.store.campaigns.get(report.campaign_id)
            if (
                campaign is None
                or device.campaign_id != campaign.campaign_id
                or device.batch_index < 0
            ):
                return False, RejectReason.NOT_IN_CAMPAIGN.value

            # 准入：批次必须已经放行（未放行时提前上报要被明确拒绝且不影响后续）
            if device.batch_index > campaign.current_batch:
                logger.info(
                    "上报被拒 device=%s campaign=%s batch=%d 当前放行批次=%d "
                    "reason=batch_not_released seq=%d",
                    device.device_id, campaign.campaign_id, device.batch_index,
                    campaign.current_batch, report.seq,
                )
                return False, RejectReason.BATCH_NOT_RELEASED.value

            # 幂等键与序号是上报自身属性，优先于“是否在途”判定：
            # 重复/乱序重试即使发生在设备已有定论之后也应给出对应原因。
            if report.idempotency_key in self.store.seen_idempotency_keys:
                return False, RejectReason.DUPLICATE.value
            if report.seq <= device.last_report_seq:
                logger.info(
                    "上报被拒 device=%s campaign=%s reason=stale_report seq=%d "
                    "已接受最大=%d",
                    device.device_id, campaign.campaign_id, report.seq,
                    device.last_report_seq,
                )
                return False, RejectReason.STALE_REPORT.value

            # 准入：批次内排队、尚未拿到指令
            if device.state == DeviceState.QUEUED:
                logger.info(
                    "上报被拒 device=%s campaign=%s batch=%d reason=queued_for_slot "
                    "seq=%d",
                    device.device_id, campaign.campaign_id, device.batch_index,
                    report.seq,
                )
                return False, RejectReason.QUEUED_FOR_SLOT.value

            # 准入：必须持有在途指令（已失败/已回滚/已收尾 都拒绝）
            if device.state != DeviceState.DELIVERED or not device.command_id:
                logger.info(
                    "上报被拒 device=%s campaign=%s state=%s reason=not_awaiting "
                    "seq=%d",
                    device.device_id, campaign.campaign_id, device.state.value,
                    report.seq,
                )
                return False, RejectReason.NOT_AWAITING.value

            # 接受：记录幂等键与序号，推进状态机
            self.store.seen_idempotency_keys.add(report.idempotency_key)
            device.last_report_seq = report.seq
            old_state = device.state
            batch_index = device.batch_index
            if report.healthy:
                device.state = DeviceState.REPORTED_OK
                device.current_version = campaign.target.version
                device.last_good_version = campaign.target.version
            else:
                device.state = DeviceState.FAILED
            logger.info(
                "状态流转 device=%s campaign=%s batch=%d %s -> %s seq=%d healthy=%s",
                device.device_id, campaign.campaign_id, batch_index,
                old_state.value, device.state.value,
                report.seq, report.healthy,
            )
            # 定论后在途名额空出，给同批次排队设备补发
            self._fill_slots(campaign, batch_index)
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
                if d.campaign_id == campaign_id
                and d.batch_index == campaign.current_batch
            ]
            ok = sum(1 for d in members if d.state == DeviceState.REPORTED_OK)
            failed = sum(1 for d in members if d.state in
                         (DeviceState.FAILED, DeviceState.ROLLED_BACK))
            samples = ok + failed
            # 哈希分桶后该批次恰好没有成员：没有健康风险，直接跳过该批次
            if not members:
                logger.info("批次无成员，跳过 campaign=%s batch=%d",
                            campaign_id, campaign.current_batch)
            elif samples < campaign.min_samples:
                logger.info("推进被拒 campaign=%s 原因=样本不足 samples=%d 需要=%d",
                            campaign_id, samples, campaign.min_samples)
                return False
            elif ok / samples < campaign.min_health_ratio:
                logger.info("推进被拒 campaign=%s 原因=健康率不足 ok=%d/%d",
                            campaign_id, ok, samples)
                return False

            campaign.current_batch += 1
            if campaign.current_batch >= len(campaign.batch_ratios):
                campaign.status = "done"
                logger.info("发布完成 campaign=%s", campaign_id)
            else:
                # 新批次放行：在在途上限内发指令，其余排队，等名额空出再补发
                self._release_batch(campaign, campaign.current_batch)
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
            logger.info("状态流转 device=%s campaign=%s batch=%d %s -> %s 回滚到=%s 原因=%s",
                        device.device_id, device.campaign_id, device.batch_index,
                        old_state.value, device.state.value,
                        device.current_version, reason)
            # 与自动收尾一致：名额空出后给同批次排队设备补发
            campaign = self.store.campaigns.get(device.campaign_id or "")
            if campaign is not None and device.batch_index >= 0:
                self._fill_slots(campaign, device.batch_index)
            self.store.save()
            return True
