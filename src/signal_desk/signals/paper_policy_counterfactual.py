"""Research-only sensitivity of paper adds and exit timing on observed prices.

The no-add arm keeps the *fraction* of the initial lot sold at each observed
exit. This is an accounting sensitivity, not an executable order replay: actual
exit sizes and decisions could change without additions. Fixed h5/h20 exits
use raw subsequent daily closes, never a same-day or backfilled fill.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from zoneinfo import ZoneInfo

from signal_desk import market_clock

VERSION = "paper-policy-sensitivity-v1"
ROUND_TRIP_BPS = 25
HORIZONS = (5, 20)
_ZONES = {"kr": "Asia/Seoul", "us": "America/New_York"}


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _summary(episodes: list[dict], key: str) -> dict:
    paired = [row for row in episodes if row.get(key) is not None]
    values = [row[key] for row in paired]
    return {"eligible": len(values), "unavailable": len(episodes) - len(values),
            "mean_net_25bps_pct": round(sum(values) / len(values), 3) if values else None,
            "positive_after_cost": sum(value > 0 for value in values),
            "mean_delta_vs_actual_same_episodes_pp": round(
                sum(row[key] - row["actual_net_25bps_pct"] for row in paired) / len(paired), 3
            ) if paired else None}


def analyze(trades: list[dict], closes: dict[str, tuple[list[str], list[float | None]]],
            *, market: str, now: dt.datetime | None = None) -> dict:
    """Compare closed paper episodes without silently dropping price gaps.

    Mean returns are equal-weighted *episodes*, not a portfolio NAV. All arms
    subtract the same assumed 25bp round trip; observed fills have unverified
    old cash/fee rows and daily closes are not executable fills.
    """
    if market not in _ZONES:
        raise ValueError("unsupported market")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    completed = market_clock.latest_completed_session(market, now)
    zone = ZoneInfo(_ZONES[market])
    open_by: dict[str, dict] = {}
    closed: list[dict] = []
    gaps: dict[str, int] = defaultdict(int)
    blocked: set[str] = set()
    last_id = -1
    for trade in trades:
        ticker = str(trade.get("ticker") or "")
        side = str(trade.get("side") or "").lower()
        qty, price = _positive(trade.get("qty")), _positive(trade.get("price"))
        ts = _positive(trade.get("ts"))
        try:
            trade_id = int(trade.get("id"))
            day = dt.datetime.fromtimestamp(ts, zone).date().isoformat() if ts else None
        except (TypeError, ValueError, OverflowError, OSError):
            trade_id, day = -1, None
        if trade_id <= last_id:
            raise ValueError("journal IDs must increase")
        last_id = trade_id
        if (not ticker or side not in {"buy", "sell"} or qty is None
                or not qty.is_integer() or price is None or day is None
                or not market_clock.is_session(market, day)):
            gaps["invalid_trade"] += 1
            if ticker:
                blocked.add(ticker)
                open_by.pop(ticker, None)
            continue
        if ticker in blocked:
            gaps["blocked_trade"] += 1
            continue
        if side == "buy":
            episode = open_by.get(ticker)
            if episode is None:
                open_by[ticker] = {"ticker": ticker, "entry_id": trade_id, "entry_day": day,
                                   "entry_price": price, "initial_qty": qty,
                                   "actual_qty": qty, "actual_cost": qty * price,
                                   "actual_proceeds": 0.0, "no_add_qty": qty,
                                   "no_add_proceeds": 0.0, "add_count": 0}
            else:
                episode["actual_qty"] += qty
                episode["actual_cost"] += qty * price
                episode["add_count"] += 1
            continue
        episode = open_by.get(ticker)
        if episode is None or qty > episode["actual_qty"]:
            gaps["unmatched_sell"] += 1
            blocked.add(ticker)
            open_by.pop(ticker, None)
            continue
        fraction = qty / episode["actual_qty"]
        no_add_sold = episode["no_add_qty"] * fraction
        episode["no_add_proceeds"] += no_add_sold * price
        episode["no_add_qty"] -= no_add_sold
        episode["actual_proceeds"] += qty * price
        episode["actual_qty"] -= qty
        if episode["actual_qty"]:
            continue
        actual_gross = (episode["actual_proceeds"] / episode["actual_cost"] - 1) * 100
        no_add_gross = (episode["no_add_proceeds"] /
                        (episode["initial_qty"] * episode["entry_price"]) - 1) * 100
        row = {"ticker": ticker, "entry_id": episode["entry_id"],
               "exit_id": trade_id, "entry_day": episode["entry_day"], "exit_day": day,
               "add_count": episode["add_count"], "initial_notional": round(
                   episode["initial_qty"] * episode["entry_price"], 2),
               "added_notional": round(episode["actual_cost"] -
                                       episode["initial_qty"] * episode["entry_price"], 2),
               "actual_net_25bps_pct": round(actual_gross - ROUND_TRIP_BPS / 100, 3),
               "no_add_net_25bps_pct": round(no_add_gross - ROUND_TRIP_BPS / 100, 3),
               "no_add_minus_actual_pp": round(no_add_gross - actual_gross, 3),
               "h5_net_25bps_pct": None, "h20_net_25bps_pct": None,
               "h5_status": "unavailable", "h20_status": "unavailable"}
        series = closes.get(ticker)
        if not series or len(series) != 2 or len(series[0]) != len(series[1]):
            gaps["missing_or_misaligned_closes"] += 1
        elif series[0] != sorted(set(series[0])):
            gaps["invalid_close_dates"] += 1
        else:
            panel = dict(zip(series[0], series[1]))
            for horizon in HORIZONS:
                days = market_clock.next_sessions(market, episode["entry_day"], horizon)
                status = "ready"
                if len(days) != horizon or completed is None or days[-1] > completed:
                    status = "pending"
                elif _positive(panel.get(episode["entry_day"])) is None:
                    status = "missing_entry_close"
                elif any(_positive(panel.get(d)) is None for d in days):
                    status = "missing_close"
                if status == "ready":
                    exit_price = _positive(panel[days[-1]])
                    row[f"h{horizon}_net_25bps_pct"] = round(
                        (exit_price / episode["entry_price"] - 1) * 100
                        - ROUND_TRIP_BPS / 100, 3)
                else:
                    gaps[status + f"_h{horizon}"] += 1
                row[f"h{horizon}_status"] = status
        closed.append(row)
        del open_by[ticker]

    return {"version": VERSION, "mode": "research_sensitivity", "live_eligible": False,
            "source": "paper_fills_and_raw_daily_closes", "registered_verdict": False,
            "total_return_verified": False, "round_trip_cost_assumption_bps": ROUND_TRIP_BPS,
            "completed_session": completed, "journal_rows": len(trades),
            "closed_episodes": len(closed), "open_episodes": len(open_by),
            "blocked_tickers": sorted(blocked), "coverage_gaps": dict(gaps),
            "summary": {key: _summary(closed, key) for key in (
                "actual_net_25bps_pct", "no_add_net_25bps_pct",
                "h5_net_25bps_pct", "h20_net_25bps_pct")},
            "episodes": closed,
            "note": ("같은 첫 매수의 가격 민감도입니다. 추가매수 없음은 실제 매도 때마다 같은 비율을 "
                     "줄인 가상 경로이며, 5·20거래일은 당시 체결가가 아닌 뒤늦게 확인한 종가입니다. "
                     "과거 비용·기업행사·배당·미체결·현금 재투자와 의사결정 변화는 검증되지 않았습니다. "
                     "평균은 회차별 동일 가중이고 계좌 수익률이나 개선 효과가 아닙니다.")}
