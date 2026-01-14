from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 간 대원거리(km)."""
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


@dataclass
class CacheItem:
    value: Any
    expires_at: float


class TTLCache:
    """아주 단순한 메모리 TTL 캐시(단일 프로세스)."""

    def __init__(self, ttl_seconds: int = 120):
        self.ttl_seconds = ttl_seconds
        self._store: Dict[str, CacheItem] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        if item.expires_at < time.time():
            self._store.pop(key, None)
            return None
        return item.value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._store[key] = CacheItem(value=value, expires_at=time.time() + ttl)

    def clear(self) -> None:
        self._store.clear()
