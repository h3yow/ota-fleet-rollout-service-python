"""批次划分：同一设备必须稳定归属同一批次。

规则：对 device_id 做 SHA-256，取前 8 字节映射到 [0, 100) 的桶；
批次比例为累计比例（如 [0.1, 0.5, 1.0]），桶号 < ratio*100 即落入该批次。
纯函数、无状态，因此设备归属与重启、并发、上报顺序无关。
"""
from __future__ import annotations

import hashlib


def device_bucket(device_id: str) -> int:
    """将设备稳定映射到 [0, 100) 的桶。"""
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return value % 100


def batch_of(device_id: str, batch_ratios: list[float]) -> int:
    """根据累计比例计算设备所属批次序号（0 起）。

    超出最后一个比例（如比例未覆盖到 1.0）的设备归入最后一个批次之后，
    返回 len(batch_ratios)，表示本次发布不覆盖。
    """
    bucket = device_bucket(device_id)
    for i, ratio in enumerate(batch_ratios):
        if bucket < ratio * 100:
            return i
    return len(batch_ratios)
