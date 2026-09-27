"""R15 prospective US-filing -> linked KR supplier event study, never orders."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from signal_desk import db, market_clock, store
from signal_desk.broker import execution
from signal_desk.reference import sectors
from signal_desk.signals import relation_graph

VERSION = "r15-us-kr-event-v1"
COHORT_VERSION = "r15-us-kr-cohort-v1"
START_DATE = "2026-09-28"
HISTORY = 61
HORIZON = 20
NOTIONAL = 100_000_000.0
MIN_CONTROLS = 3
MAX_EXPOSURE_GAP_PP = 1.0
MAX_SOURCE_AGE_DAYS = 7  # freshness guard, not an alpha parameter
EVENT_TYPES = {"guidance_raise": 1, "guidance_cut": -1,
               "material_order_gain": 1, "material_order_loss": -1}
_US = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _finite(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def candidate(data: dict) -> dict:
    """Manual official SEC event assertion; no old KB detected_at or news score."""
    if not isinstance(data, dict):
        raise ValueError("미국 사건 입력 형식 오류")
    ticker = str(data.get("us_ticker") or "").strip().upper()
    kind = str(data.get("event_type") or "")
    if not _US.fullmatch(ticker) or kind not in EVENT_TYPES:
        raise ValueError("미국 종목코드 또는 사건 유형 오류")
    url = str(data.get("source_url") or "").strip()
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}
            or not parsed.path.startswith("/Archives/edgar/data/")
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or "%" in parsed.path
            or any(part in ("", ".", "..") for part in parsed.path.split("/")[1:])
            or len(url) > 1200):
        raise ValueError("SEC EDGAR 원문 HTTPS 주소 필요")
    filing = re.fullmatch(r"/Archives/edgar/data/(\d+)/(\d+)/[^/]+", parsed.path)
    if not filing:
        raise ValueError("SEC EDGAR CIK/접수번호/문서 경로 필요")
    url = "https://www.sec.gov" + parsed.path
    quote = " ".join(str(data.get("evidence_quote") or "").split())
    if not 30 <= len(quote) <= 1200:
        raise ValueError("미국 사건 원문 인용 필요")
    filed = str(data.get("source_filed_date") or "")
    try:
        if not _DATE.fullmatch(filed):
            raise ValueError
        dt.date.fromisoformat(filed)
    except ValueError:
        raise ValueError("SEC 공시 날짜는 YYYY-MM-DD") from None
    proof = {"version": VERSION, "us_ticker": ticker, "event_type": kind,
             "direction": EVENT_TYPES[kind], "source_url": url,
             "source_filing_key": f"{int(filing.group(1))}/{filing.group(2)}",
             "evidence_quote": quote, "source_filed_date": filed,
             "source_available_at_verified": False, "mode": "research_only",
             "live_eligible": False}
    proof["evidence_hash"] = hashlib.sha256(
        json.dumps(proof, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return proof


def source_fresh_at(proof: dict, observed_at: int) -> bool:
    """Filing date cannot be future or a stale document relabeled as a new event."""
    filed = dt.date.fromisoformat(proof["source_filed_date"])
    observed = dt.datetime.fromtimestamp(observed_at, ZoneInfo("America/New_York")).date()
    return 0 <= (observed - filed).days <= MAX_SOURCE_AGE_DAYS


def capture_session_for_review(reviewed_at: int) -> str | None:
    now = dt.datetime.fromtimestamp(reviewed_at, dt.timezone.utc)
    prior = market_clock.latest_completed_session("kr", now)
    if not prior:
        return None
    upcoming = market_clock.next_sessions("kr", prior, 1)
    return upcoming[0] if upcoming else None


def _expected_days(session: str) -> list[str]:
    start = (dt.date.fromisoformat(session) - dt.timedelta(days=135)).isoformat()
    cal = market_clock._calendar("kr")
    return [d.date().isoformat() for d in cal.sessions_in_range(start, session)][-HISTORY:]


def freeze(event: dict, *, session: str, observed_at: dt.datetime,
           prices: dict[str, list[float]], dates: dict[str, list[str]]) -> dict:
    """Freeze common-origin raw closes, economic links and same-sector controls."""
    review = event.get("review") or {}
    if (event.get("version") != VERSION or review.get("verdict") != "approved"
            or review.get("capture_session") != session or session < START_DATE):
        return {"ready": False, "reason": "사건 검토/첫 관측 세션 불일치"}
    usable_at = max(int(event["observed_at"]), int(review["reviewed_at"]))
    if not source_fresh_at(event, usable_at):
        return {"ready": False, "reason": "SEC 공시일 대비 사건 관측·검토 지연"}
    edge_cutoff = int(event["observed_at"]) - 1
    edges = relation_graph.active_edges(
        db.relation_edges_as_of(event["us_ticker"], edge_cutoff), observed_at=edge_cutoff)
    if not edges:
        return {"ready": False, "reason": "사건 관측 전에 검증된 고객·공급 관계 없음"}
    if any(session > edge["valid_until"] for edge in edges):
        return {"ready": False, "reason": "사건 첫 국내 가격 세션 전에 관계 근거 만료"}
    expected = _expected_days(session)
    if len(expected) != HISTORY:
        return {"ready": False, "reason": "61개 공식 거래 세션 확보 실패"}
    valid = {}
    for ticker, series in prices.items():
        ds = dates.get(ticker)
        if (ds and len(ds) == len(series) and ds[-HISTORY:] == expected
                and len(series) >= HISTORY and all((p := _finite(v)) is not None and p > 0
                                               for v in series[-HISTORY:])):
            valid[ticker] = float(series[-1])
    linked = sorted({edge["kr_supplier"] for edge in edges})
    if any(t not in valid or not sectors.sector_of(t) for t in linked):
        return {"ready": False, "reason": "연결 종목 가격/섹터/61거래일 이력 결손"}
    groups = []
    for target in linked:
        sector = sectors.sector_of(target)
        controls = sorted(t for t in sectors.by_sector(sector) if t not in linked and t in valid)
        if len(controls) < MIN_CONTROLS:
            return {"ready": False, "reason": "같은 업종 비관계 가격 대조군 3종목 미만"}
        edge = next(e for e in edges if e["kr_supplier"] == target)
        groups.append({"target": target, "sector": sector, "controls": controls,
                       "edge_id": edge["id"], "edge_hash": edge["evidence_hash"],
                       "revenue_exposure_pct": edge["revenue_exposure_pct"]})
    assumptions = execution.cost_assumptions("kr")
    quantities: dict[str, dict[str, int]] = {"linked": {}, "sector_control": {}}
    allocation = NOTIONAL / len(groups)

    def add_qty(policy: str, ticker: str, budget: float) -> None:
        unit = -execution.calculate(valid[ticker], 1, "buy", "kr", assumptions=assumptions).cash_change
        qty = int(budget // unit) if unit > 0 else 0
        quantities[policy][ticker] = quantities[policy].get(ticker, 0) + qty

    for group in groups:
        add_qty("linked", group["target"], allocation)
        for ticker in group["controls"]:
            add_qty("sector_control", ticker, allocation / len(group["controls"]))
    if any(not q for panel in quantities.values() for q in panel.values()):
        return {"ready": False, "reason": "가상 원금으로 정수주 구성 불가"}
    def exposure(policy: str) -> float:
        return sum(-execution.calculate(valid[t], q, "buy", "kr",
                                        assumptions=assumptions).cash_change
                   for t, q in quantities[policy].items()) / NOTIONAL * 100
    exposures = {p: exposure(p) for p in quantities}
    if abs(exposures["linked"] - exposures["sector_control"]) > MAX_EXPOSURE_GAP_PP:
        return {"ready": False, "reason": "동결 정수주 현금노출 차이 1%p 초과"}
    selected = set(quantities["linked"]) | set(quantities["sector_control"])
    return {"ready": True, "version": COHORT_VERSION, "mode": "research_only",
            "live_eligible": False, "source_available_at_verified": False,
            "event_id": event["id"], "event_hash": event["evidence_hash"],
            "event_type": event["event_type"], "direction": event["direction"],
            "us_ticker": event["us_ticker"], "event_first_observed_at": event["observed_at"],
            "event_reviewed_at": review["reviewed_at"], "session": session,
            "source_filed_date": event["source_filed_date"],
            "source_lag_calendar_days": (dt.datetime.fromtimestamp(usable_at, ZoneInfo("America/New_York")).date()
                                         - dt.date.fromisoformat(event["source_filed_date"])).days,
            "captured_at": observed_at.isoformat(), "history_sessions": HISTORY,
            "horizon_sessions": HORIZON, "notional": NOTIONAL,
            "cost_assumptions": assumptions, "exposure_at_decision_pct": exposures,
            "groups": groups, "quantities": quantities,
            "selected": {t: {"price": valid[t], "sector": sectors.sector_of(t)} for t in selected},
            "note": "공식 공시 수동 검토 후 첫 국내 완료 세션 동결. 섹터 대조군·가격은 이 시점 고정; 원천 공개시각/호가/실체결 미검증."}


def capture(now: dt.datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    session = market_clock.latest_completed_session("kr", now)
    if not session or market_clock.is_open("kr", now):
        return {"saved": 0, "reason": "KR 완료 세션 없음"}
    close = market_clock._calendar("kr").schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=12):
        return {"saved": 0, "reason": "최초 관측 시간창 밖"}
    pending = db.relation_events_pending_capture()
    if not pending:
        return {"saved": 0, "halted": 0, "session": session}
    prices, dates = store.load_portfolio_close_bundle("kr")
    saved = halted = 0
    for event in pending:
        due = (event.get("review") or {}).get("capture_session")
        if not due or due > session:
            continue
        if due < session:
            db.relation_event_halt(event["id"], "사전 고정 세션 관측창 경과 — 소급 입력 금지")
            halted += 1
            continue
        snapshot = freeze(event, session=session, observed_at=now, prices=prices, dates=dates)
        if not snapshot.get("ready"):
            db.relation_event_halt(event["id"], snapshot["reason"])
            halted += 1
            continue
        saved += int(db.relation_event_snapshot_add_once(event["id"], snapshot, int(now.timestamp())))
    return {"saved": saved, "halted": halted, "session": session}
