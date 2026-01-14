from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .data_go_kr import build_client_from_env, default_recent_range
from .geo_kr import geocode_korea
from .utils import TTLCache, haversine_km


# =========================================================
# 기본 설정
# =========================================================
SERVICE_NAME = "TyphoonActionGuide"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.1.0")

ALLOWED_HOSTS = [
    "typhoon-action-guide-mcp.onrender.com",
    "*.onrender.com",
    "*",  # 제출/데모용 (운영 전환 시 좁히기)
]

cache = TTLCache(int(os.getenv("CACHE_TTL_SECONDS", "120")))
_data_client = None

SESSIONS: Dict[str, Dict[str, Any]] = {}


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
    return {"type": "text", "text": text}


def _ensure_session(session_id: Optional[str]) -> Tuple[str, bool]:
    if session_id and session_id in SESSIONS:
        return session_id, False
    new_id = uuid.uuid4().hex
    SESSIONS[new_id] = {"created_at": datetime.utcnow().isoformat() + "Z"}
    return new_id, True


def _jsonrpc_ok(_id: Any, result: Any, session_id: Optional[str] = None, set_session: bool = False) -> JSONResponse:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if session_id and set_session:
        headers["Mcp-Session-Id"] = session_id
    return JSONResponse({"jsonrpc": "2.0", "id": _id, "result": result}, headers=headers)


def _jsonrpc_err(_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": _id, "error": err})


def _normalize_method(method: str) -> str:
    """
    PlayMCP/클라이언트별로 메서드 표기가 다를 수 있어서 폭넓게 수용.
    - tools/list  / tools.list
    - prompts/list / prompts.list
    """
    m = (method or "").strip()
    m = m.replace(".", "/")
    return m


# =========================================================
# MCP 메타 (tools / prompts)
# =========================================================
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_live_typhoon_summary",
        "description": "최근 기준으로 현재/근접 태풍 요약과 지역 기준 영향 가능 시간대를 반환합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "예: 제주, 서귀포, 부산, 서울"}},
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
            "properties": {"typSeq": {"type": "integer", "description": "태풍 번호(typSeq)"}},
            "required": ["typSeq"],
        },
    },
]

PROMPTS: List[Dict[str, Any]] = [
    {"name": "typhoon_action_guide_system_prompt", "description": "태풍 대응 행동 가이드 MCP 기본 프롬프트", "arguments": []}
]


# =========================================================
# MCP 메서드 구현
# =========================================================
async def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "serverInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION},
        "capabilities": {"tools": {}, "prompts": {}},
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
        return {"content": [_text_content(f"데이터 클라이언트 초기화 실패: {e}\nDATA_GO_KR_SERVICE_KEY 설정을 확인하세요.")]}

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
    near = _summarize_track_near_location(pts, loc_lat=loc[0], loc_lon=loc[1]) if (loc and pts) else None
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
        "location": {"input": location, "geocoded": {"lat": loc[0], "lon": loc[1]} if loc else None},
        "proximity": near,
        "data_range_used": {"from": str(from_d), "to": str(to_d)},
        "disclaimer": "통보문 기반 좌표로 근접 시각을 단순 추정한 값입니다.",
    }

    # JSON을 텍스트로 넣어도 되지만, 보기 좋게 json 문자열로
    import json
    return {"content": [_text_content(json.dumps(payload, ensure_ascii=False, indent=2))]}


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

    import json
    return {"content": [_text_content(json.dumps({"ok": True, "results": matches[:20]}, ensure_ascii=False, indent=2))]}


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

    import json
    return {"content": [_text_content(json.dumps({"ok": True, "typSeq": typ_seq, "count": len(pts), "points": pts}, ensure_ascii=False))]}


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
    m = _normalize_method(method)

    if m == "initialize":
        return await handle_initialize(params)

    if m == "tools/list":
        return await handle_tools_list()

    if m == "tools/call":
        return await handle_tools_call(params)

    if m == "prompts/list":
        return await handle_prompts_list()

    # 호환: 클라이언트가 "prompts/get" 같은 걸 부를 수 있어서 최소로 응답
    if m == "prompts/get":
        # 간단히 1개만 반환
        return {"prompt": {"name": "typhoon_action_guide_system_prompt", "content": "태풍 대응 행동 가이드 MCP입니다."}}

    raise ValueError(f"Unsupported method: {method}")


# =========================================================
# FastAPI 앱
# =========================================================
app = FastAPI(title=f"{SERVICE_NAME} MCP", redirect_slashes=False)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
    max_age=86400,
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ----- "정보 불러오기" 사전 확인용: GET/HEAD에서도 200을 주자 -----
@app.get("/")
@app.head("/")
def root_probe():
    return {
        "name": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "hint": "Use POST JSON-RPC to /mcp (or /) for MCP methods.",
    }


@app.get("/mcp")
@app.head("/mcp")
def mcp_probe():
    # PlayMCP가 GET/HEAD로 먼저 찌를 때 실패(405)하지 않게 200 반환
    return {
        "name": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "jsonrpc": "2.0",
        "accepts": "POST",
        "methods": ["initialize", "tools/list", "tools/call", "prompts/list"],
    }


@app.get("/mcp/")
@app.head("/mcp/")
def mcp_probe_slash():
    return mcp_probe()


@app.options("/mcp")
@app.options("/mcp/")
@app.options("/")
def options_probe():
    return Response(status_code=204)


# ----- JSON-RPC 핸들러: /mcp 와 / 모두 받기(PlayMCP 변형 대비) -----
async def _handle_jsonrpc(request: Request) -> Union[JSONResponse, Response]:
    try:
        payload = await request.json()
    except Exception:
        return _jsonrpc_err(None, -32700, "Parse error")

    incoming_session = request.headers.get("Mcp-Session-Id")
    session_id, created = _ensure_session(incoming_session)

    async def handle_one(obj: Dict[str, Any]) -> Dict[str, Any]:
        _id = obj.get("id")
        method = obj.get("method")
        params = obj.get("params") or {}

        if obj.get("jsonrpc") != "2.0" or not method:
            return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32600, "message": "Invalid Request"}}

        try:
            result = await dispatch(method, params)
            return {"jsonrpc": "2.0", "id": _id, "result": result}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32601, "message": "Method not found", "data": str(e)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32603, "message": "Internal error", "data": str(e)}}

    # 배치 요청(list) 대응
    if isinstance(payload, list):
        results = [await handle_one(p) for p in payload if isinstance(p, dict)]
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if created:
            headers["Mcp-Session-Id"] = session_id
        return JSONResponse(results, headers=headers)

    if not isinstance(payload, dict):
        return _jsonrpc_err(None, -32600, "Invalid Request")

    _id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if payload.get("jsonrpc") != "2.0" or not method:
        return _jsonrpc_err(_id, -32600, "Invalid Request")

    try:
        result = await dispatch(method, params)
        return _jsonrpc_ok(_id, result, session_id=session_id, set_session=created)
    except ValueError as e:
        return _jsonrpc_err(_id, -32601, "Method not found", data=str(e))
    except Exception as e:
        return _jsonrpc_err(_id, -32603, "Internal error", data=str(e))


@app.post("/mcp")
@app.post("/mcp/")
async def mcp_post(request: Request):
    return await _handle_jsonrpc(request)


@app.post("/")
async def root_post(request: Request):
    # PlayMCP가 루트로 때려도 통과하도록
    return await _handle_jsonrpc(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
