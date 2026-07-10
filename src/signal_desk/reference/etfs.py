"""유명 ETF 구성종목 큐레이션 — '이 ETF엔 무엇이 담겨 있나'를 서클차트로 보여주는 참고 자료.

시그널·KB와 무관(분석 대상 아님). 거장 13F(gurus.py)와 같은 '스냅샷·참고용' 성격 — 상위 보유
비중은 근사치이며 실제와 다를 수 있고 수시로 바뀐다(정확 수치는 각 운용사 공시 참고). 새 외부 API
없이 정적 큐레이션으로 유지한다. holdings 비중 합이 100 미만이면 프런트에서 '기타'로 채운다.
"""

from __future__ import annotations

# key: 내부 식별, name: 표시명, market: kr|us, desc: 한 줄 성격, holdings: [{name, weight(%)}]
ETFS = [
    {"key": "spy", "name": "SPY · S&P500", "market": "us", "desc": "미국 대형주 500 시장 전체",
     "holdings": [{"name": "애플", "weight": 7.0}, {"name": "마이크로소프트", "weight": 6.5},
                  {"name": "엔비디아", "weight": 6.2}, {"name": "아마존", "weight": 3.8},
                  {"name": "메타", "weight": 2.5}, {"name": "알파벳", "weight": 4.0},
                  {"name": "브로드컴", "weight": 2.2}]},
    {"key": "qqq", "name": "QQQ · 나스닥100", "market": "us", "desc": "나스닥 대형 기술주 100",
     "holdings": [{"name": "애플", "weight": 9.0}, {"name": "마이크로소프트", "weight": 8.2},
                  {"name": "엔비디아", "weight": 7.8}, {"name": "아마존", "weight": 5.5},
                  {"name": "브로드컴", "weight": 4.5}, {"name": "메타", "weight": 4.5},
                  {"name": "테슬라", "weight": 3.0}]},
    {"key": "schd", "name": "SCHD · 미국 배당", "market": "us", "desc": "미국 고배당 우량주",
     "holdings": [{"name": "애브비", "weight": 4.5}, {"name": "코카콜라", "weight": 4.2},
                  {"name": "시스코", "weight": 4.0}, {"name": "홈디포", "weight": 4.0},
                  {"name": "펩시코", "weight": 3.8}, {"name": "셰브론", "weight": 3.8},
                  {"name": "머크", "weight": 3.5}]},
    {"key": "soxx", "name": "SOXX · 미국 반도체", "market": "us", "desc": "미국 반도체 대표주",
     "holdings": [{"name": "엔비디아", "weight": 9.0}, {"name": "브로드컴", "weight": 8.5},
                  {"name": "AMD", "weight": 7.0}, {"name": "TI", "weight": 5.0},
                  {"name": "퀄컴", "weight": 4.5}, {"name": "마이크론", "weight": 4.0}]},
    {"key": "vti", "name": "VTI · 미국 전체시장", "market": "us", "desc": "미국 상장주식 전체(대형~소형)",
     "holdings": [{"name": "애플", "weight": 6.0}, {"name": "마이크로소프트", "weight": 5.6},
                  {"name": "엔비디아", "weight": 5.3}, {"name": "아마존", "weight": 3.2},
                  {"name": "알파벳", "weight": 3.4}, {"name": "메타", "weight": 2.1}]},
    {"key": "arkk", "name": "ARKK · 혁신성장", "market": "us", "desc": "파괴적 혁신 테마(고위험)",
     "holdings": [{"name": "테슬라", "weight": 10.0}, {"name": "코인베이스", "weight": 9.0},
                  {"name": "로쿠", "weight": 7.0}, {"name": "팔란티어", "weight": 6.0},
                  {"name": "블록", "weight": 5.0}]},
    {"key": "kodex200", "name": "KODEX 200 · 코스피200", "market": "kr", "desc": "국내 대형주 시장 대표",
     "holdings": [{"name": "삼성전자", "weight": 25.0}, {"name": "SK하이닉스", "weight": 9.0},
                  {"name": "삼성바이오로직스", "weight": 3.5}, {"name": "LG에너지솔루션", "weight": 3.0},
                  {"name": "현대차", "weight": 2.8}, {"name": "기아", "weight": 2.2}]},
    {"key": "tiger_snp", "name": "TIGER 미국S&P500", "market": "kr", "desc": "원화로 담는 S&P500",
     "holdings": [{"name": "애플", "weight": 7.0}, {"name": "마이크로소프트", "weight": 6.5},
                  {"name": "엔비디아", "weight": 6.2}, {"name": "아마존", "weight": 3.8},
                  {"name": "알파벳", "weight": 4.0}, {"name": "메타", "weight": 2.5}]},
    {"key": "kodex_2ndbat", "name": "KODEX 2차전지산업", "market": "kr", "desc": "국내 2차전지 밸류체인",
     "holdings": [{"name": "LG에너지솔루션", "weight": 18.0}, {"name": "포스코퓨처엠", "weight": 12.0},
                  {"name": "에코프로비엠", "weight": 11.0}, {"name": "삼성SDI", "weight": 10.0},
                  {"name": "LG화학", "weight": 8.0}]},
    {"key": "tiger_semi", "name": "TIGER 반도체", "market": "kr", "desc": "국내 반도체 밸류체인",
     "holdings": [{"name": "삼성전자", "weight": 22.0}, {"name": "SK하이닉스", "weight": 20.0},
                  {"name": "한미반도체", "weight": 6.0}, {"name": "DB하이텍", "weight": 4.0},
                  {"name": "리노공업", "weight": 3.5}]},
]


def all_etfs() -> list[dict]:
    return ETFS
