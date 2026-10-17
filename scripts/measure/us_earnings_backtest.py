"""Fixed US EPS panel. Archived reads only, no store or order integration."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from scripts.measure.contract_event_sources import _json, _save, load_archive
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk import config, market_clock

PANEL = {"AAPL": "NAS", "MSFT": "NAS", "GOOGL": "NAS", "AMZN": "NAS", "META": "NAS",
         "NVDA": "NAS", "JPM": "NYS", "XOM": "NYS"}
PLAN = ROOT / "docs/US-EARNINGS-SURPRISE-BACKTEST-2026-10-17.md"


def eps_events(body: dict, symbol: str) -> tuple[list[dict], list[dict]]:
    if body.get("symbol") != symbol or not isinstance(body.get("quarterlyEarnings"), list):
        raise ValueError("historical EPS identity/history unavailable")
    events, ledger, periods = [], [], set()
    for row in body["quarterlyEarnings"]:
        date = row.get("reportedDate") or ""
        if not "2024-01-01" <= date <= "2025-12-31":
            continue  # Later protected material is not a feature or outcome.
        entry = {"symbol": symbol, "reported_date": date, "fiscal_end": row.get("fiscalDateEnding")}
        try:
            reported, fiscal = dt.date.fromisoformat(date), dt.date.fromisoformat(row["fiscalDateEnding"])
            if fiscal > reported or fiscal in periods:
                raise ValueError("future or duplicate fiscal period")
            periods.add(fiscal)
            actual, expected = float(row["reportedEPS"]), float(row["estimatedEPS"])
            if not math.isfinite(actual + expected):
                raise ValueError("nonfinite EPS")
            surprise = (actual / expected - 1) * 100 if expected > 0 else None
            if surprise is not None:
                published = float(row["surprisePercentage"])
                if not math.isfinite(published) or abs(published-surprise) > .1:
                    raise ValueError("reported and calculated surprise differ")
            entry.update(actual_eps=actual, expected_eps=expected, surprise_pct=surprise,
                         eligible=bool(expected > 0 and actual > 0 and surprise >= 5-1e-10),
                         report_time_category=row.get("reportTime"))
            events.append(entry)
            entry = {**entry, "status": "positive_eps_event" if entry["eligible"] else "valid_nonbuy_report"}
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            entry["status"] = "unusable_eps:" + str(exc) if isinstance(exc, ValueError) else "unusable_eps:missing_numeric_field"
        ledger.append(entry)
    return events, ledger


def collect_eps(output: Path, aapl_probe: Path | None = None) -> dict:
    config.load_env()
    key = config.alphavantage_key()
    if not key:
        raise ValueError("existing Alpha Vantage key missing")
    files, observations, events, ledger, calls = {}, [], [], [], 0
    for symbol in PANEL:
        name = f"raw/earnings-{symbol}.json"
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        if symbol == "AAPL" and aapl_probe:
            previous, raw_files = load_archive(aapl_probe, "earnings-vendor-feasibility-v1")
            raw, observed_at = raw_files[name], previous["observed_at"]
        else:
            calls += 1
            params = {"function": "EARNINGS", "symbol": symbol, "apikey": key}
            try:
                with urllib.request.urlopen("https://www.alphavantage.co/query?" + urllib.parse.urlencode(params), timeout=20) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("oversized EPS history")
            except (ValueError, OSError):
                raw = _json({"source_error": "vendor_request_failed"})
            time.sleep(1.1)
        files[name] = raw
        try:
            parsed, audit = eps_events(json.loads(raw), symbol)
            if not parsed:
                raise ValueError("no reports in fixed years")
            events.extend(parsed)
            ledger.extend(audit)
            status = "observed"
        except (ValueError, TypeError) as exc:
            status = "source_failed:" + str(exc)
        observations.append({"symbol": symbol, "request": {"function": "EARNINGS", "symbol": symbol},
                             "source_file": name, "observed_at": observed_at, "status": status})
    _save(output, files, {"schema": "us-eps-sources-v1", "observations": observations,
                          "events": sorted(events, key=lambda e: (e["reported_date"], e["symbol"])),
                          "ledger": ledger, "request_count": calls,
                          "reused_probe_sha256": hashlib.sha256(aapl_probe.read_bytes()).hexdigest() if aapl_probe else None,
                          "strict_pit_eligible": False, "live_order_eligible": False})
    return {"requests": calls, "reports": len(events), "statuses": dict(Counter(row["status"] for row in observations)),
            "ledger": dict(Counter(row["status"] for row in ledger))}


def collect_prices(eps_source: Path, output: Path) -> dict:
    from signal_desk.broker import kis
    load_archive(eps_source, "us-eps-sources-v1")
    config.load_env()
    credentials = config.kis_credentials()
    if not credentials:
        raise ValueError("existing KIS credentials missing")
    credentials = {**credentials, "env": "real"}
    files, observations, calls = {}, [], 0
    for symbol, exchange in {**PANEL, "SPY": "AMS"}.items():
        bound, complete = "20251231", False
        for page in range(7):
            params = {"AUTH": "", "EXCD": exchange, "SYMB": symbol, "GUBN": "0", "BYMD": bound, "MODP": "0"}
            body = None
            for attempt in range(3):
                calls += 1
                body = kis._request("/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000", credentials, params)
                name = f"raw/{symbol}-{page}-{attempt}.json"
                files[name] = _json(body)
                ok = isinstance(body, dict) and body.get("rt_cd") == "0" and bool(body.get("output2"))
                observations.append({"symbol": symbol, "page": page, "attempt": attempt,
                                     "request": params, "source_file": name,
                                     "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                     "status": "observed" if ok else "provider_failed"})
                time.sleep(.35)
                if ok:
                    break
            if not ok:
                break
            oldest = min(str(row.get("xymd", "")) for row in body["output2"])
            if not oldest or oldest > bound:
                break
            if oldest <= "20231229":
                complete = True
                break
            bound = (dt.datetime.strptime(oldest, "%Y%m%d").date()-dt.timedelta(days=1)).strftime("%Y%m%d")
        observations.append({"symbol": symbol, "status": "history_complete" if complete else "history_incomplete"})
    _save(output, files, {"schema": "us-eps-prices-v1", "observations": observations, "request_count": calls,
                          "eps_archive_sha256": hashlib.sha256(eps_source.read_bytes()).hexdigest(),
                          "adjusted": False, "live_order_eligible": False})
    return {"requests": calls, "coverage": {row["symbol"]: row["status"] for row in observations if "page" not in row}}


def price_panel(manifest: dict, files: dict) -> dict:
    panel = defaultdict(dict)
    for observed in manifest["observations"]:
        if observed["status"] != "observed":
            continue
        symbol, request = observed["symbol"], observed["request"]
        if (request["SYMB"] != symbol or request["MODP"] != "0" or request["GUBN"] != "0"
                or request["EXCD"] != ({**PANEL, "SPY": "AMS"})[symbol] or request["BYMD"] > "20251231"):
            raise ValueError("price request differs from frozen raw-price contract")
        body = json.loads(files[observed["source_file"]])
        if body.get("rt_cd") != "0" or not 1 <= len(body["output2"]) <= 100:
            raise ValueError("invalid price page")
        for row in body["output2"]:
            raw_day = row["xymd"]
            day = dt.datetime.strptime(raw_day, "%Y%m%d").date().isoformat()
            if raw_day > request["BYMD"]:
                raise ValueError("price beyond requested date")
            values = {key: float(row[key]) for key in ("open", "high", "low", "clos", "tvol")}
            if (any(not math.isfinite(v) for v in values.values()) or min(values[k] for k in ("open", "high", "low", "clos")) <= 0
                    or values["tvol"] < 0 or not values["low"] <= min(values["open"], values["clos"])
                    <= max(values["open"], values["clos"]) <= values["high"]):
                raise ValueError("invalid raw OHLCV")
            if day in panel[symbol] and panel[symbol][day] != values:
                raise ValueError("conflicting daily source versions")
            panel[symbol][day] = values
    return dict(panel)


def execution_day(reported: str, delay: int = 0) -> str:
    day, sessions = dt.date.fromisoformat(reported), []
    for offset in range(1, 15):
        candidate = (day+dt.timedelta(days=offset)).isoformat()
        if market_clock.is_session("us", candidate):
            sessions.append(candidate)
            if len(sessions) == 2+delay:
                return candidate
    raise ValueError("execution calendar unavailable")


def simulate(events: list[dict], prices: dict, year: str, policy: str, cost: int, fill: float, delay: int = 0) -> dict:
    if policy not in ("eps", "all_reports", "market", "cash") or cost not in (45, 75, 120) or fill not in (0, .5, 1) or delay not in (0, 1):
        raise ValueError("outside fixed US experiment")
    chosen, candidates = {}, []
    for event in events:
        if event["reported_date"][:4] != year:
            continue
        if policy != "all_reports" and not event["eligible"]:
            continue
        day = execution_day(event["reported_date"], delay)
        if day[:4] != year:
            continue
        candidates.append((day, event))
    for day, event in sorted(candidates, key=lambda pair: (pair[0], pair[1]["symbol"], pair[1]["reported_date"])):
        # Alphabetic issuer per execution date, never the largest later return.
        chosen.setdefault(day, event)
    cash = 10_000.0
    trades, outcomes = [], []
    side = cost/20_000
    for day, event in sorted(chosen.items()):
        row = {**event, "execution_session": day, "net_pnl": 0.0}
        if policy == "cash":
            outcomes.append({**row, "state": "cash"})
            continue
        ticker = "SPY" if policy == "market" else event["symbol"]
        previous = market_clock.previous_session("us", day)
        known, execution = prices.get(ticker, {}).get(previous), prices.get(ticker, {}).get(day)
        if not known:
            return {"state": "incomplete_reference", "symbol": ticker, "date": previous, "net_return_pct": None}
        budget = cash/6
        shares = int(min(int(budget/(known["clos"]*1.30*(1+side))), int(known["tvol"]*.0001))*fill)
        if not shares:
            outcomes.append({**row, "state": "unfilled", "shares": 0})
            continue
        if not execution or execution["tvol"] == 0:
            return {"state": "incomplete_execution", "symbol": ticker, "date": day, "net_return_pct": None}
        debit = shares*execution["open"]*(1+side)
        if debit > budget:
            outcomes.append({**row, "state": "unfilled", "reason": "budget_exceeded"})
            continue
        credit = shares*execution["clos"]*(1-side)
        pnl = credit-debit
        cash += pnl
        row.update(state="filled", traded_symbol=ticker, shares=shares, entry_open=execution["open"],
                   exit_close=execution["clos"], reference_close=known["clos"], net_pnl=pnl,
                   gross_pnl=shares*(execution["clos"]-execution["open"]), cash_after=cash)
        trades.append(row)
        outcomes.append(row)
    return {"state": "complete", "events": len(chosen), "trade_count": len(trades),
            "net_return_pct": (cash/10_000-1)*100, "net_pnl": cash-10_000,
            "gross_pnl": sum(row["gross_pnl"] for row in trades), "end_cash": cash,
            "quarter_pnl": {str(q): sum(row["net_pnl"] for row in trades if (int(row["execution_session"][5:7])-1)//3+1 == q) for q in range(1, 5)},
            "quarter_trades": {str(q): sum((int(row["execution_session"][5:7])-1)//3+1 == q for row in trades) for q in range(1, 5)},
            "trades": trades, "outcomes": outcomes}


def adoption(scenarios: dict) -> dict:
    primary = scenarios["75bp_full_delay0"]["eps"]
    required = [primary, scenarios["75bp_full_delay0"]["all_reports"], scenarios["75bp_full_delay0"]["market"],
                scenarios["120bp_full_delay0"]["eps"], scenarios["75bp_full_delay1"]["eps"]]
    if any(row["state"] != "complete" for row in required):
        return {"adoption": "rejected", "reason": "incomplete_paths", "live_order_eligible": False}
    positive = defaultdict(float)
    for row in primary["trades"]:
        positive[row["symbol"]] += max(0, row["net_pnl"])
    total = sum(positive.values())
    checks = {"at_least_12_trades_and_days": primary["trade_count"] >= 12,
              "at_least_6_issuers": len({row["symbol"] for row in primary["trades"]}) >= 6,
              "two_trades_every_quarter": min(primary["quarter_trades"].values()) >= 2,
              "positive_year_and_all_quarters": primary["net_return_pct"] > 0 and min(primary["quarter_pnl"].values()) > 0,
              "excess_over_all_reports": primary["net_return_pct"] > scenarios["75bp_full_delay0"]["all_reports"]["net_return_pct"],
              "excess_over_market": primary["net_return_pct"] > scenarios["75bp_full_delay0"]["market"]["net_return_pct"],
              "positive_120bp": scenarios["120bp_full_delay0"]["eps"]["net_return_pct"] > 0,
              "positive_extra_session_delay": scenarios["75bp_full_delay1"]["eps"]["net_return_pct"] > 0,
              "positive_pnl_not_concentrated": bool(total and max(positive.values())/total <= .5)}
    return {"adoption": "shadow_candidate" if all(checks.values()) else "rejected", "checks": checks,
            "reason": "fixed_screen_passed" if all(checks.values()) else "fixed_screen_failed", "live_order_eligible": False}


def run(eps: Path, price: Path) -> dict:
    source, raw = load_archive(eps, "us-eps-sources-v1")
    events, ledger = [], []
    for row in source["observations"]:
        if row["status"] != "observed":
            continue
        batch, audit = eps_events(json.loads(raw[row["source_file"]]), row["symbol"])
        events.extend(batch)
        ledger.extend(audit)
    events.sort(key=lambda e: (e["reported_date"], e["symbol"]))
    if events != source["events"] or ledger != source["ledger"]:
        raise ValueError("EPS selection differs from frozen source")
    quotes, files = load_archive(price, "us-eps-prices-v1")
    if quotes["eps_archive_sha256"] != hashlib.sha256(eps.read_bytes()).hexdigest():
        raise ValueError("price archive differs from frozen EPS cohort")
    prices = price_panel(quotes, files)
    source_complete = (len(source["observations"]) == len(PANEL)
                       and {row["symbol"] for row in source["observations"]} == set(PANEL)
                       and all(row["status"] == "observed" for row in source["observations"]))
    years = {}
    for year in ("2024", "2025"):
        scenarios = {}
        for cost in (45, 75, 120):
            for label, fill in (("full", 1), ("half", .5), ("none", 0)):
                for delay in (0, 1):
                    scenarios[f"{cost}bp_{label}_delay{delay}"] = {
                        policy: simulate(events, prices, year, policy, cost, fill, delay) if source_complete else
                        {"state": "incomplete_eps_source", "net_return_pct": None}
                        for policy in ("eps", "all_reports", "market", "cash")}
        years[year] = scenarios
    marker = "\n## 결과\n"
    if PLAN.read_text().count(marker) != 1:
        raise ValueError("plan boundary missing")
    return {"schema": "us-eps-backtest-v1", "phase": "historical_reconstruction", "events": events, "ledger": ledger,
            "years": years, "adoption": adoption(years["2025"]), "source_complete": source_complete,
            "source_archives_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (eps, price)},
            "plan_sha256": hashlib.sha256(PLAN.read_text().split(marker, 1)[0].encode()).hexdigest(),
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "strict_pit_eligible": False, "registered_verdict": False, "live_order_eligible": False,
            "limitations": ["Fixed surviving eight-name panel, not historical market-wide constituents.",
                            "Historical estimate vintage/GAAP basis and exact first public timestamp unverified.",
                            "Delayed daily execution, not immediate intraday earnings reaction.",
                            "Costs/fills are assumptions; no overnight positions or certified total returns."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("eps", "prices", "run"))
    parser.add_argument("--eps-archive", type=Path)
    parser.add_argument("--price-archive", type=Path)
    parser.add_argument("--aapl-probe", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("immutable research output already exists")
    if args.phase == "eps":
        result = collect_eps(args.output, args.aapl_probe)
    elif args.phase == "prices" and args.eps_archive:
        result = collect_prices(args.eps_archive, args.output)
    elif args.phase == "run" and args.eps_archive and args.price_archive:
        result = run(args.eps_archive, args.price_archive)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        result = {"events": len(result["events"]), "adoption": result["adoption"]}
    else:
        parser.error("required source archives missing")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
