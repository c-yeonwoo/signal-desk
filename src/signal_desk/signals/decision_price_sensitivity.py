"""Read-only, single-capture price sensitivity. Not a strategy or PnL replay."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from signal_desk import db
from signal_desk.signals import decision_snapshot

SCHEMA = "decision-price-bundle-v1"
KINDS = ("signal_output", "gate_input", "engine_input", "price_base", "quote_delta")
MAX_BYTES = 64 * 1024 * 1024


def _provider_symbols(tickers):
    # Existing internal class-share IDs are not the provider's dot notation.
    aliases = {"BRK-B": "BRK.B", "BF-B": "BF.B"}
    mapped = {t: aliases.get(t, t) for t in tickers}
    if len(set(mapped.values())) != len(mapped):
        raise ValueError("ambiguous provider symbol mapping")
    return mapped


def export_bundle(market: str, output_id: str) -> dict:
    """Only the five public-engine artifacts reachable from one explicit output."""
    artifacts = {}

    def get(kind, artifact_id):
        item = db.decision_artifact_get(artifact_id)
        if not item or item["market"] != market or item["kind"] != kind:
            raise ValueError("artifact missing or market/kind mismatch")
        artifacts[kind] = {"id": artifact_id, "payload": item}
        return item["data"]

    output = get("signal_output", output_id)
    gate = get("gate_input", output["gate_input_id"])
    saved = get("engine_input", gate["engine_input_id"])
    get("price_base", saved["price_base_id"])
    get("quote_delta", saved["quote_delta_id"])
    bundle = {"schema": SCHEMA, "market": market, "signal_output_id": output_id,
              "artifacts": artifacts, "strict_pit_eligible": False}
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: dict) -> dict:
    if bundle.get("schema") != SCHEMA or bundle.get("market") not in ("kr", "us"):
        raise ValueError("invalid bundle schema/market")
    if len(json.dumps(bundle, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError("bundle too large")
    artifacts = bundle.get("artifacts", {})
    if set(artifacts) != set(KINDS):
        raise ValueError("incomplete artifact bundle")
    data = {}
    for kind in KINDS:
        row = artifacts[kind]
        payload = row["payload"]
        if (set(payload) != {"schema_version", "market", "kind", "data"}
                or payload["schema_version"] != 1 or payload["market"] != bundle["market"]
                or payload["kind"] != kind):
            raise ValueError("artifact identity mismatch")
        digest = db.decision_artifact_estimate(bundle["market"], kind, payload["data"])["id"]
        if digest != row["id"]:
            raise ValueError("artifact hash mismatch")
        data[kind] = payload["data"]
    links = ((data["signal_output"]["gate_input_id"], "gate_input"),
             (data["gate_input"]["engine_input_id"], "engine_input"),
             (data["engine_input"]["price_base_id"], "price_base"),
             (data["engine_input"]["quote_delta_id"], "quote_delta"),
             (data["quote_delta"]["price_base_id"], "price_base"),
             (bundle["signal_output_id"], "signal_output"))
    if any(ref != artifacts[kind]["id"] for ref, kind in links):
        raise ValueError("artifact reference mismatch")
    if len(data["engine_input"]["universe"]) != data["signal_output"]["universe_size"]:
        raise ValueError("universe size mismatch")
    return data


def _restore(data):
    series = data["price_base"]["series"]
    quotes = data["quote_delta"]["quotes"]
    unclassified = data["quote_delta"]["unclassified"]
    if set(quotes) & set(unclassified) or (set(quotes) | set(unclassified)) - set(series):
        raise ValueError("invalid quote delta tickers")
    prices = {t: list(s["closes"]) + ([quotes[t]["price"]] if t in quotes else unclassified.get(t, []))
              for t, s in series.items()}
    dates = {t: list(s["dates"]) for t, s in series.items()}
    return prices, dates


def _captured_quotes(data):
    universe = {row["ticker"] for row in data["engine_input"]["universe"]}
    return {ticker: quote for ticker, quote in data["quote_delta"]["quotes"].items() if ticker in universe}


def _close_targets(data):
    """A separate daily-close audit, never an invented intraday observation."""
    if data["quote_delta"]["quotes"] or data["quote_delta"]["unclassified"]:
        raise ValueError("recorded-close audit requires close-only inputs")
    targets = {}
    for row in data["engine_input"]["universe"]:
        ticker = row["ticker"]
        series = data["price_base"]["series"].get(ticker, {})
        dates, prices = series.get("dates", []), series.get("closes", [])
        if not dates or len(dates) != len(prices) or dates[-1] >= data["engine_input"]["today"]:
            raise ValueError("recorded close needs a completed dated bar")
        targets[ticker] = {"date": dates[-1], "price": prices[-1]}
    if not targets or len(targets) > 600 or len({q["date"] for q in targets.values()}) != 1:
        raise ValueError("recorded-close audit requires one common day and bounded universe")
    return targets


def reference_request(bundle: dict, *, basis: str = "intraday") -> dict:
    """Fixed recorded universe, not winners. Only received intraday tails are replaceable."""
    data = validate_bundle(bundle)
    if bundle["market"] != "us":
        raise ValueError("US SIP adapter only; KR requires a separate documented source")
    saved = data["engine_input"]
    if basis == "recorded-close":
        targets = _close_targets(data)
        day = dt.date.fromisoformat(next(iter(targets.values()))["date"])
        start = dt.datetime.combine(day, dt.time(), ZoneInfo("America/New_York"))
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), start.tzinfo)
        return {"symbols": ",".join(sorted(_provider_symbols(targets).values())), "timeframe": "1Day",
                "start": start.isoformat(), "end": (end - dt.timedelta(seconds=1)).isoformat(),
                "adjustment": "raw", "asof": saved["today"], "feed": "sip", "currency": "USD",
                "limit": 10000, "sort": "asc"}
    if basis != "intraday":
        raise ValueError("unsupported comparison basis")
    quotes = _captured_quotes(data)
    if not quotes or len(quotes) > 600:
        raise ValueError("no quotes or invalid quote universe")
    minutes = []
    for quote in quotes.values():
        stamp = float(quote["received_at"])
        if not math.isfinite(stamp):
            raise ValueError("invalid receipt time")
        receipt = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
        if receipt.astimezone(ZoneInfo("America/New_York")).date().isoformat() != saved["today"]:
            raise ValueError("receipt outside captured session")
        minutes.append(int(stamp // 60) * 60 - 60)
    if max(minutes) - min(minutes) > 3600:
        raise ValueError("receipt span exceeds finite capture budget")
    iso = lambda n: dt.datetime.fromtimestamp(n, dt.timezone.utc).isoformat()
    return {"symbols": ",".join(sorted(_provider_symbols(quotes).values())), "timeframe": "1Min",
            "start": iso(min(minutes)), "end": iso(max(minutes) + 60),
            "adjustment": "raw", "asof": saved["today"], "feed": "sip", "currency": "USD",
            "limit": 10000, "sort": "asc"}


def compare(bundle: dict, external: dict, *, basis: str = "intraday") -> dict:
    data = validate_bundle(bundle)
    request = reference_request(bundle, basis=basis)
    if external.get("request") != request or external.get("status") != "complete":
        raise ValueError("external source request incomplete or mismatched")
    if external.get("source_url") != "https://data.alpaca.markets/v2/stocks/bars":
        raise ValueError("unsupported external source")
    pages = external.get("pages") or []
    if not 1 <= len(pages) <= 4:
        raise ValueError("external raw pages missing")
    preserved_bars = {}
    for i, page in enumerate(pages):
        raw = page["raw"].encode()
        if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != page["sha256"]:
            raise ValueError("external page hash mismatch")
        body = json.loads(raw)
        if bool(body.get("next_page_token")) != (i < len(pages) - 1):
            raise ValueError("external pagination incomplete")
        for ticker, bars in body["bars"].items():
            preserved_bars.setdefault(ticker, []).extend(bars)
    if preserved_bars != external.get("bars"):
        raise ValueError("external bars differ from preserved pages")
    saved, gate = data["engine_input"], data["gate_input"]
    prices, dates = _restore(data)
    base = decision_snapshot.evaluate_preserved_inputs(saved, gate, prices, dates)
    report = {"schema": "decision-price-sensitivity-v1", "basis": basis, "signal_output_id": bundle["signal_output_id"],
              "market": bundle["market"], "session": saved["today"], "universe_size": len(saved["universe"]),
              "baseline_exact_match": base == data["signal_output"]["rows"],
              "profitability_verdict": "not_tested", "strict_pit_eligible": False,
              "limitations": ["prior completed minute is not an executable contemporaneous quote",
                              "historical bars may have later corrections",
                              "fixed non-price inputs; quote-derived market caps and fundamentals are intentionally unchanged",
                              "single captured cross-section, not LLM choices, holdings, orders or account returns"]}
    if basis == "recorded-close":
        report["limitations"].append("daily-close source audit only; no recorded intraday receipts and no live-price counterfactual")
    if not report["baseline_exact_match"]:
        return {**report, "status": "blocked_baseline_mismatch"}
    if saved["price_structure_status"] != "verified" or any(
            len(dates[t]) > len(prices[t]) or any(day > saved["today"] for day in dates[t]) for t in prices):
        return {**report, "status": "blocked_input_structure"}
    bars_by = external["bars"]
    quotes = _close_targets(data) if basis == "recorded-close" else _captured_quotes(data)
    symbol_map = _provider_symbols(quotes)
    if set(bars_by) - set(symbol_map.values()):
        raise ValueError("unexpected external symbols")
    report["provider_symbol_map"] = {t: s for t, s in symbol_map.items() if t != s}
    altered, pairs, missing = copy.deepcopy(prices), [], []
    for ticker, quote in sorted(quotes.items()):
        minute = int(quote["received_at"] // 60) * 60 - 60 if basis == "intraday" else None
        matches = []
        for bar in bars_by.get(symbol_map[ticker], []):
            stamp = dt.datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("external timestamp needs timezone")
            aligned = (stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat() == quote["date"]
                       if basis == "recorded-close" else stamp.timestamp() == minute)
            if aligned:
                matches.append(bar)
        if len(matches) != 1:
            missing.append(ticker)
            continue
        px = float(matches[0]["c"])
        original = float(quote["price"])
        if not math.isfinite(px) or px <= 0 or not math.isfinite(original) or original <= 0:
            raise ValueError("invalid reference price")
        altered[ticker][-1] = px
        pairs.append({"ticker": ticker, "received_at": quote.get("received_at"),
                      "external_minute": matches[0]["t"] if basis == "intraday" else None,
                      "recorded_close_date": quote.get("date"), "external_bar": matches[0]["t"],
                      "original": original, "reference": px, "difference_bps": (original / px - 1) * 10000})
    report.update(replaced_prices=len(pairs), comparison_targets=len(quotes),
                  replaced_quotes=len(pairs) if basis == "intraday" else 0,
                  captured_quotes=len(_captured_quotes(data)),
                  non_universe_quotes_unchanged=sorted(set(data["quote_delta"]["quotes"]) - set(quotes)),
                  missing_symbols=missing, price_pairs=pairs)
    if not pairs:
        return {**report, "status": "blocked_no_aligned_reference"}
    candidate = decision_snapshot.evaluate_preserved_inputs(saved, gate, altered, dates)
    by_before = {r["ticker"]: r for r in base}
    by_after = {r["ticker"]: r for r in candidate}
    fields = ("score", "kind", "rank", "rank_eligible", "gate_blocked", "gates")
    changes = []
    for ticker in sorted(set(by_before) | set(by_after)):
        old, new = by_before.get(ticker, {}), by_after.get(ticker, {})
        diff = {k: {"before": old.get(k), "after": new.get(k)} for k in fields if old.get(k) != new.get(k)}
        if diff:
            changes.append({"ticker": ticker, "changes": diff})
    buys = lambda rows: sorted(r["ticker"] for r in rows if r["kind"] in ("BUY", "STRONG_BUY")
                              and r.get("rank_eligible") is not False and not r.get("gate_blocked"))
    report.update(status="partial_reference_sensitivity" if missing else "reference_sensitivity",
                  changes=changes, original_candidates=buys(base), reference_candidates=buys(candidate),
                  unchanged_inputs_sha256=hashlib.sha256(json.dumps({"engine": saved, "gate": gate},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
    return report
