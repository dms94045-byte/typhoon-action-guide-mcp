# Typhoon Action Guide MCP (PlayMCP 제출용 프로토타입)

국민이 태풍 영향권에서 **"언제 위험한지 / 언제 안전해지는지 / 지금 뭘 해야 하는지"** 를 빠르게 확인하도록 돕는 MCP 서버입니다.

## 제공 도구(툴)
- `get_live_typhoon_summary(location?)`
  - 최근 발표 기반으로, 현재/근접 태풍 요약 + (선택) 지역 기준 근접 시각(시간대) 추정
- `search_past_typhoons(query, year?)`
  - 이름 일부 또는 연도로 과거 태풍 후보 검색
- `get_past_typhoon_track(typSeq, from_yyyymmdd?, to_yyyymmdd?)`
  - 과거 태풍 번호(typSeq)의 발표 기반 경로 포인트 반환

> 주: "정확한 지역 통과 시각"은 기상청 발표 방식/업데이트 주기에 따라 단일 시각으로 단정하기 어렵습니다. 본 데모는 **시간대(윈도우)** 를 기본으로 안내하도록 설계되었습니다.

## 빠른 실행

### 1) 환경변수 설정
`.env.example`을 복사해 `.env`로 만들고 `DATA_GO_KR_SERVICE_KEY`를 입력합니다.

### 2) 로컬 실행
```bash
pip install -r requirements.txt
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

### 3) MCP 연결
Streamable HTTP 기준으로 `/mcp` 경로에 마운트되어 있습니다.
- Health: `/health`
- MCP: `/mcp`

## Render 배포 팁(무료 플랜)
- 무료 플랜은 유휴 시 슬립될 수 있어, 첫 요청은 "깨우기" 시간이 발생할 수 있습니다.
- 데모 안정성이 중요하면:
  - 외부 핑(예: UptimeRobot)을 `/health`에 주기적으로 호출
  - 또는 Always-on 옵션이 있는 호스팅 사용

## 프롬프트(대화 룰)
- `prompts/system_prompt.txt` 참고
