"""R13a: freeze first-observed same-FY EPS revision and raw-price research inputs.

The source publication instant is unknown. Only the archive's observed_at can be
proved, so this is a new prospective cohort, not a historical announcement study.
"""

from __future__ import annotations

import datetime as dt
import math
import string

import pandas as pd

from signal_desk import db, market_clock, store
from signal_desk.reference import sectors
from signal_desk.signals import engine, revision_price

VERSION = "r13-s2-first-observed-v1"
START_SESSION = "2026-09-28"
MIN_PROVEN_CANDIDATES = 10
TOP_PCT = 10.0  # research cohort, not live buy-list; fixed before forward outcomes


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fy_eps(row: dict, fiscal: str) -> float | None:
    for i in (1, 2):
        value = row.get(f"fwd{i}_year")
        if value is None or pd.isna(value):
            continue
        year = str(value).removesuffix(".0")
        if year == fiscal:
            return _finite(row.get(f"fwd{i}_eps"))
    return None


def _valid_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in string.hexdigits for c in value)


def freeze_inputs(result: dict, observations: pd.DataFrame,
                  prices: dict[str, list[float]], dates: dict[str, list[str]],
                  now: dt.datetime) -> dict:
    """Re-prove each candidate from two archived content versions, then freeze prices."""
    session = result["price_session"]
    rows = observations.to_dict("records") if observations is not None and not observations.empty else []
    by_ticker: dict[str, list[dict]] = {}
    for row in rows:
        ticker, day = str(row.get("ticker") or ""), str(row.get("date") or "")[:10]
        observed = pd.to_datetime(row.get("observed_at"), utc=True, errors="coerce")
        if ticker and day <= session and pd.notna(observed) and observed <= pd.Timestamp(now):
            by_ticker.setdefault(ticker, []).append(row)
    proven = []
    excluded = []
    for item in result["candidates"]:
        ticker, day, fy = item["ticker"], item["revision_date"], item["eps_fiscal_year"]
        history = sorted(by_ticker.get(ticker, []), key=lambda r: (str(r.get("date")), str(r.get("observed_at"))))
        current = next((r for r in reversed(history) if str(r.get("date"))[:10] == day), None)
        prior = next((r for r in reversed(history) if str(r.get("date"))[:10] < day), None)
        current_eps = _fy_eps(current, fy) if current else None
        prior_eps = _fy_eps(prior, fy) if prior else None
        prior_seen = (pd.to_datetime(prior.get("observed_at"), utc=True, errors="coerce")
                      if prior else pd.NaT)
        current_seen = (pd.to_datetime(current.get("observed_at"), utc=True, errors="coerce")
                        if current else pd.NaT)
        hist_dates, hist_prices = dates.get(ticker) or [], prices.get(ticker) or []
        origin = (_finite(hist_prices[hist_dates.index(session)]) if session in hist_dates
                  and len(hist_dates) == len(hist_prices) else None)
        if (not current or not prior or not _valid_hash(current.get("content_hash"))
                or not _valid_hash(prior.get("content_hash"))
                or current["content_hash"] == prior["content_hash"]
                or pd.isna(prior_seen) or pd.isna(current_seen) or prior_seen > current_seen
                or current_eps is None or prior_eps is None or prior_eps <= 0
                or origin is None or origin <= 0
                or abs(round((current_eps / prior_eps - 1) * 100, 2) - item["eps_revision_pct"]) > 0.011):
            excluded.append(ticker)
            continue
        proven.append({**item, "price": origin,
                       "revision_observed_at": str(current["observed_at"]),
                       "prior_observed_at": str(prior["observed_at"]),
                       "revision_content_hash": str(current["content_hash"]),
                       "prior_content_hash": str(prior["content_hash"]),
                       "prior_eps": prior_eps, "revised_eps": current_eps,
                       "source_published_at": current.get("source_published_at"),
                       "source_available_at_verified": bool(current.get("available_at_verified") is True)})
    if len(proven) < MIN_PROVEN_CANDIDATES:
        return {"ready": False, "reason": "동일 FY·관측시각·내용 해시·세션 종가가 증명된 후보 부족",
                "research_candidates": len(result["candidates"]), "proven_candidates": len(proven),
                "excluded_tickers": excluded}
    # Same proven universe, one changed axis: unreacted-price gap vs EPS revision alone.
    k = engine.rank_slots(len(proven), TOP_PCT)
    gap = sorted(proven, key=lambda r: (-r["research_gap"], r["ticker"]))[:k]
    eps = sorted(proven, key=lambda r: (-r["eps_revision_pct"], r["ticker"]))[:k]
    chosen = {r["ticker"] for r in gap + eps}
    return {"ready": True, "version": VERSION, "mode": "shadow", "live_eligible": False,
            "session": session, "observed_at": now.isoformat(),
            "research_version": result["version"], "revision_version": result["revision_version"],
            "source_available_at_verified": False,
            "research_candidates": len(result["candidates"]), "proven_candidates": len(proven),
            "excluded_tickers": excluded, "top_k": k,
            "policies": {"revision_unreacted_price": [r["ticker"] for r in gap],
                         "eps_revision_only": [r["ticker"] for r in eps]},
            "selected": {r["ticker"]: r for r in proven if r["ticker"] in chosen},
            "note": "최초 관측 이후 접근 가능한 가격만 향후 평가. 원천 발표시각·실체결 미검증, 주문 미연결."}


def capture(now: dt.datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    session = market_clock.latest_completed_session("kr", now)
    if not session or session < START_SESSION or market_clock.is_open("kr", now):
        return {"saved": 0, "reason": "완료된 KR 세션 없음"}
    close = market_clock._calendar("kr").schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=12):
        return {"saved": 0, "reason": "최초 관측 시간창 밖"}
    if db.revision_price_get(session):
        return {"saved": 0, "reason": "이미 동결됨"}
    observations = store.load_consensus_as_of(now)
    prices, dates = store.load_portfolio_close_bundle("kr")
    result = revision_price.build(
        observations=observations, as_of=now, dates_by=dates,
        closes_by=prices, sector_by={t: sectors.sector_of(t) for t in dates})
    if not result.get("research_ready") or result.get("price_session") != session:
        return {"saved": 0, "reason": result.get("reason") or "연구 가격 세션 불일치",
                "eligible": result.get("eligible_count", 0)}
    frozen = freeze_inputs(result, observations, prices, dates, now)
    if not frozen.get("ready"):
        return {"saved": 0, "reason": frozen["reason"],
                "eligible": frozen.get("proven_candidates", 0)}
    return {"saved": int(db.revision_price_add_once(session, frozen)), "session": session,
            "eligible": frozen["proven_candidates"], "top_k": frozen["top_k"]}
