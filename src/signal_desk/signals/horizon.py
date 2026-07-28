"""모멘텀 multi-horizon 라벨 — 5/20/60일 중 가장 강한 지평.

시그널 kind와 별개. '오늘 BUY'와 '스윙 BUY'를 같은 pill에 섞지 않기 위한 관측 축.
"""

from __future__ import annotations

_HORIZONS = (("5d", 5), ("20d", 20), ("60d", 60))


def returns_at(closes: list[float], i: int | None = None) -> dict[str, float | None]:
    """인덱스 i(기본 마지막) 기준 N일 수익률(%)."""
    if not closes:
        return {k: None for k, _ in _HORIZONS}
    i = len(closes) - 1 if i is None else i
    out: dict[str, float | None] = {}
    for key, n in _HORIZONS:
        j = i - n
        if j < 0 or closes[j] <= 0:
            out[key] = None
            continue
        out[key] = round((closes[i] / closes[j] - 1.0) * 100.0, 2)
    return out


def label(rets: dict[str, float | None]) -> str | None:
    """가장 |수익|이 큰 양(+) 지평. 전부 음/None이면 None."""
    best_k, best_v = None, None
    for k, _n in _HORIZONS:
        v = rets.get(k)
        if v is None or v <= 0:
            continue
        if best_v is None or v > best_v:
            best_k, best_v = k, v
    if not best_k:
        return None
    return {"5d": "단기", "20d": "스윙", "60d": "포지션"}.get(best_k, best_k)


def compute(closes: list[float]) -> dict | None:
    if not closes or len(closes) < 6:
        return None
    rets = returns_at(closes)
    lab = label(rets)
    if lab is None and all(v is None for v in rets.values()):
        return None
    return {"rets": rets, "label": lab, "label_ko": lab}


def annotate_rows(rows: list[dict], closes_by: dict[str, list[float]]) -> list[dict]:
    for r in rows:
        r["horizon"] = compute(closes_by.get(r.get("ticker") or "") or [])
    return rows
