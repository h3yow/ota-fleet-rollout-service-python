"""发布服务：灰度推进、幂等上报、回滚与失败兜底。

一致性约定：
- 所有状态变更都在 store.lock 内完成，并发上报/推进串行化，
  不会重复计数、跳批或误推进。
- 每次变更后立即 save()，重启后从磁盘恢复，断点续传不丢不重。
- 上报以 report_id 为幂等键、seq 为设备侧单调序号，
  重复/乱序/过期上报返回可区分的拒绝原因且不改变状态。
"""
from __future__ import annotations

import logging
import time

from .batch import batch_of, device_bucket
from .models import (
    Device,
    DeviceState,
    Firmware,
    RejectReason,
    Report,
    ReportResult,
    Rollout,
    RolloutState,
    parse_version,
)
from .store import Store

logger = logging.getLogger("ota")

# 允许推进/投递的发布状态
_ACTIVE = (RolloutState.IN_PROGRESS, RolloutState.PAUSED)


class RolloutService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ---- 注册与创建 ----
    def register_device(self, device_id: str, hardware: str, version: str) -> Device:
        with self._store.lock:
            device = self._store.get_device(device_id)
            if device is None:
                device = Device(
                    device_id=device_id, hardware=hardware, current_version=version
                )
                self._store.upsert_device(device)
                self._store.save()
            logger.info(
                "register device=%s hw=%s version=%s bucket=%d",
                device_id, hardware, version, device_bucket(device_id),
            )
            return device

    def create_rollout(
        self,
        rollout_id: str,
        target: Firmware,
        batch_ratios: list[float],
        min_samples: int = 1,
        min_health_rate: float = 1.0,
        report_timeout_s: float = 300.0,
    ) -> Rollout:
        if not batch_ratios or any(r <= 0 or r > 1.0 for r in batch_ratios):
            raise ValueError("batch_ratios 必须在 (0, 1.0] 内")
        if any(b <= a for a, b in zip(batch_ratios, batch_ratios[1:])):
            raise ValueError("batch_ratios 必须严格递增（累计比例）")
        with self._store.lock:
            rollout = Rollout(
                rollout_id=rollout_id,
                target=target,
                batch_ratios=list(batch_ratios),
                state=RolloutState.IN_PROGRESS,
                min_samples=min_samples,
                min_health_rate=min_health_rate,
                report_timeout_s=report_timeout_s,
            )
            self._store.upsert_rollout(rollout)
            self._queue_current_batch(rollout)
            self._store.save()
            logger.info(
                "rollout=%s created target=%s batches=%s",
                rollout_id, target.version, batch_ratios,
            )
            return rollout

    # ---- 指令投递（幂等、可断点续传）----
    def pull_command(self, device_id: str, rollout_id: str) -> dict | None:
        """设备拉取升级指令。重复拉取返回同一指令，重启后继续投递。"""
        with self._store.lock:
            rollout = self._store.get_rollout(rollout_id)
            device = self._store.get_device(device_id)
            if rollout is None or device is None:
                return None
            if rollout.state != RolloutState.IN_PROGRESS:
                return None
            if device.state in (DeviceState.HEALTHY, DeviceState.ROLLED_BACK):
                return None
            if device.state == DeviceState.UNKNOWN:
                # 从未上报过的设备不得被强制推进
                logger.info("pull denied device=%s reason=unknown_state", device_id)
                return None
            if device.hardware not in rollout.target.compatible_hardware:
                logger.info(
                    "pull denied device=%s reason=incompatible hw=%s",
                    device_id, device.hardware,
                )
                return None
            batch = batch_of(device_id, rollout.batch_ratios)
            if batch > rollout.current_batch:
                return None
            if device.state in (DeviceState.IDLE, DeviceState.QUEUED):
                device.previous_version = device.current_version
                device.state = DeviceState.UPDATING
                device.command_at = time.time()
                self._store.upsert_device(device)
                self._store.save()
                logger.info(
                    "dispatch device=%s batch=%d/%d %s->%s",
                    device_id, batch, rollout.current_batch,
                    device.previous_version, rollout.target.version,
                )
            # UPDATING 状态下重复拉取：返回同一指令（幂等投递）
            return {
                "rollout_id": rollout_id,
                "device_id": device_id,
                "version": rollout.target.version,
            }

    # ---- 上报（幂等、乱序、过期可区分）----
    def ingest_report(self, report: Report) -> ReportResult:
        with self._store.lock:
            rollout = self._store.get_rollout(report.rollout_id)
            if rollout is None:
                return ReportResult(False, RejectReason.UNKNOWN_ROLLOUT)
            device = self._store.get_device(report.device_id)
            if device is None:
                return ReportResult(False, RejectReason.UNKNOWN_DEVICE)
            # 幂等键：已处理过的上报直接拒绝，不二次改变状态
            if report.report_id in rollout.processed_report_ids:
                return ReportResult(False, RejectReason.DUPLICATE, device.state)
            # 乱序/过期序号
            if report.seq <= device.last_seq:
                return ReportResult(False, RejectReason.STALE_SEQ, device.state)
            if rollout.state not in _ACTIVE:
                return ReportResult(False, RejectReason.ROLLOUT_CLOSED, device.state)
            # 不兼容固件与版本降级：明确拒绝而非静默接受
            if device.hardware not in rollout.target.compatible_hardware:
                return ReportResult(False, RejectReason.INCOMPATIBLE, device.state)
            if parse_version(report.version) < parse_version(device.current_version):
                return ReportResult(False, RejectReason.DOWNGRADE, device.state)
            batch = batch_of(report.device_id, rollout.batch_ratios)
            if batch > rollout.current_batch:
                return ReportResult(False, RejectReason.NOT_IN_BATCH, device.state)

            # 接受上报：一次性完成状态流转与持久化
            rollout.processed_report_ids.add(report.report_id)
            old_state = device.state
            device.last_report_id = report.report_id
            device.last_seq = report.seq
            device.last_report_at = report.ts
            device.current_version = report.version
            device.state = DeviceState.HEALTHY if report.healthy else DeviceState.FAILED
            logger.info(
                "report accepted device=%s rollout=%s seq=%d bucket=%d batch=%d "
                "healthy=%s state=%s->%s",
                report.device_id, report.rollout_id, report.seq,
                device_bucket(report.device_id), batch, report.healthy,
                old_state.value, device.state.value,
            )
            if not report.healthy:
                self._rollback_device_locked(device, reason="health_check_failed")
            self._store.upsert_device(device)
            self._store.upsert_rollout(rollout)
            self._store.save()
            return ReportResult(True, None, device.state)

    # ---- 推进 ----
    def try_advance(self, rollout_id: str) -> Rollout:
        """健康达标且样本足够才推进；否则暂停。并发调用串行化，不会跳批。"""
        with self._store.lock:
            rollout = self._store.get_rollout(rollout_id)
            if rollout is None:
                raise KeyError(f"unknown rollout: {rollout_id}")
            if rollout.state not in _ACTIVE:
                return rollout
            devices = [
                d for d in self._store.list_devices()
                if batch_of(d.device_id, rollout.batch_ratios) == rollout.current_batch
                and d.hardware in rollout.target.compatible_hardware
            ]
            reported = [
                d for d in devices
                if d.state in (DeviceState.HEALTHY, DeviceState.FAILED,
                               DeviceState.ROLLED_BACK)
            ]
            healthy = [d for d in reported if d.state == DeviceState.HEALTHY]
            rate = len(healthy) / len(reported) if reported else 0.0
            logger.info(
                "advance check rollout=%s batch=%d samples=%d/%d health=%.2f/%.2f",
                rollout_id, rollout.current_batch, len(reported),
                rollout.min_samples, rate, rollout.min_health_rate,
            )
            if len(reported) < rollout.min_samples or rate < rollout.min_health_rate:
                rollout.state = RolloutState.PAUSED
                self._store.upsert_rollout(rollout)
                self._store.save()
                return rollout
            rollout.current_batch += 1
            rollout.state = RolloutState.IN_PROGRESS
            if rollout.current_batch >= len(rollout.batch_ratios):
                rollout.state = RolloutState.COMPLETED
                logger.info("rollout=%s completed", rollout_id)
            else:
                self._queue_current_batch(rollout)
                logger.info(
                    "rollout=%s advanced to batch=%d", rollout_id, rollout.current_batch
                )
            self._store.upsert_rollout(rollout)
            self._store.save()
            return rollout

    # ---- 回滚与超时兜底 ----
    def rollback_device(self, device_id: str, rollout_id: str) -> Device:
        with self._store.lock:
            device = self._store.get_device(device_id)
            if device is None:
                raise KeyError(f"unknown device: {device_id}")
            self._rollback_device_locked(device, reason="manual")
            self._store.upsert_device(device)
            self._store.save()
            return device

    def rollback_rollout(self, rollout_id: str) -> Rollout:
        with self._store.lock:
            rollout = self._store.get_rollout(rollout_id)
            if rollout is None:
                raise KeyError(f"unknown rollout: {rollout_id}")
            for device in self._store.list_devices():
                if device.state in (DeviceState.UPDATING, DeviceState.HEALTHY,
                                    DeviceState.FAILED) and device.previous_version:
                    self._rollback_device_locked(device, reason="rollout_rollback")
                    self._store.upsert_device(device)
            rollout.state = RolloutState.ROLLED_BACK
            self._store.upsert_rollout(rollout)
            self._store.save()
            logger.info("rollout=%s rolled back", rollout_id)
            return rollout

    def sweep_timeouts(self, rollout_id: str, now: float) -> list[Device]:
        """上报超时的设备判定失败并回退到最近可用版本。"""
        with self._store.lock:
            rollout = self._store.get_rollout(rollout_id)
            if rollout is None:
                raise KeyError(f"unknown rollout: {rollout_id}")
            timed_out: list[Device] = []
            for device in self._store.list_devices():
                if (
                    device.state == DeviceState.UPDATING
                    and device.command_at is not None
                    and now - device.command_at > rollout.report_timeout_s
                ):
                    device.state = DeviceState.FAILED
                    self._rollback_device_locked(device, reason="report_timeout")
                    self._store.upsert_device(device)
                    timed_out.append(device)
                    logger.info(
                        "timeout device=%s elapsed=%.1fs -> rollback",
                        device.device_id, now - (device.command_at or now),
                    )
            if timed_out:
                self._store.save()
            return timed_out

    # ---- 内部 ----
    def _queue_current_batch(self, rollout: Rollout) -> None:
        """将落入当前批次的兼容设备置为 QUEUED；UNKNOWN 设备不被强制推进。"""
        for device in self._store.list_devices():
            if device.state != DeviceState.IDLE:
                continue
            if device.hardware not in rollout.target.compatible_hardware:
                continue
            batch = batch_of(device.device_id, rollout.batch_ratios)
            if batch <= rollout.current_batch:
                device.state = DeviceState.QUEUED
                self._store.upsert_device(device)
                logger.info(
                    "queue device=%s bucket=%d batch=%d<=%d",
                    device.device_id, device_bucket(device.device_id),
                    batch, rollout.current_batch,
                )

    @staticmethod
    def _rollback_device_locked(device: Device, reason: str) -> None:
        """回退到最近可用版本（调用方须持有锁并负责落盘）。"""
        old_state = device.state
        if device.previous_version is not None:
            device.current_version = device.previous_version
        device.state = DeviceState.ROLLED_BACK
        logger.info(
            "rollback device=%s reason=%s version=%s state=%s->%s",
            device.device_id, reason, device.current_version,
            old_state.value, device.state.value,
        )
