"""HTTP 入口冒烟测试：验证各入口与服务层结论一致。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from ota.api import create_app


def test_end_to_end(tmp_path):
    client = TestClient(create_app(str(tmp_path / "state.json")))
    client.post("/firmwares", json={"version": "2.0.0", "models": ["model-a"]})
    for i in range(6):
        client.post("/devices",
                    json={"device_id": f"d{i}", "model": "model-a",
                          "version": "1.0.0"})
    resp = client.post("/campaigns", json={
        "campaign_id": "c1", "version": "2.0.0",
        "batch_ratios": [0.5, 0.5], "min_samples": 1,
    })
    assert resp.status_code == 200

    # 设备主动拉取：首批已预投递，首次与反复拉取都拿到同一条 already_delivered
    fetch = client.get("/campaigns/c1/devices/d0/command").json()
    assert fetch["status"] == "already_delivered"
    command_id = fetch["command"]["command_id"]
    again = client.get("/campaigns/c1/devices/d0/command").json()
    assert again["status"] == "already_delivered"
    assert again["command"]["command_id"] == command_id

    # 重复上报：第二次被幂等拒绝，原因可区分
    body = {"device_id": "d0", "campaign_id": "c1",
            "idempotency_key": "k1", "seq": 1, "healthy": True}
    assert client.post("/reports", json=body).json() == {
        "accepted": True, "reason": "accepted"}
    dup = client.post("/reports", json=body).json()
    assert dup == {"accepted": False, "reason": "duplicate"}

    # 回滚入口：同批次失败设备回退到最近可用版本
    client.get("/campaigns/c1/devices/d5/command")
    client.post("/reports", json={"device_id": "d5", "campaign_id": "c1",
                                  "idempotency_key": "k2", "seq": 1,
                                  "healthy": False})
    assert client.post("/devices/d5/rollback", json={}).json()["rolled_back"]
    dev = client.get("/devices/d5").json()
    assert dev["state"] == "rolled_back" and dev["current_version"] == "1.0.0"


def test_fetch_endpoint_statuses_and_sweep(tmp_path):
    with TestClient(create_app(str(tmp_path / "state.json"))) as client:
        client.post("/firmwares", json={"version": "3.0.0", "models": ["mx"]})
        client.post("/devices", json={"device_id": "a", "model": "mx",
                                      "version": "1.0.0"})
        client.post("/devices", json={"device_id": "b", "model": "other",
                                      "version": "1.0.0"})
        client.post("/campaigns", json={
            "campaign_id": "c", "version": "3.0.0", "batch_ratios": [1.0],
            "min_samples": 1, "command_ttl_seconds": 0.2,
            "max_in_flight_per_batch": 10})
        # 已纳入：返回指令，重复拉取同一条
        f1 = client.get("/campaigns/c/devices/a/command").json()
        assert f1["status"] == "already_delivered" and f1["command"]
        cid = f1["command"]["command_id"]
        assert client.get("/campaigns/c/devices/a/command").json()[
            "command"]["command_id"] == cid
        # 未纳入 / 未知，状态可区分
        assert client.get("/campaigns/c/devices/b/command").json()[
            "status"] == "not_in_campaign"
        assert client.get("/campaigns/c/devices/x/command").json()[
            "status"] == "unknown_device"
        # 手动触发超时收尾：未过期 -> 空
        assert client.post("/campaigns/c/sweep").json()["rolled_back"] == []
