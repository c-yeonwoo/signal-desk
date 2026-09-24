"""추정치 리비전 — Δ목표가·Δ선행EPS. 점수 combine 전 IC 관측용.

consensus_readiness가 ready가 아니면 점수에 넣지 않는다(축적·측정만).
ready면 IC를 재고, 판별력이 확인되기 전엔 annotate/opp 태그만.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

# 방향 신호: 목표가 또는 선행EPS가 유의미하게 상향이면 +1
_EPS_EPS = 1e-9
_PT_EPS = 1e-6
FEATURE_VERSION = "same-fiscal-year-v2"


def _fiscal_period(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    period = str(value).strip()
    if period.endswith(".0") and period[:-2].isdigit():
        period = period[:-2]
    return period if len(period) in (4, 6) and period.isascii() and period.isdigit() else None


def _delta_between(ticker: str, a: dict, b: dict) -> dict | None:
    """연속한 두 PIT 스냅샷의 리비전. 날짜 당시에 알 수 있던 값만 쓴다."""
    def num(v):
        try:
            x = float(v)
            return x if math.isfinite(x) else None
        except (TypeError, ValueError):
            return None

    d_eps = d_pt = None
    eps_year = None
    previous = {_fiscal_period(a.get(f"fwd{i}_year")): num(a.get(f"fwd{i}_eps"))
                for i in (1, 2) if _fiscal_period(a.get(f"fwd{i}_year"))}
    for i in (1, 2):
        year = _fiscal_period(b.get(f"fwd{i}_year"))
        if not year:
            continue
        ea, eb = previous.get(year), num(b.get(f"fwd{i}_eps"))
        # A zero/negative base makes percentage revisions unstable or misleading.
        if ea is not None and eb is not None and ea > _EPS_EPS:
            eps_year = year
            d_eps = (eb - ea) / ea * 100.0
            break
    pa, pb = num(a.get("price_target_mean")), num(b.get("price_target_mean"))
    if pa is not None and pb is not None and pa > _PT_EPS:
        d_pt = (pb - pa) / pa * 100.0
    vals = [max(-50.0, min(50.0, x)) for x in (d_eps, d_pt) if x is not None]
    if not vals:
        return None
    strength = sum(vals) / len(vals)
    sig = 1 if strength > 1.0 else (-1 if strength < -1.0 else 0)
    return {
        "ticker": str(ticker), "date": str(b.get("date"))[:10],
        "feature_version": FEATURE_VERSION, "eps_fiscal_year": eps_year,
        "d_eps_pct": round(d_eps, 2) if d_eps is not None else None,
        "d_pt_pct": round(d_pt, 2) if d_pt is not None else None,
        "revision_score": round(strength, 4), "signal": sig,
    }


def delta_history_from_history(df) -> list[dict]:
    """consensus_history → 모든 날짜×종목 PIT 리비전 패널.

    종목별 최신 두 행만 남기면 매일 새 스냅샷이 들어올 때 어제의 성숙 가능한
    관측이 사라진다. IC는 역사 전체를 날짜별 횡단면으로 보존해 재다.
    """
    if df is None or getattr(df, "empty", True):
        return []
    need = {"ticker", "date"}
    if not need <= set(df.columns):
        return []
    out: list[dict] = []
    sub = df.sort_values(["ticker", "date"])
    for t, g in sub.groupby("ticker", sort=False):
        rows = g.to_dict("records")
        for a, b in zip(rows, rows[1:]):
            if str(a.get("date"))[:10] == str(b.get("date"))[:10]:
                continue
            d = _delta_between(str(t), a, b)
            if d:
                out.append(d)
    return out


def deltas_from_history(df) -> dict[str, dict]:
    """현재 주석·opp 태그용 최신 리비전. IC는 `delta_history_from_history`를 쓴다."""
    out: dict[str, dict] = {}
    for d in delta_history_from_history(df):
        out[d["ticker"]] = {k: v for k, v in d.items() if k != "ticker"}
    return out


def ic_rank(signals: list[float], forwards: list[float]) -> float | None:
    """스피어만 순위상관(간단). 표본 <8이면 None."""
    n = min(len(signals), len(forwards))
    if n < 8:
        return None
    xs, ys = signals[:n], forwards[:n]
    rx = _ranks(xs)
    ry = _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return round(num / (denx * deny), 4)


def _ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    for r, i in enumerate(order):
        ranks[i] = float(r)
    return ranks


def measure_ic(
    deltas: list[dict] | dict[str, dict],
    closes_by: dict[str, list[float]],
    dates_by: dict[str, list[str]],
    *,
    horizon: int = 20,
) -> dict[str, Any]:
    """날짜별 횡단면 리비전 IC. 동일 종목은 하나의 날짜 관측으로 묶는다."""
    from signal_desk.signals import accuracy

    if isinstance(deltas, dict):
        records = [{"ticker": t, **d} for t, d in deltas.items()]
    else:
        records = list(deltas or [])
    pairs_by_date: dict[str, list[tuple[float, float]]] = {}
    for d in records:
        t = str(d.get("ticker") or "")
        strength = d.get("revision_score")
        if strength is None:
            strength = d.get("signal")
        try:
            strength = float(strength)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(strength):
            continue
        dates = dates_by.get(t) or []
        closes = closes_by.get(t) or []
        if len(dates) != len(closes) or not dates:
            continue
        day = str(d["date"])[:10]
        fwd = accuracy.forward_returns(dates, closes, day, (int(horizon),))
        if int(horizon) in fwd:
            pairs_by_date.setdefault(day, []).append((strength, fwd[int(horizon)]))
    stat = accuracy.cross_sectional_ic(
        pairs_by_date, horizon=int(horizon), min_dates=20, min_breadth=8)
    independent = int(stat.get("independent_dates") or 0)
    ready_for_score = bool(stat.get("significant") and stat.get("ic") is not None
                           and stat["ic"] >= 0.02 and independent >= 5)
    return {
        **stat,
        "feature_version": FEATURE_VERSION,
        "n": int(stat.get("n_dates") or 0),
        "ready_for_score": ready_for_score,
        "note": ("IC≥0.02·유의·독립관측≈5개 이상일 때만 점수 투입 후보"
                 "(현재는 annotate만)"),
    }


def annotate_rows(rows: list[dict], deltas: dict[str, dict]) -> list[dict]:
    for r in rows:
        d = deltas.get(r.get("ticker") or "")
        r["revision"] = d
        if d and d.get("signal") == 1:
            tags = list(r.get("opp_tags") or [])
            if "리비전상향" not in tags:
                tags.append("리비전상향")
            r["opp_tags"] = tags
        elif d and d.get("signal") == -1:
            tags = list(r.get("opp_tags") or [])
            if "리비전하향" not in tags:
                tags.append("리비전하향")
            r["opp_tags"] = tags
    return rows


def load_deltas():
    from signal_desk import store
    return deltas_from_history(store.load_consensus_history())


def load_delta_history():
    from signal_desk import store
    return delta_history_from_history(store.load_consensus_history())
