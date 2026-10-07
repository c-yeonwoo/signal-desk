"""P2 가격 입력 보존: 일봉 기준본과 실제 사용한 장중 변경분을 분리한다.

이 모듈은 주문 자격을 결정하지 않는다. 날짜가 없는 가격을 임의의 종가로 만들지 않고,
정확한 수치 재생과 원천 시점의 검증 여부를 별개로 다룬다.
"""

from __future__ import annotations

import math

from signal_desk import db

_QUOTE_META_KEYS = ("observation_id", "provider", "price_kind", "currency",
                    "source_timestamp", "source_timestamp_field", "source_time_verified")


def _prices(values: list, *, ticker: str) -> list[float]:
    if not isinstance(values, list):
        raise ValueError(f"{ticker}: prices must be a list")
    out = []
    for value in values:
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ticker}: invalid price") from exc
        if not math.isfinite(price):
            raise ValueError(f"{ticker}: nonfinite price")
        out.append(price)
    return out


def split_price_inputs(prices: dict[str, list[float]], dates: dict[str, list[str]],
                       quote_snapshot: dict) -> tuple[dict, dict, list[dict]]:
    """엔진 가격 배열을 기준본·장중 변경분으로 나눈다. 미분류 꼬리는 숨기지 않는다."""
    if not isinstance(prices, dict) or not isinstance(dates, dict) or not isinstance(quote_snapshot, dict):
        raise ValueError("invalid price input bundle")
    base = {"legacy_source_time_verified": False, "series": {}}
    delta = {"quotes": {}, "unclassified": {}}
    issues = []
    quotes = quote_snapshot.get("quotes") or {}
    received = quote_snapshot.get("quote_updated") or {}
    meta_by = quote_snapshot.get("quote_meta") or {}
    captured_at = quote_snapshot.get("captured_at")
    for ticker in sorted(set(prices) | set(dates)):
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("invalid ticker")
        closes = _prices(prices.get(ticker) or [], ticker=ticker)
        sessions = dates.get(ticker) or []
        if not isinstance(sessions, list) or not all(isinstance(day, str) for day in sessions):
            raise ValueError(f"{ticker}: invalid dates")
        if sessions != sorted(set(sessions)):
            issues.append({"ticker": ticker, "reason": "dates_not_unique_sorted"})
        n_base = min(len(closes), len(sessions))
        base["series"][ticker] = {"dates": list(sessions), "closes": closes[:n_base]}
        if len(sessions) > len(closes):
            issues.append({"ticker": ticker, "reason": "price_missing_for_date"})
        tail = closes[n_base:]
        if not tail:
            continue
        try:
            quote_price = float(quotes.get(ticker))
            quote_received = float(received.get(ticker))
            age = float(captured_at) - quote_received
            quote_matches = (len(tail) == 1 and math.isfinite(quote_price)
                             and quote_price == tail[0] and -300 <= age <= 600)
        except (TypeError, ValueError):
            quote_matches = False
        if quote_matches:
            meta = meta_by.get(ticker) or {}
            delta["quotes"][ticker] = {
                "price": tail[0], "received_at": quote_received,
                **{key: meta.get(key) for key in _QUOTE_META_KEYS},
            }
            if not meta.get("observation_id"):
                issues.append({"ticker": ticker, "reason": "observation_id_missing"})
        else:
            delta["unclassified"][ticker] = tail
            issues.append({"ticker": ticker, "reason": "undated_price_not_matching_quote"})
    return base, delta, issues


def persist_price_inputs(market: str, prices: dict[str, list[float]],
                         dates: dict[str, list[str]], quote_snapshot: dict, *,
                         observed_at: int | None = None) -> dict:
    """가격 원장에 두 조각만 기록한다. 라이브 계산 경로에서는 아직 자동 호출하지 않는다."""
    base, delta, issues = split_price_inputs(prices, dates, quote_snapshot)
    base_id = db.decision_artifact_put(market, "price_base", base, observed_at=observed_at)
    delta_id = db.decision_artifact_put(market, "quote_delta",
                                        {"price_base_id": base_id, **delta},
                                        observed_at=observed_at)
    return {"price_base_id": base_id, "quote_delta_id": delta_id,
            "structure_status": "verified" if not issues else "partial", "issues": issues,
            "strict_pit_eligible": False}


def load_price_inputs(market: str, price_base_id: str, quote_delta_id: str) -> tuple[dict, dict]:
    """내용 해시와 참조를 검증하고 원래 엔진 가격 배열·원본 날짜를 복원한다."""
    base = db.decision_artifact_get(price_base_id)
    delta = db.decision_artifact_get(quote_delta_id)
    if not base or not delta or base["market"] != market or delta["market"] != market:
        raise ValueError("price artifact missing or market mismatch")
    if base["kind"] != "price_base" or delta["kind"] != "quote_delta":
        raise ValueError("price artifact kind mismatch")
    if delta["data"].get("price_base_id") != price_base_id:
        raise ValueError("quote delta references another price base")
    series = base["data"].get("series") or {}
    quotes = delta["data"].get("quotes") or {}
    unclassified = delta["data"].get("unclassified") or {}
    if set(quotes) & set(unclassified) or (set(quotes) | set(unclassified)) - set(series):
        raise ValueError("invalid quote delta tickers")
    prices = {}
    dates = {}
    for ticker, item in series.items():
        tail = ([quotes[ticker]["price"]] if ticker in quotes else unclassified.get(ticker, []))
        prices[ticker] = list(item["closes"]) + list(tail)
        dates[ticker] = list(item["dates"])
    return prices, dates
