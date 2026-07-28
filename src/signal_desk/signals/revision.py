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


def deltas_from_history(df) -> dict[str, dict]:
    """consensus_history DataFrame → {ticker: {date, d_eps_pct, d_pt_pct, signal}}."""
    if df is None or getattr(df, "empty", True):
        return {}
    need = {"ticker", "date"}
    if not need <= set(df.columns):
        return {}
    out: dict[str, dict] = {}
    sub = df.sort_values(["ticker", "date"])
    for t, g in sub.groupby("ticker", sort=False):
        rows = g.to_dict("records")
        if len(rows) < 2:
            continue
        a, b = rows[-2], rows[-1]
        d_eps = d_pt = None
        ea, eb = a.get("fwd1_eps"), b.get("fwd1_eps")
        if ea is not None and eb is not None:
            try:
                ea, eb = float(ea), float(eb)
                if abs(ea) > _EPS_EPS:
                    d_eps = (eb - ea) / abs(ea) * 100.0
            except (TypeError, ValueError):
                pass
        pa, pb = a.get("price_target_mean"), b.get("price_target_mean")
        if pa is not None and pb is not None:
            try:
                pa, pb = float(pa), float(pb)
                if pa > _PT_EPS:
                    d_pt = (pb - pa) / pa * 100.0
            except (TypeError, ValueError):
                pass
        if d_eps is None and d_pt is None:
            continue
        sig = 0
        if (d_eps is not None and d_eps > 1.0) or (d_pt is not None and d_pt > 1.0):
            sig = 1
        elif (d_eps is not None and d_eps < -1.0) or (d_pt is not None and d_pt < -1.0):
            sig = -1
        out[str(t)] = {
            "date": str(b.get("date"))[:10],
            "d_eps_pct": round(d_eps, 2) if d_eps is not None else None,
            "d_pt_pct": round(d_pt, 2) if d_pt is not None else None,
            "signal": sig,
        }
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
    deltas: dict[str, dict],
    closes_by: dict[str, list[float]],
    dates_by: dict[str, list[str]],
    *,
    horizon: int = 20,
) -> dict[str, Any]:
    """스냅샷 날짜 이후 horizon 거래일 수익률 vs 리비전 신호 IC."""
    sigs, fwds = [], []
    for t, d in deltas.items():
        if not d.get("signal"):
            continue
        dates = dates_by.get(t) or []
        closes = closes_by.get(t) or []
        if len(dates) != len(closes) or not dates:
            continue
        day = str(d["date"])[:10]
        try:
            i0 = next(i for i, x in enumerate(dates) if str(x)[:10] >= day)
        except StopIteration:
            continue
        i1 = i0 + horizon
        if i1 >= len(closes) or closes[i0] <= 0:
            continue
        fwds.append(closes[i1] / closes[i0] - 1.0)
        sigs.append(float(d["signal"]))
    ic = ic_rank(sigs, fwds)
    return {
        "n": len(sigs),
        "ic": ic,
        "horizon": horizon,
        "ready_for_score": bool(ic is not None and ic >= 0.02 and len(sigs) >= 20),
        "note": "IC≥0.02·표본≥20일 때만 점수 투입 후보(현재는 annotate만)",
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
