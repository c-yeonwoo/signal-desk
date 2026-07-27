"""호재 선반영 의심 — 이벤트 전 사전 상승만 관측한다(단정 금지).

정성 호재(KB confirmed · direction=positive)의 앵커일 직전 N거래일 수익률이
문턱 이상이면 '선반영 의심'. kind·점수·봇은 건드리지 않는다 — UI 툴팁 전용.

완전 인과 판정은 불가하므로 flag=True여도 '이미 반영됐다'가 아니라
'공시/이벤트 전에 이미 많이 올랐다'는 관측만 말한다.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class PricedInConfig:
    pre_window_days: int = 5          # 이벤트 직전 거래일 수
    min_pre_return_pct: float = 8.0   # 이 이상이면 의심
    max_event_age_days: int = 7       # 너무 옛 이벤트는 무시(캘린더일)
    directions: tuple[str, ...] = ("positive",)


def event_anchor_date(ev: dict) -> str | None:
    """effective_at(공시·발효) 우선, 없으면 detected_at → YYYY-MM-DD(KST)."""
    raw = ev.get("effective_at")
    if raw is None:
        raw = ev.get("detected_at")
    if raw is None:
        return None
    if isinstance(raw, str) and len(raw) >= 10 and raw[4:5] == "-":
        return raw[:10]
    try:
        return datetime.datetime.fromtimestamp(int(raw), _KST).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def pre_event_return_pct(
    dates: list[str], closes: list[float], event_date: str, window: int
) -> float | None:
    """이벤트일 직전 `window`거래일 수익률(%). 이벤트 당일 봉은 제외(당일 반영과 구분)."""
    if window <= 0 or not dates or not closes or len(dates) != len(closes):
        return None
    event_date = str(event_date)[:10]
    end = None  # 이벤트 직전 거래일 인덱스
    for i, d in enumerate(dates):
        ds = str(d)[:10]
        if ds < event_date:
            end = i
        elif ds >= event_date:
            break
    if end is None or end < window:
        return None
    start = end - window
    try:
        a, b = float(closes[start]), float(closes[end])
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0:
        return None
    return (b / a - 1.0) * 100.0


def compute(
    events: list[dict],
    dates: list[str],
    closes: list[float],
    *,
    today: str,
    cfg: PricedInConfig | None = None,
) -> dict | None:
    """조건 충족 시 priced_in dict, 아니면 None."""
    cfg = cfg or PricedInConfig()
    today_d = datetime.date.fromisoformat(str(today)[:10])
    best: dict | None = None
    best_ret = -1e9
    for ev in events or []:
        dire = (ev.get("direction") or "").lower()
        if dire not in cfg.directions:
            continue
        ed = event_anchor_date(ev)
        if not ed:
            continue
        try:
            age = (today_d - datetime.date.fromisoformat(ed)).days
        except ValueError:
            continue
        if age < 0 or age > cfg.max_event_age_days:
            continue
        ret = pre_event_return_pct(dates, closes, ed, cfg.pre_window_days)
        if ret is None or ret < cfg.min_pre_return_pct:
            continue
        if ret > best_ret:
            best_ret = ret
            summary = (ev.get("summary") or ev.get("event_type") or "호재 이벤트")[:80]
            best = {
                "flag": True,
                "pre_return_pct": round(ret, 1),
                "window_days": cfg.pre_window_days,
                "event_date": ed,
                "event_type": ev.get("event_type"),
                "severity": ev.get("severity"),
                "summary": summary,
                "tip_ko": (
                    f"선반영 의심 · 이벤트 전 {cfg.pre_window_days}거래일 "
                    f"{ret:+.1f}% · {ed}"
                ),
            }
    return best


def events_by_ticker(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for ev in events or []:
        t = ev.get("ticker")
        if t:
            out.setdefault(str(t), []).append(ev)
    return out


def annotate_rows(
    rows: list[dict],
    *,
    events_by: dict[str, list[dict]],
    dates_by: dict[str, list[str]],
    closes_by: dict[str, list[float]],
    today: str,
    cfg: PricedInConfig | None = None,
) -> list[dict]:
    for r in rows:
        t = r.get("ticker")
        r["priced_in"] = compute(
            events_by.get(t) or [],
            dates_by.get(t) or [],
            closes_by.get(t) or [],
            today=today,
            cfg=cfg,
        )
    return rows
