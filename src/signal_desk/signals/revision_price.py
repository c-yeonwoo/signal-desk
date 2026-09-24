"""Shadow study of same-FY EPS revisions versus already observed sector-relative prices.

This is a research ranking, not a return forecast or an order signal. The input panel
must come from an as-observed archive; older date-only snapshots are not admitted.
"""

from __future__ import annotations

import datetime
import math
from statistics import median

import pandas as pd
import exchange_calendars as xcals

from signal_desk.signals import portfolio_audit, revision

VERSION = "revision-price-shadow-v1"
PRICE_WINDOW = 5
MIN_PEERS = 3
MIN_CANDIDATES = 10


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) < 2:
        return {key: 0.5 for key in values}
    out = {}
    for key, value in ordered:
        below = sum(other < value for other in values.values())
        same = sum(other == value for other in values.values())
        out[key] = (below + (same - 1) / 2) / (len(values) - 1)
    return out


def build(*, observations: pd.DataFrame, as_of: datetime.datetime,
          dates_by: dict[str, list[str]], closes_by: dict[str, list[float]],
          sector_by: dict[str, str]) -> dict:
    """Rank positive revisions by the gap to their sector-relative five-session return."""
    base = {"version": VERSION, "revision_version": revision.FEATURE_VERSION,
            "mode": "shadow", "live_eligible": False, "return_forecast": None,
            "source_available_at_verified": False, "as_of": as_of.isoformat()}

    def blocked(reason: str, **fields):
        return {**base, "research_ready": False, "reason": reason, "candidates": [], **fields}

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("timezone-aware as_of required")
    clock = portfolio_audit.clock_context("kr", as_of)
    if not clock.get("ready"):
        return blocked("KRX 마지막 완료 거래일을 확인할 수 없습니다.")
    session = clock["expected_price_session"]
    if observations is None or observations.empty:
        return blocked("관측시각이 있는 컨센서스 이력이 없습니다.", price_session=session)
    required = {"ticker", "date", "observed_at", "fwd1_year", "fwd1_eps"}
    if not required <= set(observations.columns):
        return blocked("컨센서스 시점/회계연도 필드가 부족합니다.", price_session=session)
    observed_at = pd.to_datetime(observations["observed_at"], utc=True, errors="coerce")
    valid = observed_at.notna() & (observed_at <= pd.Timestamp(as_of)) & (observations["date"] <= session)
    panel = observations.loc[valid].copy()
    if panel.empty:
        return blocked("판단 시각 이전에 관측된 컨센서스가 없습니다.", price_session=session)
    panel["_observed_utc"] = observed_at.loc[panel.index]
    panel = (panel.sort_values("_observed_utc", kind="stable")
                  .drop_duplicates(["date", "ticker"], keep="last")
                  .drop(columns="_observed_utc"))

    calendar = xcals.get_calendar("XKRX")
    last = pd.Timestamp(session)
    try:
        expected = [day.date().isoformat() for day in
                    calendar.sessions_window(last, -(PRICE_WINDOW + 1))]
    except (ValueError, KeyError):
        return blocked("가격 비교 거래일을 확인할 수 없습니다.", price_session=session)
    if len(expected) != PRICE_WINDOW + 1:
        return blocked("가격 비교 거래일이 부족합니다.", price_session=session)
    returns: dict[str, float] = {}
    for ticker, dates in dates_by.items():
        closes = closes_by.get(ticker) or []
        if len(dates) != len(closes) or len(dates) < PRICE_WINDOW + 1 or dates != sorted(set(dates)):
            continue
        if session not in dates:
            continue
        end = dates.index(session)
        if end < PRICE_WINDOW or dates[end - PRICE_WINDOW:end + 1] != expected:
            continue
        try:
            first, final = float(closes[end - PRICE_WINDOW]), float(closes[end])
        except (TypeError, ValueError):
            continue
        if first > 0 and final > 0 and math.isfinite(first) and math.isfinite(final):
            returns[ticker] = final / first - 1.0

    revisions = revision.deltas_from_history(panel)
    eps: dict[str, float] = {}
    residual: dict[str, float] = {}
    evidence = {}
    for ticker, item in revisions.items():
        if (ticker not in returns or item.get("d_eps_pct") is None
                or item["d_eps_pct"] <= 0 or item["date"] < expected[0]
                or item["date"] > session):
            continue
        sector = sector_by.get(ticker)
        if not sector:
            continue
        peers = [ret for peer, ret in returns.items()
                 if peer != ticker and sector_by.get(peer) == sector]
        if len(peers) < MIN_PEERS:
            continue
        eps[ticker] = float(item["d_eps_pct"])
        residual[ticker] = returns[ticker] - median(peers)
        evidence[ticker] = {"sector": sector, "peer_count": len(peers),
                            "revision_date": item["date"], "eps_fiscal_year": item["eps_fiscal_year"]}
    if len(eps) < MIN_CANDIDATES:
        return blocked("동일 FY 상향·가격·동종업종 동시 관측 후보가 부족합니다.",
                       price_session=session, eligible_count=len(eps), min_candidates=MIN_CANDIDATES)
    eps_rank, reaction_rank = _percentiles(eps), _percentiles(residual)
    candidates = [{"ticker": ticker, **evidence[ticker], "eps_revision_pct": eps[ticker],
                   "sector_relative_return_pct": round(residual[ticker] * 100, 4),
                   "eps_rank": round(eps_rank[ticker], 4), "price_reaction_rank": round(reaction_rank[ticker], 4),
                   "research_gap": round(eps_rank[ticker] - reaction_rank[ticker], 4)}
                  for ticker in eps]
    candidates.sort(key=lambda item: (-item["research_gap"], item["ticker"]))
    return {**base, "research_ready": True, "price_session": session,
            "eligible_count": len(candidates), "candidates": candidates,
            "note": "과거 관측 기반 탐색 순위이며 수익 확률·매매 승인으로 해석할 수 없습니다."}
