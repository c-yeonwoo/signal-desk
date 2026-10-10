"""Read-only company review. Evidence here never feeds scoring or orders."""

from __future__ import annotations

import datetime as dt
import math
import re
from urllib.parse import urlsplit

from signal_desk.ingest.news import _parse_dt

BUY_KINDS = frozenset({"BUY", "STRONG_BUY"})
SELL_KINDS = frozenset({"SELL", "STRONG_SELL"})


def _iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _published(value: str | None) -> str | None:
    parsed = _parse_dt(value or "")
    if not parsed or parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def _safe_url(value: str | None) -> str | None:
    try:
        if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
            return None
        parts = urlsplit(value or "")
        return value if parts.scheme == "https" and parts.hostname else None
    except ValueError:
        return None


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def build(
    *, ticker: str, market: str, item: dict, price_date: str | None,
    expected_date: str | None, news: list[dict], official: list[dict],
    holding: dict | None, watching: bool, checked_at: int | None,
    source_check_ok: bool, provisional_at: int | float | None = None,
    confirmed_close: float | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Join existing verdict with verified evidence without synthesizing a new verdict.

    News may explain a company but is not classified as a bullish/bearish vote.
    Official events remain governed by the existing Decision path.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware now required")
    now = now.astimezone(dt.timezone.utc)
    try:
        quote_age = now.timestamp() - float(provisional_at) if provisional_at is not None else None
    except (TypeError, ValueError, OverflowError):
        quote_age = None
    quote_fresh = quote_age is not None and -300 <= quote_age <= 600
    price_status = ("unavailable" if not price_date or not expected_date else
                    "stale" if provisional_at is not None and not quote_fresh else
                    "current" if price_date == expected_date else "stale")
    price_basis = "intraday_quote" if provisional_at is not None else "confirmed_close"
    verdict = item.get("kind") if price_status == "current" else "UNAVAILABLE"
    if verdict not in BUY_KINDS | SELL_KINDS | {"HOLD"}:
        verdict = "UNAVAILABLE"
    unknowns = []
    checks = []
    evidence = []
    claims = []
    if price_status != "current":
        if not price_date:
            unknowns.append("이 종목의 확정 종가가 없어 현재 매수·매도 판정을 재확인할 수 없습니다.")
        elif not expected_date:
            unknowns.append("거래일 기준을 확인하지 못해 종가의 최신 여부를 판단할 수 없습니다.")
        elif provisional_at is not None and not quote_fresh:
            unknowns.append("장중 시세의 갱신 시각이 오래됐거나 확인되지 않았습니다.")
        else:
            unknowns.append(f"확정 종가 {price_date}와 예상 거래일 {expected_date}가 맞지 않습니다.")
        checks.append("시세 기준일과 갱신 상태를 확인한 뒤 시그널을 다시 보세요.")
    else:
        eid = (f"price:{market}:{ticker}:live:{int(provisional_at)}" if provisional_at is not None
               else f"price:{market}:{ticker}:{price_date}")
        evidence.append({"id": eid, "kind": "price", "issuer_verified": False,
                         "published_at": None, "observed_at": _iso(provisional_at), "freshness": "current",
                         "url": None})
        if verdict in BUY_KINDS and (item.get("decision") or {}).get("buy_blocked"):
            relation, sentence = "context", "정량 점수는 매수권이지만 기존 안전장치가 새 매수를 차단했습니다."
        elif verdict in BUY_KINDS:
            relation, sentence = "supports", "가격·정량 조건은 현재 매수 후보권입니다. 상승을 보장하지는 않습니다."
        elif verdict in SELL_KINDS:
            relation, sentence = "contradicts", "가격·정량 조건은 현재 매도 판정입니다. 보유 상황을 함께 확인하세요."
        elif verdict == "HOLD":
            relation, sentence = "context", "가격·정량 조건은 현재 관망입니다."
        else:
            relation, sentence = "context", "현재 시그널 판정을 확인하지 못했습니다."
        claims.append({"relation": relation, "text": sentence, "evidence_ids": [eid]})
        if provisional_at is not None:
            checks.append("장중 잠정가를 포함한 판정입니다. 확정 종가로 다시 확인하세요.")

    seen_events = set()
    for event in official:
        ident = event.get("id")
        if not ident or ident in seen_events or event.get("status") != "confirmed":
            continue
        if event.get("expires_at") is not None and event["expires_at"] < now.timestamp():
            continue
        if event.get("trust_tier") != "official" or event.get("policy_version") != "p0":
            continue
        seen_events.add(ident)
        proof = next((p for p in event.get("evidence", [])
                      if p.get("source_key") == "dart" and _safe_url(p.get("url"))), None)
        if not proof:
            continue
        eid = f"event:{ident}"
        is_risk = bool(event.get("decision_eligible") and event.get("severity") in {"critical", "serious"})
        evidence.append({"id": eid, "kind": "official_filing", "issuer_verified": True,
                         "published_at": _published(proof.get("published")),
                         "observed_at": _iso(event.get("detected_at")), "freshness": "current",
                         "url": proof["url"]})
        claims.append({"relation": "contradicts" if is_risk else "context",
                       "text": ("확인된 공식 위험: " if is_risk else "확인된 공시: ") + (event.get("summary") or "공시 원문 확인 필요"),
                       "evidence_ids": [eid]})
        if is_risk:
            checks.append("공식 위험의 해소·정정 공시를 확인하세요.")

    seen_titles = set()
    try:
        check_age = now.timestamp() - float(checked_at) if checked_at is not None else None
    except (TypeError, ValueError, OverflowError):
        check_age = None
    news_source_current = source_check_ok and check_age is not None and -300 <= check_age <= 72 * 3600
    if news_source_current:
        for row in news:
            title = (row.get("title") or "").strip()
            key = re.sub(r"\s+", " ", title).casefold()
            published = _published(row.get("published"))
            url = _safe_url(row.get("url"))
            published_dt = dt.datetime.fromisoformat(published) if published else None
            age = (now - published_dt.astimezone(dt.timezone.utc)).total_seconds() if published_dt else None
            checked = row.get("attribution_checked_at")
            if (not key or key in seen_titles or age is None or not -300 <= age <= 72 * 3600
                    or not url or not row.get("id") or not isinstance(checked, (int, float))
                    or checked < published_dt.timestamp() - 300):
                continue
            seen_titles.add(key)
            eid = f"kb:{row['id']}"
            evidence.append({"id": eid, "kind": "news", "issuer_verified": True,
                             "published_at": published,
                             "observed_at": _iso(row.get("fetched")),
                             "freshness": "current", "url": url})
            claims.append({"relation": "context", "text": title, "evidence_ids": [eid]})
            if len(seen_titles) >= 2:
                break
    if not seen_titles:
        unknowns.append("회사 귀속과 시각을 확인한 최근 뉴스가 없습니다. 이전 기사를 현재 근거로 쓰지 않습니다.")
    if not news_source_current:
        unknowns.append("최근 뉴스 수집 성공 여부를 확인하지 못했습니다.")
    if not any(e["kind"] == "official_filing" for e in evidence):
        unknowns.append("이 화면에서 연결할 최근 주요 공시가 없습니다. 공시 전체가 없다는 뜻은 아닙니다.")
    if market == "us":
        unknowns.append("미국 공식 공시의 기업별 원문 대조는 아직 제공하지 않습니다.")

    holding_context = None
    if holding:
        portfolio_relation = "held"
        holding_text = "분석용 보유 입력에 있는 종목입니다. 현재 매수 순위는 이 보유분을 더 사거나 계속 보유하라는 판정이 아닙니다. 실계좌 보유·비중은 여기서 확인하지 않았습니다."
        qty, average = _positive_number(holding.get("qty")), _positive_number(holding.get("avg_price"))
        close = _positive_number(confirmed_close)
        if qty is not None and average is not None:
            holding_context = {"source": "manual_analysis", "quantity": qty,
                               "average_price": average, "confirmed_close": None,
                               "close_date": None, "price_change_pct": None}
            if close is not None and price_status == "current":
                holding_context.update(confirmed_close=close, close_date=price_date,
                                       price_change_pct=round((close / average - 1) * 100, 2))
        checks.insert(0, "분석용 평단·수량과 실제 보유가 같은지 확인한 뒤 보유 진단에서 비중·위험을 보세요.")
    elif watching:
        portfolio_relation = "watching"
        holding_text = "관심종목입니다. 분석용 보유 입력에는 없으며 실계좌는 확인하지 않았습니다."
    else:
        portfolio_relation = "not_in_portfolio"
        holding_text = "분석용 보유 입력에는 없습니다. 실계좌 보유 여부는 확인하지 않았습니다."
    checks.append("새 가격과 기업 원문이 들어오면 가격 조건과 함께 다시 비교하세요.")
    return {
        "kind": "company_review", "ticker": ticker, "market": market,
        "as_of": {"exchange_session": expected_date, "prices_through": price_date,
                  "computed_at": now.isoformat()},
        "price_status": price_status,
        "price_basis": price_basis,
        "buy_blocked": bool((item.get("decision") or {}).get("buy_blocked")),
        "signal": {"decision": verdict, "computed_at": None,
                   "coverage": item.get("data_coverage"), "candidate_position": item.get("rank")},
        "claims": claims, "evidence": evidence,
        "portfolio_relation": portfolio_relation, "holding_text": holding_text,
        "holding_context": holding_context,
        "next_checks": checks, "unknowns": unknowns,
        "news_checked_at": _iso(checked_at),
        "not_order_advice": True,
    }
