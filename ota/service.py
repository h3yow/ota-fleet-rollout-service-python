"""发布推进、上报处理与回滚的核心逻辑。"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable

from .models import (
    Campaign,
    Command,
    Device,
    DeviceState,
    FetchResult,
    FetchStatus,
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
    def __init__(self, store: Store, clock: Callable[[], float] | None = None) -> None:
        self.store = store
        self.clock = clock or time.time

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
        command_ttl_seconds: float = 3600.0,
        max_in_flight_per_batch: int = 1_000_000,
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
                max_in_flight_per_batch=max(1, max_in_flight_per_batch),
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
            # 首批次放行：按在途上限补发指令（不再无条件全部下发）
            self._pump_batch(campaign, 0)
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

    # ---------- 指令下发与在途补发 ----------

    @staticmethod
    def _command_id(campaign_id: str, device_id: str, version: str) -> str:
        """稳定指令 ID：同一设备同一发布同一版本，反复取永远是同一条。"""
        raw = f"cmd:{campaign_id}:{device_id}:{version}"
        return "cmd-" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _command_of(self, campaign: Campaign, device: Device) -> Command:
        return Command(
            command_id=device.command_id,
            campaign_id=campaign.campaign_id,
            device_id=device.device_id,
            version=device.command_version,
            issued_at=device.command_issued_at,
            expires_at=device.command_expires_at,
        )

    def _in_flight(self, batch: int) -> int:
        """在途数：已取走指令但尚未有定论（DELIVERED）的设备数。

        调用方必须已持有 store.lock。
        """
        return sum(
            1 for d in self.store.devices.values()
            if d.batch_index == batch and d.state == DeviceState.DELIVERED
        )

    def _issue_command(self, campaign: Campaign, device: Device, now: float) -> None:
        """把一条指令写入设备并落 PENDING -> DELIVERED，调用方必须已持锁。"""
        version = campaign.target.version
        device.command_id = self._command_id(
            campaign.campaign_id, device.device_id, version)
        device.command_version = version
        device.command_issued_at = now
        device.command_expires_at = now + campaign.command_ttl_seconds
        old_state = device.state
        device.state = DeviceState.DELIVERED
        logger.info(
            "指令下发 device=%s campaign=%s batch=%d 判定依据=batch<=current_batch=%d "
            "command=%s version=%s expires_at=%.1f 状态流转 %s -> %s",
            device.device_id, campaign.campaign_id, device.batch_index,
            campaign.current_batch, device.command_id, version,
            device.command_expires_at, old_state.value, device.state.value,
        )

    def _pump_batch(self, campaign: Campaign, batch: int) -> int:
        """给已放行批次的排队设备补发指令，严格不超在途上限。

        - 仅处理 batch <= current_batch（批次已放行）；
        - 只有 PENDING 设备算排队中，按 device_id 稳定排序，结论可复现；
        - 调用方必须已持锁。返回本次新下发数量。
        """
        if batch > campaign.current_batch or campaign.status != "active":
            return 0
        queued = sorted(
            (d for d in self.store.devices.values()
             if d.batch_index == batch and d.state == DeviceState.PENDING),
            key=lambda d: d.device_id,
        )
        if not queued:
            return 0
        slots = campaign.max_in_flight_per_batch - self._in_flight(batch)
        issued = 0
        now = self.clock()
        for device in queued:
            if slots <= 0:
                logger.info(
                    "在途已满排队等待 device=%s campaign=%s batch=%d 在途=%d 上限=%d",
                    device.device_id, campaign.campaign_id, batch,
                    self._in_flight(batch), campaign.max_in_flight_per_batch,
                )
                break
            self._issue_command(campaign, device, now)
            issued += 1
            slots -= 1
        return issued

    def fetch_command(self, device_id: str, campaign_id: str) -> FetchResult:
        """设备主动拉取自己当前应执行的唯一指令。

        反复拉取同一设备始终返回同一条指令（ALREADY_DELIVERED），不会重新下发；
        批次未放行 / 未纳入发布 / 设备未知返回各自可区分的状态。
        """
        with self.store.lock:
            # 顺带自动收尾：即使后台线程未运行，拉取流量也会驱动超时判定
            if campaign_id in self.store.campaigns:
                self.sweep_timeouts(campaign_id)
            device = self.store.devices.get(device_id)
            if device is None:
                logger.info("拉取拒绝 device=%s campaign=%s status=%s",
                            device_id, campaign_id,
                            FetchStatus.UNKNOWN_DEVICE.value)
                return FetchResult(FetchStatus.UNKNOWN_DEVICE, None, "设备未注册")
            campaign = self.store.campaigns.get(campaign_id)
            if campaign is None or device.batch_index < 0:
                logger.info("拉取拒绝 device=%s campaign=%s status=%s batch=%s",
                            device_id, campaign_id,
                            FetchStatus.NOT_IN_CAMPAIGN.value, device.batch_index)
                return FetchResult(FetchStatus.NOT_IN_CAMPAIGN, None,
                                   "设备未纳入该发布")
            if device.batch_index > campaign.current_batch:
                logger.info(
                    "拉取等待 device=%s campaign=%s status=%s 判定依据=device_batch=%d>current=%d",
                    device_id, campaign_id, FetchStatus.BATCH_NOT_RELEASED.value,
                    device.batch_index, campaign.current_batch)
                return FetchResult(
                    FetchStatus.BATCH_NOT_RELEASED, None,
                    f"批次 {device.batch_index} 尚未放行，当前批次 "
                    f"{campaign.current_batch}")

            if device.state == DeviceState.DELIVERED:
                # 已有指令：反复拉取永远返回同一条，不重发、不续期
                logger.info(
                    "重复拉取返回同一条 device=%s campaign=%s batch=%d command=%s",
                    device_id, campaign_id, device.batch_index, device.command_id)
                return FetchResult(FetchStatus.ALREADY_DELIVERED,
                                   self._command_of(campaign, device),
                                   "指令已下发，返回同一条")

            if device.state == DeviceState.PENDING:
                # 批次已放行：尝试占用一个在途名额
                if self._in_flight(device.batch_index) >= campaign.max_in_flight_per_batch:
                    logger.info(
                        "拉取排队 device=%s campaign=%s batch=%d 在途=%d 上限=%d",
                        device_id, campaign_id, device.batch_index,
                        self._in_flight(device.batch_index),
                        campaign.max_in_flight_per_batch)
                    return FetchResult(
                        FetchStatus.QUEUED, None,
                        "批次在途已达上限，排队等待补发")
                self._issue_command(campaign, device, self.clock())
                self.store.save()
                return FetchResult(FetchStatus.ISSUED,
                                   self._command_of(campaign, device),
                                   "本次新下发")

            # 设备已有定论（成功/失败/已回滚）：继续返回当初那条指令，保证幂等
            if device.command_id:
                logger.info(
                    "定论后拉取返回原指令 device=%s campaign=%s state=%s command=%s",
                    device_id, campaign_id, device.state.value, device.command_id)
                return FetchResult(FetchStatus.ALREADY_DELIVERED,
                                   self._command_of(campaign, device),
                                   f"设备状态={device.state.value}，返回原指令")
            return FetchResult(FetchStatus.QUEUED, None,
                               f"设备状态={device.state.value}，无可用指令")

    # ---------- 超时自动收尾 ----------

    def sweep_timeouts(self, campaign_id: str, now: float | None = None) -> list[str]:
        """对取走指令后超过有效期仍未给出有效健康结果的设备自动判失败并回退。

        可重复触发、重启后规则一致：以设备上持久化的 expires_at 为准；
        已定论（REPORTED_OK/FAILED/ROLLED_BACK）的设备不受影响，
        回退只发生一次。超时收尾空出的在途名额立即补发给排队设备。
        返回本次被自动回退的设备 ID 列表。
        """
        with self.store.lock:
            campaign = self.store.campaigns.get(campaign_id)
            if campaign is None:
                return []
            now = self.clock() if now is None else now
            timed_out: list[Device] = [
                d for d in self.store.devices.values()
                if d.batch_index >= 0
                and d.state == DeviceState.DELIVERED
                and d.command_expires_at
                and now >= d.command_expires_at
            ]
            rolled_back: list[str] = []
            for device in sorted(timed_out, key=lambda d: d.device_id):
                old_state = device.state
                device.current_version = device.last_good_version
                device.state = DeviceState.ROLLED_BACK
                rolled_back.append(device.device_id)
                logger.info(
                    "超时自动回滚 device=%s campaign=%s batch=%d command=%s "
                    "now=%.1f expires_at=%.1f 状态流转 %s -> %s 回滚到=%s",
                    device.device_id, campaign_id, device.batch_index,
                    device.command_id, now, device.command_expires_at,
                    old_state.value, device.state.value, device.current_version,
                )
            if rolled_back:
                # 空出的在途名额按稳定顺序补发给仍在排队的设备
                affected_batches = sorted({
                    self.store.devices[d].batch_index for d in rolled_back})
                for batch in affected_batches:
                    self._pump_batch(campaign, batch)
                self.store.save()
            return rolled_back

    def sweep_all(self, now: float | None = None) -> dict[str, list[str]]:
        """扫描所有未完成发布的超时设备。返回 {campaign_id: 回退设备列表}。"""
        with self.store.lock:
            result: dict[str, list[str]] = {}
            for campaign_id, campaign in list(self.store.campaigns.items()):
                if campaign.status == "active":
                    ids = self.sweep_timeouts(campaign_id, now)
                    if ids:
                        result[campaign_id] = ids
            return result

    def start_auto_sweeper(self, interval_seconds: float = 5.0):
        """启动后台守护线程，周期性自动收尾；服务重启后重新启动即可继续生效。

        规则完全等同 sweep_timeouts：以持久化的 expires_at 判定，重复触发幂等。
        返回 threading.Thread，调用方一般无需持有（daemon 随进程退出）。
        """
        import threading

        def _loop() -> None:
            while True:
                try:
                    self.sweep_all()
                except Exception:  # pragma: no cover - 收尾异常不能打死线程
                    logger.exception("自动收尾异常")
                threading.Event().wait(interval_seconds)

        thread = threading.Thread(
            target=_loop, name="ota-timeout-sweeper", daemon=True)
        thread.start()
        logger.info("自动收尾线程已启动 interval=%.1fs", interval_seconds)
        return thread

    # ---------- 上报（幂等 + 断点续传） ----------

    def handle_report(self, report: HealthReport) -> tuple[bool, str]:
        """处理健康上报，返回 (是否接受, 原因)。重复/乱序/过期上报不二次生效。

        准入顺序（批次/发布等结构性门槛最先，拒绝不落任何状态，不影响后续正式上报）：
        设备未知 -> 不在发布 -> 批次未放行 -> 幂等重复 -> 序号乱序
        -> 未持有指令/排队中 -> 已有定论。仅 DELIVERED 设备的新有效上报被接受。
        """
        with self.store.lock:
            device = self.store.devices.get(report.device_id)
            if device is None:
                return False, RejectReason.UNKNOWN_DEVICE.value
            campaign = self.store.campaigns.get(report.campaign_id)
            if campaign is None or device.batch_index < 0:
                logger.info("上报拒绝 device=%s campaign=%s reason=%s batch=%s",
                            report.device_id, report.campaign_id,
                            RejectReason.NOT_IN_CAMPAIGN.value, device.batch_index)
                return False, RejectReason.NOT_IN_CAMPAIGN.value
            if device.batch_index > campaign.current_batch:
                logger.info(
                    "上报拒绝 device=%s campaign=%s reason=%s 判定依据=device_batch=%d>current=%d",
                    report.device_id, report.campaign_id,
                    RejectReason.BATCH_NOT_RELEASED.value,
                    device.batch_index, campaign.current_batch)
                return False, RejectReason.BATCH_NOT_RELEASED.value
            # 批次已放行：顺带自动收尾，使超时设备及时空出在途名额
            self.sweep_timeouts(report.campaign_id)
            if report.idempotency_key in self.store.seen_idempotency_keys:
                return False, RejectReason.DUPLICATE.value
            if report.seq <= device.last_report_seq:
                return False, RejectReason.STALE_REPORT.value
            if device.state == DeviceState.PENDING:
                logger.info(
                    "上报拒绝 device=%s campaign=%s reason=%s batch=%d 尚未取走指令",
                    report.device_id, report.campaign_id,
                    RejectReason.COMMAND_NOT_DELIVERED.value, device.batch_index)
                return False, RejectReason.COMMAND_NOT_DELIVERED.value
            if device.state != DeviceState.DELIVERED:
                logger.info(
                    "上报拒绝 device=%s campaign=%s reason=%s state=%s 设备已有定论",
                    report.device_id, report.campaign_id,
                    RejectReason.TERMINAL_STATE.value, device.state.value)
                return False, RejectReason.TERMINAL_STATE.value

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
                "状态流转 device=%s campaign=%s batch=%d %s -> %s seq=%d healthy=%s",
                device.device_id, report.campaign_id, device.batch_index,
                old_state.value, device.state.value,
                report.seq, report.healthy,
            )
            # 该设备已有定论、空出在途名额：给同批次排队设备补发，不超发
            self._pump_batch(campaign, device.batch_index)
            self.store.save()
            return True, "accepted"

    # ---------- 推进 ----------

    def maybe_advance(self, campaign_id: str) -> bool:
        """当前批次健康达标且样本足够时推进到下一批次。"""
        with self.store.lock:
            campaign = self.store.campaigns[campaign_id]
            if campaign.status != "active":
                return False
            # 推进前先自动收尾：超时未上报者判失败并回退，空出名额
            self.sweep_timeouts(campaign_id)
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
                # 新批次放行：按在途上限补发，超出的设备保持排队
                issued = self._pump_batch(campaign, campaign.current_batch)
                logger.info("批次放行 campaign=%s batch=%d 本次下发=%d",
                            campaign_id, campaign.current_batch, issued)
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
            # 手动回滚同样空出在途名额，给同批次排队设备补发
            for campaign in self.store.campaigns.values():
                if campaign.status == "active":
                    self._pump_batch(campaign, device.batch_index)
            self.store.save()
            return True
