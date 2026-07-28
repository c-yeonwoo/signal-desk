"""섹터 상대 모멘텀·수급 — sector_neutral v2 (관측 + 과열 표시).

밸류 v1은 valuation.scores가 이미 섹터 percentile. 여기는 모멘텀·수급을
섹터 내로 상대화해 테마 추격을 드러낸다. 점수 combine은 아직 건드리지 않는다.
"""

from __future__ import annotations

from signal_desk.reference import sectors

_MIN_SECTOR = 4


def _pct_rank_higher_better(values: dict[str, float]) -> dict[str, float]:
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 50.0}
    return {t: round(i / (n - 1) * 100.0, 1) for i, (t, _) in enumerate(items)}


def sector_percentiles(values: dict[str, float]) -> dict[str, float]:
    """섹터 내 percentile(높을수록 큼). 소표본/미분류는 유니버스 percentile."""
    if not values:
        return {}
    uni = _pct_rank_higher_better(values)
    groups: dict[str, list[str]] = {}
    for t in values:
        groups.setdefault(sectors.sector_of(t) or "_none", []).append(t)
    out = dict(uni)
    for sec, ts in groups.items():
        if sec == "_none" or len(ts) < _MIN_SECTOR:
            continue
        out.update(_pct_rank_higher_better({t: values[t] for t in ts}))
    return out


def annotate_rows(
    rows: list[dict],
    *,
    momentum_by: dict[str, float] | None = None,
    flow_by: dict[str, float] | None = None,
) -> list[dict]:
    mom_pct = sector_percentiles(momentum_by or {})
    flow_pct = sector_percentiles(flow_by or {})
    for r in rows:
        t = r.get("ticker")
        rel = {}
        if t in mom_pct:
            rel["momentum_pct"] = mom_pct[t]
            if mom_pct[t] >= 90:
                rel["momentum_hot"] = True
        if t in flow_pct:
            rel["flow_pct"] = flow_pct[t]
        r["sector_rel"] = rel or None
    return rows
