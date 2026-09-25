"""灰度推进、幂等上报、并发与回滚的测试。

日志中打印设备标识、批次判定依据与状态流转（ota.service logger）。
"""
from __future__ import annotations

import logging
import threading

import pytest

from ota.models import DeviceState, HealthReport, RejectReason
from ota.service import RolloutService
from ota.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@pytest.fixture()
def service(tmp_path):
    store = Store(str(tmp_path / "state.json"))
    svc = RolloutService(store)
    svc.register_firmware("2.0.0", ["model-a"])
    for i in range(20):
        svc.register_device(f"dev-{i:02d}", "model-a", "1.0.0")
    return svc


def _report(device_id: str, seq: int, healthy: bool, key: str | None = None):
    return HealthReport(
        device_id=device_id,
        campaign_id="camp-1",
        idempotency_key=key or f"{device_id}-{seq}",
        seq=seq,
        healthy=healthy,
    )


def _start(service: RolloutService, **kw):
    return service.start_campaign("camp-1", "2.0.0", [0.5, 0.5], **kw)


# ---------- 批次划分 ----------

def test_batch_assignment_is_stable(service):
    _start(service)
    first = {d: service.batch_of(d, "camp-1") for d in service.store.devices}
    for _ in range(5):
        for d in service.store.devices:
            assert service.batch_of(d, "camp-1") == first[d]
    assert set(first.values()) == {0, 1}


# ---------- 推进门槛 ----------

def test_no_advance_when_samples_insufficient(service):
    _start(service, min_samples=3)
    batch0 = [d for d in service.store.devices.values() if d.batch_index == 0]
    assert batch0
    accepted, _ = service.handle_report(_report(batch0[0].device_id, 1, True))
    assert accepted
    assert service.maybe_advance("camp-1") is False
    assert service.store.campaigns["camp-1"].current_batch == 0


def test_no_advance_when_health_ratio_low(service):
    _start(service, min_samples=2, min_health_ratio=0.9)
    batch0 = [d for d in service.store.devices.values() if d.batch_index == 0]
    service.handle_report(_report(batch0[0].device_id, 1, True))
    service.handle_report(_report(batch0[1].device_id, 1, False))
    assert service.maybe_advance("camp-1") is False


def test_advance_when_healthy(service):
    _start(service, min_samples=2, min_health_ratio=0.9)
    batch0 = [d for d in service.store.devices.values() if d.batch_index == 0]
    for d in batch0[:2]:
        service.handle_report(_report(d.device_id, 1, True))
    assert service.maybe_advance("camp-1") is True
    campaign = service.store.campaigns["camp-1"]
    assert campaign.current_batch == 1
    # 新批次设备收到指令，且状态为 DELIVERED
    batch1 = [d for d in service.store.devices.values() if d.batch_index == 1]
    assert all(d.state == DeviceState.DELIVERED for d in batch1)


# ---------- 幂等与断点续传 ----------

def test_duplicate_report_rejected(service):
    _start(service)
    d = next(iter(service.store.devices))
    assert service.handle_report(_report(d, 1, True, key="k1"))[0]
    accepted, reason = service.handle_report(_report(d, 1, True, key="k1"))
    assert not accepted and reason == RejectReason.DUPLICATE.value


def test_stale_report_rejected(service):
    _start(service)
    d = next(iter(service.store.devices))
    service.handle_report(_report(d, 2, True))
    accepted, reason = service.handle_report(_report(d, 1, False))
    assert not accepted and reason == RejectReason.STALE_REPORT.value
    assert service.store.devices[d].state == DeviceState.REPORTED_OK


def test_unknown_device_rejected(service):
    _start(service)
    accepted, reason = service.handle_report(_report("ghost", 1, True))
    assert not accepted and reason == RejectReason.UNKNOWN_DEVICE.value


def test_restart_preserves_state_and_idempotency(service, tmp_path):
    _start(service)
    d = next(iter(service.store.devices))
    service.handle_report(_report(d, 1, True, key="k-restart"))
    # 模拟重启：重新加载同一存储文件
    store2 = Store(str(tmp_path / "state.json"))
    store2.load()
    svc2 = RolloutService(store2)
    accepted, reason = svc2.handle_report(_report(d, 1, True, key="k-restart"))
    assert not accepted and reason == RejectReason.DUPLICATE.value
    assert svc2.store.devices[d].state == DeviceState.REPORTED_OK


# ---------- 并发 ----------

def test_concurrent_duplicate_reports_counted_once(service):
    _start(service)
    d = next(iter(service.store.devices))
    results = []

    def send():
        results.append(service.handle_report(_report(d, 1, True, key="k-conc")))

    threads = [threading.Thread(target=send) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [r for r in results if r[0]]
    assert len(accepted) == 1
    assert service.store.devices[d].last_report_seq == 1


def test_concurrent_advance_no_skip(service):
    _start(service, min_samples=1, min_health_ratio=0.5)
    batch0 = [d for d in service.store.devices.values() if d.batch_index == 0]
    service.handle_report(_report(batch0[0].device_id, 1, True))
    results = []

    def adv():
        results.append(service.maybe_advance("camp-1"))

    threads = [threading.Thread(target=adv) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    campaign = service.store.campaigns["camp-1"]
    # 并发下只推进一次，不跳批
    assert sum(results) == 1
    assert campaign.current_batch == 1 and campaign.status == "active"
    # 批次 1 上报达标后再次推进，发布完成
    batch1 = [d for d in service.store.devices.values() if d.batch_index == 1]
    service.handle_report(_report(batch1[0].device_id, 1, True))
    assert service.maybe_advance("camp-1") is True
    assert campaign.current_batch == 2 and campaign.status == "done"


# ---------- 回滚与兜底 ----------

def test_failed_device_rolls_back_to_last_good(service):
    _start(service)
    d = next(iter(service.store.devices))
    service.handle_report(_report(d, 1, False))
    assert service.rollback_device(d, "health_check_failed")
    device = service.store.devices[d]
    assert device.state == DeviceState.ROLLED_BACK
    assert device.current_version == "1.0.0"


def test_pending_device_not_forcefully_rolled_back_or_advanced(service):
    _start(service)
    pending = [d for d in service.store.devices.values()
               if d.state == DeviceState.PENDING]
    assert pending  # 批次 1 的设备从未上报
    assert not service.rollback_device(pending[0].device_id, "timeout")
    assert pending[0].state == DeviceState.PENDING


def test_downgrade_and_incompatible_rejected(service):
    service.register_device("dev-new", "model-a", "2.0.0")   # 已是目标版本
    service.register_device("dev-other", "model-b", "1.0.0")  # 型号不兼容
    rejected = _start(service)
    assert rejected["dev-new"] == RejectReason.DOWNGRADE.value
    assert rejected["dev-other"] == RejectReason.INCOMPATIBLE.value
    assert service.store.devices["dev-new"].batch_index == -1
    assert service.store.devices["dev-other"].batch_index == -1
