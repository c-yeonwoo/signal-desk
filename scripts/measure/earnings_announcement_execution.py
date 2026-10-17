"""Frozen 2022/2023 news-to-SIP execution reconstruction. Offline, never orders.

Browser gateway observations are inputs, not fabricated historical arrival logs.
Run --help for the staged, immutable data acquisition/replay contract.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.measure.contract_event_sources import _json, load_archive
from scripts.measure.us_earnings_backtest import PANEL
from signal_desk import market_clock
from signal_desk.ingest.alpaca_research import clock_ns, parameters

NY = ZoneInfo("America/New_York")
SECOND = 1_000_000_000
PLAN = Path(__file__).resolve().parents[2] / "docs/EARNINGS-ANNOUNCEMENT-EXECUTION-2026-10-17.md"
NUMBER = r"(-?\d+(?:\.\d+)?)"
EPS = re.compile(r"\bEPS(?: Excluding Significant Items)?\s*\$?" + NUMBER +
    r"\s+(?:Beats|Misses|vs\.?|In-Line With|In Line With|Inline With)\s+\$?" + NUMBER + r"\s+Est(?:imat)?", re.I)
ISSUER_TITLES = {"AAPL": r"Apple", "AMZN": r"Amazon(?:\.com)?", "GOOGL": r"Alphabet",
    "JPM": r"(?:JP Morgan|JPMorgan Chase)", "META": r"(?:Meta|Facebook)",
    "MSFT": r"Microsoft", "NVDA": r"NVIDIA", "XOM": r"(?:ExxonMobil|Exxon Mobil)"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(_json(value))


def cohort(source: Path) -> dict:
    _, files = load_archive(source, "us-eps-sources-v1")
    events = []
    for symbol in PANEL:
        body = json.loads(files[f"raw/earnings-{symbol}.json"])
        if body["symbol"] != symbol:
            raise ValueError("EPS issuer mismatch")
        # Read only report dates/periods, not historical-vendor EPS estimates.
        for row in body["quarterlyEarnings"]:
            day = row.get("reportedDate", "")
            if "2022-01-01" <= day <= "2023-12-31":
                period = row["fiscalDateEnding"]
                if dt.date.fromisoformat(period) >= dt.date.fromisoformat(day):
                    raise ValueError("invalid fiscal period")
                events.append({"id": symbol + ":" + day, "symbol": symbol,
                               "reported_date": day, "fiscal_end": period})
    if (len({e["id"] for e in events}) != 64 or
            Counter((e["symbol"], e["reported_date"][:4]) for e in events) !=
            Counter({(s, y): 4 for s in PANEL for y in ("2022", "2023")})):
        raise ValueError("fixed 64-report cohort incomplete")
    return {"schema": "earnings-announcement-cohort-v1", "source_sha256": digest(source),
            "plan_sha256": digest(PLAN), "events": sorted(events, key=lambda e: e["id"]),
            "live_order_eligible": False}


def observations(paths: list[Path], kind: str) -> list[dict]:
    out = []
    for path in sorted(paths):
        artifact = json.loads(path.read_bytes())
        for row in artifact["rows"]:
            source = row["body"]
            if source.get("schema") != f"alpaca-historical-{kind}-v1":
                raise ValueError("wrong source schema")
            source = {**source, "artifact": str(path), "artifact_sha256": digest(path)}
            if row["gateway_http_status"] != 200 or source["status"] not in ("observed", "empty"):
                out.append({**source, "body": None})
                continue
            raw = base64.b64decode(source["raw_b64"], validate=True)
            if len(raw) != source["raw_bytes"] or hashlib.sha256(raw).hexdigest() != source["raw_sha256"]:
                raise ValueError("source bytes/hash mismatch")
            out.append({**source, "body": json.loads(raw)})
    return out


def input_events(frozen: dict, sources: list[dict]) -> dict:
    events = []
    for event in frozen["events"]:
        symbol, day = event["symbol"], event["reported_date"]
        aliases = {symbol, "FB"} if symbol == "META" and day < "2022-06-09" else {symbol}
        pages = [s for s in sources if s["symbol"] in aliases and s["request"]["start"][:10] == day]
        row = {**event, "status": "news_missing", "eligible": None}
        if not pages or any(p["body"] is None for p in pages):
            events.append(row)
            continue
        tokens = {p["request"].get("page_token", "") for p in pages}
        if "" not in tokens or any(p["body"].get("next_page_token") not in tokens | {None, ""} for p in pages):
            events.append({**row, "status": "news_pagination_incomplete"})
            continue
        candidates, reports = {}, {}
        for page in pages:
            for article in page["body"]["news"]:
                headline = article["headline"]
                match = EPS.search(headline)
                marker = re.match(ISSUER_TITLES[symbol] + r"\s+Q[1-4]\s+(?:EPS\b|Adj\b|Earnings Highlights\b)", headline, re.I)
                if ((not match and not marker) or re.search(r"\b(?:Sees|Expects|Preview|Why|Recap|Yesterday|Last Week)\b", headline, re.I)
                        or not aliases.intersection(article["symbols"])):
                    continue
                # A direct single-issuer EPS headline, not a market roundup.
                if len(set(article["symbols"]) & set(PANEL)) > 1:
                    continue
                first, changed = clock_ns(article["created_at"]), clock_ns(article["updated_at"])
                ready = max(first, changed)
                if (changed < first or as_dt(ready).astimezone(NY).date().isoformat() != day
                        or as_dt(first).astimezone(NY).date().isoformat() != day):
                    continue
                observation = {"news_id": article["id"], "url": article["url"],
                    "source_sha256": page["raw_sha256"], "created_at": article["created_at"],
                    "updated_at": article["updated_at"], "ready_ns": ready + 30 * SECOND}
                reports[article["id"]] = observation
                if not match:
                    continue
                actual, expected = map(float, match.groups())
                candidates[article["id"]] = {**observation,
                    "actual_eps": actual, "expected_eps": expected,
                    "eps_basis": "adjusted" if re.search(r"Adj\.?|Adjusted|Excluding Significant Items", headline, re.I) else "unspecified"}
        # Later corrections cannot retrospectively cancel an earlier decision.
        earliest = min((r["ready_ns"] for r in candidates.values()), default=None)
        available = [r for r in candidates.values() if r["ready_ns"] == earliest]
        pairs = {(r["actual_eps"], r["expected_eps"]) for r in available}
        report_ready = min((r["ready_ns"] for r in reports.values()), default=None)
        if not pairs and reports:
            selected = min(reports.values(), key=lambda r: (r["ready_ns"], r["news_id"]))
            events.append({**row, **selected, "status": "ready", "eligible": False,
                           "report_ready_ns": report_ready,
                           "eligibility_reason": "comparison_unavailable_abstain"})
            continue
        if len(pairs) != 1:
            events.append({**row, "status": "eps_headline_conflict" if pairs else "eps_headline_missing"})
            continue
        selected = min(available, key=lambda r: (r["ready_ns"], r["news_id"]))
        actual, expected = selected["actual_eps"], selected["expected_eps"]
        surprise = (actual / expected - 1) * 100 if expected > 0 else None
        events.append({**row, **selected, "status": "ready", "surprise_pct": surprise,
                       "report_ready_ns": report_ready,
                       "eligibility_reason": "direct_headline_comparison",
                       "eligible": bool(actual > 0 and expected > 0 and surprise >= 5 - 1e-10)})
    return {"schema": "earnings-announcement-inputs-v1", "cohort": frozen, "events": events,
            "strict_pit_eligible": False, "live_order_eligible": False}


def as_dt(ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ns // SECOND, dt.timezone.utc)


def decision(event: dict, path: str, delay: int) -> int | None:
    ready = event["ready_ns"] + delay * SECOND
    current = as_dt(ready).astimezone(NY)
    if path == "immediate":
        if not market_clock.is_session("us", current.date().isoformat()) or not 4 <= current.hour < 19:
            return None
        return ready
    for offset in range(15):
        day = current + dt.timedelta(days=offset)
        window = market_clock.regular_window("us", day)
        if window:
            first, close = window
            stamp = clock_ns(first.isoformat()) + (60 + delay) * SECOND
            if stamp >= ready and stamp + 3605 * SECOND < clock_ns(close.isoformat()):
                return stamp
    raise ValueError("regular session unavailable")


def choices(inputs: dict, path: str, policy: str, delay: int = 0) -> list[dict]:
    candidates = []
    for event in inputs["events"]:
        if event["status"] != "ready" or (policy != "all_reports" and not event["eligible"]):
            continue
        timing = {**event, "ready_ns": event.get("report_ready_ns", event["ready_ns"])} if policy == "all_reports" else event
        stamp = decision(timing, path, delay)
        if stamp is not None:
            candidates.append({**event, "decision_ns": stamp})
    selected = {}
    for row in sorted(candidates, key=lambda r: (r["decision_ns"], r["symbol"])):
        day = as_dt(row["decision_ns"]).astimezone(NY).date().isoformat()
        selected.setdefault(day, row)
    return list(selected.values())


def query(symbol: str, ns: int, seconds: int = 5) -> dict:
    if symbol == "META" and as_dt(ns).astimezone(NY).date().isoformat() < "2022-06-09":
        symbol = "FB"  # Historical trading symbol, not a backfilled rename.
    first = as_dt(ns).isoformat()
    end = (as_dt(ns) + dt.timedelta(seconds=seconds)).isoformat()
    params = parameters(symbol, first, end)
    return {"symbol": symbol, "start": params["start"], "end": params["end"]}


def key(request: dict) -> tuple:
    return request["symbol"], request["start"], request["end"]


def quote_index(sources: list[dict]) -> dict:
    out = {}
    for source in sources:
        request = {"symbol": source["symbol"], **source["request"]}
        expected = parameters(request["symbol"], request["start"], request["end"])
        if any(request.get(k) != v for k, v in expected.items()):
            raise ValueError("quote request differs from fixed SIP contract")
        k = key(request)
        if k in out and source["body"] is None:
            continue  # Keep failed attempts in the artifact, not over a successful observation.
        if k in out and out[k]["body"] is None:
            out[k] = source
            continue
        if k in out and out[k].get("raw_sha256") != source.get("raw_sha256"):
            raise ValueError("conflicting quote source versions")
        out[k] = source
    return out


def _window_quote(index: dict, request: dict, lower_ns: int, side: str, quantity: int) -> dict | None:
    source = index.get(key(request))
    if not source or source["body"] is None:
        return None
    rows = source["body"]["quotes"] or []
    first, last = (clock_ns(request[x]) for x in ("start", "end"))
    clocks = [clock_ns(r["t"]) for r in rows]
    if source["body"].get("symbol") != request["symbol"] or clocks != sorted(clocks):
        raise ValueError("quote identity/order mismatch")
    if any(not first <= stamp <= last for stamp in clocks):
        raise ValueError("quote beyond requested bound")
    for r, stamp in zip(rows, clocks):
        if not first <= stamp <= last:
            raise ValueError("quote beyond requested bound")
        if stamp < lower_ns:
            continue
        try:
            bid, ask, bs, az = (float(r[k]) for k in ("bp", "ap", "bs", "as"))
            if (not all(math.isfinite(v) and v > 0 for v in (bid, ask, bs, az))
                    or bid >= ask or not bs.is_integer() or not az.is_integer()
                    or r.get("c") != ["R"] or not r.get("bx") or not r.get("ax")):
                continue
            if (az if side == "ap" else bs) >= quantity:
                return {**r, "ns": stamp, "source_sha256": source["raw_sha256"]}
        except (ValueError, TypeError, KeyError):
            continue
    return None  # Unknown if page is partial; never treat as verified no-fill.


def _observed_window(index: dict, request: dict, lower_ns: int, side: str, quantity: int):
    source = index.get(key(request))
    if source is None or source["body"] is None:
        start, end = (clock_ns(request[k]) for k in ("start", "end"))
        covering = [(k, s) for k, s in index.items() if k[0] == request["symbol"] and s["body"] is not None
                    and clock_ns(k[1]) <= start and clock_ns(k[2]) >= end]
        if covering:
            k, wide = max(covering, key=lambda item: item[0][1])
            wide_request = {"symbol": k[0], "start": k[1], "end": k[2]}
            found = _window_quote(index, wide_request, max(lower_ns, start), side, quantity)
            if found:
                return found, None, True
            clocks = [clock_ns(r["t"]) for r in wide["body"]["quotes"] or []]
            if not wide.get("has_next_page") or (clocks and max(clocks) >= end):
                return None, None, True
            # A partial wide page cannot certify its unseen suffix.
            if end - start > SECOND:
                for ns in range(start, end, SECOND):
                    result = _observed_window(index, query(request["symbol"], ns, 1), lower_ns, side, quantity)
                    if result[0] is not None or result[1] is not None or not result[2]:
                        return result
                return None, None, True
            return None, request, False
    if source is None or source["body"] is None:
        return None, request, False
    found = _window_quote(index, request, lower_ns, side, quantity)
    if found:
        return found, None, True
    if source.get("has_next_page"):
        first, last = (clock_ns(request[k]) for k in ("start", "end"))
        if last - first <= SECOND:
            return None, None, False  # Unseen subsecond page is still a real gap.
        for ns in range(first, last, SECOND):
            result = _observed_window(index, query(request["symbol"], ns, 1), lower_ns, side, quantity)
            if result[0] is not None or result[1] is not None or not result[2]:
                return result
    return None, None, True


def quote_or_need(index: dict, request: dict, lower_ns: int, side: str = "ap", quantity: int = 1):
    """Only cross complete windows, narrowing partial pages instead of skipping them."""
    for _ in range(180):
        found, need, complete = _observed_window(index, request, lower_ns, side, quantity)
        if found is not None or need is not None or not complete:
            return found, need
        request = query(request["symbol"], clock_ns(request["end"]))
    return None, None  # 15min acquisition cap is missing data, NEVER a no-fill/0%.


def first_quote(index: dict, request: dict, lower_ns: int, side: str = "ap", quantity: int = 1) -> dict | None:
    return quote_or_need(index, request, lower_ns, side, quantity)[0]


def stages(inputs: dict, index: dict, stage: str) -> list[dict]:
    requests = {}
    for path in ("immediate", "opening"):
        for policy, delay in (("eps", 0), ("all_reports", 0), ("eps", 60), ("market", 0)):
            for event in choices(inputs, path, policy, delay):
                symbol = "SPY" if policy == "market" else event["symbol"]
                reference = query(symbol, event["decision_ns"])
                request = reference
                if stage != "reference":
                    ref = first_quote(index, reference, event["decision_ns"])
                    if ref is None:
                        continue
                    # Whole-second boundary at least one second AFTER observed reference.
                    executable_ns = ((ref["ns"] + SECOND + SECOND - 1) // SECOND) * SECOND
                    request = query(symbol, executable_ns)
                    if stage == "exit":
                        entry = first_quote(index, request, executable_ns)
                        if entry is None:
                            continue
                        request = query(symbol, entry["ns"] + 3600 * SECOND)
                requests[key(request)] = request
    return [r for k, r in sorted(requests.items()) if k not in index]


def gaps(inputs: dict, index: dict) -> list[dict]:
    requests = {}
    for path in ("immediate", "opening"):
        for policy, delay in (("eps", 0), ("all_reports", 0), ("eps", 60), ("market", 0)):
            for event in choices(inputs, path, policy, delay):
                symbol = "SPY" if policy == "market" else event["symbol"]
                ref, need = quote_or_need(index, query(symbol, event["decision_ns"]), event["decision_ns"])
                if need:
                    requests[key(need)] = need
                if not ref:
                    continue
                executable_ns = ((ref["ns"] + 2 * SECOND - 1) // SECOND) * SECOND
                entry, need = quote_or_need(index, query(symbol, executable_ns), executable_ns)
                if need:
                    requests[key(need)] = need
                if not entry:
                    continue
                exit_ns = entry["ns"] + 3600 * SECOND
                _, need = quote_or_need(index, query(symbol, exit_ns), exit_ns, "bp")
                if need:
                    requests[key(need)] = need
        for year in ("2022", "2023"):
            for policy, fee, fill, delay in (("eps",10,1,0),("all_reports",10,1,0),("market",10,1,0),
                ("eps",25,1,0),("eps",50,1,0),("eps",10,.5,0),("eps",10,1,60)):
                result = simulate(inputs, index, year, path, policy, fee, fill, delay)
                for row in result["rows"]:
                    if row["status"] == "exit_missing_position_unresolved":
                        symbol = "SPY" if policy == "market" else row["issuer"]
                        ns = row["entry_ns"] + 3600 * SECOND
                        _, need = quote_or_need(index, query(symbol, ns), ns, "bp", row["quantity"])
                        if need:
                            requests[key(need)] = need
    return [query(r["symbol"], clock_ns(r["start"]), 300)
            if clock_ns(r["end"]) - clock_ns(r["start"]) == 5 * SECOND else r
            for _, r in sorted(requests.items())]


def simulate(inputs: dict, index: dict, year: str, path: str, policy: str,
             fee_bp: int = 10, fill: float = 1, delay: int = 0, size_scale: int = 1) -> dict:
    if (year not in ("2022", "2023") or path not in ("immediate", "opening")
            or policy not in ("eps", "all_reports", "market", "cash")
            or fee_bp not in (10, 25, 50) or fill not in (0, .5, 1) or delay not in (0, 60) or size_scale not in (1, 100)):
        raise ValueError("outside frozen experiment")
    selected = [e for e in choices(inputs, path, policy, delay) if e["reported_date"].startswith(year)]
    cash, peak, drawdown, rate = 10_000., 10_000., 0., fee_bp / 10_000
    rows, stopped = [], False
    for event in selected:
        row = {"id": event["id"], "issuer": event["symbol"], "decision_ns": event["decision_ns"],
               "quarter": (int(event["reported_date"][5:7]) - 1) // 3 + 1}
        if stopped:
            rows.append({**row, "status": "blocked_by_unresolved_prior_execution", "net_pnl": None})
            continue
        if policy == "cash" or fill == 0:
            rows.append({**row, "status": "no_order", "net_pnl": 0.})
            continue
        symbol = "SPY" if policy == "market" else event["symbol"]
        reference_request = query(symbol, event["decision_ns"])
        ref = first_quote(index, reference_request, event["decision_ns"])
        if ref is None:
            rows.append({**row, "status": "reference_missing", "net_pnl": None})
            stopped = True
            continue
        limit = float(ref["ap"]) * 1.003
        quantity = math.floor(min(cash / 6 / (limit * (1 + rate)), float(ref["as"]) * size_scale) * fill)
        if not quantity:
            rows.append({**row, "status": "no_affordable_whole_share", "net_pnl": 0.})
            continue
        executable_ns = ((ref["ns"] + 2 * SECOND - 1) // SECOND) * SECOND
        entry = first_quote(index, query(symbol, executable_ns), executable_ns)
        if entry is None:
            rows.append({**row, "status": "entry_missing", "net_pnl": None})
            stopped = True
            continue
        if float(entry["ap"]) > limit:
            rows.append({**row, "status": "limit_not_marketable", "net_pnl": 0.})
            continue
        quantity = min(quantity, int(entry["as"]) * size_scale)
        cost = quantity * float(entry["ap"]) * (1 + rate)
        if cost > cash + 1e-8:
            raise ValueError("cash constraint broken")
        exit_ns = entry["ns"] + 3600 * SECOND
        exit_quote = first_quote(index, query(symbol, exit_ns), exit_ns, "bp", math.ceil(quantity / size_scale))
        if exit_quote is None:
            rows.append({**row, "status": "exit_missing_position_unresolved", "net_pnl": None,
                         "quantity": quantity, "entry_ns": entry["ns"]})
            stopped = True
            continue
        proceeds = quantity * float(exit_quote["bp"]) * (1 - rate)
        gross = quantity * (float(exit_quote["bp"]) - float(entry["ap"]))
        mid_pnl = quantity * ((float(exit_quote["bp"]) + float(exit_quote["ap"])) -
                              (float(entry["bp"]) + float(entry["ap"]))) / 2
        spread_drag = quantity * ((float(entry["ap"]) - float(entry["bp"])) +
                                  (float(exit_quote["ap"]) - float(exit_quote["bp"]))) / 2
        fees = quantity * (float(entry["ap"]) + float(exit_quote["bp"])) * rate
        pnl = proceeds - cost
        if abs(pnl - (gross - fees)) > 1e-8 or abs(gross - (mid_pnl - spread_drag)) > 1e-8:
            raise ValueError("cash/PnL reconciliation failed")
        cash += pnl
        peak = max(peak, cash)
        drawdown = min(drawdown, cash / peak - 1)
        rows.append({**row, "symbol": symbol, "status": "closed", "quantity": quantity,
                     "entry_ns": entry["ns"], "exit_ns": exit_quote["ns"],
                     "reference_lag_seconds": (ref["ns"] - event["decision_ns"]) / SECOND,
                     "entry_after_reference_seconds": (entry["ns"] - ref["ns"]) / SECOND,
                     "exit_after_target_seconds": (exit_quote["ns"] - exit_ns) / SECOND,
                     "entry_ask": entry["ap"], "exit_bid": exit_quote["bp"],
                     "reference_sha256": ref["source_sha256"], "entry_sha256": entry["source_sha256"],
                     "exit_sha256": exit_quote["source_sha256"], "gross_pnl": gross, "fees": fees,
                     "mid_price_pnl": mid_pnl, "spread_drag": spread_drag,
                     "net_pnl": pnl, "trade_net_return_pct": pnl / (quantity * float(entry["ap"])) * 100})
    missing_events = [e["id"] for e in inputs["events"] if e["reported_date"].startswith(year) and e["status"] != "ready"]
    unresolved = [r["id"] for r in rows if r["net_pnl"] is None]
    trades = [r for r in rows if r["status"] == "closed"]
    complete = not missing_events and not unresolved
    return {"year": year, "path": path, "policy": policy, "fee_bp_each_side_assumed": fee_bp,
            "shares_per_raw_size_unit_assumed": size_scale,
            "fill_fraction": fill, "extra_delay_seconds": delay, "complete": complete,
            "missing_input_events": missing_events, "unresolved_executions": unresolved,
            "selected_events": len(selected), "closed_trades": len(trades),
            "issuers": len({r["issuer"] for r in trades}),
            "quarter_trade_counts": {str(q): sum(r["quarter"] == q for r in trades) for q in range(1, 5)},
            "quarter_closed_subset_pnl": {str(q): sum(r["net_pnl"] for r in trades if r["quarter"] == q) for q in range(1, 5)},
            "issuer_closed_subset_pnl": {s: sum(r["net_pnl"] for r in trades if r["issuer"] == s) for s in PANEL},
            "conditional_account_return_pct": (cash / 10_000 - 1) * 100 if complete else None,
            "actual_historical_net_return_pct": None,
            "closed_subset_gross_pnl": sum(r["gross_pnl"] for r in trades),
            "closed_subset_mid_price_pnl": sum(r["mid_price_pnl"] for r in trades),
            "closed_subset_spread_drag": sum(r["spread_drag"] for r in trades),
            "closed_subset_entry_notional": sum(r["quantity"] * float(r["entry_ask"]) for r in trades),
            "closed_subset_assumed_fees": sum(r["fees"] for r in trades),
            "closed_subset_pnl": sum(r["net_pnl"] for r in trades),
            "closed_subset_positive_fraction": sum(r["net_pnl"] > 0 for r in trades) / len(trades) if trades else None,
            "closed_subset_mean_trade_net_pct": sum(r["trade_net_return_pct"] for r in trades) / len(trades) if trades else None,
            "max_reference_lag_seconds": max((r["reference_lag_seconds"] for r in trades), default=None),
            "max_entry_after_reference_seconds": max((r["entry_after_reference_seconds"] for r in trades), default=None),
            "max_exit_after_target_seconds": max((r["exit_after_target_seconds"] for r in trades), default=None),
            "realized_close_only_drawdown_pct": drawdown * 100 if complete else None,
            "rows": rows}


def report(inputs: dict, index: dict, size_scale: int = 1) -> dict:
    results = []
    for year in ("2022", "2023"):
        for path in ("immediate", "opening"):
            for policy in ("eps", "all_reports", "market", "cash"):
                results.append(simulate(inputs, index, year, path, policy, size_scale=size_scale))
            for fee, fill, delay in ((25, 1, 0), (50, 1, 0), (10, .5, 0), (10, 0, 0), (10, 1, 60)):
                results.append(simulate(inputs, index, year, path, "eps", fee, fill, delay, size_scale))
    verdicts = []
    for path in ("immediate", "opening"):
        rows = [r for r in results if r["year"] == "2023" and r["path"] == path]
        base = rows[0]
        comparisons = rows[1:4]
        stress = [r for r in rows[4:] if r["fee_bp_each_side_assumed"] != 50 and r["fill_fraction"] != 0]
        checks = {"all_sources_complete": all(r["complete"] for r in rows),
                  "minimum_trades": base["closed_trades"] >= 12,
                  "minimum_issuers": base["issuers"] >= 6,
                  "quarter_counts": all(n >= 2 for n in base["quarter_trade_counts"].values()),
                  "all_quarters_positive": all(p > 0 for p in base["quarter_closed_subset_pnl"].values()),
                  "positive_and_beats_controls": base["complete"] and base["conditional_account_return_pct"] > 0 and
                      all(r["complete"] and base["conditional_account_return_pct"] > r["conditional_account_return_pct"] for r in comparisons),
                  "stress_positive": all(r["complete"] and r["conditional_account_return_pct"] > 0 for r in stress)}
        verdicts.append({"path": path, "checks": checks,
            "conditional_reconstruction_verdict": "passed_reconstruction_only" if all(checks.values()) else
                ("incomplete_sources" if not checks["all_sources_complete"] else "rejected_performance"),
            "adoption": "not_adopted", "live_order_eligible": False})
    return {"schema": "earnings-announcement-execution-report-v1", "cohort": inputs["cohort"],
            "shares_per_raw_size_unit_assumed": size_scale,
            "input_statuses": dict(Counter(e["status"] for e in inputs["events"])),
            "event_ledger": inputs["events"], "quote_windows": len(index), "results": results,
            "verdicts": verdicts, "strict_pit_eligible": False, "live_order_eligible": False,
            "limitations": ["historical_API_arrival_unverified", "headline_original_version_and_EPS_basis_unverified",
                "historical_account_settlement_costs_unverified",
                "historical_size_unit_conservative_raw_share_cap" if size_scale == 1 else "historical_size_unit_round_lot100_interpretation",
                "quote_does_not_guarantee_fill", "fixed_survivor_panel_not_market_universe",
                "drawdown_excludes_intraholding_mark_to_market"]}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("cohort", "inputs", "reference", "entry", "exit", "gaps", "report"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--sources", nargs="*", type=Path, default=[])
    parser.add_argument("--size-scale", type=int, choices=(1, 100), default=1,
                        help="Report-only size interpretation; 100 is not independent confirmation")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.size_scale != 1 and args.command != "report":
        parser.error("size interpretation applies only to offline report")
    if args.command == "cohort":
        result = cohort(args.source)
    elif args.command == "inputs":
        result = input_events(json.loads(args.input.read_bytes()), observations(args.sources, "news-day"))
    else:
        inputs = json.loads(args.input.read_bytes())
        index = quote_index(observations(args.sources, "quote-window"))
        if args.command == "report":
            result = report(inputs, index, args.size_scale)
        elif args.command == "gaps":
            result = {"stage": "gaps", "requests": gaps(inputs, index)}
        else:
            result = {"stage": args.command, "requests": stages(inputs, index, args.command)}
    save(args.output, result)
    print(json.dumps({"output": str(args.output), "sha256": digest(args.output),
                      "events": len(result.get("events", [])), "requests": len(result.get("requests", []))}))


if __name__ == "__main__":
    main()
