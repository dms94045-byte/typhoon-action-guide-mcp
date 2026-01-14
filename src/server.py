from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from .data_go_kr import build_client_from_env, default_recent_range
from .geo_kr import geocode_korea
from .utils import TTLCache, haversine_km

# -------------------------
# Logging
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mcp")

# 개발 중에만 true 권장(응답에 trace를 내려줌). 운영/심사 전엔 false 권장.
DEBUG_TOOL_ERRORS = os.getenv("DEBUG_TOOL_ERRORS", "true").lower() in ("1", "true", "yes", "y")


def _parse_date_yyyymmdd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _fmt_dt14_kst(typ_tm: str) -> str:
    # data.go.kr는 KST 기준이 섞여 있을 수 있고, typTm는 YYYYMMDDHHMM 형태
    try:
        dt = datetime.strptime(typ_tm, "%Y%m%d%H%M")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return typ_tm


def _pick_most_relevant_typhoon(typhoons: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return typhoons[0] if typhoons else None


def _summarize_track_near_location(points: List[Dict[str, Any]], loc_lat: float, loc_lon: float) -> Dict[str, Any]:
    # 가장 가까운 지점과 그 시각을 찾고, 근접 전후 시간을 '영향 가능 시간대'로 제공
    best = None
    for p in points:
        dist = haversine_km(loc_lat, loc_lon, p["lat"], p["lon"])
        if best is None or dist < best["dist_km"]:
            best = {"dist_km": dist, "p": p}

    if best is None:
        return {"closest": None, "impact_window": None}

    # 근접 시각 +/- 6시간을 임시 영향 가능 시간대로 제안(정밀 예보 대체가 아님)
    typ_tm = best["p"].get("typTm", "")
    try:
        dt = datetime.strptime(typ_tm, "%Y%m%d%H%M")
        start = dt - timedelta(hours=6)
        end = dt + timedelta(hours=6)
        window = {
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
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


# -------------------------
# MCP error response helpers
# -------------------------
def _text_content(text: str) -> Dict[str, str]:
    return {"type": "text", "text": text}


def _tool_error_response(msg: str, detail: Optional[str] = None) -> Dict[str, Any]:
    # PlayMCP가 그대로 표시 가능한 표준 형태
    if detail and DEBUG_TOOL_ERRORS:
        text = f"{msg}\n\n[detail]\n{detail}"
    else:
        text = msg
    return {"content": [_text_content(text)], "isError": True}


def safe_tool(fn):
    """
    FastMCP tool 실행 중 예외가 나면:
    - Render 로그에 traceback 남기고
    - MCP 표준 에러 형태로 응답
    """
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            # args/kwargs는 너무 길 수 있어 요약
            try:
                kwargs_dump = json.dumps(kwargs, ensure_ascii=False)
            except Exception:
                kwargs_dump = repr(kwargs)

            logger.error(
                "[TOOL ERROR] tool=%s kwargs=%s err=%s\n%s",
                fn.__name__,
                kwargs_dump,
                str(e),
                tb,
            )

            return _tool_error_response(
                "error while calling tool",
                detail=f"tool={fn.__name__}\nerr={str(e)}\n\ntrace:\n{tb}",
            )
    return wrapper


# -------------------------
# MCP server
# -------------------------
cache = TTLCache(int(os.getenv("CACHE_TTL_SECONDS", "120")))
_data_client = None


def _get_data_client():
    """환경변수 미설정 시에도 서버가 죽지 않도록 지연 초기화."""
    global _data_client
    if _data_client is None:
        _data_client = build_client_from_env(cache=cache)
    return _data_client


mcp = FastMCP(name="TyphoonActionGuide", stateless_http=True)


@mcp.tool(description="최근(기본: 최근 3일~내일) 기준으로 현재/근접 태풍의 요약과 사용자의 지역 기준 영향 가능 시간대를 반환합니다.")
@safe_tool
async def get_live_typhoon_summary(location: Optional[str] = None) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        # 여기서는 '툴 에러'로 처리해도 되고, 친절 메시지로 처리해도 됨.
        # 일단 PlayMCP에서 명확히 보이도록 에러 형태로 반환.
        return _tool_error_response(
            "DATA_GO_KR_SERVICE_KEY 설정이 필요합니다.",
            detail=str(e),
        )

    from_d, to_d = default_recent_range(3, 1)
    typhoons = await data_client.list_unique_typhoons_in_range(from_d, to_d)

    if not typhoons:
        return {
            "has_active_typhoon": False,
            "message": "현재 조회 범위(최근 며칠) 내에 태풍 정보가 확인되지 않습니다.",
            "range": {"from": str(from_d), "to": str(to_d)},
        }

    t = _pick_most_relevant_typhoon(typhoons)
    typ_seq = int(t.get("typSeq", 0))

    # track point는 발표 기반이므로 최근 7일 정도로 확장해서 탐색
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
        "latest_point": {
            "time": _fmt_dt14_kst(last.get("typTm", "")) if last else None,
            "lat": last.get("lat") if last else None,
            "lon": last.get("lon") if last else None,
            "typLoc": last.get("typLoc") if last else None,
            "typWs": last.get("typWs") if last else None,
            "typPs": last.get("typPs") if last else None,
        } if last else None,
        "location": {
            "input": location,
            "geocoded": {"lat": loc[0], "lon": loc[1]} if loc else None,
            "note": "지역을 제공하면 해당 지역 중심 좌표(대략)로 근접 시각을 추정합니다." if location else "지역이 없으면 전국 공통 요약만 제공합니다.",
        },
        "proximity": near,
        "data_range_used": {"from": str(from_d), "to": str(to_d)},
        "disclaimer": "이 도구는 통보문 기반 좌표를 이용해 '근접 시각'을 단순 추정합니다. 정확한 상륙/통과 시각은 기상청 최신 태풍정보/특보를 함께 확인하세요.",
    }


@mcp.tool(description="연도(기본: 최근 10년) 또는 이름 일부로 과거 태풍 후보를 검색해 목록을 반환합니다.")
@safe_tool
async def search_past_typhoons(query: str, year: Optional[int] = None) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        return _tool_error_response("DATA_GO_KR_SERVICE_KEY 설정이 필요합니다.", detail=str(e))

    q = (query or "").strip()
    if not q and year is None:
        return {"ok": False, "message": "검색어(query) 또는 연도(year) 중 하나는 필요합니다."}

    today = date.today()
    years = [year] if year is not None else list(range(today.year, max(today.year - 9, 1950), -1))

    matches: List[Dict[str, Any]] = []
    for y in years[:10]:
        from_d = date(y, 1, 1)
        to_d = date(y, 12, 31)
        typhoons = await data_client.list_unique_typhoons_in_range(from_d, to_d)
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

    matches = matches[:20]

    return {
        "ok": True,
        "query": q,
        "year": year,
        "results": matches,
        "hint": "결과의 typSeq(태풍번호)로 get_past_typhoon_track을 호출하면 경로 포인트를 받을 수 있습니다.",
    }


@mcp.tool(description="지정한 태풍번호(typSeq)의 기간 내 경로 포인트(위경도/시각)를 반환합니다. 기간이 없으면 자동 추정합니다.")
@safe_tool
async def get_past_typhoon_track(
    typSeq: int,
    from_yyyymmdd: Optional[str] = None,
    to_yyyymmdd: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        data_client = _get_data_client()
    except Exception as e:
        return _tool_error_response("DATA_GO_KR_SERVICE_KEY 설정이 필요합니다.", detail=str(e))

    if typSeq <= 0:
        return {"ok": False, "message": "typSeq는 1 이상의 정수여야 합니다."}

    if from_yyyymmdd and to_yyyymmdd:
        from_d = _parse_date_yyyymmdd(from_yyyymmdd)
        to_d = _parse_date_yyyymmdd(to_yyyymmdd)
    else:
        from_d, to_d = default_recent_range(30, 1)

    pts = await data_client.get_track_points(typ_seq=typSeq, from_d=from_d, to_d=to_d)

    if not pts and (not (from_yyyymmdd and to_yyyymmdd)):
        today = date.today()
        for y in range(today.year, today.year - 9, -1):
            pts = await data_client.get_track_points(typ_seq=typSeq, from_d=date(y, 1, 1), to_d=date(y, 12, 31))
            if pts:
                from_d, to_d = date(y, 1, 1), date(y, 12, 31)
                break

    return {
        "ok": True,
        "typSeq": typSeq,
        "range": {"from": str(from_d), "to": str(to_d)},
        "count": len(pts),
        "points": pts,
        "disclaimer": "공공데이터포털 태풍 통보문 기반 포인트입니다(베스트트랙과 다를 수 있음).",
    }


@mcp.prompt()
def typhoon_action_guide_system_prompt() -> str:
    return """태풍 대응 행동 가이드 MCP: 시스템 프롬프트는 prompts/system_prompt.txt 파일을 참고하세요."""


# -------------------------
# ASGI app
# -------------------------
def create_app() -> FastAPI:
    # FastMCP 스트리머블 HTTP 마운트
    app = FastAPI(lifespan=lambda app: mcp.session_manager.run(), title="Typhoon Action Guide MCP")
    app.mount("/mcp", mcp.streamable_http_app())

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("src.server:app", host="0.0.0.0", port=port, reload=False)
