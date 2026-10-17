"""Bounded historical SIP quotes for isolated research. No broker/order access."""

from __future__ import annotations

import base64
import calendar
import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SYMBOLS = frozenset({"AAPL", "MSFT", "GOOGL", "AMZN", "META", "FB", "NVDA", "JPM", "XOM", "SPY"})
MAX_BYTES = 512_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API authentication to a different URL.


_open = urllib.request.build_opener(_NoRedirect()).open


def availability() -> dict:
    key, secret = (bool(os.getenv(name, "").strip()) for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"))
    return {"key_configured": key, "secret_configured": secret, "configured": key and secret,
            "feed": "sip", "live_order_eligible": False}


def clock_ns(value: str) -> int:
    """Do not truncate SIP nanoseconds to datetime's microsecond precision."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("RFC3339 clock required")
    stamp = dt.datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    return calendar.timegm(stamp.astimezone(dt.timezone.utc).timetuple()) * 1_000_000_000 + int((match[2] or "0").ljust(9, "0"))


def parameters(symbol: str, start: str, end: str) -> dict:
    if symbol not in SYMBOLS:
        raise ValueError("fixed research panel only")
    try:
        clock_ns(start), clock_ns(end)
        if "." in start or "." in end:
            raise ValueError("request windows use whole seconds")
        first, last = (dt.datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))
        if first.utcoffset() is None or last.utcoffset() is None:
            raise ValueError("explicit timezone required")
        first, last = (t.astimezone(dt.timezone.utc) for t in (first, last))
        if not (dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc) <= first < last
                < dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)):
            raise ValueError("only 2022–2025 historical research dates")
        if last - first > dt.timedelta(seconds=5):
            raise ValueError("one historical window must be at most five seconds")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("valid timezone-aware 2022–2025 window of at most five seconds required") from None
    return {"start": first.isoformat(), "end": last.isoformat(), "feed": "sip", "currency": "USD",
            "asof": first.astimezone(ZoneInfo("America/New_York")).date().isoformat(), "limit": "1000", "sort": "asc"}


def quote_window(symbol: str, start: str, end: str) -> dict:
    params = parameters(symbol, start, end)
    result = {"schema": "alpaca-historical-quote-window-v1", "symbol": symbol, "request": params,
              "live_order_eligible": False, "strict_pit_eligible": False}
    if not availability()["configured"]:
        return {**result, "status": "credentials_missing"}
    request = urllib.request.Request("https://data.alpaca.markets/v2/stocks/" + symbol + "/quotes?" +
        urllib.parse.urlencode(params), headers={"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"].strip(),
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"].strip()})
    result["requested_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        with _open(request, timeout=15) as response:
            status, raw = response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Do not return error bodies/headers: an upstream error may echo authentication.
        return {**result, "status": "provider_rejected", "http_status": exc.code,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    except OSError:
        return {**result, "status": "transport_failed", "http_status": None,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    result.update(http_status=status, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    if len(raw) > MAX_BYTES:
        return {**result, "status": "response_size_limit"}
    if any(os.environ[name].strip().encode() in raw for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")):
        return {**result, "status": "invalid_source_response"}
    try:
        body = json.loads(raw)
        rows = body.get("quotes") if isinstance(body, dict) else None
        if (not isinstance(rows, list) or body.get("symbol") != symbol or len(rows) > 1000):
            raise ValueError("symbol/schema mismatch")
        bounds = [clock_ns(params[name]) for name in ("start", "end")]
        clocks = []
        for row in rows:
            clock = clock_ns(row["t"])
            if not bounds[0] <= clock <= bounds[1]:
                raise ValueError("quote outside requested historical window")
            clocks.append(clock)
        if clocks != sorted(clocks):
            raise ValueError("quotes not ascending")
    except (ValueError, TypeError, KeyError, AttributeError):
        return {**result, "status": "invalid_source_response"}
    return {**result, "status": "observed" if rows else "empty", "quote_count": len(rows),
            "has_next_page": bool(body.get("next_page_token")), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": len(raw), "raw_b64": base64.b64encode(raw).decode("ascii")}
