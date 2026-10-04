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
    command_ttl_seconds: float = 300.0
    max_in_flight_per_batch: int = 2_000_000_000


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
                body.command_ttl_seconds, body.max_in_flight_per_batch,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"campaign_id": body.campaign_id, "rejected": rejected}

    @app.get("/devices/{device_id}/commands")
    def pull_command(device_id: str, campaign_id: str | None = None) -> dict:
        """设备主动取当前应执行的指令；反复取始终是同一条。"""
        result = service.pull_command(device_id, campaign_id)
        command = result.command
        return {
            "status": result.status.value,
            "command": None if command is None else {
                "command_id": command.command_id,
                "campaign_id": command.campaign_id,
                "device_id": command.device_id,
                "version": command.version,
                "issued_at": command.issued_at,
                "deadline": command.deadline,
            },
        }

    @app.post("/campaigns/{campaign_id}/sweep")
    def sweep(campaign_id: str) -> dict:
        """可重复触发的自动收尾入口：超时未上报的设备自动判失败并回退。"""
        if campaign_id not in store.campaigns:
            raise HTTPException(status_code=404, detail="发布不存在")
        rolled_back = service.sweep_expired(campaign_id)
        return {"rolled_back": rolled_back}

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
            "campaign_id": d.campaign_id,
            "command_id": d.command_id,
            "command_deadline": d.command_deadline,
        }

    return app
