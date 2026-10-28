"""One exported capture -> exact replay -> prior-minute SIP sensitivity.

No account writes, orders, LLM, registered looks or profitability claim.
Fetch is explicit and capped at four pages / 8 MB; incomplete responses fail.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from signal_desk.signals import decision_price_sensitivity as audit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_reference(request: dict) -> dict:
    from signal_desk import config
    config.load_env()
    keys = [os.getenv(k, "").strip() for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")]
    if not all(keys):
        raise ValueError("Alpaca credentials missing")
    url = "https://data.alpaca.markets/v2/stocks/bars"
    result = {"status": "incomplete", "source_url": url, "request": request,
              "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "pages": [], "bars": {}}
    token, seen = None, set()
    for _ in range(4):
        params = {**request, **({"page_token": token} if token else {})}
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={
            "APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000 or any(key.encode() in raw for key in keys):
            raise ValueError("unsafe response")
        body = json.loads(raw)
        if not isinstance(body.get("bars"), dict) or set(body["bars"]) - set(request["symbols"].split(",")):
            raise ValueError("unexpected reference data")
        result["pages"].append({"sha256": hashlib.sha256(raw).hexdigest(), "raw": raw.decode()})
        for ticker, bars in body["bars"].items():
            result["bars"].setdefault(ticker, []).extend(bars)
        token = body.get("next_page_token")
        if not token:
            result["status"] = "complete"
            return result
        if token in seen:
            raise ValueError("repeated page token")
        seen.add(token)
    raise ValueError("reference exceeded page budget; do not compare partial response")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--external", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    if args.output.exists() or (args.fetch and args.external.exists()):
        parser.error("output already exists; preserve prior evidence")
    with gzip.open(args.bundle, "rb") as f:
        raw = f.read(audit.MAX_BYTES + 1)
    if len(raw) > audit.MAX_BYTES:
        parser.error("bundle too large")
    bundle = json.loads(raw)
    request = audit.reference_request(bundle)
    # Baseline failure must stop BEFORE any external requests.
    data = audit.validate_bundle(bundle)
    prices, dates = audit._restore(data)
    baseline = audit.decision_snapshot.evaluate_preserved_inputs(data["engine_input"], data["gate_input"], prices, dates)
    if baseline != data["signal_output"]["rows"]:
        parser.error("baseline replay mismatch; external fetch not attempted")
    if args.fetch:
        external = fetch_reference(request)
        args.external.write_text(json.dumps(external, ensure_ascii=False) + "\n")
    else:
        external = json.loads(args.external.read_bytes())
    report = audit.compare(bundle, external)
    report["bundle_file_sha256"] = hashlib.sha256(args.bundle.read_bytes()).hexdigest()
    report["external_file_sha256"] = hashlib.sha256(args.external.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report.get(k) for k in ("status", "baseline_exact_match", "universe_size",
        "captured_quotes", "replaced_quotes", "original_candidates", "reference_candidates", "profitability_verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
