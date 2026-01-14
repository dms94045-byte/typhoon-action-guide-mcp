from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from .data_go_kr import build_client_from_env, default_recent_range
from .geo_kr import geocode_korea
from .utils import TTLCache, haversine_km


# ======================
# 유틸
# ======================

def _parse_date_yyyymmdd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _fmt_dt14_kst(typ_tm: str) -> str:
    try:
        dt = datetime.strptime(typ_tm, "%Y%m%d%H%M")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return typ_tm


def _pick_most_relevant_typhoon(typhoons: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return typhoons[0] if typhoons else None


def _summarize_track_near_location(points, loc_lat, loc_lon):
    best = None
    for p in points:
        dist = haversine_km(loc_lat, loc_lon, p["lat"], p["lon"])
        if best is None or dist < best["dist_km"]:
            best = {"dist_km": dist, "p": p}

    if not best:
        return None

    try:
        dt = datetime.strptime(best["p"]["typTm"], "%Y%m%d%H%M")
        window = {
            "center": dt.strftime("%Y-%m-%d %H:%M"),
            "start": (dt - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
            "end": (dt + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
        }
    except Exception:
        window = None

    return {
        "closest": {
            "time": _fmt_dt14_kst(best["p"]["typTm"]),
            "distance_km": round(best["dist_km"], 1),
            "lat": best["p"]["lat"],
            "lon": best["p"]["lon"],
            "loc": best["p"].get("typLoc"),
        },
        "impact_window": window,
    }


# ======================
# MCP 설정
# ======================

cache = TTLCache(int(os.getenv("CACHE_TTL_SECONDS", "120")))
_data_client = None


def _get_data_client():
    global _data_client
    if _data_client is None:
        _data_client = build_client_from_env(cache=cache)
    return _data_client


mcp = FastMCP(
    name="TyphoonActionGuide",
    stateless_http=True,
)


# ======================
# MCP TOOLS
# ======================

@mcp.tool(description="현재 또는 근접 태풍 요약과 지역 기준 영향 가능 시간대를 반환합니다.")
async def get_live_typhoon_summary(location: Optional[str] = None) -> Dict[str, Any]:
    try:
        client = _get_data_client()
    except Exception as e:
        return {"error": str(e), "hint": "DATA_GO_KR_SERVICE_KEY 환경변수를 확인하세요."}

    from_d, to_d = default_recent_range(3, 1)
    typhoons = await client.list_unique_typhoons_in_range(from_d, to_d)

    if not typhoons:
        return {
            "has_active_typhoon": False,
            "message": "현재 영향권에 있는 태풍이 없습니다."
        }

    t = _pick_most_relevant_typhoon(typhoons)
    typ_seq = int(t["typSeq"])

    points = await client.get_track_points(
        typ_seq,
        date.today() - timedelta(days=7),
        date.today() + timedelta(days=2),
    )

    loc = geocode_korea(location or "")
    proximity = None
    if loc and points:
        proximity = _summarize_track_near_location(points, loc[0], loc[1])

    last = points[-1] if points else None

    return {
        "has_active_typhoon": True,
        "typhoon": {
            "name": t.get("typName"),
            "name_en": t.get("typEn"),
            "number": typ_seq,
        },
        "latest": {
            "time": _fmt_dt14_kst(last["typTm"]) if last else None,
            "lat": last["lat"] if last else None,
            "lon": last["lon"] if last else None,
            "pressure": last.get("typPs"),
            "wind": last.get("typWs"),
        },
        "location": location,
        "proximity": proximity,
        "disclaimer": "본 정보는 기상청 태풍 통보문 기반 참고용입니다.",
    }


@mcp.tool(description="과거 태풍을 이름 또는 연도로 검색합니다.")
async def search_past_typhoons(query: str, year: Optional[int] = None):
    client = _get_data_client()
    today = date.today()
    years = [year] if year else range(today.year, today.year - 10, -1)

    results = []
    for y in years:
        ts = await client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        for t in ts:
            if query.lower() in str(t.get("typName", "")).lower() or \
               query.lower() in str(t.get("typEn", "")).lower():
                results.append(t)
        if results:
            break

    return {"results": results[:20]}


@mcp.tool(description="과거 태풍의 이동 경로를 반환합니다.")
async def get_past_typhoon_track(typSeq: int):
    client = _get_data_client()
    pts = await client.get_track_points(
        typSeq,
        date.today() - timedelta(days=365 * 10),
        date.today(),
    )
    return {"count": len(pts), "points": pts}


# ======================
# FastAPI 앱 (핵심!)
# ======================

app = FastAPI(title="Typhoon Action Guide MCP")

# ✅ MCP를 루트에 직접 마운트
app.mount("/", mcp.streamable_http_app())

@app.get("/health")
def health():
    return {"status": "ok"}
