"""사전 동결한 관점 조합의 가격 전진 비교. 주문·승격 권한이 없는 연구 도구."""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from signal_desk import market_clock

VERSION = "lens-forward-v1"
SLOTS = 10
HOLD_SESSIONS = 5
SIDE_COST = 0.002  # 체결·수수료의 보수적 연구 가정; 실측 비용이 아니다.
MIN_INDEPENDENT_COHORTS = 20
_ZONES = {"kr": "Asia/Seoul", "us": "America/New_York"}
MIN_FRESH_FRACTION = .95
SCHEDULE_START = "2026-10-05"  # 이 배포 다음의 첫 완전한 주; 과거 소급 동결 금지.
COMBOS = {
    "base": "기본 매수 후보",
    "event": "확인된 호재까지",
    "entry": "진입 품질까지",
    "event_entry": "호재·진입 모두",
}


def iso_week(market: str, observed_at: int) -> str:
    local = dt.datetime.fromtimestamp(observed_at, dt.timezone.utc).astimezone(ZoneInfo(_ZONES[market]))
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def scheduled_capture_window(market: str, now: dt.datetime) -> dict | None:
    """각 시장 첫 거래일 마감 후 정해진 KST 16~21시에만 표본을 만든다.

    미국은 그 거래일의 다음 KST 날짜에 국내 일일 시세 갱신이 끝난 뒤 동결한다.
    창을 놓치면 사후 유리한 시각을 선택하지 않고 그 주는 결손으로 둔다.
    """
    if market not in _ZONES or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("valid market and timezone-aware now required")
    session = market_clock.latest_completed_session(market, now)
    if not session or session < SCHEDULE_START:
        return None
    previous = market_clock.previous_session(market, session)
    current_date = dt.date.fromisoformat(session)
    if previous and dt.date.fromisoformat(previous).isocalendar()[:2] == current_date.isocalendar()[:2]:
        return None
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    capture_date = close.astimezone(ZoneInfo("Asia/Seoul")).date()
    if local.date() != capture_date or not 16 <= local.hour < 21:
        return None
    year, week, _ = current_date.isocalendar()
    return {"market": market, "session": session, "iso_week": f"{year}-W{week:02d}"}


def expected_capture_weeks(market: str, now: dt.datetime) -> list[str]:
    """정시 창이 끝난 거래 주차. 서비스가 꺼져 놓친 주도 분모에서 숨기지 않는다."""
    if market not in _ZONES or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("valid market and timezone-aware now required")
    start = dt.date.fromisoformat(SCHEDULE_START)
    end = now.astimezone(ZoneInfo(_ZONES[market])).date()
    if end < start:
        return []
    calendar = market_clock._calendar(market)
    end = min(end, calendar.last_session.date())
    try:
        sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
    except (KeyError, ValueError):
        return []
    weeks = []
    for stamp in sessions:
        day = stamp.date()
        year, week, _ = day.isocalendar()
        key = f"{year}-W{week:02d}"
        if key in weeks:
            continue
        close = calendar.schedule.loc[day.isoformat()]["close"].to_pydatetime()
        capture_day = close.astimezone(ZoneInfo("Asia/Seoul")).date()
        window_end = dt.datetime.combine(capture_day, dt.time(21), ZoneInfo("Asia/Seoul"))
        if now >= window_end:
            weeks.append(key)
    return weeks


def _close_map(history: list[dict]) -> dict[str, float]:
    out = {}
    for item in history:
        try:
            value = float(item["close"])
            if math.isfinite(value) and value > 0:
                out[str(item["date"])[:10]] = value
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _selected(row: dict, combo: str) -> bool:
    lenses = row.get("lenses") or {}
    if combo in ("event", "event_entry") and (lenses.get("event") or {}).get("verdict") != "pass":
        return False
    if combo in ("entry", "event_entry") and (lenses.get("entry") or {}).get("verdict") != "pass":
        return False
    return True


def _candidate(row: dict, expected_bar: str | None) -> bool:
    lenses = row.get("lenses") or {}
    quant = lenses.get("quant") or {}
    return (row.get("kind") in ("BUY", "STRONG_BUY")
            and quant.get("verdict") == "pass" and quant.get("as_of") == expected_bar)


def _drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = max(worst, 1.0 - equity / peak)
    return worst


def evaluate(cohorts: list[dict], market: str, price_loader, *, now: dt.datetime | None = None,
             price_marker=None, capture_source: str = "unverified",
             expected_weeks: list[str] | None = None) -> dict:
    """미래가 이미 완료된 표본만 읽고 동일 원본 후보를 네 방식으로 비교한다.

    종가 매수/매도는 실전 체결 가능 가격을 보장하지 않는다. 후보 10칸 중 미선정 칸은
    현금으로 둬 필터가 적은 종목만 고른 데 따른 분모 축소 편향을 피한다.
    """
    if market not in _ZONES:
        raise ValueError("invalid lens market")
    if capture_source not in ("scheduled", "unverified"):
        raise ValueError("invalid capture source")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    latest = market_clock.latest_completed_session(market, now)
    episodes: list[dict] = []
    exclusions: dict[str, int] = {}
    cache: dict[str, dict[str, float]] = {}
    previous_exit = ""

    def skip(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for cohort in sorted(cohorts, key=lambda c: int(c["observed_at"])):
        snapshot = cohort.get("snapshot") or {}
        observed = dt.datetime.fromtimestamp(int(cohort["observed_at"]), dt.timezone.utc)
        local_day = observed.astimezone(ZoneInfo(_ZONES[market])).date().isoformat()
        completed_at_decision = market_clock.latest_completed_session(market, observed)
        anchor = local_day if market_clock.is_session(market, local_day) else completed_at_decision
        sessions = market_clock.next_sessions(market, anchor, HOLD_SESSIONS + 1) if anchor else []
        if snapshot.get("market") != market or snapshot.get("mode") != "read_only" or not sessions:
            skip("판단 시점·시장 불명")
            continue
        entry, exit_day = sessions[0], sessions[-1]
        if not latest or exit_day > latest:
            skip("성과 관측 대기")
            continue
        if entry <= previous_exit:
            skip("보유 구간 겹침")
            continue
        raw = snapshot.get("rows") or []
        # 원본 순위만 사용한다. 과거 성과로 재정렬하거나 누락 데이터로 후보를 메우지 않는다.
        ranked = sorted(enumerate(raw), key=lambda pair: (
            int(pair[1]["rank"]) if str(pair[1].get("rank", "")).isdigit() else 999999,
            pair[0]))
        buy_rows = [row for _, row in ranked if row.get("kind") in ("BUY", "STRONG_BUY")]
        if any(((row.get("lenses") or {}).get("quant") or {}).get("as_of") != completed_at_decision
               for row in buy_rows):
            skip("매수 후보 가격 기준 불일치")
            continue
        picked = [row for row in buy_rows if _candidate(row, completed_at_decision)][:SLOTS]
        slot_returns = {}
        missing = False
        for row in picked:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                missing = True
                break
            if ticker not in cache:
                try:
                    cache[ticker] = _close_map(price_loader(ticker))
                except Exception:  # noqa: BLE001 — 원천 조회 실패는 연구 표본 제외
                    cache[ticker] = {}
            prices = cache[ticker]
            entry_price, exit_price = prices.get(entry), prices.get(exit_day)
            if price_marker:
                try:
                    entry_price = price_marker(market, cohort["snapshot_id"], ticker, entry, entry_price)
                    exit_price = price_marker(market, cohort["snapshot_id"], ticker, exit_day, exit_price)
                except Exception:  # noqa: BLE001 — 원장 실패면 현재값으로 계속 평가하지 않는다
                    missing = True
                    break
            if entry_price is None or exit_price is None:
                missing = True
                break
            slot_returns[ticker] = exit_price / entry_price - 1.0 - 2 * SIDE_COST
        if missing:
            skip("진입·청산 종가 누락")
            continue
        comparison = {}
        for combo in COMBOS:
            selected = [row for row in picked if _selected(row, combo)]
            comparison[combo] = {
                "selected": len(selected),
                "coverage": len(selected) / SLOTS,
                "net_return": sum(slot_returns[row["ticker"]] for row in selected) / SLOTS,
                "turnover": 2 * len(selected) / SLOTS,
            }
        episodes.append({"week": cohort["iso_week"], "snapshot_id": cohort["snapshot_id"],
                         "signal_policy_id": snapshot.get("signal_policy_id"),
                         "observed_at": cohort["observed_at"], "entry": entry, "exit": exit_day,
                         "candidate_count": len(picked), "combos": comparison})
        previous_exit = exit_day

    summary = {}
    for combo, label in COMBOS.items():
        returns = [ep["combos"][combo]["net_return"] for ep in episodes]
        paired = [value - ep["combos"]["base"]["net_return"]
                  for value, ep in zip(returns, episodes)]
        n = len(returns)
        summary[combo] = {
            "label": label,
            "episodes": n,
            "mean_net_return": sum(returns) / n if n else None,
            "mean_excess_vs_base": sum(paired) / n if n else None,
            "mean_coverage": sum(ep["combos"][combo]["coverage"] for ep in episodes) / n if n else None,
            "mean_turnover": sum(ep["combos"][combo]["turnover"] for ep in episodes) / n if n else None,
            "max_episode_drawdown": _drawdown(returns) if n else None,
        }
    expected = expected_weeks if expected_weeks is not None else []
    observed_weeks = {str(c.get("iso_week")) for c in cohorts}
    return {"version": VERSION, "market": market, "mode": "research_only",
            "live_eligible": False, "capture_source": capture_source,
            "cohorts_seen": len(cohorts), "independent_episodes": len(episodes),
            "expected_capture_weeks": len(expected),
            "missing_capture_weeks": [w for w in expected if w not in observed_weeks],
            "capture_coverage": len(observed_weeks.intersection(expected)) / len(expected) if expected else None,
            "minimum_episodes": MIN_INDEPENDENT_COHORTS,
            "cost_per_side": SIDE_COST, "slots": SLOTS, "hold_sessions": HOLD_SESSIONS,
            "exclusions": exclusions, "summary": summary, "episodes": episodes,
            "note": "정시 원장만 비교합니다. 과거 조회 기반 표본은 제외합니다. 실제 체결·배당·세금·기업행동 보정이 없는 종가 연구 대용치이며 주문에 연결되지 않습니다."}
