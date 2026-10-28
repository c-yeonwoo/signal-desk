"""Finite 2026-10-26 receipt-vs-SIP diagnostic, NOT a strategy backtest.

Uses every retained price row in the session monitor, including SOLV after it
left the candidate set. A prior completed minute is a comparison reference,
not an executable quote or certified contemporaneous last trade. No API import,
orders, LLM, protected research look, production writes or provider fallback.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SESSION = "2026-10-26"
SYMBOLS = ("ALL", "APA", "OXY", "SOLV")
START = f"{SESSION}T13:30:00Z"
END = f"{SESSION}T20:00:00Z"


def observations(folder: Path) -> tuple[list, dict]:
    rows, hashes = [], {}
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_bytes())
        if data.get("schema") not in {"us-session-monitor-baseline-v1", "us-session-monitor-observation-v1"}:
            continue
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        baseline = data["schema"] == "us-session-monitor-baseline-v1"
        sig = data if baseline else data.get("signals", {})
        at = sig.get("signal_quote_received_at" if baseline else "quote_received_at")
        candidates = list(sig.get("eligible_candidates" if baseline else "eligible", []))
        if isinstance(sig.get("solv"), dict):
            candidates.append({**sig["solv"], "ticker": "SOLV"})
        for item in candidates:
            px = item.get("price")
            if item.get("ticker") not in SYMBOLS or not isinstance(px, (float, int)) or not math.isfinite(px) or px <= 0:
                continue
            if not isinstance(at, (float, int)) or not math.isfinite(at):
                continue
            if dt.datetime.fromtimestamp(at, dt.timezone.utc).date().isoformat() != SESSION:
                raise ValueError("receipt outside fixed session")
            rows.append({"file": path.name, "ticker": item["ticker"], "received_at": at,
                         "recorded_display_price": px})
    return rows, hashes


def compare(rows: list, bars_by: dict) -> list:
    result = []
    for row in rows:
        # Only a completed minute strictly before the receipt. Do not use the
        # receipt minute's high/low/close: they include later trades.
        minute = int(row["received_at"] // 60) * 60 - 60
        bars = bars_by.get(row["ticker"], [])
        matches = [b for b in bars if dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp() == minute]
        if len(matches) != 1:
            result.append({**row, "status": "external_minute_missing_or_duplicate"})
            continue
        bar = matches[0]
        close = float(bar["c"])
        if not math.isfinite(close) or close <= 0:
            raise ValueError("nonpositive external close")
        result.append({**row, "status": "reference_compared_not_certified", "external_minute": bar["t"],
                       "external_prior_minute_close": close,
                       "difference_bps": round((row["recorded_display_price"] / close - 1) * 10000, 3)})
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    from signal_desk import config
    config.load_env()
    rows, hashes = observations(ROOT / f"data/research/us-session-monitor-{SESSION}")
    report = {"schema": "receipt-price-audit-v1", "session": SESSION, "input_hashes": hashes,
              "recorded_price_rows": len(rows), "symbols": SYMBOLS,
              "engine_counterfactual_complete": False, "profitability_verdict": "not_tested",
              "limitations": ["periodic display prices, not full decision inputs or a continuous execution path",
                              "batch-level quote_received_at, not a preserved per-symbol receipt timestamp",
                              "provider timestamp semantics and executable quote not certified",
                              "latest historical minute aggregates may contain later corrections",
                              "SIP prior-minute reference does not isolate data causality or engine quality"],
              "source_documentation": "https://docs.alpaca.markets/us/reference/stockbars"}
    params = {"symbols": ",".join(SYMBOLS), "timeframe": "1Min", "start": START, "end": END,
              "adjustment": "raw", "asof": SESSION, "feed": "sip", "currency": "USD",
              "limit": 10000, "sort": "asc"}
    report["request"] = params
    keys = [os.getenv(k, "").strip() for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")]
    report["status"] = "credentials_missing"
    if all(keys):
        req = urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(params),
            headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
                raw = response.read(2_000_001)
                report["http_status"] = response.status
            if len(raw) > 2_000_000 or any(k.encode() in raw for k in keys):
                raise ValueError("unsafe response")
            body = json.loads(raw)
            if not isinstance(body.get("bars"), dict) or body.get("next_page_token"):
                raise ValueError("incomplete response")
            if set(body["bars"]) - set(SYMBOLS):
                raise ValueError("unexpected symbols")
            report["source_sha256"] = hashlib.sha256(raw).hexdigest()
            report["external_bars"] = body["bars"]
            report["comparisons"] = compare(rows, body["bars"])
            report["status"] = "reference_comparison_only"
        except urllib.error.HTTPError as exc:
            report.update(status="provider_rejected", http_status=exc.code)
        except OSError:
            report["status"] = "transport_failed"
        except (ValueError, TypeError, KeyError):
            report["status"] = "invalid_or_incomplete_response"
    report["observed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    target = ROOT / "data/research/receipt-price-audit-2026-10-28.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    matched = [r for r in report.get("comparisons", []) if "difference_bps" in r]
    print(json.dumps({"status": report["status"], "http_status": report.get("http_status"),
                      "recorded_price_rows": len(rows), "compared_rows": len(matched),
                      "max_abs_difference_bps": max((abs(r["difference_bps"]) for r in matched), default=None),
                      "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
