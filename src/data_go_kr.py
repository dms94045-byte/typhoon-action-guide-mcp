from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .utils import TTLCache

BASE_URL = "https://apis.data.go.kr/1360000/TyphoonInfoService/getTyphoonInfo"


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _as_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    # data.go.kr 응답 구조: response.body.items.item
    try:
        body = payload.get("response", {}).get("body", {})
        items = body.get("items", {})
        item = items.get("item", [])
        if isinstance(item, dict):
            return [item]
        return list(item)
    except Exception:
        return []


def _total_count(payload: Dict[str, Any]) -> int:
    try:
        body = payload.get("response", {}).get("body", {})
        return _safe_int(body.get("totalCount", 0), 0)
    except Exception:
        return 0


def _parse_dt_14(dt_str: str) -> Optional[datetime]:
    # e.g., 201709031600
    try:
        return datetime.strptime(dt_str, "%Y%m%d%H%M")
    except Exception:
        return None


class DataGoKrTyphoonClient:
    def __init__(self, service_key: str, cache: Optional[TTLCache] = None):
        self.service_key = service_key
        self.cache = cache or TTLCache(120)

    async def fetch_typhoon_info(
        self,
        from_yyyymmdd: str,
        to_yyyymmdd: str,
        page_no: int = 1,
        num_of_rows: int = 100,
    ) -> Dict[str, Any]:
        cache_key = f"getTyphoonInfo:{from_yyyymmdd}:{to_yyyymmdd}:{page_no}:{num_of_rows}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "serviceKey": self.service_key,
            "pageNo": page_no,
            "numOfRows": num_of_rows,
            "dataType": "JSON",
            "fromTmFc": from_yyyymmdd,
            "toTmFc": to_yyyymmdd,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        self.cache.set(cache_key, data)
        return data

    async def list_unique_typhoons_in_range(
        self,
        from_d: date,
        to_d: date,
        max_pages: int = 20,
        num_of_rows: int = 100,
    ) -> List[Dict[str, Any]]:
        """기간 내 등장하는 태풍(typSeq) 요약 목록을 만든다."""
        from_s = _yyyymmdd(from_d)
        to_s = _yyyymmdd(to_d)

        page = 1
        seen: Dict[str, Dict[str, Any]] = {}

        while page <= max_pages:
            payload = await self.fetch_typhoon_info(from_s, to_s, page_no=page, num_of_rows=num_of_rows)
            items = _as_items(payload)
            if not items:
                break

            for it in items:
                typ_seq = str(it.get("typSeq", "")).strip()
                if not typ_seq:
                    continue

                name_kr = str(it.get("typName", "")).strip()
                name_en = str(it.get("typEn", "")).strip()
                typ_tm = str(it.get("typTm", "")).strip()
                dt = _parse_dt_14(typ_tm)

                if typ_seq not in seen:
                    seen[typ_seq] = {
                        "typSeq": _safe_int(typ_seq, 0),
                        "typName": name_kr,
                        "typEn": name_en,
                        "firstTypTm": typ_tm,
                        "lastTypTm": typ_tm,
                        "firstDt": dt,
                        "lastDt": dt,
                    }
                else:
                    if dt and (seen[typ_seq]["firstDt"] is None or dt < seen[typ_seq]["firstDt"]):
                        seen[typ_seq]["firstDt"] = dt
                        seen[typ_seq]["firstTypTm"] = typ_tm
                    if dt and (seen[typ_seq]["lastDt"] is None or dt > seen[typ_seq]["lastDt"]):
                        seen[typ_seq]["lastDt"] = dt
                        seen[typ_seq]["lastTypTm"] = typ_tm

            total = _total_count(payload)
            if page * num_of_rows >= total:
                break
            page += 1

        # 정렬: 최근 먼저
        out = list(seen.values())
        out.sort(key=lambda x: (x.get("lastDt") or datetime.min), reverse=True)

        # 내부 dt 제거
        for o in out:
            o.pop("firstDt", None)
            o.pop("lastDt", None)
        return out

    async def get_track_points(
        self,
        typ_seq: int,
        from_d: date,
        to_d: date,
        max_pages: int = 20,
        num_of_rows: int = 100,
    ) -> List[Dict[str, Any]]:
        """기간 내 특정 typSeq의 track point(발표 기반)를 뽑아 시간순 정렬."""
        from_s = _yyyymmdd(from_d)
        to_s = _yyyymmdd(to_d)

        page = 1
        pts: List[Dict[str, Any]] = []
        while page <= max_pages:
            payload = await self.fetch_typhoon_info(from_s, to_s, page_no=page, num_of_rows=num_of_rows)
            items = _as_items(payload)
            if not items:
                break

            for it in items:
                if _safe_int(it.get("typSeq", 0), 0) != typ_seq:
                    continue
                typ_tm = str(it.get("typTm", "")).strip()
                dt = _parse_dt_14(typ_tm)
                try:
                    lat = float(it.get("typLat"))
                    lon = float(it.get("typLon"))
                except Exception:
                    continue

                pts.append(
                    {
                        "typTm": typ_tm,
                        "dt": dt,
                        "lat": lat,
                        "lon": lon,
                        "typDir": it.get("typDir"),
                        "typSp": it.get("typSp"),
                        "typPs": it.get("typPs"),
                        "typWs": it.get("typWs"),
                        "typLoc": it.get("typLoc"),
                        "tmFc": it.get("tmFc"),
                        "tmSeq": it.get("tmSeq"),
                    }
                )

            total = _total_count(payload)
            if page * num_of_rows >= total:
                break
            page += 1

        pts.sort(key=lambda x: x.get("dt") or datetime.min)
        for p in pts:
            p.pop("dt", None)
        return pts


def build_client_from_env(cache: Optional[TTLCache] = None) -> DataGoKrTyphoonClient:
    key = os.getenv("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        raise RuntimeError("DATA_GO_KR_SERVICE_KEY 환경변수가 비어있습니다. .env를 설정하세요.")
    return DataGoKrTyphoonClient(service_key=key, cache=cache)


def default_recent_range(days_back: int = 3, days_forward: int = 1) -> Tuple[date, date]:
    today = date.today()
    return (today - timedelta(days=days_back), today + timedelta(days=days_forward))
