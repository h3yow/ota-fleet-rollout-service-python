"""并发推进与重复上报场景测试。

日志中打印设备标识、批次判定依据（bucket/batch）与状态流转，
可通过 `uv run pytest -v --log-cli-level=INFO` 观察。
"""
import logging
import threading
import time

import pytest

from ota.batch import batch_of, device_bucket
from ota.models import (
    DeviceState,
    Firmware,
    RejectReason,
    Report,
    RolloutState,
)
from ota.service import RolloutService
from ota.store import Store

logger = logging.getLogger("ota.test")

FW = Firmware("2.0.0", ["hw-a"])


@pytest.fixture()
def svc(tmp_path):
    store = Store(str(tmp_path / "store.json"))
    return RolloutService(store)


def make_report(rid, device, seq, healthy=True, version="2.0.0", rollout="r1"):
    return Report(rid, rollout, device, seq, healthy, version, time.time())


# ---- 批次划分 ----

def test_batch_assignment_is_stable():
    for i in range(50):
        did = f"dev-{i}"
        assert device_bucket(did) == device_bucket(did)
        assert batch_of(did, [0.1, 0.5, 1.0]) == batch_of(did, [0.1, 0.5, 1.0])
        logger.info("device=%s bucket=%d batch=%d",
                    did, device_bucket(did), batch_of(did, [0.1, 0.5, 1.0]))


def test_batch_of_respects_cumulative_ratios():
    ratios = [0.1, 1.0]
    for i in range(200):
        b = batch_of(f"d-{i}", ratios)
        assert b == (0 if device_bucket(f"d-{i}") < 10 else 1)


# ---- 幂等与乱序上报 ----

def test_duplicate_report_rejected_without_double_count(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0], min_samples=1)
    svc.pull_command("d1", "r1")
    r1 = svc.ingest_report(make_report("rep-1", "d1", 1))
    assert r1.accepted
    r2 = svc.ingest_report(make_report("rep-1", "d1", 1))
    assert not r2.accepted and r2.reason == RejectReason.DUPLICATE
    dev = svc._store.get_device("d1")
    assert dev.state == DeviceState.HEALTHY and dev.last_seq == 1
    logger.info("device=d1 state=%s after duplicate reject", dev.state.value)


def test_stale_and_out_of_order_seq_rejected(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    svc.pull_command("d1", "r1")
    assert svc.ingest_report(make_report("rep-1", "d1", 5)).accepted
    stale = svc.ingest_report(make_report("rep-2", "d1", 3))
    assert not stale.accepted and stale.reason == RejectReason.STALE_SEQ
    assert svc._store.get_device("d1").last_seq == 5


def test_unknown_device_and_rollout_rejected(svc):
    svc.create_rollout("r1", FW, [1.0])
    r = svc.ingest_report(make_report("rep-1", "ghost", 1))
    assert not r.accepted and r.reason == RejectReason.UNKNOWN_DEVICE
    svc.register_device("d1", "hw-a", "1.0.0")
    r = svc.ingest_report(make_report("rep-2", "d1", 1, rollout="nope"))
    assert not r.accepted and r.reason == RejectReason.UNKNOWN_ROLLOUT


def test_downgrade_and_incompatible_rejected(svc):
    svc.register_device("d1", "hw-a", "2.0.0")
    svc.register_device("d2", "hw-b", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    svc.pull_command("d1", "r1")
    down = svc.ingest_report(make_report("rep-1", "d1", 1, version="1.5.0"))
    assert not down.accepted and down.reason == RejectReason.DOWNGRADE
    inc = svc.ingest_report(make_report("rep-2", "d2", 1))
    assert not inc.accepted and inc.reason == RejectReason.INCOMPATIBLE
    assert svc._store.get_device("d2").state == DeviceState.IDLE


def test_not_in_batch_report_rejected(svc):
    # 找一个落入批次 1 的设备
    did = next(f"d-{i}" for i in range(1000) if batch_of(f"d-{i}", [0.1, 1.0]) == 1)
    svc.register_device(did, "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [0.1, 1.0])
    r = svc.ingest_report(make_report("rep-1", did, 1))
    assert not r.accepted and r.reason == RejectReason.NOT_IN_BATCH
    logger.info("device=%s bucket=%d batch=1 rejected while current_batch=0",
                did, device_bucket(did))


# ---- 推进门控 ----

def test_advance_blocked_when_samples_insufficient(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.register_device("d2", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [0.5, 1.0], min_samples=2)
    r = svc.try_advance("r1")
    assert r.state == RolloutState.PAUSED and r.current_batch == 0
    logger.info("rollout=r1 paused: samples insufficient")


def test_advance_blocked_when_health_rate_low(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.register_device("d2", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0], min_samples=2, min_health_rate=0.9)
    svc.pull_command("d1", "r1")
    svc.pull_command("d2", "r1")
    svc.ingest_report(make_report("rep-1", "d1", 1, healthy=True))
    svc.ingest_report(make_report("rep-2", "d2", 1, healthy=False))
    r = svc.try_advance("r1")
    assert r.state == RolloutState.PAUSED
    # 失败设备已被回滚
    d2 = svc._store.get_device("d2")
    assert d2.state == DeviceState.ROLLED_BACK and d2.current_version == "1.0.0"


def test_concurrent_advance_never_skips_batch(svc):
    devices = [f"d-{i}" for i in range(20)]
    for did in devices:
        svc.register_device(did, "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [0.5, 1.0], min_samples=1)
    # 当前批次全部健康上报
    for did in devices:
        if batch_of(did, [0.5, 1.0]) == 0:
            svc.pull_command(did, "r1")
            svc.ingest_report(make_report(f"rep-{did}", did, 1))
    results = []

    def advance():
        results.append(svc.try_advance("r1").current_batch)

    threads = [threading.Thread(target=advance) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rollout = svc._store.get_rollout("r1")
    assert rollout.current_batch == 1  # 恰好推进一次，不跳批
    assert set(results) == {1}
    logger.info("concurrent advance results=%s final_batch=%d",
                results, rollout.current_batch)


def test_concurrent_duplicate_reports_counted_once(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0], min_samples=1)
    svc.pull_command("d1", "r1")
    outcomes = []

    def send():
        outcomes.append(svc.ingest_report(make_report("rep-1", "d1", 1)).accepted)

    threads = [threading.Thread(target=send) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(True) == 1  # 同一幂等键只被接受一次
    assert svc._store.get_device("d1").state == DeviceState.HEALTHY
    logger.info("device=d1 accepted=%d rejected=%d",
                outcomes.count(True), outcomes.count(False))


# ---- 回滚与兜底 ----

def test_unhealthy_report_triggers_rollback(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    svc.pull_command("d1", "r1")
    r = svc.ingest_report(make_report("rep-1", "d1", 1, healthy=False))
    assert r.accepted and r.device_state == DeviceState.ROLLED_BACK
    dev = svc._store.get_device("d1")
    assert dev.current_version == "1.0.0"
    logger.info("device=d1 rolled back to %s", dev.current_version)


def test_timeout_sweep_rolls_back_stale_devices(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0], report_timeout_s=10.0)
    svc.pull_command("d1", "r1")
    now = time.time() + 100
    timed_out = svc.sweep_timeouts("r1", now)
    assert [d.device_id for d in timed_out] == ["d1"]
    dev = svc._store.get_device("d1")
    assert dev.state == DeviceState.ROLLED_BACK and dev.current_version == "1.0.0"


def test_unknown_state_device_not_force_advanced(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    # 人为构造从未上报过的 UNKNOWN 设备
    from ota.models import Device
    svc._store.upsert_device(Device("ghost", "hw-a", "1.0.0",
                                    state=DeviceState.UNKNOWN))
    assert svc.pull_command("ghost", "r1") is None
    assert svc._store.get_device("ghost").state == DeviceState.UNKNOWN


def test_rollout_level_rollback(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.register_device("d2", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    svc.pull_command("d1", "r1")
    svc.pull_command("d2", "r1")
    svc.ingest_report(make_report("rep-1", "d1", 1, healthy=True))
    r = svc.rollback_rollout("r1")
    assert r.state == RolloutState.ROLLED_BACK
    for did in ("d1", "d2"):
        dev = svc._store.get_device(did)
        assert dev.state == DeviceState.ROLLED_BACK
        assert dev.current_version == "1.0.0"
        logger.info("device=%s rolled back to %s", did, dev.current_version)
    # 已回滚的发布不再接受上报
    late = svc.ingest_report(make_report("rep-2", "d2", 1))
    assert not late.accepted and late.reason == RejectReason.ROLLOUT_CLOSED


# ---- 断点续传 ----

def test_restart_resumes_without_loss_or_duplicate(tmp_path):
    path = str(tmp_path / "store.json")
    svc = RolloutService(Store(path))
    # 选一个稳定落入批次 0 的设备
    did = next(f"d-{i}" for i in range(1000) if batch_of(f"d-{i}", [0.5, 1.0]) == 0)
    svc.register_device(did, "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [0.5, 1.0], min_samples=1)
    svc.pull_command(did, "r1")
    svc.ingest_report(make_report("rep-1", did, 1))
    # 模拟重启
    svc2 = RolloutService(Store(path))
    dup = svc2.ingest_report(make_report("rep-1", did, 1))
    assert not dup.accepted and dup.reason == RejectReason.DUPLICATE
    r = svc2.try_advance("r1")
    assert r.current_batch == 1 and r.state == RolloutState.IN_PROGRESS
    # 再次推进不会重复计数已处理的幂等键
    r = svc2.try_advance("r1")
    assert r.state in (RolloutState.PAUSED, RolloutState.IN_PROGRESS)
    logger.info("after restart rollout state=%s batch=%d",
                r.state.value, r.current_batch)


def test_pull_command_idempotent_redelivery(svc):
    svc.register_device("d1", "hw-a", "1.0.0")
    svc.create_rollout("r1", FW, [1.0])
    c1 = svc.pull_command("d1", "r1")
    c2 = svc.pull_command("d1", "r1")
    assert c1 == c2 and c1["version"] == "2.0.0"
    assert svc._store.get_device("d1").state == DeviceState.UPDATING
