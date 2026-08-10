"""퀄리티 팩터 — Piotroski F-Score 정신의 축약(5점). '싸다'와 별개로 '재무가 건강하고 개선 중인가'.

완전한 9점 F-Score는 현금흐름(CFO)·총자산·유동비율·매출총이익률까지 필요한데 현재 DART 추출
범위 밖이라, 가진 항목(순이익·ROE·부채비율·매출성장) + 전년 대비로 5개 체크만 본다:
  ① 순이익 흑자  ② ROE 양(+)  ③ ROE 개선(전년비)  ④ 부채비율 개선(감소)  ⑤ 매출 성장
레벨 기반 재무 팩터(fundamental)와 달리 '방향·건전성'에 초점 → 저평가 가치함정 방어에 보완적.
"""

from __future__ import annotations


def evaluate(cur: dict | None, prev: dict | None) -> dict:
    """당해(cur)·전년(prev) 재무로 축약 F-Score. 반환 {points, max, evaluable, checks[], has}.

    **`max` 는 실제로 판정할 수 있었던 항목 수다 — 5 고정이 아니다.**

    고정 5였을 때의 버그(2026-08-08 실측): `component` 가 `(pts/max)*2-1` 로 정규화하는데,
    미국 재무(EDGAR)는 순이익·자기자본만 있어 평가 가능한 항목이 **2개**뿐이다. 그러면
    **재무가 완벽한 미국 기업도 `(2/5)*2-1 = -0.2` 로 음수**를 받는다 — 판정할 수 없었던 항목을
    실패로 세는 것이고, 시장 전체에 걸린 조용한 감점이다.

    분모를 평가 가능한 개수로 두면 "본 것 중 몇 개를 통과했나"가 된다. 국내는 DART가 5개 항목을
    모두 주므로 `max=5` 그대로여서 **점수가 바뀌지 않는다**(전년 재무가 없는 종목만 달라지고,
    그 경우 예전 값은 판정 불가를 실패로 센 값이었다).
    """
    cur, prev = cur or {}, prev or {}
    ni, roe = cur.get("net_income"), cur.get("roe")
    dr, rg = cur.get("debt_ratio"), cur.get("revenue_growth")
    roe_p, dr_p = prev.get("roe"), prev.get("debt_ratio")
    # (이름, 판정 가능?, 통과?) — 가능 여부와 통과 여부를 **따로** 센다.
    spec = [
        ("순이익 흑자", ni is not None, lambda: ni > 0),
        ("ROE 양(+)", roe is not None, lambda: roe > 0),
        ("ROE 개선", roe is not None and roe_p is not None, lambda: roe > roe_p),
        ("부채비율 개선", dr is not None and dr_p is not None, lambda: dr < dr_p),
        ("매출 성장", rg is not None, lambda: rg > 0),
    ]
    pts, evaluable, checks = 0, 0, []
    for name, can, ok in spec:
        if not can:
            continue
        evaluable += 1
        if ok():
            pts += 1
            checks.append(name)
    have = sum(1 for v in (ni, roe, dr, rg) if v is not None)
    return {"points": pts, "max": evaluable, "evaluable": evaluable,
            "checks": checks, "has": have >= 2 and evaluable >= 2}


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
