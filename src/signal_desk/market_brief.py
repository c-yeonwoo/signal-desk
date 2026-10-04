"""Read-only daily market card from dated closes and already-computed signals.

This is presentation, not a second trading engine.  Never infer a fresh market
state from old bars or use the card's prose as an order input.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping, Sequence

from signal_desk.signals import regime


def _last_date_counts(dates: Mapping[str, Sequence[str]], tickers: Sequence[str]) -> Counter:
    return Counter(str(dates[t][-1])[:10] for t in tickers if dates.get(t))


def _nearby_session(day: str | None, expected: str | None, previous: str | None) -> bool:
    return bool(day and day in {expected, previous})


def build(
    market: str,
    *,
    prices: Mapping[str, Sequence[float]],
    dates: Mapping[str, Sequence[str]],
    tickers: Sequence[str],
    expected: str | None,
    previous: str | None = None,
    flow: Mapping | None = None,
    macro_indicators: Sequence[Mapping] = (),
    selection: Mapping | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Produce a small, source-dated card. Missing close dates fail closed.

    ``expected`` is the latest *completed* exchange session, not today's wall
    clock date. One stale ticker is disclosed; a stale modal date withholds the
    market-state claim altogether. Only same-session closes enter the breadth.
    """
    if market not in {"kr", "us"}:
        raise ValueError("unsupported market")
    now = now or dt.datetime.now(dt.timezone.utc)
    universe = list(dict.fromkeys(str(t) for t in tickers))
    counts = _last_date_counts(dates, universe)
    observed = max(counts, key=lambda day: (counts[day], day)) if counts else None
    base = {
        "market": market,
        "expected_as_of": expected,
        "price_as_of": observed,
        "generated_at": now.isoformat(),
        "universe_count": len(universe),
        "price_count": counts.get(expected, 0) if expected else 0,
        "state": None,
        "headline": "오늘 시장을 아직 요약할 수 없어요",
        "summary": "종가를 확인한 뒤 관찰 종목의 흐름을 보여드릴게요.",
        "facts": [],
        "selection": None,
        "unknown": [],
        "not_order_advice": True,
    }
    if not expected or not universe or not observed:
        return {**base, "status": "unavailable", "unknown": ["시장 날짜나 종가를 확인할 수 없습니다."]}
    if observed != expected:
        return {**base, "status": "stale", "unknown": [
            f"최근 완료 거래일은 {expected}이지만 저장된 종가의 주된 날짜는 {observed}입니다.",
            "가격 갱신 전에는 오늘의 시장 상태와 매수 현황을 표시하지 않습니다.",
        ]}

    current = {}
    for ticker in universe:
        ds, ps = dates.get(ticker) or (), prices.get(ticker) or ()
        if (ds and ps and len(ds) == len(ps) and str(ds[-1])[:10] == expected
                and len(ps) >= 61 and all(isinstance(p, (int, float)) and math.isfinite(p)
                                          and p > 0 for p in ps[-61:])):
            current[ticker] = list(ps)
    reading = regime.classify(current)
    if not reading["ready"]:
        return {**base, "status": "unavailable", "unknown": [
            f"{expected} 종가는 있으나 최근 60개 종가 평균과 20개 종가 변화를 계산할 이력이 부족합니다."
        ]}

    covered = reading["n"]
    above = sum(ps[-1] > sum(ps[-60:]) / 60 for ps in current.values())
    rising = sum(ps[-1] > ps[-21] for ps in current.values())
    rising_pct = round(rising / covered * 100, 1)
    facts = [
        {"label": "최근 평균보다 높은 종목", "value": f"{above}/{covered}개",
         "percent": reading["breadth_pct"],
         "detail": "관찰 종목의 가격 흐름", "technical": f'최근 60개 종가 평균 상회 {reading["breadth_pct"]:.1f}%',
         "as_of": expected, "source": "저장된 종가"},
        {"label": "20개 종가 전보다 오른 종목", "value": f"{rising}/{covered}개",
         "percent": rising_pct, "detail": "최근 가격 변화의 방향",
         "technical": f'20개 종가 간 평균 변동 {reading["avg_momentum_pct"]:+.2f}%',
         "as_of": expected, "source": "저장된 종가"},
    ]
    unknown = ["개별 종목 공시·뉴스의 영향과 다음 거래일 방향은 이 카드만으로 알 수 없습니다."]
    if covered < len(universe):
        unknown.append(f"전체 {len(universe)}종목 중 {len(universe) - covered}종목은 날짜가 다르거나 계산 이력이 부족해 제외했습니다.")

    if market == "kr":
        flow = flow or {}
        flow_day = str(flow.get("as_of") or "")[:10]
        net = flow.get("smart_net_20d")
        if (_nearby_session(flow_day, expected, previous) and isinstance(net, (int, float))
                and not isinstance(net, bool) and math.isfinite(net)):
            facts.append({"label": "외국인·기관 자금", "value": f"{net:+.2f}조원",
                          "detail": "코스피 전체 20일 순매수 합계",
                          "technical": "양수는 순매수, 음수는 순매도", "as_of": flow_day,
                          "source": "시장 수급"})
        else:
            unknown.append("외국인·기관 수급은 최근 완료 거래일 기준으로 확인되지 않았습니다.")
    else:
        nasdaq = next((i for i in macro_indicators if i.get("key") == "NASDAQCOM"), None)
        nas_day = str((nasdaq or {}).get("asof") or "")[:10]
        change = (nasdaq or {}).get("change")
        if (_nearby_session(nas_day, expected, previous) and isinstance(change, (int, float))
                and not isinstance(change, bool) and math.isfinite(change)):
            facts.append({"label": "나스닥 지수 변화", "value": f"{change:+.2f}%",
                          "detail": "미국 시장 참고 지표", "technical": "관찰 종목 전체의 수익률은 아님",
                          "as_of": nas_day,
                          "source": "FRED", "source_url": (nasdaq or {}).get("source_url")})
        else:
            unknown.append("나스닥 참고 지표는 해당 거래일 근처의 관측을 확인하지 못했습니다.")

    # The selection is a separately time-stamped *current* engine reading. It
    # never changes the historical close-based market state above.
    current_selection = None
    if selection and covered == len(universe):
        current_selection = {
            "buy_count": int(selection.get("buy_count") or 0),
            "strong_buy_count": int(selection.get("strong_buy_count") or 0),
            "slots": selection.get("slots"),
            "computed_at": selection.get("computed_at"),
        }
    if covered < len(universe):
        unknown.append("일부 종목의 종가가 빠져 매수 판정 건수는 이 카드에서 보류합니다.")
    state = reading["regime"] if market == "kr" else f'평균가격 위 종목 {reading["breadth_pct"]:.1f}%'
    if reading["breadth_pct"] <= 40:
        headline = "관찰 종목 다수의 흐름이 약해요"
        interpretation = "상승 흐름이 넓게 퍼졌다고 보기는 어려워요."
    elif reading["breadth_pct"] >= 60:
        headline = "관찰 종목 다수의 흐름이 견조해요"
        interpretation = "최근 평균보다 높은 종목이 많은 편이에요."
    else:
        headline = "관찰 종목의 흐름이 엇갈려요"
        interpretation = "강한 종목과 약한 종목이 섞여 있어요."
    summary = f"{covered}개 중 {above}개의 가격이 각자의 최근 평균보다 높아요. {interpretation}"
    basis = ("국내 유니버스의 최근 60개 종가 평균·20개 종가 변화" if market == "kr"
             else "미국 유니버스 종가 관측 · 국내 국면 분류와 별개")
    return {**base, "status": "partial" if covered < len(universe) else "ready",
            "state": state, "state_basis": basis, "headline": headline, "summary": summary,
            "facts": facts, "selection": current_selection, "unknown": unknown}
