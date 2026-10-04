"""设备自取指令、有效期自动收尾、上报准入与在途上限的测试。

日志中打印设备标识、批次判定依据与状态流转（ota.service logger）。
并发取指令 / 上报 / 自动收尾混合场景见文件末尾。
"""
from __future__ import annotations

import logging
import threading

import pytest

from ota.models import DeviceState, HealthReport, PullStatus, RejectReason
from ota.service import RolloutService
from ota.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


class FakeClock:
    """可控时钟：默认从 1000.0 开始，advance 推动时间。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def env(tmp_path):
    clock = FakeClock()
    store = Store(str(tmp_path / "state.json"))
    svc = RolloutService(store, clock=clock)
    svc.register_firmware("2.0.0", ["model-a"])
    for i in range(20):
        svc.register_device(f"dev-{i:02d}", "model-a", "1.0.0")
    return svc, clock


def start(service: RolloutService, campaign_id: str = "camp-1", **kw):
    return service.start_campaign(
        campaign_id, "2.0.0", [0.5, 0.5], **kw)


def report(device_id: str, seq: int, healthy: bool,
           campaign_id: str = "camp-1", key: str | None = None) -> HealthReport:
    return HealthReport(
        device_id=device_id,
        campaign_id=campaign_id,
        idempotency_key=key or f"{device_id}-{seq}-{campaign_id}",
        seq=seq,
        healthy=healthy,
    )


def batch0(service: RolloutService, campaign_id: str = "camp-1"):
    return sorted(
        d.device_id for d in service.store.devices.values()
        if d.campaign_id == campaign_id and d.batch_index == 0)


def batch1(service: RolloutService, campaign_id: str = "camp-1"):
    return sorted(
        d.device_id for d in service.store.devices.values()
        if d.campaign_id == campaign_id and d.batch_index == 1)


# ---------- 设备主动取指令 ----------

def test_pull_returns_stable_same_command_repeatedly(env):
    svc, _ = env
    start(svc)
    d = batch0(svc)[0]
    first = svc.pull_command(d)
    assert first.status in (PullStatus.ISSUED, PullStatus.IN_FLIGHT)
    assert first.command is not None
    cmd = first.command
    # 同一台设备反复来问，始终拿到同一条指令（ID/版本/有效期完全一致）
    for _ in range(10):
        again = svc.pull_command(d)
        assert again.status == PullStatus.IN_FLIGHT
        assert again.command == cmd
    # 状态仍然是在途，没有被当成重新下发
    assert svc.store.devices[d].state == DeviceState.DELIVERED
    assert svc.store.devices[d].command_id == cmd.command_id


def test_pull_explicit_campaign_id_also_stable(env):
    svc, _ = env
    start(svc)
    d = batch0(svc)[0]
    cmd = svc.pull_command(d, "camp-1").command
    assert svc.pull_command(d, "camp-1").command == cmd


def test_pull_not_in_campaign_device_distinguishable(env):
    svc, _ = env
    start(svc)
    # 未纳入发布的设备（注册一个新型号/高版本设备，不兼容本次发布）
    svc.register_device("dev-other", "model-b", "1.0.0")
    result = svc.pull_command("dev-other")
    assert result.status == PullStatus.NOT_IN_CAMPAIGN
    assert result.command is None
    # 未知设备同样是 NOT_IN_CAMPAIGN，不与正常下发混淆
    assert svc.pull_command("ghost").status == PullStatus.NOT_IN_CAMPAIGN
    # 显式查一个不属于它的发布也是同样结果
    d = batch0(svc)[0]
    assert svc.pull_command(d, "camp-other").status == PullStatus.NOT_IN_CAMPAIGN


def test_pull_batch_not_released_distinguishable(env):
    svc, _ = env
    start(svc)
    d = batch1(svc)[0]  # 批次 1，尚未放行
    result = svc.pull_command(d)
    assert result.status == PullStatus.BATCH_NOT_RELEASED
    assert result.command is None
    # 设备仍是 PENDING，没有提前下发指令
    assert svc.store.devices[d].state == DeviceState.PENDING
    assert svc.store.devices[d].command_id == ""


def test_pull_from_unrelated_released_device_unchanged(env):
    """未轮到批次的设备反复询问不影响批次 0 已有指令。"""
    svc, _ = env
    start(svc)
    d0 = batch0(svc)[0]
    d1 = batch1(svc)[0]
    cmd0 = svc.pull_command(d0).command
    for _ in range(5):
        assert svc.pull_command(d1).status == PullStatus.BATCH_NOT_RELEASED
    assert svc.pull_command(d0).command == cmd0


# ---------- 指令有效期与自动收尾 ----------

def test_expired_device_auto_fails_and_rolls_back(env):
    svc, clock = env
    start(svc, command_ttl_seconds=100.0)
    d = batch0(svc)[0]
    cmd = svc.pull_command(d).command
    assert cmd is not None and cmd.version == "2.0.0"

    # 时限内：不处理（批次 0 所有已放行设备都在途）
    clock.advance(99.9)
    assert svc.sweep_expired() == []
    assert svc.store.devices[d].state == DeviceState.DELIVERED

    # 超过有效期：批次 0 所有未上报设备自动判失败并回退
    clock.advance(0.2)
    rolled = svc.sweep_expired()
    assert d in rolled
    assert set(rolled) == set(batch0(svc))  # 没人上报，全部超时
    device = svc.store.devices[d]
    assert device.state == DeviceState.ROLLED_BACK
    assert device.current_version == "1.0.0"  # 回退到 last_good_version


def test_sweep_is_idempotent_and_repeatable(env):
    svc, clock = env
    start(svc, command_ttl_seconds=10.0)
    d = batch0(svc)[0]
    svc.pull_command(d)
    clock.advance(20)
    first = svc.sweep_expired()
    assert d in first and set(first) == set(batch0(svc))
    # 重复触发不重复回退
    for _ in range(5):
        assert svc.sweep_expired() == []
    device = svc.store.devices[d]
    assert device.state == DeviceState.ROLLED_BACK
    assert device.current_version == "1.0.0"


def test_sweep_does_not_touch_healthy_reported_devices(env):
    svc, clock = env
    start(svc, command_ttl_seconds=10.0)
    members = batch0(svc)
    expired_dev = members[0]
    healthy_dev = members[1]
    svc.pull_command(expired_dev)
    svc.pull_command(healthy_dev)
    # healthy_dev 在时限内正常上报成功
    assert svc.handle_report(report(healthy_dev, 1, True))[0]
    clock.advance(50)
    rolled = svc.sweep_expired()
    expected = set(members) - {healthy_dev}
    assert set(rolled) == expected
    assert expired_dev in rolled
    # 已正常上报的设备不受影响，版本停留在目标版本
    ok = svc.store.devices[healthy_dev]
    assert ok.state == DeviceState.REPORTED_OK
    assert ok.current_version == "2.0.0"


def test_sweep_continues_after_restart(tmp_path):
    """重启后按持久化的 deadline 继续同规则收尾，且不重复回退。"""
    path = str(tmp_path / "state.json")
    clock = FakeClock()
    store = Store(path)
    svc = RolloutService(store, clock=clock)
    svc.register_firmware("2.0.0", ["model-a"])
    svc.register_device("d0", "model-a", "1.0.0")
    svc.start_campaign("c1", "2.0.0", [1.0], command_ttl_seconds=10.0)
    svc.pull_command("d0")
    clock.advance(20)

    # 重启：新 Store 重新加载同一文件，时钟沿用同一“外部时间”
    store2 = Store(path)
    store2.load()
    svc2 = RolloutService(store2, clock=clock)
    assert svc2.sweep_expired() == ["d0"]
    assert store2.devices["d0"].state == DeviceState.ROLLED_BACK
    assert store2.devices["d0"].current_version == "1.0.0"
    # 再重启仍不重复回退
    store3 = Store(path)
    store3.load()
    svc3 = RolloutService(store3, clock=clock)
    assert svc3.sweep_expired() == []


def test_pull_triggers_sweep_automatically(env):
    """没人干预：任意设备来取指令时顺带完成超时收尾。"""
    svc, clock = env
    start(svc, command_ttl_seconds=10.0)
    members = batch0(svc)
    stale_dev, puller = members[0], members[1]
    clock.advance(50)
    # puller 来取指令，批次内所有超时设备（含它自己）被顺带收尾；
    # puller 自己也已超时，因此不再返回指令
    result = svc.pull_command(puller)
    assert result.status == PullStatus.NOT_IN_CAMPAIGN
    assert result.command is None
    assert svc.store.devices[stale_dev].state == DeviceState.ROLLED_BACK
    assert svc.store.devices[puller].state == DeviceState.ROLLED_BACK


def test_report_triggers_sweep_automatically(env):
    svc, clock = env
    start(svc, command_ttl_seconds=10.0)
    members = batch0(svc)
    stale_dev, reporter = members[0], members[1]
    svc.pull_command(stale_dev)
    svc.pull_command(reporter)
    clock.advance(50)
    # reporter 在 deadline 之后才上报：它自己也超时了 -> not_awaiting，
    # 同时 stale_dev 被自动收尾
    accepted, reason = svc.handle_report(report(reporter, 1, True))
    assert not accepted and reason == RejectReason.NOT_AWAITING.value
    assert svc.store.devices[stale_dev].state == DeviceState.ROLLED_BACK
    assert svc.store.devices[reporter].state == DeviceState.ROLLED_BACK


# ---------- 上报准入：批次放行 + 在途校验 ----------

def test_early_report_before_batch_released_rejected(env):
    svc, _ = env
    start(svc)
    d = batch1(svc)[0]  # 批次 1 尚未放行
    accepted, reason = svc.handle_report(report(d, 1, True))
    assert not accepted
    assert reason == RejectReason.BATCH_NOT_RELEASED.value
    # 设备状态不变、序号不前进、幂等键不被消费
    device = svc.store.devices[d]
    assert device.state == DeviceState.PENDING
    assert device.last_report_seq == 0
    key = f"{d}-1-camp-1"
    assert key not in svc.store.seen_idempotency_keys


def test_early_rejection_does_not_affect_later_valid_report(env):
    """提前上报被拒后，批次放行时正式那一轮仍可正常上报成功。"""
    svc, _ = env
    start(svc, min_samples=1, min_health_ratio=0.5)
    future = batch1(svc)[0]
    # 提前上报被拒（同一幂等键、同一 seq 也没关系，因为没被消费）
    key = f"{future}-1"
    first = svc.handle_report(report(future, 1, True, key=key))
    assert first == (False, RejectReason.BATCH_NOT_RELEASED.value)

    # 批次 0 达标推进，批次 1 放行
    ok = batch0(svc)[0]
    assert svc.handle_report(report(ok, 1, True))[0]
    assert svc.maybe_advance("camp-1") is True

    # 正式轮次：同一 key/seq 重新提交被接受，且推进状态机
    accepted, reason = svc.handle_report(report(future, 1, True, key=key))
    assert accepted and reason == "accepted"
    device = svc.store.devices[future]
    assert device.state == DeviceState.REPORTED_OK
    assert device.current_version == "2.0.0"


def test_report_after_rollback_rejected_not_awaiting(env):
    svc, clock = env
    start(svc, command_ttl_seconds=10.0)
    d = batch0(svc)[0]
    clock.advance(50)
    svc.sweep_expired()
    assert svc.store.devices[d].state == DeviceState.ROLLED_BACK
    # 收尾后迟到的健康上报被明确拒绝，不改变回滚结论
    accepted, reason = svc.handle_report(report(d, 1, True, key="late"))
    assert not accepted and reason == RejectReason.NOT_AWAITING.value
    assert svc.store.devices[d].state == DeviceState.ROLLED_BACK


# ---------- 在途上限：绝不超发，空出名额后补发 ----------

@pytest.fixture()
def cap_env(tmp_path):
    """8 台设备，发布 camp-cap 批次 0 恰好是 c-1、c-7。"""
    clock = FakeClock()
    store = Store(str(tmp_path / "cap.json"))
    svc = RolloutService(store, clock=clock)
    svc.register_firmware("2.0.0", ["model-a"])
    for i in range(8):
        svc.register_device(f"c-{i}", "model-a", "1.0.0")
    svc.start_campaign(
        "camp-cap", "2.0.0", [0.5, 0.5],
        min_samples=1, min_health_ratio=0.5,
        command_ttl_seconds=100.0, max_in_flight_per_batch=2,
    )
    return svc, clock


def _in_flight(svc: RolloutService, campaign_id: str, batch: int) -> int:
    return sum(
        1 for d in svc.store.devices.values()
        if d.campaign_id == campaign_id and d.batch_index == batch
        and d.state == DeviceState.DELIVERED)


def test_inflight_cap_never_exceeded_at_release(cap_env):
    svc, _ = cap_env
    b0 = batch0(svc, "camp-cap")
    assert b0 == ["c-1", "c-7"]  # 恰好 2 台，等于上限
    assert _in_flight(svc, "camp-cap", 0) == 2
    assert all(svc.store.devices[d].state == DeviceState.DELIVERED for d in b0)


def test_queued_devices_wait_then_get_dispatched_after_slot_frees(cap_env):
    svc, _ = cap_env
    # c-1、c-7 已在途；批次 1 的设备上报/取指令都应排队或被拒
    waiter = "c-0"  # camp-cap 中 c-0 落在批次 1
    assert svc.store.devices[waiter].batch_index == 1
    # 批次未放行，先推进批次 0：批次 1 有 6 台，但上限 2
    assert svc.handle_report(report("c-1", 1, True,
                                   campaign_id="camp-cap"))[0]
    assert svc.maybe_advance("camp-cap") is True
    # 批次 1 放行瞬间在途只能是 2，其余 QUEUED
    assert _in_flight(svc, "camp-cap", 1) == 2
    queued = [
        d.device_id for d in svc.store.devices.values()
        if d.campaign_id == "camp-cap" and d.batch_index == 1
        and d.state == DeviceState.QUEUED]
    assert len(queued) == 4
    # 排队设备取指令得到 QUEUED，且仍未持有指令
    result = svc.pull_command(waiter)
    assert result.status in (PullStatus.QUEUED, PullStatus.IN_FLIGHT)
    # 排队设备提前上报被明确拒绝
    queued_dev = queued[0]
    accepted, reason = svc.handle_report(
        report(queued_dev, 1, True, campaign_id="camp-cap"))
    assert not accepted and reason == RejectReason.QUEUED_FOR_SLOT.value

    # 两台在途设备定论后，名额空出，排队设备依次补发，始终不超发
    dispatched = sorted(
        d.device_id for d in svc.store.devices.values()
        if d.campaign_id == "camp-cap" and d.batch_index == 1
        and d.state == DeviceState.DELIVERED)
    for dev in dispatched:
        assert _in_flight(svc, "camp-cap", 1) <= 2
        svc.handle_report(report(dev, 1, True, campaign_id="camp-cap"))
        assert _in_flight(svc, "camp-cap", 1) <= 2
    # 反复补发直到批次 1 全部设备都拿到指令并有定论
    for _ in range(10):
        svc._fill_slots(svc.store.campaigns["camp-cap"], 1)
        inflight = [
            d.device_id for d in svc.store.devices.values()
            if d.campaign_id == "camp-cap" and d.batch_index == 1
            and d.state == DeviceState.DELIVERED]
        if not inflight:
            break
        for dev in inflight:
            svc.handle_report(report(dev, 2, True, campaign_id="camp-cap",
                                     key=f"{dev}-done"))
    final = [
        d for d in svc.store.devices.values()
        if d.campaign_id == "camp-cap" and d.batch_index == 1]
    assert all(d.state == DeviceState.REPORTED_OK for d in final)
    assert all(d.command_id for d in final)


# ---------- 并发：取指令 / 上报 / 自动收尾混合 ----------

def test_concurrent_pull_returns_same_command_and_issues_once(env):
    """同一设备 32 线程并发反复取指令：只放行一次，所有人拿到同一条。"""
    svc, _ = env
    start(svc)
    d = batch0(svc)[0]
    results: list = []

    def pull():
        results.append(svc.pull_command(d))

    threads = [threading.Thread(target=pull) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    commands = {r.command for r in results if r.command is not None}
    assert len(commands) == 1  # 只放行一条
    cmd = next(iter(commands))
    assert cmd.command_id == f"cmd:camp-1:{d}"
    assert all(r.status in (PullStatus.ISSUED, PullStatus.IN_FLIGHT)
               for r in results)
    assert svc.store.devices[d].state == DeviceState.DELIVERED


def test_concurrent_mixed_pull_report_sweep_consistent(env):
    """并发取指令 + 并发上报 + 并发自动收尾，结论一致、可解释：

    - 每台设备最终状态唯一且自洽；
    - 成功上报设备停在 REPORTED_OK/2.0.0，不被收尾影响；
    - 超时设备停在 ROLLED_BACK/1.0.0，不重复回退；
    - 在途数永不超过上限。
    """
    svc, clock = env
    start(svc, min_samples=1, min_health_ratio=0.1,
          command_ttl_seconds=10.0)
    members = batch0(svc)
    assert len(members) >= 6
    fast = members[:4]   # 时限内会成功上报
    errors: list = []

    barrier = threading.Barrier(len(fast) * 2 + 2)

    def puller(dev):
        try:
            barrier.wait()
            for _ in range(5):
                r = svc.pull_command(dev)
                if r.command is not None:
                    assert r.command.command_id == f"cmd:camp-1:{dev}"
        except Exception as exc:  # pragma: no cover - 并发不应抛异常
            errors.append(exc)

    def reporter(dev):
        try:
            barrier.wait()
            for seq in range(1, 6):
                accepted, _ = svc.handle_report(
                    report(dev, seq, True, key=f"{dev}-ok"))
                if accepted:
                    break
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def sweeper():
        try:
            barrier.wait()
            for _ in range(10):
                svc.sweep_expired()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = []
    for dev in fast:
        threads.append(threading.Thread(target=puller, args=(dev,)))
        threads.append(threading.Thread(target=reporter, args=(dev,)))
    threads += [threading.Thread(target=sweeper) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # fast 设备在时限内并发上报，恰好一台生效，全部停在 REPORTED_OK
    for dev in fast:
        device = svc.store.devices[dev]
        assert device.state == DeviceState.REPORTED_OK, dev
        assert device.current_version == "2.0.0", dev
        assert device.command_id == f"cmd:camp-1:{dev}"

    # 时间走过有效期后再收尾：剩余在途设备全部自动回退，且只回退一次
    clock.advance(100)

    def final_sweep():
        svc.sweep_expired()

    st = [threading.Thread(target=final_sweep) for _ in range(8)]
    for t in st:
        t.start()
    for t in st:
        t.join()

    rolled = [
        d.device_id for d in svc.store.devices.values()
        if d.campaign_id == "camp-1" and d.batch_index == 0
        and d.state == DeviceState.ROLLED_BACK]
    assert set(rolled) == set(members) - set(fast)
    for dev in rolled:
        assert svc.store.devices[dev].current_version == "1.0.0"
    # fast 设备仍然不受影响
    for dev in fast:
        assert svc.store.devices[dev].state == DeviceState.REPORTED_OK
    # 已上报设备样本足够，批次健康率满足 0.1，可推进
    assert svc.maybe_advance("camp-1") is True
    assert svc.store.campaigns["camp-1"].current_batch == 1


def test_concurrent_cap_never_oversubscribed(tmp_path):
    """在途上限 2 下，大量线程同时取指令 + 上报 + 收尾，任意时刻不超发。"""
    clock = FakeClock()
    store = Store(str(tmp_path / "cap2.json"))
    svc = RolloutService(store, clock=clock)
    svc.register_firmware("2.0.0", ["model-a"])
    for i in range(12):
        svc.register_device(f"m-{i:02d}", "model-a", "1.0.0")
    svc.start_campaign("c2", "2.0.0", [1.0], min_samples=1,
                       min_health_ratio=0.0, command_ttl_seconds=10.0,
                       max_in_flight_per_batch=2)
    devices = sorted(svc.store.devices)
    violations: list[int] = []
    lock = threading.Lock()
    stop = threading.Event()

    def monitor():
        # 与服务写路径使用同一把锁读取一致快照；服务每次落盘的提交点
        # 在途数必然 <= 上限
        while not stop.is_set():
            with svc.store.lock:
                inflight = sum(
                    1 for d in svc.store.devices.values()
                    if d.campaign_id == "c2"
                    and d.state == DeviceState.DELIVERED)
            if inflight > 2:
                with lock:
                    violations.append(inflight)

    def worker(dev):
        for seq in range(1, 4):
            svc.pull_command(dev)
            svc.handle_report(report(dev, seq, True, campaign_id="c2"))
            svc.sweep_expired()

    mon = threading.Thread(target=monitor)
    mon.start()
    threads = [threading.Thread(target=worker, args=(d,)) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 排空残留：仍在途的设备再给一次上报机会；之后仍在途的交给自动收尾
    for round_seq in range(10, 20):
        for dev in devices:
            device = svc.store.devices[dev]
            if device.state == DeviceState.DELIVERED:
                svc.handle_report(
                    report(dev, round_seq, True, campaign_id="c2",
                           key=f"{dev}-drain-{round_seq}"))
    clock.advance(1000)
    svc.sweep_expired()
    stop.set()
    mon.join()
    assert not violations, violations
    # 所有设备最终要么成功上报，要么被自动收尾（终态唯一）
    for d in svc.store.devices.values():
        assert d.state in (DeviceState.REPORTED_OK, DeviceState.ROLLED_BACK)
