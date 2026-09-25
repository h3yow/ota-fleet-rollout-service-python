"""FastAPI 入口：设备注册、上报、推进与回滚的 HTTP 接口。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .models import HealthReport
from .service import RolloutService
from .store import Store


class DeviceIn(BaseModel):
    device_id: str
    model: str
    version: str


class FirmwareIn(BaseModel):
    version: str
    models: list[str]


class CampaignIn(BaseModel):
    campaign_id: str
    version: str
    batch_ratios: list[float]
    min_samples: int = 3
    min_health_ratio: float = 0.9


class ReportIn(BaseModel):
    device_id: str
    campaign_id: str
    idempotency_key: str
    seq: int
    healthy: bool


class RollbackIn(BaseModel):
    reason: str = "health_check_failed"


def create_app(store_path: str = "ota_state.json") -> FastAPI:
    store = Store(store_path)
    store.load()
    service = RolloutService(store)
    app = FastAPI(title="OTA Fleet Rollout Service")

    @app.post("/devices")
    def register_device(body: DeviceIn) -> dict:
        d = service.register_device(body.device_id, body.model, body.version)
        return {"device_id": d.device_id, "state": d.state.value}

    @app.post("/firmwares")
    def register_firmware(body: FirmwareIn) -> dict:
        fw = service.register_firmware(body.version, body.models)
        return {"version": fw.version}

    @app.post("/campaigns")
    def start_campaign(body: CampaignIn) -> dict:
        try:
            rejected = service.start_campaign(
                body.campaign_id, body.version, body.batch_ratios,
                body.min_samples, body.min_health_ratio,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"campaign_id": body.campaign_id, "rejected": rejected}

    @app.post("/reports")
    def report(body: ReportIn) -> dict:
        accepted, reason = service.handle_report(HealthReport(
            device_id=body.device_id,
            campaign_id=body.campaign_id,
            idempotency_key=body.idempotency_key,
            seq=body.seq,
            healthy=body.healthy,
        ))
        return {"accepted": accepted, "reason": reason}

    @app.post("/campaigns/{campaign_id}/advance")
    def advance(campaign_id: str) -> dict:
        if campaign_id not in store.campaigns:
            raise HTTPException(status_code=404, detail="发布不存在")
        return {"advanced": service.maybe_advance(campaign_id)}

    @app.post("/devices/{device_id}/rollback")
    def rollback(device_id: str, body: RollbackIn) -> dict:
        return {"rolled_back": service.rollback_device(device_id, body.reason)}

    @app.get("/devices/{device_id}")
    def get_device(device_id: str) -> dict:
        d = store.devices.get(device_id)
        if d is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        return {
            "device_id": d.device_id,
            "state": d.state.value,
            "current_version": d.current_version,
            "batch_index": d.batch_index,
        }

    return app
