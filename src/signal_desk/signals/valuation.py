"""저평가(밸류에이션) 스크리닝 — PER/PBR 낮은 순 상대 랭킹.

Signal APT의 저평가 탭(입지 대비 가격 저평가율)을 주식으로 옮긴 버전. 아직 섹터/업종 분류가
없어서(BACKLOG phase2 #10) 지금은 전체 유니버스 내 상대 순위로 근사한다 — 섹터별 비교가
붙으면 그룹 내 순위로 정교화할 예정. PER/PBR 둘 다 있는 종목만 대상으로 한다(적자 기업 등
PER 없는 종목은 이 스크리닝에서 제외 — 시그널/기본점수 쪽엔 여전히 반영됨).
"""

from __future__ import annotations


def _percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """작을수록(저평가) 낮은 percentile(0)을 받도록. 동순위는 평균 랭크로 처리."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j < n and items[j][1] == items[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2
        pct = avg_rank / (n - 1) * 100 if n > 1 else 0.0
        for k in range(i, j):
            ranks[items[k][0]] = pct
        i = j
    return ranks


def screen(universe: list[dict], fundamentals: dict[str, dict]) -> list[dict]:
    """PER/PBR 둘 다 있는 종목을 valuation_score(0=가장 저평가, 100=가장 고평가) 오름차순 반환."""
    names = {u["ticker"]: u["name"] for u in universe}
    eligible = {
        t: m for t, m in fundamentals.items()
        if m.get("per") is not None and m.get("pbr") is not None
    }
    if not eligible:
        return []

    per_pct = _percentile_rank({t: m["per"] for t, m in eligible.items()})
    pbr_pct = _percentile_rank({t: m["pbr"] for t, m in eligible.items()})

    rows = []
    for t, m in eligible.items():
        valuation_score = round((per_pct[t] + pbr_pct[t]) / 2, 1)
        rows.append({
            "ticker": t,
            "name": names.get(t, t),
            "per": m["per"],
            "pbr": m["pbr"],
            "roe": m.get("roe"),
            "valuation_score": valuation_score,
        })
    rows.sort(key=lambda r: r["valuation_score"])
    return rows


def scores(universe: list[dict], fundamentals: dict[str, dict]) -> dict[str, float]:
    """ticker -> valuation_score(0=가장 저평가, 100=가장 고평가). screen()과 동일 계산이지만
    종합 시그널(signals/engine.py)이 종목별로 바로 조회해 쓸 수 있게 dict로 반환한다."""
    return {row["ticker"]: row["valuation_score"] for row in screen(universe, fundamentals)}
