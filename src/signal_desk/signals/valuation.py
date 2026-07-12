"""저평가(밸류에이션) 스크리닝 — PER/PBR 낮은 순 상대 랭킹.

Signal APT의 저평가 탭(입지 대비 가격 저평가율)을 주식으로 옮긴 버전. PER/PBR 둘 다 있는 종목만
대상으로 한다(적자 기업 등 PER 없는 종목은 이 스크리닝에서 제외 — 시그널/기본점수 쪽엔 여전히 반영됨).

섹터 중립화(v1): PER/PBR은 섹터별로 근본적으로 다르므로(반도체 vs 은행 vs 유틸) 엔진 팩터(scores)는
**섹터 내 percentile**로 상대화한다 — 섹터 편향 제거(반도체는 원래 고PER인데 유니버스 비교하면 항상
고평가로 찍힘). 섹터 표본이 작거나(<_MIN_SECTOR) 미분류면 유니버스 percentile로 fallback.
저평가 스크리너(screen)는 '절대 저평가' UX를 위해 유니버스 기준 유지. (레퍼런스: Barra value, sector-neutral)
"""

from __future__ import annotations

from signal_desk.reference import sectors

_MIN_SECTOR = 4   # 섹터 내 percentile 신뢰 최소 표본(미만이면 유니버스 fallback)


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


def _eligible(fundamentals: dict[str, dict]) -> dict[str, dict]:
    return {t: m for t, m in fundamentals.items()
            if m.get("per") is not None and m.get("pbr") is not None}


def _valuation_scores(eligible: dict[str, dict], *, sector_neutral: bool) -> dict[str, float]:
    """ticker -> valuation_score(0=가장 저평가, 100=가장 고평가). sector_neutral이면 섹터 내
    percentile(작은/미분류 섹터는 유니버스 fallback), 아니면 유니버스 percentile."""
    uni_per = _percentile_rank({t: m["per"] for t, m in eligible.items()})
    uni_pbr = _percentile_rank({t: m["pbr"] for t, m in eligible.items()})
    per_pct, pbr_pct = dict(uni_per), dict(uni_pbr)   # 기본값=유니버스(=fallback)
    if sector_neutral:
        groups: dict[str, list[str]] = {}
        for t in eligible:
            groups.setdefault(sectors.sector_of(t) or "_none", []).append(t)
        for sec, ts in groups.items():
            if sec == "_none" or len(ts) < _MIN_SECTOR:
                continue                                # 표본 부족 → 유니버스 유지
            per_pct.update(_percentile_rank({t: eligible[t]["per"] for t in ts}))
            pbr_pct.update(_percentile_rank({t: eligible[t]["pbr"] for t in ts}))
    return {t: round((per_pct[t] + pbr_pct[t]) / 2, 1) for t in eligible}


def screen(universe: list[dict], fundamentals: dict[str, dict]) -> list[dict]:
    """저평가 스크리너 — 유니버스 기준 '절대 저평가'(0=가장 저평가) 오름차순. (사용자 스크리너 UX 보존)"""
    names = {u["ticker"]: u["name"] for u in universe}
    eligible = _eligible(fundamentals)
    if not eligible:
        return []
    sc = _valuation_scores(eligible, sector_neutral=False)
    rows = [{"ticker": t, "name": names.get(t, t), "per": m["per"], "pbr": m["pbr"],
             "roe": m.get("roe"), "valuation_score": sc[t]} for t, m in eligible.items()]
    rows.sort(key=lambda r: r["valuation_score"])
    return rows


def scores(universe: list[dict], fundamentals: dict[str, dict]) -> dict[str, float]:
    """종합 시그널(engine)이 쓰는 밸류 팩터 점수 — **섹터 중립화**(섹터 내 저평가 상대 위치).
    ticker -> valuation_score(0=섹터 내 가장 저평가, 100=섹터 내 가장 고평가)."""
    eligible = _eligible(fundamentals)
    return _valuation_scores(eligible, sector_neutral=True) if eligible else {}
