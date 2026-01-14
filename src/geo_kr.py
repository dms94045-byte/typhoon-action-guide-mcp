from __future__ import annotations

from typing import Optional, Tuple

# 간단 좌표(대략, 도/광역시 중심값). 정밀한 읍면동 수준은 추후 확장.
KOREA_REGION_CENTERS = {
    # 특별/광역시
    "서울": (37.5665, 126.9780),
    "부산": (35.1796, 129.0756),
    "대구": (35.8714, 128.6014),
    "인천": (37.4563, 126.7052),
    "광주": (35.1595, 126.8526),
    "대전": (36.3504, 127.3845),
    "울산": (35.5384, 129.3114),
    "세종": (36.4800, 127.2890),

    # 도
    "경기": (37.4138, 127.5183),
    "강원": (37.8228, 128.1555),
    "충북": (36.6357, 127.4912),
    "충남": (36.5184, 126.8000),
    "전북": (35.7175, 127.1530),
    "전남": (34.8161, 126.4630),
    "경북": (36.4919, 128.8889),
    "경남": (35.4606, 128.2132),
    "제주": (33.4996, 126.5312),

    # 자주 쓰는 도시/권역
    "제주시": (33.4996, 126.5312),
    "서귀포": (33.2541, 126.5601),
}


def normalize_region(text: str) -> str:
    t = (text or "").strip()
    # 흔한 표기 보정
    t = t.replace("특별자치도", "").replace("광역시", "").replace("특별시", "")
    t = t.replace("도", "").replace("시", "")
    return t


def geocode_korea(region: str) -> Optional[Tuple[float, float]]:
    if not region:
        return None
    raw = region.strip()
    # 1) 원문 키워드 직접 매칭
    for k, v in KOREA_REGION_CENTERS.items():
        if k in raw:
            return v
    # 2) 정규화 후 매칭
    n = normalize_region(raw)
    for k, v in KOREA_REGION_CENTERS.items():
        if normalize_region(k) == n:
            return v
    return None
