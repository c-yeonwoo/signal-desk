"""거장 포트폴리오 큐레이션 — 공개 13F를 제공하는 유명 기관투자자 목록(CIK).

CIK가 틀리거나 최신 13F가 없으면 store.fetch_gurus()가 조용히 건너뛴다(그레이스풀).
표시는 '참고용 · 분기 공시 스냅샷(지연)'으로 — 추천·모방 유도 아님(BACKLOG 규제 톤).
"""

from __future__ import annotations

# key: 내부 식별자, name: 표시명, cik: SEC 중앙식별번호, desc: 한 줄 스타일 설명
GURUS = [
    {"key": "berkshire", "name": "워런 버핏 · 버크셔 해서웨이", "cik": "1067983",
     "desc": "장기 가치투자 — 브랜드·현금흐름 우량주 집중"},
    {"key": "pershing", "name": "빌 애크먼 · 퍼싱스퀘어", "cik": "1336528",
     "desc": "소수 종목 집중 액티비스트"},
    {"key": "scion", "name": "마이클 버리 · 사이언", "cik": "1649339",
     "desc": "역발상·거시 헤지(《빅쇼트》)"},
    {"key": "bridgewater", "name": "레이 달리오 · 브리지워터", "cik": "1350694",
     "desc": "전천후·거시 분산"},
    {"key": "appaloosa", "name": "데이비드 테퍼 · 아팔루사", "cik": "1656456",
     "desc": "경기순환·기술주 비중 조절"},
    {"key": "duquesne", "name": "스탠리 드러켄밀러 · 듀케인", "cik": "1536411",
     "desc": "거시·성장주 기민한 로테이션"},
]


def all_gurus() -> list[dict]:
    return GURUS


# 13F 발행사명 ↔ S&P500 종목명 매칭용 — 접미사·법인격 표기를 걷어내고 비교
import re

_SUFFIXES = [" INCORPORATED", " INC", " CORPORATION", " CORP", " COMPANY", " CO", " LTD",
             " LLC", " PLC", " THE", " COM", " CLASS A", " CLASS B", " CLASS C", " CL A",
             " CL B", " CL C", " HOLDINGS", " HLDGS", " GROUP", " SA", " NV", " AG", " & CO"]


def _norm(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())  # 구두점 먼저 제거(INC. → INC)
    s = " " + re.sub(r"\s+", " ", s).strip() + " "
    changed = True
    while changed:  # 접미사가 겹쳐 붙은 경우(… GROUP INC) 반복 제거
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf + " "):
                s = s[: -len(suf) - 1] + " "
                changed = True
    return s.strip()


def build_name_index(us_universe: list[dict]) -> dict[str, str]:
    """S&P500 정규화종목명 → ticker 색인."""
    idx = {}
    for u in us_universe:
        key = _norm(u.get("name", ""))
        if key and key not in idx:
            idx[key] = u["ticker"]
    return idx


def match_ticker(issuer_name: str, name_index: dict[str, str]) -> str | None:
    """13F 발행사명 → S&P500 ticker. 정확 정규화 일치 우선, 없으면 접두 포함 매칭."""
    key = _norm(issuer_name)
    if not key:
        return None
    if key in name_index:
        return name_index[key]
    for nkey, tk in name_index.items():  # "ALPHABET" ⊂ "ALPHABET" 류 부분 매칭
        if nkey.startswith(key) or key.startswith(nkey):
            return tk
    return None
