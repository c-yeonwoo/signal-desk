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
