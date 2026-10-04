"""设备主动拉取、指令有效期自动收尾、在途上限与上报准入的测试。

日志中打印设备标识、批次判定依据（device_batch vs current_batch）
与状态流转（ota.service logger）。
"""
from __future__ import annotations

import logging
import threading

import pytest

from ota.models import (
    DeviceState,
    FetchStatus,
    HealthReport,
    RejectReason,
)
from ota.service import RolloutService
from ota.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def ctx(tmp_path):
    """构建 12 台兼容设备 + 1 台型号不兼容设备，返回带伪时钟的上下文。"""

    class Ctx:
        pass

    c = Ctx()
    c.clock = FakeClock()
    c.store = Store(str(tmp_path / "state.json"))
    c.svc = RolloutService(c.store, clock=c.clock)
    c.svc.register_firmware("2.0.0", ["model-a"])
    for i in range(12):
        c.svc.register_device(f"dev-{i:02d}", "model-a", "1.0.0")
    c.svc.register_device("dev-other", "model-b", "1.0.0")  # 不纳入发布
    return c


def _start(ctx: Ctx, **kw):
    defaults = dict(
        campaign_id="camp-1", version="2.0.0",
        batch_ratios=[0.5, 0.5], min_samples=1, min_health_ratio=0.5,
        command_ttl_seconds=100.0,
    )
    defaults.update(kw)
    ctx.svc.start_campaign(**defaults)


def _report(device_id: str, seq: int, healthy: bool, key: str | None = None,
            campaign_id: str = "camp-1"):
    return HealthReport(
        device_id=device_id,
        campaign_id=campaign_id,
        idempotency_key=key or f"{device_id}-{seq}",
        seq=seq, healthy=healthy,
    )


def _batches(ctx):
    b0 = sorted(d.device_id for d in ctx.store.devices.values()
                if d.batch_index == 0)
    b1 = sorted(d.device_id for d in ctx.store.devices.values()
                if d.batch_index == 1)
    return b0, b1


# ---------- 主动拉取：同一条指令、可区分结果 ----------

def test_repeated_fetch_returns_same_command(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    b0, _ = _batches(ctx)
    first = ctx.svc.fetch_command(b0[0], "camp-1")
    # 首批可能在发布时已预投递：要么 issued 要么 already_delivered，但必须有指令
    assert first.status in (FetchStatus.ISSUED, FetchStatus.ALREADY_DELIVERED)
    assert first.command is not None
    assert first.command.version == "2.0.0"
    seen = {first.command.command_id}
    for _ in range(10):
        r = ctx.svc.fetch_command(b0[0], "camp-1")
        assert r.status == FetchStatus.ALREADY_DELIVERED
        assert r.command.command_id in seen
        assert r.command.issued_at == first.command.issued_at
        assert r.command.expires_at == first.command.expires_at  # 不续期
    # 设备只被投递过一次：issued_at 不随反复拉取变化
    assert ctx.store.devices[b0[0]].command_issued_at == first.command.issued_at


def test_fetch_distinguishable_statuses(ctx):
    _start(ctx, max_in_flight_per_batch=1)
    b0, b1 = _batches(ctx)
    # 首批只预投递 1 台，其余排队
    delivered = [d for d in b0
                 if ctx.store.devices[d].state == DeviceState.DELIVERED]
    queued = [d for d in b0 if d not in delivered]
    assert len(delivered) == 1 and queued

    # 已投递 -> 同一条；排队中 -> queued
    assert ctx.svc.fetch_command(delivered[0], "camp-1").status in (
        FetchStatus.ALREADY_DELIVERED, FetchStatus.ISSUED)
    assert ctx.svc.fetch_command(queued[0], "camp-1").status == FetchStatus.QUEUED
    # 还没轮到批次 -> batch_not_released
    r = ctx.svc.fetch_command(b1[0], "camp-1")
    assert r.status == FetchStatus.BATCH_NOT_RELEASED and r.command is None
    assert f"批次 {ctx.store.devices[b1[0]].batch_index}" in r.detail
    # 未纳入发布 -> not_in_campaign
    assert ctx.svc.fetch_command("dev-other", "camp-1").status == \
        FetchStatus.NOT_IN_CAMPAIGN
    # 未知设备
    assert ctx.svc.fetch_command("ghost", "camp-1").status == \
        FetchStatus.UNKNOWN_DEVICE
    # 未知发布
    assert ctx.svc.fetch_command(b0[0], "nope").status == \
        FetchStatus.NOT_IN_CAMPAIGN


# ---------- 在途上限：排队、补发、不超发 ----------

def test_in_flight_cap_never_exceeded_on_fetch(ctx):
    _start(ctx, max_in_flight_per_batch=2)
    b0, _ = _batches(ctx)
    assert len(b0) > 2
    statuses = [ctx.svc.fetch_command(d, "camp-1").status for d in b0]
    delivered = [s for s in statuses
                 if s in (FetchStatus.ISSUED, FetchStatus.ALREADY_DELIVERED)]
    assert len(delivered) == 2
    assert all(s == FetchStatus.QUEUED for s in statuses[len(delivered):])
    in_flight = sum(1 for d in b0
                    if ctx.store.devices[d].state == DeviceState.DELIVERED)
    assert in_flight == 2  # 任何情况下都不能超发


def test_slot_refilled_after_report_within_cap(ctx):
    _start(ctx, max_in_flight_per_batch=2)
    b0, _ = _batches(ctx)
    for d in b0:
        ctx.svc.fetch_command(d, "camp-1")
    assert sum(1 for d in b0
               if ctx.store.devices[d].state == DeviceState.DELIVERED) == 2
    delivered = [d for d in b0
                 if ctx.store.devices[d].state == DeviceState.DELIVERED]
    # 一台上报成功，空出名额，补发泵立即把名额给排队最前者（仍不超上限）
    assert ctx.svc.handle_report(_report(delivered[0], 1, True))[0]
    refilled = [d for d in b0
                if ctx.store.devices[d].state == DeviceState.DELIVERED]
    assert len(refilled) == 2
    new_guy = next(d for d in refilled if d != delivered[1])
    got = ctx.svc.fetch_command(new_guy, "camp-1")
    # 泵已下发：设备拉到的就是同一条（已补发的）指令
    assert got.status == FetchStatus.ALREADY_DELIVERED
    assert got.command.version == "2.0.0"
    assert sum(1 for d in b0
               if ctx.store.devices[d].state == DeviceState.DELIVERED) == 2


def test_cap_applies_when_new_batch_released(ctx):
    _start(ctx, max_in_flight_per_batch=2)
    b0, b1 = _batches(ctx)
    # 让批次 0 全部成功（先给所有 b0 设备放行：放大上限推进，再还原场景）
    ctx.store.campaigns["camp-1"].max_in_flight_per_batch = 100
    for d in b0:
        ctx.svc.fetch_command(d, "camp-1")
        ctx.svc.handle_report(_report(d, 1, True))
    ctx.store.campaigns["camp-1"].max_in_flight_per_batch = 2
    assert ctx.svc.maybe_advance("camp-1") is True
    delivered_b1 = [d for d in b1
                    if ctx.store.devices[d].state == DeviceState.DELIVERED]
    assert len(delivered_b1) == 2  # 新批次也只按上限下发
    assert all(ctx.svc.fetch_command(d, "camp-1").status == FetchStatus.QUEUED
               for d in b1 if ctx.store.devices[d].state == DeviceState.PENDING)


# ---------- 上报准入：批次未放行 / 未取指令 ----------

def test_early_report_rejected_and_does_not_affect_later(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    _, b1 = _batches(ctx)
    target = b1[0]
    accepted, reason = ctx.svc.handle_report(_report(target, 1, True, key="early"))
    assert not accepted and reason == RejectReason.BATCH_NOT_RELEASED.value
    # 提前上报不记录幂等键、不推进 seq
    assert "early" not in ctx.store.seen_idempotency_keys
    assert ctx.store.devices[target].last_report_seq == 0
    assert ctx.store.devices[target].state == DeviceState.PENDING

    # 批次放行后用同一个键、同一个序号正式上报，必须正常接受
    # 先让批次 0 达标推进
    b0, _ = _batches(ctx)
    for d in b0:
        ctx.svc.handle_report(_report(d, 1, True))
    assert ctx.svc.maybe_advance("camp-1") is True
    ctx.svc.fetch_command(target, "camp-1")
    accepted2, reason2 = ctx.svc.handle_report(
        _report(target, 1, True, key="early"))
    assert accepted2 and reason2 == "accepted"
    assert ctx.store.devices[target].state == DeviceState.REPORTED_OK


def test_report_without_command_rejected(ctx):
    _start(ctx, max_in_flight_per_batch=1)
    b0, _ = _batches(ctx)
    pending = [d for d in b0
               if ctx.store.devices[d].state == DeviceState.PENDING]
    assert pending
    accepted, reason = ctx.svc.handle_report(
        _report(pending[0], 1, True, key="p1"))
    assert not accepted and reason == RejectReason.COMMAND_NOT_DELIVERED.value
    assert "p1" not in ctx.store.seen_idempotency_keys


def test_report_after_terminal_state_rejected(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    b0, _ = _batches(ctx)
    d = b0[0]
    ctx.svc.fetch_command(d, "camp-1")
    ctx.svc.handle_report(_report(d, 1, True))
    accepted, reason = ctx.svc.handle_report(
        _report(d, 2, False, key="later"))
    assert not accepted and reason == RejectReason.TERMINAL_STATE.value


# ---------- 有效期与自动收尾 ----------

def test_timeout_auto_rollback_and_idempotent(ctx):
    _start(ctx, max_in_flight_per_batch=1)
    b0, _ = _batches(ctx)
    delivered = [d for d in b0
                 if ctx.store.devices[d].state == DeviceState.DELIVERED]
    target = delivered[0]

    # 未到时限：不收尾
    ctx.clock.advance(99)
    assert ctx.svc.sweep_timeouts("camp-1") == []
    assert ctx.store.devices[target].state == DeviceState.DELIVERED

    # 刚到有效期：判失败并各自回退到最近可用版本
    ctx.clock.advance(1)
    rolled = ctx.svc.sweep_timeouts("camp-1")
    assert target in rolled
    dev = ctx.store.devices[target]
    assert dev.state == DeviceState.ROLLED_BACK
    assert dev.current_version == "1.0.0" == dev.last_good_version

    # 同一时刻重复触发：同一台设备绝不被二次回退
    assert ctx.svc.sweep_timeouts("camp-1") == []
    assert dev.state == DeviceState.ROLLED_BACK
    assert dev.current_version == "1.0.0"

    # 时间继续走、反复收尾：该设备状态不反弹（补发的后续设备超时不影响它）
    ctx.clock.advance(5000)
    for _ in range(3):
        ctx.svc.sweep_timeouts("camp-1")
        assert dev.state == DeviceState.ROLLED_BACK
        assert dev.current_version == "1.0.0"


def test_sweep_does_not_touch_healthy_devices(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    b0, _ = _batches(ctx)
    ok_dev = b0[0]
    slow = b0[1]
    ctx.svc.handle_report(_report(ok_dev, 1, True))  # 已正常上报
    ctx.clock.advance(200)
    rolled = ctx.svc.sweep_timeouts("camp-1")
    assert slow in rolled and ok_dev not in rolled
    assert ctx.store.devices[ok_dev].state == DeviceState.REPORTED_OK
    assert ctx.store.devices[ok_dev].current_version == "2.0.0"


def test_sweep_frees_slot_and_refills_queue(ctx):
    _start(ctx, max_in_flight_per_batch=1)
    b0, _ = _batches(ctx)
    delivered = sorted(d for d in b0
                       if ctx.store.devices[d].state == DeviceState.DELIVERED)
    queued = sorted(d for d in b0 if d not in delivered)
    assert len(delivered) == 1 and queued
    ctx.clock.advance(101)
    rolled = ctx.svc.sweep_timeouts("camp-1")
    assert rolled == delivered
    # 空出的名额立即补发给排队最前者，且仍不超上限
    new_in_flight = [d for d in b0
                     if ctx.store.devices[d].state == DeviceState.DELIVERED]
    assert new_in_flight == [queued[0]]
    assert len(new_in_flight) == 1


def test_sweep_rules_continue_after_restart(ctx, tmp_path):
    _start(ctx, max_in_flight_per_batch=1)
    b0, _ = _batches(ctx)
    target = next(d for d in b0
                  if ctx.store.devices[d].state == DeviceState.DELIVERED)
    ctx.clock.advance(101)

    # 重启前不做收尾；重启后用更晚时钟加载，规则一致，照样回退
    path = str(tmp_path / "state.json")
    store2 = Store(path)
    store2.load()
    clock2 = FakeClock(ctx.clock.now)
    svc2 = RolloutService(store2, clock=clock2)
    first = svc2.sweep_timeouts("camp-1")
    assert target in first
    # 同一时刻再收尾不重复回退；时间推进后该设备状态也不反弹
    assert svc2.sweep_timeouts("camp-1") == []
    clock2.advance(9999)
    later = svc2.sweep_timeouts("camp-1")
    assert target not in later
    assert store2.devices[target].state == DeviceState.ROLLED_BACK
    assert store2.devices[target].current_version == "1.0.0"


# ---------- 并发：取指令 / 上报 / 收尾 混合 ----------

def test_concurrent_fetch_each_device_gets_one_stable_command(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    b0, _ = _batches(ctx)
    results: dict[str, list] = {d: [] for d in b0}
    barrier = threading.Barrier(len(b0) * 2)

    def pull(d):
        barrier.wait()
        for _ in range(5):
            r = ctx.svc.fetch_command(d, "camp-1")
            results[d].append(r)

    threads = [threading.Thread(target=pull, args=(d,))
               for d in b0 for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for d, rs in results.items():
        ids = {r.command.command_id for r in rs if r.command}
        assert len(ids) == 1  # 同一台设备始终同一条
    # 每台设备只被下发一次，全部在途但不超过上限
    assert sum(1 for d in b0
               if ctx.store.devices[d].state == DeviceState.DELIVERED) == \
        min(len(b0), 100)


def test_concurrent_cap_never_exceeded(ctx):
    _start(ctx, max_in_flight_per_batch=3)
    b0, _ = _batches(ctx)
    assert len(b0) >= 4
    barrier = threading.Barrier(len(b0))
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def pull(d):
        barrier.wait()
        r = ctx.svc.fetch_command(d, "camp-1")
        with lock:
            outcomes.append((d, r.status.value))

    threads = [threading.Thread(target=pull, args=(d,)) for d in b0]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    in_flight = sum(1 for d in b0
                    if ctx.store.devices[d].state == DeviceState.DELIVERED)
    assert in_flight == 3
    issued = {d for d, s in outcomes
              if s in FetchStatus.ISSUED.value
              or s == FetchStatus.ALREADY_DELIVERED.value}
    assert len(issued) == 3


def test_concurrent_reports_and_sweep_consistent(ctx):
    _start(ctx, max_in_flight_per_batch=100, command_ttl_seconds=100.0)
    b0, _ = _batches(ctx)
    for d in b0:
        ctx.svc.fetch_command(d, "camp-1")
    # 一半设备在到期瞬间上报成功，同时反复收尾
    ctx.clock.advance(100)
    reporters = b0[: len(b0) // 2]
    sweep_results: list[list[str]] = []
    barrier = threading.Barrier(len(reporters) + 1)

    def report(d):
        barrier.wait()
        ctx.svc.handle_report(_report(d, 1, True, key=f"{d}-ok"))

    def sweep():
        barrier.wait()
        for _ in range(5):
            sweep_results.append(ctx.svc.sweep_timeouts("camp-1"))

    threads = [threading.Thread(target=report, args=(d,)) for d in reporters]
    threads.append(threading.Thread(target=sweep))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for d in reporters:
        # 成功上报与超时回退互斥：设备只能是 REPORTED_OK 或 ROLLED_BACK 之一
        assert ctx.store.devices[d].state in (
            DeviceState.REPORTED_OK, DeviceState.ROLLED_BACK)
    # 每台设备至多被回退一次
    rolled_once = [d for d in b0
                   if ctx.store.devices[d].state == DeviceState.ROLLED_BACK]
    counted = [d for ids in sweep_results for d in ids]
    assert sorted(counted) == sorted(rolled_once)
    assert len(counted) == len(set(counted))


def test_concurrent_early_reports_never_accept(ctx):
    _start(ctx, max_in_flight_per_batch=100)
    _, b1 = _batches(ctx)
    barrier = threading.Barrier(len(b1))
    results = []
    lock = threading.Lock()

    def report(d):
        barrier.wait()
        with lock:
            results.append(ctx.svc.handle_report(
                _report(d, 1, True, key=f"{d}-early")))

    threads = [threading.Thread(target=report, args=(d,)) for d in b1]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(not ok and reason == RejectReason.BATCH_NOT_RELEASED.value
               for ok, reason in results)
    assert all(d.state == DeviceState.PENDING for d in
               [ctx.store.devices[x] for x in b1])


def test_background_sweeper_auto_rolls_back_without_intervention(ctx):
    import time as _time

    # 用真实时钟 + 很短有效期，批次只放 1 台，验证守护线程自动收尾
    real = RolloutService(ctx.store)
    real.store.campaigns.clear()
    for d in real.store.devices.values():
        d.state = DeviceState.IDLE
        d.batch_index = -1
    real.start_campaign("camp-real", "2.0.0", [1.0], min_samples=1,
                        command_ttl_seconds=0.2, max_in_flight_per_batch=1)
    target = next(d.device_id for d in real.store.devices.values()
                  if d.state == DeviceState.DELIVERED)
    real.start_auto_sweeper(interval_seconds=0.05)
    deadline = _time.time() + 3
    while _time.time() < deadline:
        if ctx.store.devices[target].state == DeviceState.ROLLED_BACK:
            break
        _time.sleep(0.02)
    assert ctx.store.devices[target].state == DeviceState.ROLLED_BACK
    assert ctx.store.devices[target].current_version == "1.0.0"
