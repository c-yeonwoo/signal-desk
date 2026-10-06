"""Read-only daily market card from dated closes and already-computed signals.

This is presentation, not a second trading engine.  Never infer a fresh market
state from old bars or use the card's prose as an order input.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict
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
    sector_by_ticker: Mapping[str, str] | None = None,
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
        "today_headline": "오늘 시장을 아직 요약할 수 없어요",
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
    # 오늘 등락은 실제로 직전 거래일과 날짜가 이어지는 관측치만 사용한다.
    # 종목마다 마지막 두 행이 서로 다른 날짜라면 같은 날의 상승·하락으로 세지 않는다.
    daily_changes: dict[str, float] = {}
    if previous:
        for ticker, ps in current.items():
            ds = dates.get(ticker) or ()
            if len(ds) >= 2 and str(ds[-2])[:10] == previous:
                daily_changes[ticker] = (float(ps[-1]) / float(ps[-2]) - 1) * 100
    advances = sum(change > 0 for change in daily_changes.values())
    declines = sum(change < 0 for change in daily_changes.values())
    unchanged = len(daily_changes) - advances - declines
    daily_count = len(daily_changes)
    advance_pct = round(advances / daily_count * 100, 1) if daily_count else None
    ordered_daily = sorted(daily_changes.values())
    median_change = (ordered_daily[daily_count // 2] if daily_count % 2 else
                     (ordered_daily[daily_count // 2 - 1] + ordered_daily[daily_count // 2]) / 2
                     if daily_count else None)
    if median_change is not None:
        median_change = round(median_change, 2)
    above = sum(ps[-1] > sum(ps[-60:]) / 60 for ps in current.values())
    rising = sum(ps[-1] > ps[-21] for ps in current.values())
    rising_pct = round(rising / covered * 100, 1)
    facts = []
    if daily_count:
        facts.extend([
            {"label": "오늘 오른 관찰 종목", "value": f"{advances}/{daily_count}개",
             "percent": advance_pct, "detail": f"내린 {declines}개 · 보합 {unchanged}개",
             "technical": "직전 거래일 종가와 비교", "as_of": expected,
             "source": "저장된 종가"},
            {"label": "관찰 종목의 오늘 중앙 변동", "value": f"{median_change:+.2f}%",
             "detail": "관찰 종목을 절반씩 나눴을 때 가운데에 있는 등락률",
             "technical": "시장 지수 수익률은 아님", "as_of": expected,
             "source": "저장된 종가"},
        ])
    else:
        unknown_daily = "직전 거래일과 이어지는 종가가 부족해 오늘 상승·하락 폭을 계산하지 못했습니다."
    facts.extend([
        {"label": "최근 평균보다 높은 종목", "value": f"{above}/{covered}개",
         "percent": reading["breadth_pct"],
         "detail": "관찰 종목의 가격 흐름", "technical": f'최근 60개 종가 평균 상회 {reading["breadth_pct"]:.1f}%',
         "as_of": expected, "source": "저장된 종가"},
        {"label": "20개 종가 전보다 오른 종목", "value": f"{rising}/{covered}개",
         "percent": rising_pct, "detail": "최근 가격 변화의 방향",
         "technical": f'20개 종가 간 평균 변동 {reading["avg_momentum_pct"]:+.2f}%',
         "as_of": expected, "source": "저장된 종가"},
    ])
    unknown = ["개별 종목 공시·뉴스의 영향과 다음 거래일 방향은 이 카드만으로 알 수 없습니다."]
    if not daily_count:
        unknown.append(unknown_daily)
    elif daily_count < covered:
        unknown.append(f"{covered}개 분석 종목 중 {covered - daily_count}개는 직전 거래일 종가가 없어 오늘 등락 계산에서 제외했습니다.")
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
    # 화면의 대표 문장은 매매 판정이 아니라 설명이다. 일부 종목만 들어온
    # 경우에도 전체 시장의 방향처럼 읽히지 않도록 범위를 먼저 확인한다.
    directional_coverage = daily_count / len(universe)
    if daily_count and directional_coverage < 0.8:
        today_headline = "오늘 시장 방향은 자료가 부족해요"
        summary = (f"관찰 종목 {len(universe)}개 중 {daily_count}개만 직전 거래일과 비교할 수 있어요. "
                   f"확인한 종목은 상승 {advances}개·하락 {declines}개·보합 {unchanged}개지만 "
                   "이 결과를 전체 관찰 종목의 흐름으로 넓혀 말하지 않겠습니다.")
    elif daily_count:
        imbalance_pct = abs(advances - declines) / daily_count * 100
        if imbalance_pct < 15:
            today_headline = "오늘 오른 종목과 내린 종목이 비슷해요"
            today_interpretation = "관찰 종목의 등락이 갈려 한쪽으로 기울었다고 보기 어려워요."
        elif advances > declines:
            today_headline = "오늘은 오른 종목이 더 많았어요"
            today_interpretation = f"관찰 종목 {daily_count}개 중 {advances}개가 직전 거래일보다 올랐어요."
        else:
            today_headline = "오늘은 내린 종목이 더 많았어요"
            today_interpretation = f"관찰 종목 {daily_count}개 중 {declines}개가 직전 거래일보다 내렸어요."
        if daily_count < len(universe):
            today_headline = f"확인한 {daily_count}개에서는 " + today_headline.removeprefix("오늘은 ").removeprefix("오늘 ")
        trend_context = ("다만 최근 평균 아래인 종목이 더 많아요." if reading["breadth_pct"] < 40 else
                         "최근 평균 위인 종목이 더 많아요." if reading["breadth_pct"] >= 60 else
                         "최근 평균 위·아래 종목은 비슷해요.")
        summary = f"{today_interpretation} {trend_context}"
    else:
        today_headline = "오늘 등락은 아직 확인하기 어려워요"
        summary = "직전 거래일과 이어지는 종가가 부족해 오늘 방향을 보류했어요."

    sector_summary = []
    if sector_by_ticker and daily_changes:
        grouped: dict[str, list[float]] = defaultdict(list)
        for ticker, change in daily_changes.items():
            sector = sector_by_ticker.get(ticker)
            if sector:
                grouped[str(sector)].append(change)
        eligible = []
        for sector, changes in grouped.items():
            if len(changes) < 3:
                continue
            ordered = sorted(changes)
            size = len(ordered)
            middle = (ordered[size // 2] if size % 2 else
                      (ordered[size // 2 - 1] + ordered[size // 2]) / 2)
            eligible.append({"sector": sector, "count": size, "median_change_pct": round(middle, 2)})
        sector_summary = sorted(eligible, key=lambda item: (item["median_change_pct"], item["sector"]), reverse=True)
        if sector_summary and directional_coverage >= 0.8:
            best, weakest = sector_summary[0], sector_summary[-1]
            best_action = "상승 폭이 컸어요" if best["median_change_pct"] > 0 else "하락 폭이 작았어요"
            weak_action = ("상승 폭이 작았어요" if weakest["median_change_pct"] > 0 else
                           "하락 폭이 컸어요" if weakest["median_change_pct"] < 0 else "보합이었어요")
            summary += (f" 업종별로는 {best['sector']}({best['median_change_pct']:+.2f}%)의 {best_action}. "
                        f"{weakest['sector']}({weakest['median_change_pct']:+.2f}%)는 {weak_action}.")
        elif sector_summary:
            sector_summary = []
            unknown.append("당일 비교 가능한 종목이 충분하지 않아 업종별 방향도 요약하지 않습니다.")
        elif market == "kr":
            unknown.append("업종별로 비교할 수 있는 종목이 충분하지 않습니다. 종목이 3개 이상인 업종만 표시합니다.")
    elif market == "us":
        unknown.append("해외 업종 분류의 기준 시점을 확인할 수 없어 업종별 비교는 보류했습니다.")

    # 기존 trend-only headline은 API 소비자 호환을 위해 둔다. 사용자 화면과 그림은 today_headline을 쓴다.
    if reading["breadth_pct"] <= 40:
        headline = "관찰 종목 다수의 흐름이 약해요"
    elif reading["breadth_pct"] >= 60:
        headline = "관찰 종목 다수의 흐름이 견조해요"
    else:
        headline = "관찰 종목의 흐름이 엇갈려요"
    basis = ("오늘 등락은 직전 거래일 종가와 비교 · 추세는 최근 60개 종가 평균 기준 · 관찰 유니버스 한정"
             if market == "kr" else
             "오늘 등락은 직전 거래일 종가와 비교 · 추세는 최근 60개 종가 평균 기준 · 미국 관찰 유니버스 한정")
    return {**base, "status": "partial" if covered < len(universe) else "ready",
            "state": state, "state_basis": basis, "headline": headline,
            "today_headline": today_headline, "summary": summary,
            "daily_coverage": {"available": daily_count, "analyzed": covered},
            "sector_summary": sector_summary,
            "facts": facts, "selection": current_selection, "unknown": unknown}
