"""퀄리티 팩터 — Piotroski F-Score 정신의 축약(5점). '싸다'와 별개로 '재무가 건강하고 개선 중인가'.

완전한 9점 F-Score는 현금흐름(CFO)·총자산·유동비율·매출총이익률까지 필요한데 현재 DART 추출
범위 밖이라, 가진 항목(순이익·ROE·부채비율·매출성장) + 전년 대비로 5개 체크만 본다:
  ① 순이익 흑자  ② ROE 양(+)  ③ ROE 개선(전년비)  ④ 부채비율 개선(감소)  ⑤ 매출 성장
레벨 기반 재무 팩터(fundamental)와 달리 '방향·건전성'에 초점 → 저평가 가치함정 방어에 보완적.
"""

from __future__ import annotations


def evaluate(cur: dict | None, prev: dict | None) -> dict:
    """당해(cur)·전년(prev) 재무로 축약 F-Score. 반환 {points, max, checks[], has}."""
    cur, prev = cur or {}, prev or {}
    pts, checks = 0, []
    ni, roe = cur.get("net_income"), cur.get("roe")
    dr, rg = cur.get("debt_ratio"), cur.get("revenue_growth")
    roe_p, dr_p = prev.get("roe"), prev.get("debt_ratio")
    if ni is not None and ni > 0:
        pts += 1; checks.append("순이익 흑자")
    if roe is not None and roe > 0:
        pts += 1; checks.append("ROE 양(+)")
    if roe is not None and roe_p is not None and roe > roe_p:
        pts += 1; checks.append("ROE 개선")
    if dr is not None and dr_p is not None and dr < dr_p:
        pts += 1; checks.append("부채비율 개선")
    if rg is not None and rg > 0:
        pts += 1; checks.append("매출 성장")
    have = sum(1 for v in (ni, roe, dr, rg) if v is not None)
    return {"points": pts, "max": 5, "checks": checks, "has": have >= 2}


def component(metrics: dict | None, weight: float) -> tuple[float, float, list[str], int | None, bool]:
    """fundamentals[ticker]에 저장된 quality dict → (norm[-1,1], weight, reasons, points, has_quality).
    계산 근거(체크) 부족하면 가중치 0(제외)."""
    q = (metrics or {}).get("quality")
    if not q or not q.get("has"):
        return 0.0, 0.0, [], None, False
    pts, mx = int(q.get("points", 0)), int(q.get("max", 5)) or 5
    norm = (pts / mx) * 2 - 1  # 0점→-1, 만점→+1
    label = f"[퀄리티] {pts}/{mx}" + (f" — {', '.join(q.get('checks', [])[:3])}" if q.get("checks") else "")
    return round(norm, 3), weight, [label], pts, True
