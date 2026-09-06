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


def sector_map(universe: list[dict] | None) -> dict[str, str]:
    """ticker -> 섹터. **유니버스 행의 `sector` 를 우선 쓰고** 없으면 국내 큐레이션 맵으로 폴백.

    2026-09-06 진단: `sectors.SECTOR_OF` 는 **국내 6자리 코드만** 담고 있어서
    `sector_of("AAPL")` 이 늘 None이었다. 그래서 `_valuation_scores(sector_neutral=True)` 가
    미국 503종목을 전부 `_none` 그룹으로 보내고 **섹터 중립화를 통째로 건너뛰었다** —
    이 모듈 docstring이 경고한 바로 그 상황이다("반도체는 원래 고PER인데 유니버스 비교하면
    항상 고평가로 찍힘").

    실측 결과가 그대로였다: 미국 정보기술이 은행(PER 6.5)·항공(PER 10)과 한 줄로 비교돼
    AAPL 87.4분위 · NVDA 87.7 · MSFT 73.8을 받았다. 섹터 내로 재면 각각 71.9 · 68.0 · **47.7**이다.

    **특혜가 아니라 편향 제거다** — 같은 계산에서 GOOGL은 40.2 → 66.7, META는 57.4 → 75.0으로
    오히려 나빠진다(커뮤니케이션 섹터 안에서는 싼 편이 아니다).

    국내 `universe.json` 행에는 `sector` 키가 없으므로(실측 0/200) 폴백이 걸려 **국내 점수는
    한 자리도 바뀌지 않는다.** 국내는 사전등록 대상이라 그게 중요하다.
    """
    out: dict[str, str] = {}
    for u in universe or []:
        t, sec = u.get("ticker"), u.get("sector")
        if t and sec:
            out[str(t)] = str(sec)
    return out


def _valuation_scores(eligible: dict[str, dict], *, sector_neutral: bool,
                      sector_of: dict[str, str] | None = None) -> dict[str, float]:
    """ticker -> valuation_score(0=가장 저평가, 100=가장 고평가). sector_neutral이면 섹터 내
    percentile(작은/미분류 섹터는 유니버스 fallback), 아니면 유니버스 percentile.

    `sector_of` 를 주면 그 매핑을 먼저 보고, 없는 종목만 국내 큐레이션 맵으로 폴백한다.
    """
    uni_per = _percentile_rank({t: m["per"] for t, m in eligible.items()})
    uni_pbr = _percentile_rank({t: m["pbr"] for t, m in eligible.items()})
    per_pct, pbr_pct = dict(uni_per), dict(uni_pbr)   # 기본값=유니버스(=fallback)
    if sector_neutral:
        groups: dict[str, list[str]] = {}
        for t in eligible:
            sec = (sector_of or {}).get(t) or sectors.sector_of(t)
            groups.setdefault(sec or "_none", []).append(t)
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
    return (_valuation_scores(eligible, sector_neutral=True,
                             sector_of=sector_map(universe))
            if eligible else {})
