"""本地 JSON 存储：模拟设备、固件、发布与上报的持久化。"""
from __future__ import annotations

import json
import os
import threading

from .models import Campaign, Device, DeviceState, Firmware


class Store:
    """线程安全的内存 + JSON 落盘存储。

    所有写操作在锁内完成并立即落盘，保证重启后不丢失、不重复推进。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.firmwares: dict[str, Firmware] = {}
        self.campaigns: dict[str, Campaign] = {}
        self.seen_idempotency_keys: set[str] = set()

    # ---------- 序列化 ----------

    def _device_to_dict(self, d: Device) -> dict:
        return {
            "device_id": d.device_id,
            "model": d.model,
            "current_version": d.current_version,
            "state": d.state.value,
            "batch_index": d.batch_index,
            "last_report_seq": d.last_report_seq,
            "last_good_version": d.last_good_version,
            "command_id": d.command_id,
            "command_version": d.command_version,
            "command_issued_at": d.command_issued_at,
            "command_expires_at": d.command_expires_at,
        }

    def _device_from_dict(self, data: dict) -> Device:
        return Device(
            device_id=data["device_id"],
            model=data["model"],
            current_version=data["current_version"],
            state=DeviceState(data["state"]),
            batch_index=data["batch_index"],
            last_report_seq=data["last_report_seq"],
            last_good_version=data["last_good_version"],
            command_id=data.get("command_id", ""),
            command_version=data.get("command_version", ""),
            command_issued_at=data.get("command_issued_at", 0.0),
            command_expires_at=data.get("command_expires_at", 0.0),
        )

    def _campaign_to_dict(self, c: Campaign) -> dict:
        return {
            "campaign_id": c.campaign_id,
            "target": {
                "version": c.target.version,
                "compatible_models": list(c.target.compatible_models),
            },
            "batch_ratios": list(c.batch_ratios),
            "min_samples": c.min_samples,
            "min_health_ratio": c.min_health_ratio,
            "current_batch": c.current_batch,
            "status": c.status,
            "command_ttl_seconds": c.command_ttl_seconds,
            "max_in_flight_per_batch": c.max_in_flight_per_batch,
        }

    def _campaign_from_dict(self, data: dict) -> Campaign:
        return Campaign(
            campaign_id=data["campaign_id"],
            target=Firmware(
                version=data["target"]["version"],
                compatible_models=tuple(data["target"]["compatible_models"]),
            ),
            batch_ratios=list(data["batch_ratios"]),
            min_samples=data["min_samples"],
            min_health_ratio=data["min_health_ratio"],
            current_batch=data["current_batch"],
            status=data["status"],
            command_ttl_seconds=data.get("command_ttl_seconds", 3600.0),
            max_in_flight_per_batch=data.get("max_in_flight_per_batch", 1_000_000),
        )

    # ---------- 持久化 ----------

    def load(self) -> None:
        with self.lock:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.devices = {
                k: self._device_from_dict(v) for k, v in data.get("devices", {}).items()
            }
            self.firmwares = {
                k: Firmware(version=v["version"],
                            compatible_models=tuple(v["compatible_models"]))
                for k, v in data.get("firmwares", {}).items()
            }
            self.campaigns = {
                k: self._campaign_from_dict(v)
                for k, v in data.get("campaigns", {}).items()
            }
            self.seen_idempotency_keys = set(data.get("idempotency_keys", []))

    def save(self) -> None:
        with self.lock:
            data = {
                "devices": {k: self._device_to_dict(v) for k, v in self.devices.items()},
                "firmwares": {
                    k: {"version": v.version,
                        "compatible_models": list(v.compatible_models)}
                    for k, v in self.firmwares.items()
                },
                "campaigns": {k: self._campaign_to_dict(v)
                              for k, v in self.campaigns.items()},
                "idempotency_keys": sorted(self.seen_idempotency_keys),
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换，避免写一半损坏
