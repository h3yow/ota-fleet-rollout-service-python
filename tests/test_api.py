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

    # 重复上报：第二次被幂等拒绝，原因可区分
    body = {"device_id": "d0", "campaign_id": "c1",
            "idempotency_key": "k1", "seq": 1, "healthy": True}
    assert client.post("/reports", json=body).json() == {
        "accepted": True, "reason": "accepted"}
    dup = client.post("/reports", json=body).json()
    assert dup == {"accepted": False, "reason": "duplicate"}

    # 回滚入口：失败设备回退到最近可用版本
    client.post("/reports", json={"device_id": "d1", "campaign_id": "c1",
                                  "idempotency_key": "k2", "seq": 1,
                                  "healthy": False})
    assert client.post("/devices/d1/rollback", json={}).json()["rolled_back"]
    dev = client.get("/devices/d1").json()
    assert dev["state"] == "rolled_back" and dev["current_version"] == "1.0.0"
