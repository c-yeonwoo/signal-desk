"""실행 품질 게이트 — 추격(late)·선반영 의심이면 BUY→HOLD.

점수(score)는 그대로 두고 kind만 강등한다(급락·악재 게이트와 동일).
봇은 is_buy만 보므로 엔진/후처리에서 kind를 내려야 매수가 멈춘다.
UI·봇 공통 — `apply_from_store`를 evaluate 직후 한 번 호출한다.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from signal_desk.signals import entry_quality, priced_in
from signal_desk.signals.engine import HOLD, BUY_KINDS, SignalResult, is_buy

log = logging.getLogger("signal_desk.execution_gate")
_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class ExecutionGateConfig:
    block_late: bool = True
    block_priced_in: bool = True
    # extended(추격)는 사이즈 축소 후보 — 기본은 하드 차단 안 함
    block_extended: bool = False


def _demote(r: SignalResult, tag: str, detail: str) -> None:
    if r.kind in BUY_KINDS:
        r.kind = HOLD
    r.gate_blocked = True
    r.rank_eligible = False
    reason = f"[{tag}] {detail}"
    if reason not in (r.reasons or []):
        r.reasons = [*(r.reasons or []), reason]


def apply(
    results: list[SignalResult],
    *,
    hist_by: dict[str, list[tuple[str, str]]],
    dates_by: dict[str, list[str]],
    closes_by: dict[str, list[float]],
    events_by: dict[str, list[dict]],
    today: str,
    cfg: ExecutionGateConfig | None = None,
) -> list[SignalResult]:
    """매수권 결과에 진입·선반영 게이트 적용(in-place)."""
    cfg = cfg or ExecutionGateConfig()
    today = str(today)[:10]
    for r in results:
        if not is_buy(r.kind):
            continue
        closes = closes_by.get(r.ticker) or []
        dates = dates_by.get(r.ticker) or []
        try:
            price = float(closes[-1]) if closes else None
        except (TypeError, ValueError, IndexError):
            price = None
        entry = entry_quality.compute(
            r.ticker, kind=r.kind, price=price,
            hist_days=hist_by.get(r.ticker) or [],
            dates=dates, closes=closes, today=today,
        )
        if entry:
            q = entry.get("quality")
            if cfg.block_late and q == "late":
                _demote(
                    r, "추격",
                    f"진입 늦음(발동가 대비 {entry.get('run_up_pct', 0)}%, "
                    f"{entry.get('age_days', 0)}일) — 신규 매수 보류",
                )
                continue
            if cfg.block_extended and q == "extended":
                _demote(
                    r, "추격",
                    f"진입 추격(발동가 대비 {entry.get('run_up_pct', 0)}%) — 신규 매수 보류",
                )
                continue
        if cfg.block_priced_in:
            pi = priced_in.compute(
                events_by.get(r.ticker) or [],
                dates, closes, today=today,
            )
            if pi and pi.get("flag"):
                _demote(
                    r, "선반영",
                    f"호재 전 사전상승 {pi.get('pre_return_pct', 0)}% "
                    f"({pi.get('event_date')}) — 신규 매수 보류",
                )
    return results


def apply_from_store(
    results: list[SignalResult],
    *,
    market: str = "kospi",
    today: str | None = None,
    cfg: ExecutionGateConfig | None = None,
) -> list[SignalResult]:
    """store/db에서 시계열·이벤트를 읽어 게이트 적용. 실패해도 원본 결과 유지."""
    if not results:
        return results
    today = today or datetime.datetime.now(_KST).date().isoformat()
    try:
        from signal_desk import db, store
        hist_by = entry_quality.history_kinds_by_ticker(store.load_signal_history())
        if market == "us":
            closes_by = store.load_us_price_series()
            dates_by = store.load_us_dates_by_ticker()
        else:
            closes_by = store.load_price_series()
            dates_by = store.load_dates_by_ticker()
        events = db.kb_events_active()
        events_by = priced_in.events_by_ticker(events)
        return apply(
            results, hist_by=hist_by, dates_by=dates_by, closes_by=closes_by,
            events_by=events_by, today=today, cfg=cfg,
        )
    except Exception as e:
        log.warning("실행 게이트 스킵: %s", type(e).__name__)
        return results
