"""시장별 국면 노출의 전진 관측. 탐색 지표이며 주문 정책 승격 판정이 아니다."""

from __future__ import annotations

import math

from signal_desk import market_clock, store
from signal_desk.signals import regime


def report(market: str) -> dict:
    if market not in ("kr", "us"):
        raise ValueError("unsupported market")
    history = store.regime_history(market)
    returns = store.market_return_by_date(market)
    pairs = []
    excluded = {"unready": 0, "session_or_price_missing": 0}
    for row in history:
        day = str(row.get("date") or "")
        next_days = market_clock.next_sessions(market, day, 1)
        exposure = row.get("exposure")
        if row.get("ready") is False or not isinstance(exposure, (int, float)) or \
                not math.isfinite(exposure) or not 0 <= exposure <= 1:
            excluded["unready"] += 1
            continue
        if not next_days or next_days[0] not in returns or not math.isfinite(returns[next_days[0]]):
            excluded["session_or_price_missing"] += 1
            continue
        pairs.append((row, returns[next_days[0]]))
    timing = regime.timing_skill([r["exposure"] for r, _ in pairs],
                                 [forward for _, forward in pairs])
    return {"market": market, "mode": "forward_observation", "binding": False,
            "live_eligible": False, "observed_sessions": len(history),
            "paired_sessions": len(pairs), "excluded": excluded,
            "timing": timing,
            "by_regime": regime.exposure_by_regime([r.get("regime") for r, _ in pairs],
                                                   [forward for _, forward in pairs]),
            "note": "각 시장의 다음 거래 세션 수익과만 비교한 탐색 지표. 현재 캐시 종목의 동일가중 수익률은 "
                    "PIT 시장 기준선이 아니며 독립 표본·비용·낙폭 검증도 아니다. 여기서 매매 규칙은 변경되지 않는다."}
