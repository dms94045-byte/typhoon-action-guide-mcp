from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

from .utils import TTLCache

# NOTE:
# 기상청 API허브(https://apihub.kma.go.kr)는 로그인이 필요한 경우가 많고,
# API 종류/URL이 계정별로 달라질 수 있습니다.
# 이 모듈은 '붙일 자리'를 제공하는 수준으로, 실제 URL/파라미터는
# 사용자가 API허브에서 발급받은 문서를 기준으로 업데이트하는 것을 권장합니다.


class KmaApiHubClient:
    def __init__(self, auth_key: str, base_url: str, cache: Optional[TTLCache] = None):
        self.auth_key = auth_key
        self.base_url = base_url.rstrip("/")
        self.cache = cache or TTLCache(60)

    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        # API허브는 일반적으로 authKey를 쿼리로 요구
        params.setdefault("authKey", self.auth_key)

        url = f"{self.base_url}/{path.lstrip('/')}"
        cache_key = f"apihub:{url}:{sorted(params.items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        self.cache.set(cache_key, data)
        return data


def build_client_from_env(cache: Optional[TTLCache] = None) -> Optional[KmaApiHubClient]:
    auth_key = os.getenv("KMA_APIHUB_KEY", "").strip()
    base_url = os.getenv("KMA_APIHUB_BASE_URL", "").strip()
    if not auth_key or not base_url:
        return None
    return KmaApiHubClient(auth_key=auth_key, base_url=base_url, cache=cache)
