"""Bounded source entitlement/date probes, never a bar-open execution backtest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.measure.contract_event_sources import _json, _save as _save_archive, load_archive
from signal_desk import config

SCHEMA = "earnings-fast-entry-access-v1"
APPLE = {
    "2023": "https://www.apple.com/newsroom/2023/02/apple-reports-first-quarter-results/",
    "2025": "https://www.apple.com/newsroom/2025/01/apple-reports-first-quarter-results/",
}
KIS_DOC = ("https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/"
           "examples_llm/overseas_stock/inquire_time_itemchartprice/inquire_time_itemchartprice.py")


def _save(output: Path, files: dict, manifest: dict) -> None:
    # This offline CLI may archive private commission/settlement evidence.
    previous_mask = os.umask(0o077)
    try:
        _save_archive(output, files, manifest)
    finally:
        os.umask(previous_mask)


def _get(url: str) -> tuple[int | None, bytes]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "SignalDeskResearch/1.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                return response.status, _json({"source_error": "response_size_limit"})
            return response.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(50_000)
    except OSError:
        return None, _json({"source_error": "transport_failed"})


def _decode(raw: bytes):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return None


def minute_access(body: object, target: str) -> dict:
    """Inspect clocks only; never read prices returned outside the target date."""
    if not isinstance(body, dict) or body.get("rt_cd") != "0":
        return {"status": "provider_failed", "target_date": target}
    rows = body.get("output2")
    if not isinstance(rows, list) or not rows:
        return {"status": "no_bars", "target_date": target, "rows": 0}
    dates = sorted({str(row.get("xymd", "")) for row in rows if isinstance(row, dict)})
    valid = all(isinstance(row, dict) and re.fullmatch(r"\d{8}", str(row.get("xymd", "")))
                and re.fullmatch(r"\d{6}", str(row.get("xhms", ""))) for row in rows)
    matched = [row for row in rows if isinstance(row, dict) and row.get("xymd") == target]
    return {"status": "target_bars_observed" if valid and matched else "requested_history_not_returned",
            "target_date": target, "rows": len(rows), "target_rows": len(matched), "returned_dates": dates,
            "quote_execution_eligible": False}


def release_metadata(raw: bytes) -> list[str]:
    """Website metadata is evidence of a published clock, not certified first receipt."""
    source = raw.decode("utf-8", errors="replace")
    candidates = re.findall(r'"datePublished"\s*:\s*"([^"]+)"', source)
    # Keep a date-only value date-only; never borrow dateModified or a call time.
    candidates += re.findall(r'<time[^>]*datetime=[\'\"]([^\'\"]+)[\'\"]', source, re.I)
    return sorted(set(candidates))


def collect(output: Path) -> dict:
    if output.exists():
        raise FileExistsError("source archive already exists")
    config.load_env()
    files, observations = {}, []

    def public(name, url, request=None):
        status, raw = _get(url)
        files[name] = raw
        row = {"source_file": name, "http_status": status,
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "request": request if request is not None else {"url": url}}
        observations.append(row)
        return row, raw

    for year, url in APPLE.items():
        row, raw = public(f"raw/apple-{year}.html", url)
        row.update(published_metadata=release_metadata(raw), first_publication_verified=False)
    public("raw/kis-minute-reference.py", KIS_DOC)
    key = config.alphavantage_key()
    if key:
        params = {"function": "TIME_SERIES_INTRADAY", "symbol": "AAPL", "interval": "1min",
                  "month": "2023-02", "adjusted": "false", "extended_hours": "true", "outputsize": "full"}
        row, raw = public("raw/av-intraday.json", "https://www.alphavantage.co/query?" +
                          urllib.parse.urlencode({**params, "apikey": key}), params)
        try:
            body = json.loads(raw)
            has_series = isinstance(body, dict) and isinstance(body.get("Time Series (1min)"), dict)
            row.update(status="series_returned" if has_series else "series_not_returned",
                       response_fields=sorted(body) if isinstance(body, dict) else [])
        except (ValueError, TypeError):
            row.update(status="invalid_response")
    credentials = config.kis_credentials()
    _save(output, files, {"schema": SCHEMA, "observations": observations,
                          "credentials_present": {"alphavantage": bool(key), "kis": bool(credentials),
                              "alpaca": bool(os.getenv("APCA_API_KEY_ID") and os.getenv("APCA_API_SECRET_KEY")),
                              "databento": bool(os.getenv("DATABENTO_API_KEY"))},
                          "strict_pit_eligible": False, "live_order_eligible": False})
    return summary(output)


def summary(source: Path) -> dict:
    manifest, files = load_archive(source, SCHEMA)
    bar_access, fee_access = [], []
    for row in manifest["observations"]:
        name = row["source_file"]
        if name.startswith(("raw/toss-candles-", "raw/toss-afterhours-")):
            body = _decode(files[name])
            bar_access.append(toss_bar_access(body, row["request"]["params"]["before"],
                                             opening_hour="afterhours" not in name))
        if name == "private/toss-orders-2023.json":
            fee_access.append(execution_cost_access(_decode(files[name])))
    return {"schema": SCHEMA, "credentials_present": manifest["credentials_present"],
            "archive_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "archived_observation_count": len(manifest["observations"]),
            "observations": [{k: v for k, v in row.items() if k != "returned_clocks"}
                             for row in manifest["observations"]],
            "bar_access": bar_access, "execution_cost_access": fee_access,
            "missing": ["historical_bid_ask_and_sizes", "matched_release_clock_and_feature_vintage",
                        "full_period_buy_sell_cost_evidence", "extended_session_execution_permission"],
            "backtest_state": "not_run_missing_execution_sources", "net_return_pct": None,
            "verdict": "blocked_missing_execution_sources", "strategy_adoption": "undecided",
            "registered_verdict": False, "strict_pit_eligible": False, "live_order_eligible": False}


def toss_bar_access(body: object, before: str, *, opening_hour: bool = True) -> dict:
    """Validate source clocks before price access. Access ≠ an executable quote."""
    result = body.get("result") if isinstance(body, dict) else None
    rows = result.get("candles") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return {"status": "no_bars", "quote_execution_eligible": False}
    try:
        bound = dt.datetime.fromisoformat(before)
        if bound.utcoffset() is None:
            raise ValueError("timezone missing")
        if bound.astimezone(ZoneInfo("America/New_York")).date().isoformat() >= "2026-08-04":
            raise ValueError("protected study date")
        stamps = [dt.datetime.fromisoformat(row["timestamp"]) for row in rows]
        if any(t.utcoffset() is None or t > bound for t in stamps):
            raise ValueError("clock exceeds requested bound")
        if stamps != sorted(set(stamps), reverse=True):
            raise ValueError("duplicate or unsorted bars")
        if any(row.get("currency") != "USD" for row in rows):
            raise ValueError("currency mismatch")
        opening = bound.astimezone(ZoneInfo("America/New_York")).replace(
            hour=9 if opening_hour else 16, minute=30, second=0, microsecond=0)
        required = {opening + dt.timedelta(minutes=i) for i in range(1, 63)}
        if not required.issubset(stamps):
            raise ValueError("opening hour coverage missing")
        return {"status": "historical_bars_observed", "rows": len(rows),
                "start": min(stamps).isoformat(), "end": max(stamps).isoformat(),
                "window": "regular_opening_hour" if opening_hour else "after_hours_access_probe",
                "timestamp_semantics": "bar_end", "quote_execution_eligible": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"status": "invalid_bar_clocks_or_currency", "quote_execution_eligible": False}


def execution_cost_access(body: object) -> dict:
    """Redacted, reconciled actual fees; no order ID/account/position values leave this audit."""
    from decimal import Decimal, InvalidOperation
    result = body.get("result") if isinstance(body, dict) else None
    rows = result.get("orders") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return {"status": "no_execution_cost_rows", "full_period_costs_verified": False}
    accepted, failed = [], 0
    for row in rows:
        try:
            execution = row["execution"]
            at = dt.datetime.fromisoformat(execution["filledAt"])
            ordered = dt.datetime.fromisoformat(row["orderedAt"])
            settled = dt.date.fromisoformat(execution["settlementDate"]) if execution.get("settlementDate") else None
            if (row["symbol"] != "AAPL" or row["currency"] != "USD" or row["status"] != "FILLED"
                    or row["side"] not in {"BUY", "SELL"} or at.utcoffset() is None
                    or ordered.utcoffset() is None or ordered > at or (settled is not None and settled < at.date())
                    or not "2023-01-01" <= ordered.date().isoformat() <= "2023-12-31"
                    or not "2023-01-01" <= at.date().isoformat() <= "2023-12-31"):
                raise ValueError("identity, period or settlement mismatch")
            amount, qty, price, commission, tax = (Decimal(str(execution[key])) for key in
                ("filledAmount", "filledQuantity", "averageFilledPrice", "commission", "tax"))
            if (not all(v.is_finite() for v in (amount, qty, price, commission, tax))
                    or min(amount, qty, price) <= 0 or min(commission, tax) < 0
                    or abs(amount - qty * price) > Decimal(".02")):
                raise ValueError("execution money does not reconcile")
            accepted.append({"side": row["side"], "date": at.date().isoformat(),
                             "settled": settled is not None,
                             "commission_bps": float(commission / amount * 10_000)})
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            failed += 1
    return {"status": "partial_execution_costs_observed" if accepted else "invalid_execution_costs",
            "accepted_rows": len(accepted), "invalid_rows": failed, "has_next": result.get("hasNext"),
            "settled_rows": sum(r["settled"] for r in accepted),
            "sides": sorted({r["side"] for r in accepted}),
            "date_span": [min(r["date"] for r in accepted), max(r["date"] for r in accepted)] if accepted else None,
            "commission_bps_range": [min(r["commission_bps"] for r in accepted),
                                     max(r["commission_bps"] for r in accepted)] if accepted else None,
            "full_period_costs_verified": False}


def diagnose(source: Path, output: Path, environment: str = "real") -> dict:
    """Keep HTTP error bodies: the app wrapper deliberately reduces them to None."""
    from signal_desk.broker import kis

    if output.exists():
        raise FileExistsError("source archive already exists")
    previous, files = load_archive(source, SCHEMA)
    config.load_env()
    credentials = config.kis_credentials()
    if not credentials:
        raise ValueError("existing KIS credentials missing")
    if environment not in {"real", "demo"}:
        raise ValueError("unknown read environment")
    credentials = {**credentials, "env": environment}
    token = kis.get_token(credentials)
    if not token:
        raise ValueError("existing KIS authentication unavailable")
    observations = list(previous["observations"])
    requests = [("minute-" + day,
                 "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice", "HHDFS76950200",
                 {"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL", "NMIN": "1", "PINC": "1",
                  "NEXT": "1", "NREC": "120", "FILL": "", "KEYB": day + "110000"})
                for day in ("20230203", "20250131")]
    requests.append(("daily-control", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
                     {"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL", "GUBN": "0",
                      "BYMD": "20230203", "MODP": "0"}))
    for label, path, tr_id, params in requests:
        if any(row.get("environment") == environment and row.get("request") == params
               and row.get("http_status") == 200 for row in observations):
            continue
        time.sleep(1.2)  # The demo endpoint allows far fewer calls than the real domain.
        host = "https://openapi.koreainvestment.com:9443" if environment == "real" else "https://openapivts.koreainvestment.com:29443"
        request = urllib.request.Request(host + path + "?" +
            urllib.parse.urlencode(params), headers={"authorization": "Bearer " + token,
                "appkey": credentials["app_key"], "appsecret": credentials["app_secret"],
                "tr_id": tr_id, "custtype": "P"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status, raw = response.status, response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(50_000)
        except OSError:
            status, raw = None, _json({"source_error": "transport_failed"})
        name = f"raw/diagnostic-{environment}-{label}-{len(observations)}.json"
        if name in files:
            raise ValueError("diagnostic observation already exists; preserve its original bytes")
        files[name] = raw
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        observations.append({"source_file": name, "request": params, "http_status": status, "environment": environment,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "response_observed", "provider_code": body.get("msg_cd") if isinstance(body, dict) else None,
            "provider_message": body.get("msg1") if isinstance(body, dict) else None,
            **(minute_access(body, params["KEYB"][:8]) if "KEYB" in params else {})})
    _save(output, files, {"schema": SCHEMA, "observations": observations,
                          "credentials_present": previous["credentials_present"],
                          "parent_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "strict_pit_eligible": False, "live_order_eligible": False})
    return summary(output)


def supplement(source: Path, output: Path) -> dict:
    """Public alternative-source evidence; no price samples or new subscriptions."""
    if output.exists():
        raise FileExistsError("source archive already exists")
    previous, files = load_archive(source, SCHEMA)
    observations = list(previous["observations"])
    urls = {
        "apple-wire-2025": "https://www.businesswire.com/news/home/20250130261281/en/Apple-reports-first-quarter-results",
        "nvda-2023": "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2023",
        "alpaca-access": "https://docs.alpaca.markets/us/docs/market-data-faq",
        "kibot-samples": "https://www.kibot.com/free-historical-intraday-data.html",
        "kis-public-fees": "https://www.truefriend.com/main/bond/research/_static/TF03ca050000.jsp",
        "nvda-wire-2023": ("https://www.globenewswire.com/news-release/2023/02/22/2613783/0/en/"
                           "NVIDIA-Announces-Financial-Results-for-Fourth-Quarter-and-Fiscal-2023.html"),
    }
    for label, url in urls.items():
        name = f"raw/{label}.html"
        if name in files:
            continue  # Reuse the original observation, including a failed observation.
        status, raw = _get(url)
        files[name] = raw
        observations.append({"source_file": name, "http_status": status, "request": {"url": url},
                             "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "published_metadata": release_metadata(raw), "first_publication_verified": False})
    _save(output, files, {"schema": SCHEMA, "observations": observations,
                          "credentials_present": previous["credentials_present"],
                          "parent_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "strict_pit_eligible": False, "live_order_eligible": False})
    return summary(output)


def collect_toss(source: Path, output: Path, *, after_hours: bool = False) -> dict:
    """Existing account GET access only. Current fee rates never certify 2023 costs."""
    from signal_desk.ingest import toss
    if output.exists():
        raise FileExistsError("source archive already exists")
    previous, files = load_archive(source, SCHEMA)
    if not after_hours and any(name.startswith(("raw/toss-", "private/toss-")) for name in files):
        raise ValueError("Toss observation already exists; no network refresh")
    if after_hours and any(name.startswith("raw/toss-afterhours-") for name in files):
        raise ValueError("Toss observation already exists; no network refresh")
    config.load_env()
    token = toss._access_token()
    if not token:
        raise ValueError("existing Toss authentication unavailable")
    observations = list(previous["observations"])
    if "raw/toss-openapi.json" not in files:
        doc_status, raw = _get("https://openapi.tossinvest.com/openapi-docs/latest/openapi.json")
        files["raw/toss-openapi.json"] = raw
        observations.append({"source_file": "raw/toss-openapi.json", "http_status": doc_status,
                             "request": {"url": "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json"},
                             "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    requests = [("candles-" + day, "/api/v1/candles", {"symbol": "AAPL", "interval": "1m", "count": 120,
                  "adjusted": "false", "before": day + "T11:00:00-05:00"}, False)
                for day in ("2023-02-03", "2025-01-31")]
    requests += [("commissions", "/api/v1/commissions", {}, True),
                 ("orders-2023", "/api/v1/orders", {"status": "CLOSED", "symbol": "AAPL",
                    "from": "2023-01-01", "to": "2023-12-31", "limit": 20}, True)]
    if after_hours:
        requests = [("afterhours-" + day, "/api/v1/candles", {"symbol": "AAPL", "interval": "1m", "count": 120,
                     "adjusted": "false", "before": day + "T17:40:00-05:00"}, False)
                    for day in ("2023-02-02", "2025-01-30")]
    for label, path, params, private in requests:
        name = ("private/" if private else "raw/") + "toss-" + label + ".json"
        if name in files:
            raise ValueError("Toss observation already exists")
        headers = {"Authorization": "Bearer " + token}
        if private:
            headers["X-Tossinvest-Account"] = config.toss_account()
        url = "https://openapi.tossinvest.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
                status, raw = response.status, response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(50_000)
        except OSError:
            status, raw = None, _json({"source_error": "transport_failed"})
        files[name] = raw
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        result = body.get("result") if isinstance(body, dict) else None
        audit = {"source_file": name, "http_status": status, "request": {"path": path, "params": params},
                 "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "historical_costs_verified": False}
        if label.startswith(("candles", "afterhours")):
            audit.update(toss_bar_access(body, params["before"], opening_hour=not after_hours))
        if label == "commissions" and isinstance(result, list):
            audit["current_rates"] = [{key: row.get(key) for key in ("marketCountry", "commissionRate", "startDate", "endDate")}
                                      for row in result if isinstance(row, dict)]
        if label == "orders-2023":
            audit.update(execution_cost_access(body))
        observations.append(audit)
    _save(output, files, {"schema": SCHEMA, "observations": observations,
                          "credentials_present": {**previous["credentials_present"], "toss": True},
                          "parent_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "strict_pit_eligible": False, "live_order_eligible": False})
    output.chmod(0o600)
    return summary(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("collect", "summary", "diagnose", "supplement", "toss"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--environment", choices=("real", "demo"), default="real")
    parser.add_argument("--after-hours", action="store_true")
    args = parser.parse_args()
    if args.phase in {"diagnose", "supplement", "toss"} and args.source is None:
        parser.error("--source is required for an append-only follow-up")
    result = (collect(args.archive) if args.phase == "collect" else
              diagnose(args.source, args.archive, args.environment) if args.phase == "diagnose" else
              supplement(args.source, args.archive) if args.phase == "supplement" else
              collect_toss(args.source, args.archive, after_hours=args.after_hours) if args.phase == "toss" else summary(args.archive))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
