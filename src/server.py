from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from mcp.server.fastmcp import FastMCP

from .data_go_kr import build_client_from_env, default_recent_range
from .geo_kr import geocode_korea
from .utils import TTLCache, haversine_km


# -----------------------------
# 내부 유틸
# -----------------------------
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


def _summarize_track_near_location(
    points: List[Dict[str, Any]], loc_lat: float, loc_lon: float
) -> Dict[str, Any]:
    best = None
    for p in points:
        dist = haversine_km(loc_lat, loc_lon, p["lat"], p["lon"])
        if best is None or dist < best["dist_km"]:
            best = {"dist_km": dist, "p": p}

    if best is None:
        return {"closest": None, "impact_window": None}

    typ_tm = best["p"].get("typTm", "")
    try:
        dt = datetime.strptime(typ_tm, "%Y%m%d%H%M")
        window = {
            "start": (dt - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
            "end": (dt + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
            "center": dt.strftime("%Y-%m-%d %H:%M"),
        }
    except Exception:
        window = None

    closest = {
        "time": _fmt_dt14_kst(typ_tm),
        "distance_km": round(best["dist_km"], 1),
        "typhoon_lat": best["p"].get("lat"),
        "typhoon_lon": best["p"].get("lon"),
        "typLoc": best["p"].get("typLoc"),
    }

    return {"closest": closest, "impact_window": window}


# -----------------------------
# MCP 서버 세팅
# -----------------------------
cache = TTLCache(int(os.getenv("CACHE_TTL_SECONDS", "120")))
_data_client = None


def _get_data_client():
    global _data_client
    if _data_client is None:
        _data_client = build_client_from_env(cache=cache)
    return _data_client


mcp = FastMCP(name="TyphoonActionGuide", stateless_http=True)


# -----------------------------
# Tools
# -----------------------------
@mcp.tool(
    description="최근(기본: 최근 3일~내일) 기준으로 현재/근접 태풍의 요약과 사용자의 지역 기준 영향 가능 시간대를 반환합니다."
)
async def get_live_typhoon_summary(location: Optional[str] = None) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        return {
            "has_active_typhoon": None,
            "error": str(e),
            "hint": "DATA_GO_KR_SERVICE_KEY를 Render Environment에 설정하세요.",
        }

    from_d, to_d = default_recent_range(3, 1)
    typhoons = await data_client.list_unique_typhoons_in_range(from_d, to_d)

    if not typhoons:
        return {
            "has_active_typhoon": False,
            "message": "현재 조회 범위 내 태풍 정보가 확인되지 않습니다.",
            "range": {"from": str(from_d), "to": str(to_d)},
        }

    t = _pick_most_relevant_typhoon(typhoons)
    typ_seq = int(t.get("typSeq", 0))

    pts = await data_client.get_track_points(
        typ_seq=typ_seq,
        from_d=date.today() - timedelta(days=7),
        to_d=date.today() + timedelta(days=2),
    )

    loc = geocode_korea(location or "")
    near = None
    if loc and pts:
        near = _summarize_track_near_location(pts, loc_lat=loc[0], loc_lon=loc[1])

    last = pts[-1] if pts else None

    return {
        "has_active_typhoon": True,
        "typhoon": {
            "typSeq": typ_seq,
            "typName": t.get("typName"),
            "typEn": t.get("typEn"),
            "firstTypTm": t.get("firstTypTm"),
            "lastTypTm": t.get("lastTypTm"),
        },
        "latest_point": last,
        "location": {
            "input": location,
            "geocoded": {"lat": loc[0], "lon": loc[1]} if loc else None,
        },
        "proximity": near,
        "data_range_used": {"from": str(from_d), "to": str(to_d)},
    }


@mcp.tool(description="연도 또는 이름 일부로 과거 태풍 후보를 검색해 목록을 반환합니다.")
async def search_past_typhoons(query: str, year: Optional[int] = None) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        return {"ok": False, "error": str(e), "hint": "DATA_GO_KR_SERVICE_KEY 설정 필요"}

    q = (query or "").strip()
    if not q and year is None:
        return {"ok": False, "message": "query 또는 year 중 하나는 필요합니다."}

    today = date.today()
    years = [year] if year is not None else list(range(today.year, max(today.year - 9, 1950), -1))

    matches: List[Dict[str, Any]] = []
    for y in years[:10]:
        typhoons = await data_client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        for t in typhoons:
            name_kr = str(t.get("typName", ""))
            name_en = str(t.get("typEn", ""))
            if q:
                if (q in name_kr) or (q.lower() in name_en.lower()):
                    matches.append(t)
            else:
                matches.append(t)
        if matches and year is None:
            break

    return {"ok": True, "results": matches[:20]}


@mcp.tool(description="지정한 태풍번호(typSeq)의 경로 포인트(위경도/시각)를 반환합니다.")
async def get_past_typhoon_track(typSeq: int) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        return {"ok": False, "error": str(e), "hint": "DATA_GO_KR_SERVICE_KEY 설정 필요"}

    pts = await data_client.get_track_points(
        typ_seq=typSeq,
        from_d=date.today() - timedelta(days=365 * 10),
        to_d=date.today() + timedelta(days=1),
    )
    return {"ok": True, "count": len(pts), "points": pts}


@mcp.prompt()
def typhoon_action_guide_system_prompt() -> str:
    return "태풍 대응 행동 가이드 MCP입니다."


# -----------------------------
# ASGI 앱
# -----------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="Typhoon Action Guide MCP",
        redirect_slashes=False,
        lifespan=lambda app: mcp.session_manager.run(),
    )

    # ✅ 421 방지: Host 헤더 허용
    # Render/프록시 환경에서 Host가 달라질 수 있으니 넓게 허용(제출용)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "typhoon-action-guide-mcp.onrender.com",
            "*.onrender.com",
            "*",
        ],
    )

    # ✅ CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    # ✅ PlayMCP 호환: /mcp 로 MCP 앱 제공
    sub = mcp.streamable_http_app()
    app.mount("/mcp", sub)
    app.mount("/mcp/", sub)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
