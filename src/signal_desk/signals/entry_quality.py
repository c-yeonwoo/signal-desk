"""매수 시그널 에피소드 · 진입 품질 — kind와 별축.

시그널(kind)은 '오늘 매수권이냐'만 말한다. 같은 우선매수라도 발동가 1000원과
추격가 1700원은 진입 품질이 다르다. 이 모듈은 PIT `signal_history`로 연속 매수권
구간(에피소드)의 최초 발동일·발동가를 찾고, 현재가 대비 추격도를 등급화한다.

- kind를 바꾸지 않는다(게이트 관망과 혼동 금지).
- 봇 미반영 — UI 툴팁·추후 shadow용. 임계값은 여기 Config에만.
"""

from __future__ import annotations

from dataclasses import dataclass

from signal_desk.signals.engine import is_buy

_QUALITY_KO = {
    "fresh": "신선",
    "ok": "여유",
    "extended": "추격",
    "late": "늦음",
}


@dataclass(frozen=True)
class EntryQualityConfig:
    """진입 품질 임계 — 하드코딩 분산 금지. 봇 반영 전 shadow로 캘리브레이션."""
    fresh_run_up_pct: float = 5.0       # 발동 대비 +N% 이하면 신선
    fresh_age_days: int = 2             # 또는 발동 후 N거래일 이내
    ok_run_up_pct: float = 12.0
    extended_run_up_pct: float = 25.0   # 초과면 늦음(목표가 없을 때)
    late_remain_upside_pct: float = 5.0  # 남은 여력 ≤N%면 늦음(목표가 있을 때)


def _price_on(dates: list[str], closes: list[float], day: str) -> float | None:
    if not dates or not closes or len(dates) != len(closes):
        return None
    # 날짜 키가 짧을 수 있음(YYYY-MM-DD)
    day = str(day)[:10]
    for d, c in zip(dates, closes):
        if str(d)[:10] == day:
            try:
                v = float(c)
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None
    return None


def _episode_from_kinds(days_kinds: list[tuple[str, str]]) -> tuple[str, str] | None:
    """끝에서부터 연속 매수권 구간의 (fire_date, fire_kind). 없으면 None."""
    if not days_kinds:
        return None
    i = len(days_kinds) - 1
    if not is_buy(days_kinds[i][1]):
        return None
    fire_i = i
    while fire_i > 0 and is_buy(days_kinds[fire_i - 1][1]):
        fire_i -= 1
    return days_kinds[fire_i][0], days_kinds[fire_i][1]


def classify_quality(run_up_pct: float, age_days: int,
                     remain_upside_pct: float | None = None,
                     cfg: EntryQualityConfig | None = None) -> str:
    cfg = cfg or EntryQualityConfig()
    if remain_upside_pct is not None and remain_upside_pct <= cfg.late_remain_upside_pct:
        return "late"
    if age_days <= cfg.fresh_age_days or run_up_pct <= cfg.fresh_run_up_pct:
        return "fresh"
    if run_up_pct <= cfg.ok_run_up_pct:
        return "ok"
    if run_up_pct <= cfg.extended_run_up_pct:
        return "extended"
    return "late"


def compute(ticker: str, *, kind: str, price: float | None,
            hist_days: list[tuple[str, str]],
            dates: list[str], closes: list[float],
            today: str,
            remain_upside_pct: float | None = None,
            cfg: EntryQualityConfig | None = None) -> dict | None:
    """단일 종목 진입 품질. 매수권이 아니면 None.

    hist_days: 과거 PIT [(date, kind), ...] 오름차순(오늘 미포함 가능).
    오늘 kind가 매수면 가상 일자로 이어 붙인다(장중 재계산 반영).
    """
    if not is_buy(kind) or not price or price <= 0:
        return None
    today = str(today)[:10]
    series = [(str(d)[:10], k) for d, k in hist_days if str(d)[:10] < today]
    if not series or series[-1][0] != today:
        series = [*series, (today, kind)]
    else:
        series = [*series[:-1], (today, kind)]
    ep = _episode_from_kinds(series)
    if not ep:
        return None
    fire_date, _ = ep
    fire_price = _price_on(dates, closes, fire_date)
    if fire_price is None and fire_date == today:
        fire_price = float(price)
    if fire_price is None or fire_price <= 0:
        return None
    run_up = (float(price) / fire_price - 1.0) * 100.0
    ep_len = 0
    for _d, k in reversed(series):
        if not is_buy(k):
            break
        ep_len += 1
    age_days = max(0, ep_len - 1)  # 당일 발동=0
    quality = classify_quality(run_up, age_days, remain_upside_pct, cfg)
    out = {
        "quality": quality,
        "quality_ko": _QUALITY_KO[quality],
        "fire_date": fire_date,
        "fire_price": round(float(fire_price), 4),
        "run_up_pct": round(run_up, 1),
        "age_days": age_days,
    }
    if remain_upside_pct is not None:
        out["remain_upside_pct"] = round(remain_upside_pct, 1)
    return out


def history_kinds_by_ticker(df) -> dict[str, list[tuple[str, str]]]:
    """load_signal_history DataFrame → {ticker: [(date, kind), ...] 오름차순}."""
    if df is None or getattr(df, "empty", True):
        return {}
    if not {"date", "ticker", "kind"} <= set(df.columns):
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    sub = df[["date", "ticker", "kind"]].copy()
    sub["date"] = sub["date"].astype(str).str[:10]
    sub = sub.sort_values(["ticker", "date"])
    for t, g in sub.groupby("ticker", sort=False):
        out[str(t)] = [(str(r.date), str(r.kind)) for r in g.itertuples(index=False)]
    return out


def annotate_rows(rows: list[dict], *,
                  hist_by: dict[str, list[tuple[str, str]]],
                  dates_by: dict[str, list[str]],
                  closes_by: dict[str, list[float]],
                  today: str,
                  cfg: EntryQualityConfig | None = None) -> list[dict]:
    """리스트/상세 행에 `entry` 필드를 채운다(매수권만)."""
    for r in rows:
        t = r.get("ticker")
        entry = compute(
            t,
            kind=str(r.get("kind") or ""),
            price=r.get("price"),
            hist_days=hist_by.get(t) or [],
            dates=dates_by.get(t) or [],
            closes=closes_by.get(t) or [],
            today=today,
            remain_upside_pct=r.get("remain_upside_pct"),
            cfg=cfg,
        )
        r["entry"] = entry
    return rows
