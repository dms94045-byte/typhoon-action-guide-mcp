from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from .data_go_kr import build_client_from_env, default_recent_range
from .geo_kr import geocode_korea
from .utils import TTLCache, haversine_km


# =========================================================
# 설정
# =========================================================
SERVICE_NAME = "TyphoonActionGuide"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.1.0")

# Render/프록시 환경에서 421 방지
ALLOWED_HOSTS = [
    "typhoon-action-guide-mcp.onrender.com",
    "*.onrender.com",
    "*",  # 제출/데모용(운영 전환 시 제거 권장)
]

# CORS (PlayMCP 웹 호출 대비)
CORS_ALLOW_ORIGINS = ["*"]

# 세션(가볍게만 사용)
SESSIONS: Dict[str, Dict[str, Any]] = {}

# 데이터 클라이언트 캐시
cache = TTLCache(int(os.getenv("CACHE_TTL_SECONDS", "120")))
_data_client = None


def _get_data_client():
    global _data_client
    if _data_client is None:
        _data_client = build_client_from_env(cache=cache)
    return _data_client


# =========================================================
# 유틸
# =========================================================
def _fmt_dt14_kst(typ_tm: str) -> str:
    try:
        dt = datetime.strptime(typ_tm, "%Y%m%d%H%M")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return typ_tm


def _pick_most_relevant_typhoon(typhoons: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return typhoons[0] if typhoons else None


def _summarize_track_near_location(points: List[Dict[str, Any]], loc_lat: float, loc_lon: float) -> Dict[str, Any]:
    best = None
    for p in points:
        dist = haversine_km(loc_lat, loc_lon, p["lat"], p["lon"])
        if best is None or dist < best["dist_km"]:
            best = {"dist_km": dist, "p": p}

    if best is None:
        return {"closest": None, "impact_window": None}

    typ_tm = str(best["p"].get("typTm", ""))
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


def _text_content(text: str) -> Dict[str, Any]:
    # MCP Tool 결과(content 배열)로 반환
    return {"type": "text", "text": text}


def _ensure_session(session_id: Optional[str]) -> Tuple[str, bool]:
    """세션ID 없으면 새로 발급."""
    if session_id and session_id in SESSIONS:
        return session_id, False
    new_id = uuid.uuid4().hex
    SESSIONS[new_id] = {"created_at": datetime.utcnow().isoformat() + "Z"}
    return new_id, True


def _jsonrpc_ok(_id: Any, result: Any, session_id: Optional[str] = None, set_session: bool = False) -> JSONResponse:
    headers = {}
    if session_id and set_session:
        headers["Mcp-Session-Id"] = session_id
    return JSONResponse({"jsonrpc": "2.0", "id": _id, "result": result}, headers=headers)


def _jsonrpc_err(_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": _id, "error": err})


# =========================================================
# MCP 도구 정의(스키마)
# =========================================================
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_live_typhoon_summary",
        "description": "최근 기준으로 현재/근접 태풍 요약과 지역 기준 영향 가능 시간대를 반환합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "예: 제주, 서귀포, 부산, 서울 등",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_past_typhoons",
        "description": "이름 일부/연도로 과거 태풍 후보를 검색해 목록을 반환합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "태풍 이름(한글/영문 일부)"},
                "year": {"type": "integer", "description": "연도(예: 2020)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_past_typhoon_track",
        "description": "지정한 태풍번호(typSeq)의 경로 포인트(위경도/시각)를 반환합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "typSeq": {"type": "integer", "description": "태풍 번호(typSeq)"},
            },
            "required": ["typSeq"],
        },
    },
]

PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "typhoon_action_guide_system_prompt",
        "description": "태풍 대응 행동 가이드 MCP의 기본 안내 프롬프트",
        "arguments": [],
    }
]


# =========================================================
# MCP 메서드 구현
# =========================================================
async def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    # PlayMCP가 “서버 정보 불러오기”에 가장 민감한 부분
    return {
        "serverInfo": {
            "name": SERVICE_NAME,
            "version": SERVICE_VERSION,
        },
        "capabilities": {
            "tools": {},
            "prompts": {},
        },
        "instructions": (
            "태풍 대응 행동 가이드 MCP입니다. "
            "지역을 입력하면 근접 시각/거리 등을 단순 추정해 요약합니다. "
            "정확한 상륙/통과 시각은 기상청 최신 발표를 함께 확인하세요."
        ),
    }


async def handle_tools_list() -> Dict[str, Any]:
    return {"tools": TOOLS}


async def handle_prompts_list() -> Dict[str, Any]:
    return {"prompts": PROMPTS}


async def tool_get_live_typhoon_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    location = args.get("location")

    try:
        client = _get_data_client()
    except Exception as e:
        return {
            "content": [
                _text_content(
                    f"데이터 클라이언트 초기화 실패: {e}\n"
                    f"Render Environment에 DATA_GO_KR_SERVICE_KEY 설정을 확인하세요."
                )
            ]
        }

    from_d, to_d = default_recent_range(3, 1)
    typhoons = await client.list_unique_typhoons_in_range(from_d, to_d)

    if not typhoons:
        return {"content": [_text_content("현재 조회 범위 내 태풍 정보가 확인되지 않습니다.")]}

    t = _pick_most_relevant_typhoon(typhoons)
    typ_seq = int(t.get("typSeq", 0))

    pts = await client.get_track_points(
        typ_seq=typ_seq,
        from_d=date.today() - timedelta(days=7),
        to_d=date.today() + timedelta(days=2),
    )

    loc = geocode_korea(location or "")
    near = None
    if loc and pts:
        near = _summarize_track_near_location(pts, loc_lat=loc[0], loc_lon=loc[1])

    last = pts[-1] if pts else None

    payload = {
        "has_active_typhoon": True,
        "typhoon": {
            "typSeq": typ_seq,
            "typName": t.get("typName"),
            "typEn": t.get("typEn"),
            "firstTypTm": t.get("firstTypTm"),
            "lastTypTm": t.get("lastTypTm"),
        },
        "latest_point": {
            "time": _fmt_dt14_kst(str(last.get("typTm"))) if last else None,
            "lat": last.get("lat") if last else None,
            "lon": last.get("lon") if last else None,
            "typLoc": last.get("typLoc") if last else None,
            "typWs": last.get("typWs") if last else None,
            "typPs": last.get("typPs") if last else None,
        }
        if last
        else None,
        "location": {
            "input": location,
            "geocoded": {"lat": loc[0], "lon": loc[1]} if loc else None,
        },
        "proximity": near,
        "data_range_used": {"from": str(from_d), "to": str(to_d)},
        "disclaimer": "통보문 기반 좌표로 근접 시각을 단순 추정한 값입니다.",
    }

    return {"content": [_text_content(str(payload))]}


async def tool_search_past_typhoons(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    year = args.get("year")

    if not query and year is None:
        return {"content": [_text_content("query 또는 year 중 하나는 필요합니다.")]}

    client = _get_data_client()
    today = date.today()
    years = [int(year)] if year is not None else list(range(today.year, max(today.year - 9, 1950), -1))

    matches: List[Dict[str, Any]] = []
    for y in years[:10]:
        typhoons = await client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        for t in typhoons:
            name_kr = str(t.get("typName", ""))
            name_en = str(t.get("typEn", ""))
            if query:
                if (query in name_kr) or (query.lower() in name_en.lower()):
                    matches.append(t)
            else:
                matches.append(t)
        if matches and year is None:
            break

    payload = {"ok": True, "results": matches[:20]}
    return {"content": [_text_content(str(payload))]}


async def tool_get_past_typhoon_track(args: Dict[str, Any]) -> Dict[str, Any]:
    typ_seq = int(args.get("typSeq", 0))
    if typ_seq <= 0:
        return {"content": [_text_content("typSeq는 1 이상의 정수여야 합니다.")]}

    client = _get_data_client()
    pts = await client.get_track_points(
        typ_seq=typ_seq,
        from_d=date.today() - timedelta(days=365 * 10),
        to_d=date.today() + timedelta(days=1),
    )

    payload = {"ok": True, "typSeq": typ_seq, "count": len(pts), "points": pts}
    return {"content": [_text_content(str(payload))]}


async def handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if name == "get_live_typhoon_summary":
        return await tool_get_live_typhoon_summary(arguments)
    if name == "search_past_typhoons":
        return await tool_search_past_typhoons(arguments)
    if name == "get_past_typhoon_track":
        return await tool_get_past_typhoon_track(arguments)

    return {"content": [_text_content(f"알 수 없는 tool: {name}")]}


async def dispatch(method: str, params: Dict[str, Any]) -> Any:
    if method == "initialize":
        return await handle_initialize(params)
    if method == "tools/list":
        return await handle_tools_list()
    if method == "tools/call":
        return await handle_tools_call(params)
    if method == "prompts/list":
        return await handle_prompts_list()

    # PlayMCP가 info 불러오기 시 최소로 쓰는 것들만 우선 구현
    raise ValueError(f"Unsupported method: {method}")


# =========================================================
# FastAPI 앱
# =========================================================
app = FastAPI(title=f"{SERVICE_NAME} MCP", redirect_slashes=False)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
    max_age=86400,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.options("/mcp")
def mcp_options():
    # 프리플라이트에 빠르게 응답
    return Response(status_code=204)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    # JSON-RPC 2.0 단일 요청 처리
    try:
        payload = await request.json()
    except Exception:
        return _jsonrpc_err(None, -32700, "Parse error")

    _id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if payload.get("jsonrpc") != "2.0" or not method:
        return _jsonrpc_err(_id, -32600, "Invalid Request")

    # 세션 처리(PlayMCP가 헤더로 유지할 수 있음)
    incoming_session = request.headers.get("Mcp-Session-Id")
    session_id, created = _ensure_session(incoming_session)

    try:
        result = await dispatch(method, params)
        return _jsonrpc_ok(_id, result, session_id=session_id, set_session=created)
    except ValueError as e:
        return _jsonrpc_err(_id, -32601, "Method not found", data=str(e))
    except Exception as e:
        return _jsonrpc_err(_id, -32603, "Internal error", data=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
