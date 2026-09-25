"""OTA 灰度发布与回滚服务的 HTTP 入口。

本地存储模拟设备、固件与健康上报；所有入口共享同一个
RolloutService，保证灰度推进、状态流转与回滚结论一致。
"""
import os
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ota.models import Firmware, Report
from ota.service import RolloutService
from ota.store import Store

STORE_PATH = os.environ.get("OTA_STORE_PATH", "ota_store.json")
service = RolloutService(Store(STORE_PATH))
app = FastAPI(title="OTA Fleet Rollout Service")


class DeviceIn(BaseModel):
    device_id: str
    hardware: str
    version: str


class RolloutIn(BaseModel):
    rollout_id: str
    target_version: str
    compatible_hardware: list[str]
    batch_ratios: list[float]
    min_samples: int = 1
    min_health_rate: float = 1.0
    report_timeout_s: float = 300.0


class ReportIn(BaseModel):
    report_id: str
    rollout_id: str
    device_id: str
    seq: int
    healthy: bool
    version: str
    ts: float | None = None


@app.post("/devices")
def register_device(body: DeviceIn):
    d = service.register_device(body.device_id, body.hardware, body.version)
    return d.to_dict()


@app.post("/rollouts")
def create_rollout(body: RolloutIn):
    try:
        r = service.create_rollout(
            body.rollout_id,
            Firmware(body.target_version, body.compatible_hardware),
            body.batch_ratios,
            body.min_samples,
            body.min_health_rate,
            body.report_timeout_s,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return r.to_dict()


@app.get("/rollouts/{rollout_id}")
def get_rollout(rollout_id: str):
    r = service._store.get_rollout(rollout_id)
    if r is None:
        raise HTTPException(status_code=404, detail="unknown rollout")
    return r.to_dict()


@app.post("/rollouts/{rollout_id}/commands/{device_id}")
def pull_command(rollout_id: str, device_id: str):
    cmd = service.pull_command(device_id, rollout_id)
    if cmd is None:
        raise HTTPException(status_code=409, detail="no command available")
    return cmd


@app.post("/reports")
def ingest_report(body: ReportIn):
    report = Report(
        report_id=body.report_id,
        rollout_id=body.rollout_id,
        device_id=body.device_id,
        seq=body.seq,
        healthy=body.healthy,
        version=body.version,
        ts=body.ts if body.ts is not None else time.time(),
    )
    result = service.ingest_report(report)
    if not result.accepted:
        # 被拒绝的上报给出可区分原因
        raise HTTPException(
            status_code=409,
            detail={
                "reason": result.reason.value if result.reason else "unknown",
                "device_state": result.device_state.value if result.device_state else None,
            },
        )
    return {"accepted": True, "device_state": result.device_state.value}


@app.post("/rollouts/{rollout_id}/advance")
def advance(rollout_id: str):
    try:
        return service.try_advance(rollout_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown rollout")


@app.post("/rollouts/{rollout_id}/rollback")
def rollback_rollout(rollout_id: str):
    try:
        return service.rollback_rollout(rollout_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown rollout")


@app.post("/devices/{device_id}/rollback")
def rollback_device(device_id: str, rollout_id: str):
    try:
        return service.rollback_device(device_id, rollout_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown device")


@app.post("/rollouts/{rollout_id}/sweep")
def sweep_timeouts(rollout_id: str, now: float | None = None):
    try:
        timed_out = service.sweep_timeouts(rollout_id, now if now is not None else time.time())
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown rollout")
    return {"timed_out": [d.device_id for d in timed_out]}


@app.get("/devices/{device_id}")
def get_device(device_id: str):
    d = service._store.get_device(device_id)
    if d is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return d.to_dict()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
