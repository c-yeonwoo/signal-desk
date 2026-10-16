"""Bounded, retrospective raw-close observations for paper-policy research only.

The provider's unadjusted switch is a *request*, not proof that returned bars
match historical fills or that corporate-action cash flows are complete. This
archive never writes the signal price caches or supplies the live order path.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from signal_desk import config, market_clock
from signal_desk.signals.paper_cash_replay import DEVELOPMENT_END

SCHEMA = "research-raw-close-observation-v1"
MAX_CALENDAR_DAYS = 45
_EXCHANGES = frozenset({"NAS", "NYS", "AMS"})


def _json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _validate_scope(market: str, ticker: str, start: str, end: str, exchange: str | None) -> list[str]:
    if market not in {"kr", "us"}:
        raise ValueError("market must be kr or us")
    pattern = r"[0-9]{6}" if market == "kr" else r"[A-Z][A-Z0-9.-]{0,9}"
    if not re.fullmatch(pattern, ticker):
        raise ValueError("invalid ticker")
    if market == "us" and exchange not in _EXCHANGES:
        raise ValueError("US research requires an explicit KIS exchange")
    if market == "kr" and exchange is not None:
        raise ValueError("KR research does not use a KIS exchange")
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or first > last:
        raise ValueError("invalid date window")
    if last.isoformat() > DEVELOPMENT_END or (last - first).days >= MAX_CALENDAR_DAYS:
        raise ValueError("outside bounded development window")
    sessions = [(first + dt.timedelta(days=n)).isoformat()
                for n in range((last - first).days + 1)
                if market_clock.is_session(market, first + dt.timedelta(days=n))]
    if not sessions:
        raise ValueError("window has no exchange sessions")
    return sessions


def _fetch_kr(ticker: str, start: str, end: str, exchange: str | None) -> list[dict]:
    # pykrx/KRX authentication prints the login identifier to stdout. It is
    # irrelevant to price evidence and must not leak into CLI logs.
    with contextlib.redirect_stdout(io.StringIO()):
        from pykrx import stock

        frame = stock.get_market_ohlcv_by_date(start.replace("-", ""), end.replace("-", ""),
                                               ticker, adjusted=False)
    if frame is None or frame.empty:
        raise ValueError("KR provider returned no raw bars; check KRX access")
    rows = []
    for date, row in frame.iterrows():
        raw = {str(key): (value.item() if hasattr(value, "item") else value)
               for key, value in row.items()}
        rows.append({"date": date.strftime("%Y-%m-%d"), "close": raw.get("종가"),
                     "source_row": raw})
    return rows


def _fetch_us(ticker: str, start: str, end: str, exchange: str | None) -> list[dict]:
    from signal_desk.ingest import us

    creds = config.kis_credentials()
    if not creds:
        raise ValueError("KIS credentials unavailable; no raw bars collected")
    # BYMD pins the query to the requested historical end; 45 calendar days
    # fit in one provider page (up to 100 sessions). No unbounded pagination.
    raw = us._fetch_page(ticker, exchange, creds, end.replace("-", ""), adjusted=False)
    if raw is None or not raw:
        raise ValueError("KIS returned no unadjusted historical page")
    rows = []
    for row in raw:
        day = str(row.get("xymd") or "")
        if len(day) == 8 and day.isdecimal():
            rows.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                         "close": row.get("clos"), "source_row": row})
    return rows


def capture(*, market: str, ticker: str, start: str, end: str, root: Path,
            exchange: str | None = None, observed_at: dt.datetime | None = None,
            fetch: Callable[[str, str, str, str | None], list[dict]] | None = None) -> dict:
    """Preserve one bounded provider response, including gaps and provenance.

    `requested_unadjusted` is not `unadjusted_verified`. A complete date panel
    still needs independent basis and corporate-action reconciliation.
    """
    sessions = _validate_scope(market, ticker, start, end, exchange)
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    rows = (fetch or (_fetch_kr if market == "kr" else _fetch_us))(ticker, start, end, exchange)
    if not isinstance(rows, list) or not rows:
        raise ValueError("source returned no raw bars")
    accepted, seen = [], set()
    for row in rows:
        day = row.get("date")
        if not isinstance(day, str) or day < start or day > end:
            continue  # Provider may include an earlier page; never infer a missing day.
        if day in seen or day not in sessions or not isinstance(row.get("source_row"), dict):
            raise ValueError("duplicate, non-session, or unsupported raw bar")
        try:
            close = float(row["close"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid raw close") from None
        if not math.isfinite(close) or close <= 0:
            raise ValueError("invalid raw close")
        seen.add(day)
        accepted.append({"date": day, "close": close, "source_row": row["source_row"]})
    if not accepted:
        raise ValueError("no source bars in requested window")
    accepted.sort(key=lambda item: item["date"])
    missing = sorted(set(sessions) - seen)
    params = ({"adjusted": False, "start": start.replace("-", ""),
               "end": end.replace("-", ""), "ticker": ticker} if market == "kr" else
              {"AUTH": "", "EXCD": exchange, "SYMB": ticker, "GUBN": "0",
               "BYMD": end.replace("-", ""), "MODP": "0"})
    payload = {"schema": SCHEMA, "market": market, "ticker": ticker,
               "start": start, "end": end, "provider": "pykrx_krx" if market == "kr" else "kis_dailyprice",
               "request": params, "observed_at": observed_at.astimezone(dt.timezone.utc).isoformat(),
               "price_basis": "requested_unadjusted_unverified",
               "strict_pit_eligible": False, "live_eligible": False,
               "coverage": "date_panel_only" if not missing else "missing_sessions",
               "missing_sessions": missing, "rows": accepted,
               # KIS can return dates preceding the requested start. Retain the
               # whole returned page so the observation is independently auditable.
               "source_response_rows": [row["source_row"] for row in rows]}
    material = _json(payload)
    observation_id = hashlib.sha256(material).hexdigest()
    destination = root / market / ticker / f"{start}_{end}" / f"{observation_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".raw-close-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(material)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != material:
                raise ValueError("immutable observation collision") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"observation_id": observation_id, "path": str(destination),
            "market": market, "ticker": ticker, "rows": len(accepted),
            "missing_sessions": missing, "price_basis": payload["price_basis"],
            "live_eligible": False}


def verify(path: Path) -> dict:
    """Read back the exact archived bytes; never repair a corrupt observation."""
    material = path.read_bytes()
    if path.stem != hashlib.sha256(material).hexdigest():
        raise ValueError("raw close observation checksum mismatch")
    payload = json.loads(material)
    if payload.get("schema") != SCHEMA or payload.get("price_basis") != "requested_unadjusted_unverified":
        raise ValueError("unsupported raw close observation")
    _validate_scope(payload["market"], payload["ticker"], payload["start"],
                    payload["end"], payload["request"].get("EXCD"))
    if (not payload.get("rows") or not payload.get("source_response_rows")
            or payload.get("strict_pit_eligible") is not False
            or payload.get("live_eligible") is not False):
        raise ValueError("invalid raw close observation")
    return payload


def audit_fill_range(trades: list[dict], observation_paths: list[Path], *, market: str) -> dict:
    """Flag impossible-looking paper fills; this is not fill/basis certification.

    An intraday paper fill outside the same-session raw daily high/low may have
    used a stale reference, another adjustment basis, or a bad timestamp. It
    requires investigation, never an automatic price rewrite.
    """
    if market not in {"kr", "us"}:
        raise ValueError("invalid market")
    zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
    bars: dict[tuple[str, str], dict] = {}
    conflicts: set[tuple[str, str]] = set()
    historical_closes: dict[tuple[str, str], float] = {}
    historical_conflicts: set[tuple[str, str]] = set()

    def remember_close(ticker: str, day: str, raw_close: object) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day > DEVELOPMENT_END:
            return
        try:
            close = float(raw_close)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(close) or close <= 0:
            return
        key = (ticker, day)
        if key in historical_closes and not math.isclose(historical_closes[key], close,
                                                         rel_tol=1e-9, abs_tol=1e-6):
            historical_conflicts.add(key)
        else:
            historical_closes[key] = close

    for path in observation_paths:
        item = verify(path)
        if item["market"] != market:
            raise ValueError("mixed-market raw close observations")
        for row in item["rows"]:
            key = (item["ticker"], row["date"])
            if key in bars and bars[key] != row["source_row"]:
                conflicts.add(key)
            else:
                bars[key] = row["source_row"]
            remember_close(item["ticker"], row["date"], row["close"])
        # US KIS returns up to 100 older bars on the same retained page. They
        # are prior-close comparison evidence, not validated coverage of the
        # requested range and never an input to the signal/backtest engine.
        if market == "us":
            for source in item["source_response_rows"]:
                raw_day = str(source.get("xymd") or "")
                if re.fullmatch(r"\d{8}", raw_day):
                    remember_close(item["ticker"],
                                   f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}",
                                   source.get("clos"))
    counts = {"trades": 0, "within_daily_range": 0, "outside_daily_range": 0,
              "missing_bar": 0, "conflicting_bar": 0, "invalid_bar_or_trade": 0,
              "excluded_protected_trades": 0,
              "outside_prior_close_match": 0, "outside_prior_close_unmatched": 0,
              "outside_prior_close_unknown": 0}
    examples = []
    for trade in trades:
        try:
            day = dt.datetime.fromtimestamp(float(trade["ts"]), zone).date().isoformat()
            ticker = str(trade["ticker"])
            price = float(trade["price"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            counts["invalid_bar_or_trade"] += 1
            continue
        if day > DEVELOPMENT_END:
            counts["excluded_protected_trades"] += 1
            continue
        counts["trades"] += 1
        key = (ticker, day)
        if key in conflicts:
            counts["conflicting_bar"] += 1
            continue
        row = bars.get(key)
        if row is None:
            counts["missing_bar"] += 1
            continue
        try:
            low = float(row["저가" if market == "kr" else "low"])
            high = float(row["고가" if market == "kr" else "high"])
        except (KeyError, TypeError, ValueError, OverflowError):
            counts["invalid_bar_or_trade"] += 1
            continue
        if not all(math.isfinite(v) for v in (price, low, high)) or low <= 0 or high < low or price <= 0:
            counts["invalid_bar_or_trade"] += 1
        elif low <= price <= high:
            counts["within_daily_range"] += 1
        else:
            counts["outside_daily_range"] += 1
            gap_bps = 10_000 * ((low - price) / low if price < low else (price - high) / high)
            prior = [(past_day, past_close) for (past_ticker, past_day), past_close in historical_closes.items()
                     if past_ticker == ticker and past_day < day]
            if not prior or any(past_ticker == ticker and past_day < day
                                for past_ticker, past_day in historical_conflicts):
                prior_status, matched_day = "unknown", None
                counts["outside_prior_close_unknown"] += 1
            else:
                matched = [past_day for past_day, past_close in prior
                           if math.isclose(price, past_close, rel_tol=1e-8, abs_tol=1e-6)]
                matched_day = max(matched) if matched else None
                prior_status = "match" if matched_day else "unmatched"
                counts["outside_prior_close_match" if matched_day else
                       "outside_prior_close_unmatched"] += 1
            if len(examples) < 20:
                examples.append({"trade_id": trade.get("id"), "ticker": ticker, "session": day,
                                 "fill_price": price, "daily_low": low, "daily_high": high,
                                 "gap_bps": round(gap_bps, 2), "prior_close_status": prior_status,
                                 "matched_prior_close_session": matched_day})
    return {"schema": "research-fill-range-audit-v1", "market": market,
            "mode": "research_only", "live_eligible": False,
            "price_basis_verified": False, "verified_total_return": None,
            "state": "no_data" if not counts["trades"] else "needs_review" if any(counts[key] for key in
                    ("outside_daily_range", "missing_bar", "conflicting_bar", "invalid_bar_or_trade"))
                    else "range_check_only",
            **counts, "examples": examples}


def main() -> None:
    # Unlike the API/CLI entrypoints, `python -m ...research_raw_closes` does
    # not load .env automatically. Keep credentials in the local environment;
    # never include them in the archived request or printed result.
    config.load_env()
    parser = argparse.ArgumentParser(description="Capture bounded research-only unadjusted close evidence")
    parser.add_argument("--market", choices=("kr", "us"), required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--exchange", choices=sorted(_EXCHANGES))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = capture(market=args.market, ticker=args.ticker, start=args.start,
                     end=args.end, exchange=args.exchange, root=args.root)
    verify(Path(result["path"]))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
