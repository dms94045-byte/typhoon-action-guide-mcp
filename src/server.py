from __future__ import annotations

import os
import uuid
import json
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

# MCP 프로토콜 버전(클라이언트가 주면 그걸 echo, 없으면 기본값)
DEFAULT_PROTOCOL_VERSION = os.getenv("MCP_PROTOCOL_VERSION", "2024-11-05")

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
    m = (method or "").strip()
    # tools.list → tools/list 같은 변형 수용
    return m.replace(".", "/")


def _log(msg: str):
    # Render 로그에 그대로 찍힘
    print(msg, flush=True)


# =========================================================
# MCP 메타 (tools / prompts / resources)
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
    # MCP 표준: protocolVersion을 반드시 포함(클라이언트가 보내면 echo)
    pv = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION

    return {
        "protocolVersion": pv,
        "serverInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION},
        "capabilities": {
            "tools": {},      # 최소 형태
            "prompts": {},    # 최소 형태
            "resources": {},  # 최소 형태
            "logging": {},    # 최소 형태
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


async def handle_prompts_get(params: Dict[str, Any]) -> Dict[str, Any]:
    # prompts/get 스텁(클라이언트가 요구할 수 있음)
    name = params.get("name") or "typhoon_action_guide_system_prompt"
    return {
        "prompt": {
            "name": name,
            "description": "태풍 대응 행동 가이드 MCP 기본 프롬프트",
            "messages": [{"role": "system", "content": [{"type": "text", "text": "태풍 대응 행동 가이드 MCP입니다."}]}],
        }
    }


async def handle_resources_list() -> Dict[str, Any]:
    # 리소스 기능 안 쓰면 빈 배열로
    return {"resources": []}


async def handle_ping() -> Dict[str, Any]:
    return {"ok": True}


async def handle_logging_set_level(params: Dict[str, Any]) -> Dict[str, Any]:
    # 로깅레벨 설정 요청이 와도 실패하지 않게 수용
    return {"ok": True}


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
        "latest_point": last,
        "location": {"input": location, "geocoded": {"lat": loc[0], "lon": loc[1]} if loc else None},
        "proximity": near,
        "data_range_used": {"from": str(from_d), "to": str(to_d)},
        "disclaimer": "통보문 기반 좌표로 근접 시각을 단순 추정한 값입니다.",
    }

    return {"content": [_text_content(json.dumps(payload, ensure_ascii=False, indent=2))]}


async def tool_search_past_typhoons(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    year = args.get("year")

    if not query and year is None:
        return {"content": [_text_content("query 또는 year 중 하나는 필요합니다.")]}

    client = _get_data_client()
    today = date.today()

    # ✅ year를 주면 해당 연도만 정확히
    if year is not None:
        y = int(year)
        typhoons = await client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        matches = []
        for t in typhoons:
            name_kr = str(t.get("typName", ""))
            name_en = str(t.get("typEn", ""))
            if (query in name_kr) or (query.lower() in name_en.lower()):
                matches.append(t)
        return {"content": [_text_content(json.dumps({"ok": True, "year": y, "results": matches[:30]}, ensure_ascii=False, indent=2))]}

    # ✅ year가 없으면: 최근 10년만 보지 말고, 오래된 태풍도 찾도록 과거로 확장
    # 너무 과도한 호출을 막기 위해 "최대 70년 전"까지만 (필요하면 숫자 늘려도 됨)
    max_years_back = int(os.getenv("PAST_TY_SEARCH_MAX_YEARS_BACK", "70"))
    start_year = today.year
    end_year = max(today.year - max_years_back, 1950)

    all_matches: List[Dict[str, Any]] = []
    for y in range(start_year, end_year - 1, -1):
        typhoons = await client.list_unique_typhoons_in_range(date(y, 1, 1), date(y, 12, 31))
        matches = []
        for t in typhoons:
            name_kr = str(t.get("typName", ""))
            name_en = str(t.get("typEn", ""))
            if (query in name_kr) or (query.lower() in name_en.lower()):
                matches.append(t)

        if matches:
            # ✅ 찾는 즉시 반환 (가장 최신 연도부터 내려가므로, 보통 여기서 끝남)
            all_matches.extend(matches)
            break

    return {"content": [_text_content(json.dumps({"ok": True, "query": query, "results": all_matches[:30]}, ensure_ascii=False, indent=2))]}



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

    # ✅ 자주 쓰는 표준 메서드
    if m == "initialize":
        return await handle_initialize(params)
    if m == "tools/list":
        return await handle_tools_list()
    if m == "tools/call":
        return await handle_tools_call(params)
    if m == "prompts/list":
        return await handle_prompts_list()
    if m == "prompts/get":
        return await handle_prompts_get(params)

    # ✅ PlayMCP/클라이언트가 추가로 찌를 수 있는 것들
    if m == "resources/list":
        return await handle_resources_list()
    if m == "ping":
        return await handle_ping()
    if m == "logging/setLevel":
        return await handle_logging_set_level(params)

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


# ---- 사전검증용 GET/HEAD ----
@app.get("/")
@app.head("/")
def root_probe():
    return {"name": SERVICE_NAME, "version": SERVICE_VERSION, "hint": "POST JSON-RPC to /mcp"}


@app.get("/mcp")
@app.head("/mcp")
def mcp_probe():
    return {
        "name": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "jsonrpc": "2.0",
        "accepts": "POST",
        "methods": ["initialize", "tools/list", "tools/call", "prompts/list", "resources/list", "ping"],
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


# ---- JSON-RPC 핸들러 ----
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

        _log(f"[MCP] method={method} params_keys={list(params.keys()) if isinstance(params, dict) else type(params)}")

        try:
            result = await dispatch(method, params if isinstance(params, dict) else {})
            return {"jsonrpc": "2.0", "id": _id, "result": result}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32601, "message": "Method not found", "data": str(e)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32603, "message": "Internal error", "data": str(e)}}

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if created:
        headers["Mcp-Session-Id"] = session_id

    # 배치 요청(list)
    if isinstance(payload, list):
        results = [await handle_one(p) for p in payload if isinstance(p, dict)]
        return JSONResponse(results, headers=headers)

    # 단일 요청(dict)
    if not isinstance(payload, dict):
        return _jsonrpc_err(None, -32600, "Invalid Request")

    _id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if payload.get("jsonrpc") != "2.0" or not method:
        return _jsonrpc_err(_id, -32600, "Invalid Request")

    _log(f"[MCP] method={method} params_keys={list(params.keys()) if isinstance(params, dict) else type(params)}")

    try:
        result = await dispatch(method, params if isinstance(params, dict) else {})
        return _jsonrpc_ok(_id, result, session_id=session_id, set_session=created)
    except ValueError as e:
        return _jsonrpc_err(_id, -32601, "Method not found", data=str(e))
    except Exception as e:
        return _jsonrpc_err(_id, -32603, "Internal error", data=str(e))


@app.post("/mcp")
@app.post("/mcp/")
async def mcp_post(request: Request):
    return await _handle_jsonrpc(request)


# 루트 POST로도 받기(클라이언트 변형 대비)
@app.post("/")
async def root_post(request: Request):
    return await _handle_jsonrpc(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
