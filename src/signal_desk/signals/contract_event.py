"""One frozen filing-reaction hypothesis; historical research only.

Bar labels are treated as unavailable for another minute. Selection slices all
prices at its decision clock. Execution alone can read future OHLCV.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Seoul")
POLICIES = ("confirmed", "immediate", "market_control", "cash")


@dataclass(frozen=True)
class Minute:
    label: int
    open: float
    high: float
    low: float
    close: float
    volume: int


def decode_minutes(body: dict, session: str) -> dict[int, Minute]:
    if not isinstance(body, dict) or body.get("rt_cd") != "0":
        raise ValueError("provider_failed")
    rows = body.get("output2")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 120 or session >= "2026-08-04":
        raise ValueError("invalid_minute_page")
    result = {}
    for row in rows:
        day = str(row.get("stck_bsop_date") or "")
        if day < session.replace("-", "") and len(day) == 8 and day.isdigit():
            continue
        if day != session.replace("-", ""):
            raise ValueError("minute_date_not_attested")
        clock = str(row.get("stck_cntg_hour") or "")
        if len(clock) != 6 or not clock.isdigit():
            raise ValueError("invalid_minute_clock")
        stamp = dt.datetime.strptime(day + clock, "%Y%m%d%H%M%S").replace(tzinfo=ZONE)
        if stamp.second or not dt.time(9) <= stamp.time() <= dt.time(15, 30):
            raise ValueError("invalid_minute_clock")
        values = [float(row[key]) for key in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr")]
        volume = int(row["cntg_vol"])
        if (any(not math.isfinite(value) or value <= 0 for value in values)
                or volume < 0 or not values[2] <= min(values[0], values[3])
                <= max(values[0], values[3]) <= values[1]):
            raise ValueError("invalid_minute_ohlcv")
        label = int(stamp.timestamp())
        if label in result:
            raise ValueError("duplicate_minute")
        result[label] = Minute(label, *values, volume)
    if not result:
        raise ValueError("empty_requested_session")
    return result


def decide(event: dict, bars: dict[int, Minute], *, immediate: bool = False) -> dict:
    published = dt.datetime.fromisoformat(event["published_at"])
    if (published.utcoffset() is None or published.second or published.microsecond
            or published.astimezone(ZONE).date().isoformat() != event["session"]
            or event["session"] >= "2026-08-04"):
        raise ValueError("invalid_event_clock")
    p = int(published.timestamp())
    clock = p + (120 if immediate else 420)
    known = {label: bar for label, bar in bars.items() if label + 60 <= clock}
    last = clock - 60
    recent = [known.get(label) for label in range(last - 240, last + 1, 60)]
    if any(bar is None for bar in recent) or sum(bar.volume for bar in recent) <= 0:
        return {"state": "abstain", "reason": "known_five_minute_window_missing", "decision_at": clock}
    reason = "filing_immediate"
    reaction = {}
    if not immediate:
        before = [known.get(label) for label in range(p - 300, p, 60)]
        if any(bar is None for bar in before) or sum(bar.volume for bar in before) <= 0:
            return {"state": "abstain", "reason": "pre_event_window_missing", "decision_at": clock}
        move = (recent[-1].close / before[-1].close - 1) * 100
        ratio = sum(bar.volume for bar in recent) / sum(bar.volume for bar in before)
        reaction = {"reaction_move_pct": move, "local_volume_ratio": ratio}
        if not 0.5 - 1e-10 <= move <= 3.0 + 1e-10 or ratio < 1.5:
            return {"state": "abstain", "reason": "fixed_reaction_conditions_not_met",
                    "decision_at": clock, **reaction}
        reason = "verified_contract_and_confirmed_reaction"
    return {"state": "planned", "reason": reason, "decision_at": clock,
            "entry_label": clock + 120, "reference_price": recent[-1].close,
            "known_five_minute_volume": sum(bar.volume for bar in recent), **reaction}


def run_path(records: list[dict], policy: str, *, cost_bps: int = 75,
             fill_fraction: float = 1.0, additional_delay_minutes: int = 0) -> dict:
    if (policy not in POLICIES or cost_bps not in (45, 75, 120)
            or fill_fraction not in (0, 0.5, 1) or additional_delay_minutes not in (0, 2)):
        raise ValueError("outside_fixed_event_scenarios")
    days = [record["event"]["session"] for record in records]
    if not records or days != sorted(set(days)) or any(day >= "2026-08-04" for day in days):
        raise ValueError("one_preprotected_event_per_day_required")
    cash = peak = 10_000_000.0
    max_drawdown = 0.0
    cost = cost_bps / 20_000
    trades, outcomes, skips = [], [], Counter()
    for record in records:
        event = record["event"]
        if policy == "cash":
            outcomes.append({"receipt": event["receipt"], "state": "cash", "net_pnl": 0.0})
            continue
        if record.get("source_error"):
            return {"state": "incomplete_source", "receipt": event["receipt"],
                    "net_return_pct": None, "source_error": record["source_error"]}
        proposal = decide(event, record["stock"], immediate=policy == "immediate")
        bars = record["stock"]
        if policy == "market_control" and proposal["state"] == "planned":
            bars = record["control"]
            known_last = proposal["decision_at"] - 60
            reference = [bars.get(label) for label in range(known_last - 240, known_last + 1, 60)]
            if any(bar is None for bar in reference):
                return {"state": "incomplete_market_control", "receipt": event["receipt"], "net_return_pct": None}
            proposal = {**proposal, "reference_price": reference[-1].close,
                        "known_five_minute_volume": sum(bar.volume for bar in reference)}
        row = {"receipt": event["receipt"], "ticker": event["ticker"], "session": event["session"],
               "proposal": proposal, "net_pnl": 0.0}
        if proposal["state"] != "planned":
            outcomes.append({**row, "state": "abstain"})
            skips[proposal["reason"]] += 1
            continue
        budget = cash / 6
        requested = min(int(budget / (proposal["reference_price"] * 1.30 * (1 + cost))),
                        int(proposal["known_five_minute_volume"] * 0.01))
        qty = int(requested * fill_fraction)
        entry_at = proposal["entry_label"] + additional_delay_minutes * 60
        exit_at = entry_at + 3600
        entry, exit_bar = bars.get(entry_at), bars.get(exit_at)
        if qty == 0:
            outcomes.append({**row, "state": "unfilled", "reason": "quantity_or_fill_assumption_zero"})
            skips["quantity_or_fill_assumption_zero"] += 1
            continue
        if entry is None:
            return {"state": "incomplete_entry", "receipt": event["receipt"], "net_return_pct": None}
        debit = qty * entry.open * (1 + cost)
        if entry.volume == 0 or debit > budget + 1e-8:
            reason = "no_entry_volume" if entry.volume == 0 else "budget_exceeded"
            outcomes.append({**row, "state": "unfilled", "reason": reason})
            skips[reason] += 1
            continue
        if exit_bar is None or exit_bar.volume == 0:
            return {"state": "incomplete_exit", "receipt": event["receipt"], "net_return_pct": None}
        credit = qty * exit_bar.close * (1 - cost)
        pnl = credit - debit
        previous_cash, cash = cash, cash + pnl
        peak = max(peak, cash)
        max_drawdown = min(max_drawdown, cash / peak - 1)
        trade = {**row, "state": "filled", "requested_shares": requested, "shares": qty,
                 "entry_label": entry_at, "exit_label": exit_at,
                 "entry_open": entry.open, "exit_close": exit_bar.close,
                 "net_pnl": pnl, "gross_pnl": qty * (exit_bar.close - entry.open),
                 "trade_net_pct": (credit / debit - 1) * 100,
                 "entry_exposure": qty * entry.open / previous_cash, "cash_after": cash}
        trades.append(trade)
        outcomes.append(trade)
    return {"state": "complete", "events": len(records), "trade_count": len(trades),
            "net_return_pct": (cash / 10_000_000 - 1) * 100,
            "end_cash": cash, "max_close_drawdown_pct": max_drawdown * 100,
            "gross_pnl": sum(row["gross_pnl"] for row in trades),
            "net_pnl": sum(row["net_pnl"] for row in trades),
            "distinct_traded_tickers": len({row["ticker"] for row in trades}),
            "positive_trades": sum(row["net_pnl"] > 0 for row in trades),
            "skips": dict(skips), "outcomes": outcomes, "trades": trades}


def adoption(months: dict) -> dict:
    if set(months) != {"2026-06", "2026-07"}:
        return {"adoption": "rejected", "evidence_status": "insufficient_months", "live_order_eligible": False}
    compared = [month["75bp_full_delay0"][policy] for month in months.values()
                for policy in ("confirmed", "immediate", "market_control")]
    stresses = [month[scenario]["confirmed"] for month in months.values()
                for scenario in ("120bp_full_delay0", "75bp_full_delay2")]
    if any(row["state"] != "complete" for row in compared + stresses):
        return {"adoption": "rejected", "evidence_status": "incomplete_paths", "live_order_eligible": False}
    primary = [month["75bp_full_delay0"]["confirmed"] for month in months.values()]
    checks = {"at_least_12_source_days": sum(row["events"] for row in primary) >= 12,
              "at_least_8_trades_and_3_per_month": sum(row["trade_count"] for row in primary) >= 8
              and min(row["trade_count"] for row in primary) >= 3,
              "both_months_net_positive_75bp": all(row["net_return_pct"] > 0 for row in primary)}
    for scenario in ("120bp_full_delay0", "75bp_full_delay2"):
        checks[scenario + "_mean_positive"] = statistics.mean(
            month[scenario]["confirmed"]["net_return_pct"] for month in months.values()) > 0
    impact = defaultdict(float)
    for control in ("immediate", "market_control"):
        checks["mean_excess_over_" + control] = statistics.mean(
            month["75bp_full_delay0"]["confirmed"]["net_return_pct"]
            - month["75bp_full_delay0"][control]["net_return_pct"] for month in months.values()) > 0
        if control == "immediate":
            for month in months.values():
                original = {row["receipt"]: row for row in month["75bp_full_delay0"][control]["outcomes"]}
                for row in month["75bp_full_delay0"]["confirmed"]["outcomes"]:
                    impact[row["ticker"]] += row["net_pnl"] - original[row["receipt"]]["net_pnl"]
    denominator = sum(abs(value) for value in impact.values())
    concentration = max((abs(value) / denominator for value in impact.values()), default=0) if denominator else None
    checks["improvement_not_concentrated_in_one_ticker"] = concentration is not None and concentration <= 0.5
    return {"adoption": "shadow_candidate" if all(checks.values()) else "rejected",
            "evidence_status": "fixed_screen_passed" if all(checks.values()) else "fixed_screen_failed",
            "checks": checks, "largest_absolute_improvement_ticker_share": concentration,
            "live_order_eligible": False}
