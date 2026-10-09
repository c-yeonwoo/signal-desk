"""Read-only audit of *recorded* daily signals against later unadjusted price bars.

This is an exploratory diagnostic. It does not reconstruct missing factors or run the
registered harness, and its output is never a trading or promotion verdict.
"""

from __future__ import annotations

import math
import hashlib
import json
from datetime import date
from collections import defaultdict
from statistics import mean, median

import pandas as pd

from signal_desk import market_clock

HORIZONS = (1, 5, 20)
FACTOR_COLUMNS = ("technical", "fundamental", "valuation", "reversion",
                  "flow", "quality", "momentum", "short")
MAJOR_KR_TICKERS = ("005930", "000660", "005380", "000270", "035420",
                    "035720", "373220", "207940", "267250", "105560")
MAJOR_US_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
                    "META", "TSLA", "JPM", "AVGO", "WMT")
EXPORT_SIGNAL_COLUMNS = ("date", "ticker", "score", "kind", *FACTOR_COLUMNS,
                         "qualitative", "rank", "rank_eligible", "gate_blocked",
                         "event_risk", "low_coverage", "data_coverage",
                         "decision_blocked", "reasons_json", "observed_at", "bar_asof")
EXPORT_PRICE_COLUMNS = ("date", "ticker", "open", "close", "volume")
CASEBOOK_PLAN_VERSION = "recorded-signal-casebook-v1"


def plan_recorded_casebook(signals: pd.DataFrame, *, market: str,
                           protected_start: str, signal_sha256: str) -> dict:
    """Freeze outcome-blind cases from saved signals, never inspecting price bars.

    Select one hash-minimum ticker-date in each market/week/period/entry-state
    stratum. This is a diagnostic casebook, not an independent performance sample.
    """
    if market not in {"kr", "us"} or not {"date", "ticker", "kind"} <= set(signals.columns):
        raise ValueError("invalid market or signal casebook columns")
    try:
        first_protected = date.fromisoformat(protected_start)
    except (TypeError, ValueError):
        raise ValueError("invalid protected start") from None
    if len(signal_sha256) != 64 or any(c not in "0123456789abcdef" for c in signal_sha256):
        raise ValueError("invalid signal SHA-256")
    frame = signals.copy()
    if "market" in frame:
        frame = frame[frame["market"].fillna("kr").astype(str) == market]
    frame["date"] = frame["date"].astype(str)
    frame["ticker"] = frame["ticker"].astype(str)
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate signal ticker-date")

    candidate_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    chosen: dict[tuple[str, str, str], tuple[str, dict]] = {}
    exclusions: dict[str, int] = defaultdict(int)
    prior_by_ticker: dict[str, dict] = {}
    buy_kinds = {"BUY", "STRONG_BUY"}
    for row in frame.sort_values(["date", "ticker"]).to_dict("records"):
        day, ticker, kind = str(row["date"]), str(row["ticker"]), str(row["kind"])
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            exclusions["invalid_date"] += 1
            continue
        if not market_clock.is_session(market, day):
            exclusions["non_session"] += 1
            continue
        if not ticker or ticker in {"nan", "None"} or kind not in {
                "STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"}:
            exclusions["invalid_ticker_or_kind"] += 1
            continue
        prior = prior_by_ticker.get(ticker)
        continuous = bool(prior and market_clock.consecutive_sessions(market, prior["date"], day))
        if not continuous:
            stratum = "context_gap"
        elif kind in buy_kinds and prior["kind"] not in buy_kinds:
            stratum = "new_buy"
        elif kind in buy_kinds:
            stratum = "continuing_buy"
        elif prior["kind"] in buy_kinds:
            stratum = "buy_exit"
        elif kind in {"SELL", "STRONG_SELL"}:
            stratum = "sell"
        else:
            stratum = "hold"
        prior_by_ticker[ticker] = {"date": day, "kind": kind}
        period = "protected" if parsed >= first_protected else "development"
        week = f"{parsed.isocalendar().year}-W{parsed.isocalendar().week:02d}"
        key = (period, week, stratum)
        candidate_counts[key] += 1
        digest = hashlib.sha256(
            f"{CASEBOOK_PLAN_VERSION}|{market}|{period}|{week}|{stratum}|{day}|{ticker}".encode()
        ).hexdigest()
        sample = {"date": day, "ticker": ticker, "kind": kind,
                  "source_level": "C_legacy_snapshot_unverified",
                  "observed_at_present": bool(pd.notna(row.get("observed_at"))),
                  "bar_asof_present": bool(pd.notna(row.get("bar_asof"))),
                  "factor_output_missing": [name for name in FACTOR_COLUMNS
                                            if name not in row or pd.isna(row[name])],
                  "raw_factor_inputs_verified": False,
                  "source_publication_time_verified": False}
        if key not in chosen or digest < chosen[key][0]:
            chosen[key] = (digest, sample)
    slots = [{"period": period, "week": week, "stratum": stratum,
              "eligible_signal_rows": candidate_counts[(period, week, stratum)],
              "selection_sha256": chosen[(period, week, stratum)][0],
              **chosen[(period, week, stratum)][1]}
             for period, week, stratum in sorted(chosen)]
    result = {"schema": CASEBOOK_PLAN_VERSION, "market": market,
              "signal_sha256": signal_sha256, "protected_start": protected_start,
              "selection": "one_sha256_min_per_period_iso_week_entry_state_no_price_access",
              "signal_rows_seen": len(frame), "excluded_rows": dict(sorted(exclusions.items())),
              "slots": slots,
              "limitations": ["가격·미래 수익·기업행사·뉴스 원문을 열지 않고 선정한 진단 사례집입니다.",
                              "보호 구간 사례의 성과를 합산하거나 가중치·주문 규칙을 바꾸는 근거가 아닙니다.",
                              "저장 신호에는 당시 원시 팩터와 원천 공개시각 검증이 없습니다."]}
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["plan_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return result


def select_forensic_case_keys(signals: pd.DataFrame, *, market: str,
                              major_tickers: tuple[str, ...] = (),
                              score_delta_threshold: float = 0.5) -> set[tuple[str, str]]:
    """Choose recorded incidents using signal metadata only, before reading outcomes."""
    if market not in {"kr", "us"} or score_delta_threshold < 0:
        raise ValueError("invalid market or score threshold")
    if not {"date", "ticker", "score", "kind"} <= set(signals.columns):
        raise ValueError("signal history lacks required columns")
    frame = signals[["date", "ticker", "score", "kind"]].copy()
    frame["date"] = frame["date"].astype(str)
    frame["ticker"] = frame["ticker"].astype(str)
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate signal ticker-date: select one archived observation first")
    keys: set[tuple[str, str]] = set()
    prior_by_ticker: dict[str, dict] = {}
    major_set = set(major_tickers)
    for signal in frame.sort_values(["date", "ticker"]).to_dict("records"):
        if not market_clock.is_session(market, signal["date"]):
            # Holiday snapshots are archived observations, not tradeable decisions
            # or the previous regular-session decision for a later transition.
            continue
        ticker = signal["ticker"]
        prior = prior_by_ticker.get(ticker)
        score = _number(signal["score"])
        prior_score = _number(prior["score"]) if prior else None
        kind_change = prior is not None and signal["kind"] != prior["kind"]
        score_change = (score is not None and prior_score is not None
                        and abs(round(score - prior_score, 4)) >= score_delta_threshold)
        if kind_change or score_change or ticker in major_set:
            keys.add((signal["date"], ticker))
        prior_by_ticker[ticker] = signal
    return keys


def inventory_recorded_inputs(signals: pd.DataFrame, prices: pd.DataFrame, *,
                              market: str, protected_start: str) -> dict:
    """Describe input coverage without reading returns or opening protected outcomes.

    Column presence and non-null timestamps are *not* proof that a provider published
    that version before the decision. The export is therefore C-level only.
    """
    if market not in {"kr", "us"} or not {"date", "ticker"} <= set(signals.columns):
        raise ValueError("invalid market or signal inventory columns")
    if not {"date", "ticker", "open", "close"} <= set(prices.columns):
        raise ValueError("price inventory lacks required columns")
    if not isinstance(protected_start, str) or len(protected_start) != 10:
        raise ValueError("invalid protected start")
    # The bounded admin export has already selected the requested market and
    # intentionally omits `market`.  Do not mistake a US-only export for KR.
    frame = signals
    if "market" in frame:
        frame = frame[frame["market"].fillna("kr").astype(str) == market]
    dates = frame["date"].astype(str)
    price_dates = prices["date"].astype(str)
    invalid_dates = {day for day in dates.unique() if not market_clock.is_session(market, day)}

    def bounds(values: pd.Series) -> list[str] | None:
        return [values.min(), values.max()] if not values.empty else None

    def present(name: str) -> dict:
        return {"column_present": name in frame,
                "non_null_rows": int(frame[name].notna().sum()) if name in frame else 0,
                "source_time_verified": False}

    observed_membership = [
        {"date": day, "tickers": sorted(group["ticker"].dropna().astype(str).unique().tolist())}
        for day, group in frame.assign(date=dates).groupby("date", sort=True)
    ]
    signal_fields_by_date = [
        {"date": day, "rows": len(group),
         "non_null": {name: int(group[name].notna().sum()) for name in EXPORT_SIGNAL_COLUMNS
                      if name not in ("date", "ticker") and name in group}}
        for day, group in frame.assign(date=dates).groupby("date", sort=True)
    ]
    price_fields_by_date = [
        {"date": day, "rows": len(group),
         "open": int(group["open"].notna().sum()),
         "close": int(group["close"].notna().sum()),
         "volume": int(group["volume"].notna().sum()) if "volume" in group else 0}
        for day, group in prices.assign(date=price_dates).groupby("date", sort=True)
    ]

    return {
        "market": market, "mode": "metadata_only_no_outcomes",
        "source_level": "C_legacy_snapshot_unverified",
        "signal_rows": len(frame), "signal_dates": bounds(dates),
        "signal_sessions": int(dates.nunique()),
        "signal_tickers": int(frame["ticker"].dropna().astype(str).nunique()),
        "missing_signal_ticker_rows": int(frame["ticker"].isna().sum()),
        "observed_membership_by_date": observed_membership,
        "signal_fields_by_date": signal_fields_by_date,
        "duplicate_signal_ticker_dates": int(frame.duplicated(["date", "ticker"]).sum()),
        "protected_start": protected_start,
        "development_rows": int((dates < protected_start).sum()),
        "protected_rows": int((dates >= protected_start).sum()),
        "invalid_signal_session_rows": int(dates.isin(invalid_dates).sum()),
        "invalid_signal_sessions": sorted(invalid_dates),
        "saved_signal_output_fields": {name: present(name) for name in EXPORT_SIGNAL_COLUMNS
                                       if name not in ("date", "ticker")},
        "price_rows": len(prices), "price_dates": bounds(price_dates),
        "price_fields_by_date": price_fields_by_date,
        "price_tickers": int(prices["ticker"].dropna().astype(str).nunique()),
        "price_missing_open_rows": int(prices["open"].isna().sum()),
        "price_missing_close_rows": int(prices["close"].isna().sum()),
        "price_volume_rows": int(prices["volume"].notna().sum()) if "volume" in prices else 0,
        "price_duplicate_ticker_dates": int(prices.duplicated(["date", "ticker"]).sum()),
        "strict_pit_eligible": False,
        "unverified": ["original source publication time and version",
                       "historical full-universe membership and corporate actions",
                       "historical fundamental, flow, short, and event input versions"],
    }


def select_recorded_inputs(signals: pd.DataFrame, prices: pd.DataFrame, *,
                           market: str, sessions: int = 45) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bounded, read-only export. Never backfill or synthesize absent rows."""
    if market not in {"kr", "us"} or not 1 <= sessions <= 61:
        raise ValueError("invalid market or session count")
    if not {"date", "ticker", "score", "kind"} <= set(signals.columns):
        raise ValueError("signal history lacks required columns")
    if not {"date", "ticker", "open", "close"} <= set(prices.columns):
        raise ValueError("price history lacks required columns")
    frame = signals.copy()
    if "market" in frame:
        frame = frame[frame["market"].fillna("kr").astype(str) == market]
    elif market == "us":
        frame = frame.iloc[0:0]
    frame["date"] = frame["date"].astype(str)
    dates = sorted(frame["date"].unique())[-sessions:]
    frame = frame[frame["date"].isin(dates)]
    if len(frame) > 35000:
        raise ValueError("signal export exceeds row budget")
    frame = frame[[name for name in EXPORT_SIGNAL_COLUMNS if name in frame]].copy()
    bars = prices.copy()
    bars["date"] = bars["date"].astype(str)
    bars["ticker"] = bars["ticker"].astype(str)
    if dates:
        tickers = set(frame["ticker"].astype(str))
        bars = bars[bars["date"].ge(dates[0]) & bars["ticker"].isin(tickers)]
    else:
        bars = bars.iloc[0:0]
    if len(bars) > 45000:
        raise ValueError("price export exceeds row budget")
    bars = bars[[name for name in EXPORT_PRICE_COLUMNS if name in bars]].copy()
    return frame.reset_index(drop=True), bars.reset_index(drop=True)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flag(row: dict, name: str) -> bool | None:
    value = row.get(name)
    if value is None or pd.isna(value):
        return None
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    return None


def _selection_evidence(row: dict) -> dict:
    """Saved metadata only; a changed eligibility bit is not a proven root cause."""
    from signal_desk.signals.pick_reason import parse_reasons_json

    reasons = parse_reasons_json(row.get("reasons_json"))
    selection_reasons = [reason for reason in reasons if reason.startswith("[선정]")]
    # Engine trend/earnings/crash/event gates and the post-engine execution gate
    # share gate_blocked. A relaxed trend reason is not itself a blocking cause.
    gate_reasons = [reason for reason in reasons
                    if reason.startswith(("[추세]", "[실적]", "[급락]", "[악재]", "[선반영]", "[추격]"))
                    and ("매수 차단" in reason or "매수 보류" in reason)]
    return {
        "rank": _number(row.get("rank")),
        "rank_eligible": _flag(row, "rank_eligible"),
        "gate_blocked": _flag(row, "gate_blocked"),
        "event_risk": _flag(row, "event_risk"),
        "low_coverage": _flag(row, "low_coverage"),
        "data_coverage": _number(row.get("data_coverage")),
        "decision_blocked": _flag(row, "decision_blocked"),
        "selection_reason": selection_reasons[-1] if selection_reasons else None,
        "gate_reason": gate_reasons[-1] if gate_reasons else None,
    }


def _score_results(rows: list[dict], horizon: int) -> dict:
    ready = [row for row in rows if row["outcomes"][str(horizon)]["state"] == "matured"]
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        statuses[row["outcomes"][str(horizon)]["state"]] += 1
    if not ready:
        return {"observations": len(rows), "matured": 0, "dates": 0,
                "statuses": dict(sorted(statuses.items()))}
    net = [row["outcomes"][str(horizon)]["net_pct"] for row in ready]
    excess = [row["outcomes"][str(horizon)]["excess_pct"] for row in ready
              if row["outcomes"][str(horizon)]["excess_pct"] is not None]
    return {
        "observations": len(rows), "matured": len(ready),
        "dates": len({row["date"] for row in ready}),
        "mean_net_pct": round(mean(net), 4),
        "median_net_pct": round(median(net), 4),
        "mean_excess_pct": round(mean(excess), 4) if excess else None,
        "loss_count": sum(value < 0 for value in net),
        "loss_10pct_count": sum(value <= -10 for value in net),
        "statuses": dict(sorted(statuses.items())),
    }


def _recorded_sell_before_loss(*, sessions: list[str], kind: str,
                               ticker_bars: dict[str, dict],
                               ticker_signals: dict[str, dict],
                               last_price_day: str) -> dict:
    """Case-level close-path audit, not a holder exit or a trading-rule backtest.

    A SELL recorded on the loss session is too late to count as a *prior* warning.
    Missing signal sessions cannot be silently treated as HOLD.
    """
    if kind not in {"BUY", "STRONG_BUY"}:
        return {"state": "not_buy_signal"}
    if not sessions:
        return {"state": "invalid_signal_session"}
    if sessions[0] > last_price_day:
        return {"state": "not_matured", "entry_date": sessions[0], "observed_sessions": 0}
    entry = ticker_bars.get(sessions[0])
    entry_open = _number(entry.get("open")) if entry else None
    if entry_open is None or entry_open <= 0:
        return {"state": "missing_entry_open", "entry_date": sessions[0]}
    observed = []
    first_loss = None
    for session in sessions:
        if session > last_price_day:
            break
        bar = ticker_bars.get(session)
        close = _number(bar.get("close")) if bar else None
        if close is None or close <= 0:
            return {"state": "price_gap", "entry_date": sessions[0],
                    "first_missing_date": session}
        observed.append(session)
        if close <= entry_open * 0.9:
            first_loss = session
            break
    if first_loss is None:
        return {"state": "no_loss_in_observed_window" if len(observed) == 20 else "not_matured",
                "entry_date": sessions[0], "observed_sessions": len(observed)}
    earlier = [session for session in observed if session < first_loss]
    missing = [session for session in earlier if session not in ticker_signals]
    sell_dates = [session for session in earlier
                  if ticker_signals.get(session, {}).get("kind") in {"SELL", "STRONG_SELL"}]
    later_sell_dates = [session for session in sessions
                        if first_loss <= session <= last_price_day
                        and ticker_signals.get(session, {}).get("kind") in {"SELL", "STRONG_SELL"}]
    entry_block_fields = ("gate_blocked", "event_risk", "decision_blocked")
    entry_blocks = [(session, [field for field in entry_block_fields
                               if _flag(ticker_signals[session], field) is True])
                    for session in earlier if session in ticker_signals]
    entry_blocks = [(session, fields) for session, fields in entry_blocks if fields]
    entry_metadata_incomplete = any(
        _flag(ticker_signals[session], field) is None
        for session in earlier if session in ticker_signals for field in entry_block_fields
    )
    return {"state": "loss_observed", "entry_date": sessions[0],
            "first_loss_date": first_loss, "first_sell_before_loss": sell_dates[0] if sell_dates else None,
            "first_sell_on_or_after_loss": later_sell_dates[0] if later_sell_dates else None,
            "first_new_entry_block_before_loss": entry_blocks[0][0] if entry_blocks else None,
            "new_entry_block_fields": entry_blocks[0][1] if entry_blocks else [],
            "new_entry_block_evidence": ("recorded_before_loss" if entry_blocks else
                                         "unknown_signal_gap" if missing else
                                         "unknown_metadata" if entry_metadata_incomplete else
                                         "none_recorded"),
            "missing_signal_sessions_before_loss": len(missing),
            "first_missing_signal_date": missing[0] if missing else None,
            "prior_sell_evidence": ("recorded_before_loss" if sell_dates else
                                    "unknown_signal_gap" if missing else "none_recorded"),
            "warning_scope": "sell_or_new_entry_block_record_only_not_holder_exit_or_notification"}


def audit_snapshots(signals: pd.DataFrame, prices: pd.DataFrame, *, market: str,
                    major_tickers: tuple[str, ...] = (),
                    score_delta_threshold: float = 0.5,
                    roundtrip_cost_pct: float = 0.25,
                    include_aggregates: bool = True,
                    case_keys: set[tuple[str, str]] | None = None) -> dict:
    """Evaluate every observed ticker-date, or only preselected diagnostic cases.

    Signals are historical observations. The next *scheduled* session's open is entry,
    and its h-th session's close is exit. A missing intermediate bar stays missing.
    A case subset cannot produce an aggregate score or a complete denominator.
    """
    if market not in {"kr", "us"}:
        raise ValueError("market must be kr or us")
    if score_delta_threshold < 0 or roundtrip_cost_pct < 0:
        raise ValueError("negative threshold or cost")
    if case_keys is not None and include_aggregates:
        raise ValueError("case subset cannot produce aggregate results")
    if not {"date", "ticker", "score", "kind"} <= set(signals.columns):
        raise ValueError("signal history lacks required columns")
    if not {"date", "ticker", "open", "close"} <= set(prices.columns):
        raise ValueError("price history lacks required columns")
    snapshots = signals.copy()
    bars = prices.copy()
    for frame in (snapshots, bars):
        frame["date"] = frame["date"].astype(str)
        frame["ticker"] = frame["ticker"].astype(str)
    if snapshots.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate signal ticker-date: select one archived observation first")
    if bars.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate price ticker-date: price version is ambiguous")
    if snapshots.empty:
        result = {"market": market, "source_level": "legacy_snapshot_unverified", "rows": []}
        if case_keys is not None:
            result["signal_rows"] = result["selected_case_rows"] = 0
        if include_aggregates:
            result["summary"] = {"all": {str(h): _score_results([], h) for h in HORIZONS}}
        return result

    selected_tickers = {ticker for _, ticker in case_keys} if case_keys is not None else None
    if selected_tickers is not None:
        bars = bars[bars["ticker"].isin(selected_tickers)]
    bars_by_ticker = {
        ticker: {str(row["date"]): row for row in group.to_dict("records")}
        for ticker, group in bars.groupby("ticker", sort=False)
    }
    signals_by_ticker = {
        ticker: {str(row["date"]): row for row in group.to_dict("records")}
        for ticker, group in snapshots.groupby("ticker", sort=False)
    }
    # Maturity is based on the complete archive's latest bar, not the chosen
    # names; otherwise a missing case ticker could look merely "not matured".
    last_price_day = max(prices["date"].astype(str)) if not prices.empty else ""
    major_set = set(major_tickers)
    expected: dict[str, list[str]] = {}
    rows: list[dict] = []
    prior_by_ticker: dict[str, dict] = {}
    for signal in snapshots.sort_values(["date", "ticker"]).to_dict("records"):
        day, ticker = signal["date"], signal["ticker"]
        if case_keys is not None and not market_clock.is_session(market, day):
            continue
        prior = prior_by_ticker.get(ticker)
        score = _number(signal.get("score"))
        prior_score = _number(prior.get("score")) if prior else None
        delta = (round(score - prior_score, 4)
                 if score is not None and prior_score is not None else None)
        kind_change = prior is not None and signal.get("kind") != prior.get("kind")
        score_change = delta is not None and abs(delta) >= score_delta_threshold
        gap = bool(prior and not market_clock.consecutive_sessions(market, prior["date"], day))
        selection = _selection_evidence(signal)
        previous_selection = _selection_evidence(prior) if prior else None
        selection_changes = ([key for key in ("rank", "rank_eligible", "gate_blocked",
                                             "event_risk", "low_coverage", "decision_blocked")
                              if previous_selection[key] is not None and selection[key] is not None
                              and previous_selection[key] != selection[key]]
                             if previous_selection else [])
        gate_release_reentry = bool(
            prior and not gap and previous_selection["gate_blocked"] is True
            and selection["gate_blocked"] is False
            and prior.get("kind") == "HOLD"
            and signal.get("kind") in {"BUY", "STRONG_BUY"}
            and score is not None and prior_score is not None and score <= prior_score
        )
        prior_by_ticker[ticker] = signal
        if case_keys is not None and (day, ticker) not in case_keys:
            continue
        factor_changes = {}
        if prior:
            for name in FACTOR_COLUMNS:
                current, previous = _number(signal.get(name)), _number(prior.get(name))
                if current is not None and previous is not None:
                    factor_changes[name] = round(current - previous, 4)

        if day not in expected:
            expected[day] = market_clock.next_sessions(market, day, max(HORIZONS))
        sessions = expected[day]
        outcomes = {}
        ticker_bars = bars_by_ticker.get(ticker, {})
        for h in HORIZONS:
            if not sessions:
                outcomes[str(h)] = {"state": "invalid_signal_session"}
                continue
            entry_day, exit_day = sessions[0], sessions[h - 1]
            if last_price_day < exit_day:
                outcomes[str(h)] = {"state": "not_matured", "exit_date": exit_day}
                continue
            entry = ticker_bars.get(entry_day)
            entry_px = _number(entry.get("open")) if entry else None
            if entry_px is None or entry_px <= 0:
                outcomes[str(h)] = {"state": "missing_entry_open", "entry_date": entry_day}
                continue
            closes = []
            for session in sessions[:h]:
                bar = ticker_bars.get(session)
                px = _number(bar.get("close")) if bar else None
                if px is None or px <= 0:
                    break
                closes.append(px)
            if len(closes) != h:
                outcomes[str(h)] = {"state": "price_gap", "entry_date": entry_day,
                                    "exit_date": exit_day, "first_missing_date": sessions[len(closes)]}
                continue
            net = (closes[-1] / entry_px - 1.0) * 100 - roundtrip_cost_pct
            outcomes[str(h)] = {
                "state": "matured", "entry_date": entry_day, "exit_date": exit_day,
                "entry_open": entry_px, "exit_close": closes[-1],
                "gross_pct": round((closes[-1] / entry_px - 1.0) * 100, 4),
                "net_pct": round(net, 4),
                "close_mae_pct": round((min([entry_px, *closes]) / entry_px - 1.0) * 100, 4),
                "close_mfe_pct": round((max([entry_px, *closes]) / entry_px - 1.0) * 100, 4),
                "excess_pct": None,
            }
        rows.append({
            "market": market, "date": day, "ticker": ticker,
            "kind": str(signal.get("kind")), "score": score,
            "previous_date": prior["date"] if prior else None,
            "previous_kind": str(prior.get("kind")) if prior else None,
            "score_delta": delta, "kind_change": kind_change,
            "score_change": score_change, "snapshot_gap": gap,
            "selection": selection, "previous_selection": previous_selection,
            "selection_changes": selection_changes,
            "gate_release_reentry_without_score_gain": gate_release_reentry,
            "major": ticker in major_set, "factor_changes": factor_changes,
            "loss_warning_path": _recorded_sell_before_loss(
                sessions=sessions, kind=str(signal.get("kind")),
                ticker_bars=ticker_bars, ticker_signals=signals_by_ticker.get(ticker, {}),
                last_price_day=last_price_day),
            "outcomes": outcomes,
        })

    result = {
        "market": market, "source_level": "legacy_snapshot_unverified",
        "method": "next_scheduled_open_to_hth_close_price_only_costed",
        "roundtrip_cost_pct": roundtrip_cost_pct,
        "score_delta_threshold": score_delta_threshold,
        "signal_dates": sorted(set(snapshots["date"])),
        "price_data_to": last_price_day,
        "signal_rows": len(snapshots), "snapshot_gap_rows": sum(row["snapshot_gap"] for row in rows),
        "rows": rows,
        "caveats": ["legacy signal snapshots lack verified original-source availability",
                    "raw open/close returns exclude unverified corporate actions and dividends",
                    "same-day tickers and overlapping horizons are not independent samples",
                    "when enabled, comparison is only the observed ticker cohort on each signal day"],
    }
    if case_keys is not None:
        result["selected_case_rows"] = len(rows)
    if include_aggregates:
        # Only unprotected development data may reach this branch in the CLI.
        # The comparison universe is the observed cohort, not a causal control.
        grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
        for row in rows:
            for h in HORIZONS:
                outcome = row["outcomes"][str(h)]
                if outcome["state"] == "matured":
                    grouped[(row["date"], h)].append(outcome["net_pct"])
        comparison = {f"{day}:{h}": {"n": len(values), "mean_net_pct": round(mean(values), 4)}
                      for (day, h), values in sorted(grouped.items())}
        for row in rows:
            for h in HORIZONS:
                outcome = row["outcomes"][str(h)]
                if outcome["state"] == "matured":
                    outcome["excess_pct"] = round(
                        outcome["net_pct"] - comparison[f"{row['date']}:{h}"]["mean_net_pct"], 4)
        cohorts = {
            "all": rows,
            "buy": [row for row in rows if row["kind"] in {"BUY", "STRONG_BUY"}],
            "kind_changed": [row for row in rows if row["kind_change"]],
            "score_changed": [row for row in rows if row["score_change"]],
            "major": [row for row in rows if row["major"]],
        }
        result["cohort_comparison"] = comparison
        result["summary"] = {name: {str(h): _score_results(items, h) for h in HORIZONS}
                             for name, items in cohorts.items()}
    return result
