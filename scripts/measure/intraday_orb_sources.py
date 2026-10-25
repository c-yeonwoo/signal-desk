"""Bounded local ORB research source collector; no orders, production DB, or retries.

Bars plan: {schema: intraday-orb-source-plan-v1, market: us,
            symbols: [the fixed eight symbols], sessions: [ISO trading dates]}.
Quote plan: {schema: intraday-orb-quote-plan-v1, market: us,
             windows: [{symbol, start, end}]} (quote end is inclusive).
Run from the repository root with an explicit --max-requests budget.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from signal_desk import config, market_clock
from signal_desk.ingest import alpaca_research as source

SYMBOLS = ("AAPL", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "XOM")
FIRST, LAST = "2023-12-01", "2024-12-31"
MAX_REQUESTS = 2400
NY = ZoneInfo("America/New_York")


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _read(path: Path, limit: int) -> dict:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("source file is a symlink or exceeds its size limit")
    return json.loads(path.read_bytes())


def _save_new(path: Path, value: dict) -> str:
    raw = _json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete file without replacing any prior evidence. A temporary
    # file and final path share a filesystem, so link() is atomic and exclusive.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as target:
            temporary = Path(target.name)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def _window(day: str) -> tuple[dt.datetime, dt.datetime]:
    parsed = dt.date.fromisoformat(day)
    if day != parsed.isoformat() or not FIRST <= day <= LAST:
        raise ValueError("research dates must be 2023-12-01 through 2024-12-31")
    window = market_clock.regular_window("us", dt.datetime.combine(parsed, dt.time(12), NY))
    if window is None:
        raise ValueError("plan contains a holiday or unknown session")
    return window


def requests_for_plan(plan: dict, *, quote: bool = False) -> list[dict]:
    schema = "intraday-orb-quote-plan-v1" if quote else "intraday-orb-source-plan-v1"
    if not isinstance(plan, dict) or plan.get("schema") != schema or plan.get("market") != "us":
        raise ValueError("invalid fixed US research plan")
    requests = []
    if quote:
        windows = plan.get("windows")
        if not isinstance(windows, list) or not 1 <= len(windows) <= 1000:
            raise ValueError("quote plan must contain 1–1000 explicit windows")
        for item in windows:
            if not isinstance(item, dict) or item.get("symbol") not in SYMBOLS:
                raise ValueError("fixed eight-symbol quote panel only")
            symbol, start, end = item["symbol"], item.get("start"), item.get("end")
            params = source.parameters(symbol, start, end)
            first, last = (dt.datetime.fromisoformat(params[key]) for key in ("start", "end"))
            day = first.astimezone(NY).date().isoformat()
            opening, closing = _window(day)
            if not opening <= first < last <= closing:
                raise ValueError("quote window must lie within one regular session")
            name = f"{first.strftime('%Y%m%dT%H%M%SZ')}_{last.strftime('%Y%m%dT%H%M%SZ')}.json"
            requests.append({"kind": "quotes", "symbol": symbol, "session": day,
                             "start": params["start"], "end": params["end"], "request": params,
                             "path": f"quotes/{symbol}/{name}"})
    else:
        symbols, sessions = plan.get("symbols"), plan.get("sessions")
        if (not isinstance(symbols, list) or len(symbols) != len(SYMBOLS)
                or any(not isinstance(symbol, str) for symbol in symbols) or set(symbols) != set(SYMBOLS)):
            raise ValueError("bars plan must contain exactly the fixed eight symbols")
        if (not isinstance(sessions, list) or not 1 <= len(sessions) <= 300
                or any(not isinstance(day, str) for day in sessions)
                or sessions != sorted(set(sessions))):
            raise ValueError("explicit sorted, unique session list required")
        for day in sessions:
            opening, closing = _window(day)
            for symbol in SYMBOLS:
                start, end = opening.isoformat(), closing.isoformat()
                requests.append({"kind": "bars", "symbol": symbol, "session": day,
                                 "start": start, "end": end,
                                 "request": source.bar_parameters(symbol, start, end),
                                 "path": f"bars/{symbol}/{day}.json"})
    if len({item["path"] for item in requests}) != len(requests) or len(requests) > MAX_REQUESTS:
        raise ValueError("duplicate requests or request plan exceeds 2400")
    return requests


def validate_envelope(envelope: dict, item: dict) -> None:
    """Validate request binding and source bytes before storing/reusing the envelope."""
    schema = f"alpaca-historical-{'bar' if item['kind'] == 'bars' else 'quote'}-window-v1"
    if (not isinstance(envelope, dict) or envelope.get("schema") != schema
            or envelope.get("symbol") != item["symbol"] or envelope.get("request") != item["request"]
            or envelope.get("live_order_eligible") is not False
            or envelope.get("strict_pit_eligible") is not False):
        raise ValueError("source identity mismatch")
    if envelope.get("status") not in {"observed", "empty"}:
        if any(key in envelope for key in ("raw_b64", "raw_sha256", "raw_bytes")):
            raise ValueError("failure envelope must not reflect an unverified source body")
        return
    raw = base64.b64decode(envelope["raw_b64"], validate=True)
    if (len(raw) > source.MAX_BYTES or len(raw) != envelope.get("raw_bytes")
            or hashlib.sha256(raw).hexdigest() != envelope.get("raw_sha256")):
        raise ValueError("source byte hash mismatch")
    body = json.loads(raw)
    if not isinstance(body, dict) or item["kind"] not in body:
        raise ValueError("source body/schema mismatch")
    rows = body.get(item["kind"])
    if rows is None and not body.get("next_page_token"):
        rows = []
    count_key = "bar_count" if item["kind"] == "bars" else "quote_count"
    if (body.get("symbol") != item["symbol"] or not isinstance(rows, list) or len(rows) > 1000
            or len(rows) != envelope.get(count_key)
            or envelope.get("status") != ("observed" if rows else "empty")
            or envelope.get("has_next_page") is not bool(body.get("next_page_token"))):
        raise ValueError("source body/schema mismatch")
    if item["kind"] == "bars":
        expected = [{"t": row["t"], "open": row["o"], "high": row["h"], "low": row["l"],
                     "close": row["c"], "volume": row["v"]} for row in rows]
        if (envelope.get("bars") != expected
                or envelope.get("response_complete") is not (not bool(body.get("next_page_token")))):
            raise ValueError("normalized bars diverge from preserved source")


def collect(plan: dict, outdir: Path, max_requests: int, *, quote: bool = False, workers: int = 1) -> dict:
    """Stop new launches on failure; preserve all already-started responses before returning."""
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= MAX_REQUESTS:
        raise ValueError("explicit max_requests must be between 1 and 2400")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers not in (1, 4):
        raise ValueError("workers must be 1 or 4")
    requests = requests_for_plan(plan, quote=quote)
    config.load_env()
    if config.is_prod() or os.getenv("RAILWAY_ENVIRONMENT_ID"):
        raise ValueError("collector is local research only")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": "intraday-orb-source-manifest-v1", "plan_sha256": hashlib.sha256(_json(plan)).hexdigest(),
                "kind": "quotes" if quote else "bars", "planned": len(requests), "max_requests": max_requests,
                "workers": workers,
                "attempted": 0, "request_successes": 0, "request_failures": 0, "reused": 0,
                "source_bytes": 0, "new_source_bytes": 0, "sources": [], "unmet": [],
                "live_order_eligible": False, "strict_pit_eligible": False,
                "coverage_status": "session_continuity_and_execution_conditions_not_certified"}
    lock = outdir / ".collector.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    stop_reason, last_request_at = None, None
    stopped, state_lock, rate_lock = threading.Event(), threading.Lock(), threading.Lock()
    missing, failures = [], {}

    def stop(reason):
        nonlocal stop_reason
        with state_lock:
            if stop_reason is None:
                stop_reason = reason
            stopped.set()

    def record(item, envelope, *, reused, digest):
        manifest["source_bytes"] += envelope.get("raw_bytes", 0)
        manifest["sources"].append({"path": item["path"], "status": envelope["status"], "reused": reused,
            "envelope_sha256": digest, "raw_sha256": envelope.get("raw_sha256"),
            "raw_bytes": envelope.get("raw_bytes", 0), "has_next_page": envelope.get("has_next_page", False)})
        if envelope["status"] not in {"observed", "empty"}:
            failures[item["path"]] = "source_" + envelope["status"]
            stop(failures[item["path"]])
        elif envelope.get("has_next_page"):
            failures[item["path"]] = "partial_source"
            stop("partial_source")
        elif envelope["status"] == "empty":
            failures[item["path"]] = "empty_source"

    def retrieve(item):
        nonlocal last_request_at
        # One limiter for all worker threads. Waiting workers recheck the stop
        # signal, so a response failure cannot unlock another queued request.
        with rate_lock:
            if stopped.is_set():
                return {"started": False}
            if last_request_at is not None:
                time.sleep(max(0.0, 0.5 - (time.monotonic() - last_request_at)))
            if stopped.is_set():
                return {"started": False}
            last_request_at = time.monotonic()
        reader = source.quote_window if quote else source.bar_window
        try:
            envelope = reader(item["symbol"], item["start"], item["end"])
        except Exception:
            envelope = {"schema": f"alpaca-historical-{'quote' if quote else 'bar'}-window-v1",
                        "symbol": item["symbol"], "request": item["request"],
                        "live_order_eligible": False, "strict_pit_eligible": False,
                        "status": "reader_exception"}
        try:
            validate_envelope(envelope, item)
        except (ValueError, TypeError, KeyError):
            stop("source_integrity_failed")
            return {"started": True, "failure": "source_integrity_failed"}
        if envelope["status"] not in {"observed", "empty"}:
            stop("source_" + envelope["status"])
        elif envelope.get("has_next_page"):
            stop("partial_source")
        try:
            digest = _save_new(outdir / item["path"], envelope)
        except OSError:
            stop("source_persist_failed")
            return {"started": True, "failure": "source_persist_failed"}
        return {"started": True, "envelope": envelope, "digest": digest}

    def completed(item, future):
        if future.cancelled():
            return
        result = future.result()
        if not result["started"]:
            return
        manifest["attempted"] += 1
        if result.get("failure"):
            manifest["request_failures"] += 1
            failures[item["path"]] = result["failure"]
            return
        envelope = result["envelope"]
        manifest["request_successes" if envelope["status"] in {"observed", "empty"} else "request_failures"] += 1
        manifest["new_source_bytes"] += envelope.get("raw_bytes", 0)
        record(item, envelope, reused=False, digest=result["digest"])

    try:
        # Validate all preserved sources first. In particular, an old failure or
        # partial page must stop a resumed run before any new network request.
        for item in requests:
            path = outdir / item["path"]
            if path.exists() or path.is_symlink():
                try:
                    envelope = _read(path, 2 * source.MAX_BYTES)
                    validate_envelope(envelope, item)
                except (ValueError, TypeError, KeyError, OSError):
                    stop("cached_source_invalid")
                    failures[item["path"]] = "cached_source_invalid"
                    continue
                manifest["reused"] += 1
                record(item, envelope, reused=True, digest=hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                missing.append(item)
        if missing and not stopped.is_set() and not source.availability()["configured"]:
            stop("credentials_missing")
        active, submitted = {}, 0
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            while not stopped.is_set() and (submitted < min(len(missing), max_requests) or active):
                while not stopped.is_set() and len(active) < workers and submitted < min(len(missing), max_requests):
                    item = missing[submitted]
                    future = executor.submit(retrieve, item)
                    active[future] = item
                    submitted += 1
                if active:
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        completed(active.pop(future), future)
        except KeyboardInterrupt:
            stop("interrupted")
        finally:
            # Ctrl-C stops launches but drains up to four bounded requests. Each
            # worker publishes its complete response even if the main wait ended.
            executor.shutdown(wait=True, cancel_futures=True)
            for future, item in active.items():
                completed(item, future)
        if not stopped.is_set() and submitted < len(missing):
            stop("request_budget_exhausted")
        observed = {item["path"] for item in manifest["sources"]}
        manifest["unmet"] = [{"path": item["path"], "reason": failures.get(item["path"],
                              "not_started_after_" + (stop_reason or "unknown"))}
                             for item in requests if item["path"] in failures or item["path"] not in observed]
        manifest["sources"].sort(key=lambda item: item["path"])
        manifest.update(status="blocked" if stop_reason else "incomplete" if manifest["unmet"] else "collected",
                        stop_reason=stop_reason, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        name = "manifest-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8] + ".json"
        _save_new(outdir / name, manifest)
        return {**manifest, "manifest_path": str(outdir / name)}
    finally:
        lock.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", type=Path)
    modes.add_argument("--quote-plan", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 4), default=1)
    args = parser.parse_args(argv)
    plan = _read(args.quote_plan or args.plan, 256_000)
    result = collect(plan, args.outdir, args.max_requests, quote=bool(args.quote_plan), workers=args.workers)
    print(json.dumps({key: result[key] for key in ("status", "stop_reason", "attempted", "reused", "manifest_path")}))
    return 0 if result["status"] == "collected" else 2


if __name__ == "__main__":
    raise SystemExit(main())
