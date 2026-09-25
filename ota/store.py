"""本地 JSON 持久化存储：模拟设备、固件与发布状态的本地存储。

所有读写在同一把 RLock 下完成，写盘采用 tmp+rename 原子替换，
重启后从磁盘恢复，保证断点续传不丢不重。
"""
from __future__ import annotations

import json
import os
import threading

from .models import Device, Rollout


class Store:
    """线程安全的本地 JSON 存储。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._rollouts: dict[str, Rollout] = {}
        self.load()

    # ---- 持久化 ----
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._devices = {
                d["device_id"]: Device.from_dict(d) for d in data.get("devices", [])
            }
            self._rollouts = {
                r["rollout_id"]: Rollout.from_dict(r) for r in data.get("rollouts", [])
            }

    def save(self) -> None:
        """原子写盘：先写临时文件再 rename，避免崩溃时写坏数据。"""
        with self._lock:
            payload = {
                "devices": [d.to_dict() for d in self._devices.values()],
                "rollouts": [r.to_dict() for r in self._rollouts.values()],
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)

    # ---- 设备 ----
    def upsert_device(self, device: Device) -> None:
        with self._lock:
            self._devices[device.device_id] = device

    def get_device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def list_devices(self) -> list[Device]:
        with self._lock:
            return list(self._devices.values())

    # ---- 发布 ----
    def upsert_rollout(self, rollout: Rollout) -> None:
        with self._lock:
            self._rollouts[rollout.rollout_id] = rollout

    def get_rollout(self, rollout_id: str) -> Rollout | None:
        with self._lock:
            return self._rollouts.get(rollout_id)

    def list_rollouts(self) -> list[Rollout]:
        with self._lock:
            return list(self._rollouts.values())

    # ---- 锁 ----
    @property
    def lock(self) -> threading.RLock:
        return self._lock
