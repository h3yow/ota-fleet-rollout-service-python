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
    command_ttl_seconds: float = 3600.0
    max_in_flight_per_batch: int = 1_000_000


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

    @app.on_event("startup")
    def _start_sweeper() -> None:
        # 无人值守的超时自动收尾；重启后以持久化的 expires_at 继续判定
        service.start_auto_sweeper(interval_seconds=5.0)

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

    @app.get("/campaigns/{campaign_id}/devices/{device_id}/command")
    def fetch_command(campaign_id: str, device_id: str) -> dict:
        """设备主动拉取自己当前应执行的指令（反复拉取幂等，同一条指令）。"""
        result = service.fetch_command(device_id, campaign_id)
        payload: dict = {
            "device_id": device_id,
            "campaign_id": campaign_id,
            "status": result.status.value,
            "detail": result.detail,
        }
        if result.command is not None:
            payload["command"] = {
                "command_id": result.command.command_id,
                "version": result.command.version,
                "issued_at": result.command.issued_at,
                "expires_at": result.command.expires_at,
            }
        else:
            payload["command"] = None
        return payload

    @app.post("/campaigns/{campaign_id}/sweep")
    def sweep_timeouts(campaign_id: str) -> dict:
        """手动触发一次超时自动收尾（可重复触发，规则与后台收尾完全一致）。"""
        if campaign_id not in store.campaigns:
            raise HTTPException(status_code=404, detail="发布不存在")
        rolled_back = service.sweep_timeouts(campaign_id)
        return {"rolled_back": rolled_back, "count": len(rolled_back)}

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
            "last_good_version": d.last_good_version,
            "command_id": d.command_id,
            "command_version": d.command_version,
            "command_expires_at": d.command_expires_at,
        }

    return app
