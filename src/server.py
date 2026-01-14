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


# =========================================================
# 내부 유틸
# =========================================================
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

    return {
        "closest": {
            "time": _fmt_dt14_kst(typ_tm),
            "distance_km": round(best["dist_km"], 1),
            "typhoon_lat": best["p"].get("lat"),
            "typhoon_lon": best["p"].get("lon"),
            "typLoc": best["p"].get("typLoc"),
        },
        "impact_window": window,
    }


# =========================================================
# MCP 서버
# =========================================================
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


# =========================================================
# MCP Tools
# =========================================================
@mcp.tool(description="현재/근접 태풍 요약과 지역 기준 영향 가능 시간대 제공")
async def get_live_typhoon_summary(location: Optional[str] = None) -> Dict[str, Any]:
    try:
        client = _get_data_client()
    except Exception as e:
        return {"error": str(e), "hint": "DATA_GO_KR_SERVICE_KEY 설정 필요"}

    from_d, to_d = default_recent_range(3, 1)
    typhoons = await client.list_unique_typhoons_in_range(from_d, to_d)

    if not typhoons:
        return {"has_active_typhoon": False}

    t = _pick_most_relevant_typhoon(typhoons)
    typ_seq = int(t.get("typSeq", 0))

    pts = await client.get_track_points(
        typ_seq=typ_seq,
        from_d=date.today() - timedelta(days=7),
        to_d=date.today() + timedelta(days=2),
    )

    loc = geocode_korea(location or "")
    near = _summarize_track_near_location(pts, loc[0], loc[1]) if (loc and pts) else None
    last = pts[-1] if pts else None

    return {
        "has_active_typhoon": True,
        "typhoon": t,
        "latest_point": last,
        "location": location,
        "proximity": near,
    }


@mcp.tool(description="과거 태풍 검색")
async def search_past_typhoons(query: str, year: Optional[int] = None) -> Dict[str, Any]:
    client = _get_data_client()
    today = date.today()
    years = [year] if year else range(today.year, today.year - 10, -1)

    results = []
    for y in years:
        typhoons = await client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        for t in typhoons:
            if query in str(t.get("typName", "")) or query.lower() in str(t.get("typEn", "")).lower():
                results.append(t)
        if results:
            break

    return {"results": results[:20]}


@mcp.tool(description="과거 태풍 경로 조회")
async def get_past_typhoon_track(typSeq: int) -> Dict[str, Any]:
    client = _get_data_client()
    pts = await client.get_track_points(
        typ_seq=typSeq,
        from_d=date.today() - timedelta(days=365 * 10),
        to_d=date.today(),
    )
    return {"count": len(pts), "points": pts}


@mcp.prompt()
def typhoon_action_guide_system_prompt() -> str:
    return "태풍 대응 행동 가이드 MCP입니다."


# =========================================================
# FastAPI 앱 (PlayMCP 최종 호환)
# =========================================================
def create_app() -> FastAPI:
    app = FastAPI(
        title="Typhoon Action Guide MCP",
        redirect_slashes=False,
        lifespan=lambda app: mcp.session_manager.run(),
    )

    # ✅ 핵심 1: Host 헤더 허용 (Render + PlayMCP)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "typhoon-action-guide-mcp.onrender.com",
            "*.onrender.com",
            "*",
        ],
    )

    # ✅ 핵심 2: CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ✅ 핵심 3: MCP를 루트에 마운트
    app.mount("/", mcp.streamable_http_app())

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
