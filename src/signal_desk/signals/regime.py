"""시장 국면(강세·과열·조정·약세) 판정 — 유니버스 전체의 이동평균 상회 비율(breadth)과
평균 N일 모멘텀만으로 근사한다. 지수·금리·거래대금 데이터는 별도 API 승인/구독이 필요해
범위 밖(BACKLOG #5·#6의 정식 지수 기반 판정이 붙기 전까지의 1차 근사) — 이미 갖고 있는
유니버스 종가 시계열만으로 계산 가능해 새 데이터 소스가 필요 없다.
"""

from __future__ import annotations

from dataclasses import dataclass

from signal_desk.signals import indicators as ind


@dataclass
class RegimeConfig:
    ma_period: int = 60
    momentum_days: int = 20
    bull_breadth: float = 60.0  # MA 상회 종목 비율(%) 이상이면 강세권
    bear_breadth: float = 40.0  # 이하면 약세권
    overheat_momentum: float = 15.0  # 강세권 + 평균 모멘텀(%) 이상이면 과열
    correction_momentum: float = -10.0  # 약세권 + 평균 모멘텀(%) 이하면 조정(급락 중)


def classify(prices_by_ticker: dict[str, list[float]], config: RegimeConfig | None = None) -> dict:
    """전 종목 종가 시계열만으로 국면을 근사 판정. 판정 불가(표본 부족)면 ready=False."""
    config = config or RegimeConfig()
    min_len = max(config.ma_period, config.momentum_days) + 1
    above, momentums, n = 0, [], 0

    for closes in prices_by_ticker.values():
        if len(closes) < min_len:
            continue
        n += 1
        ma = ind.sma(closes, config.ma_period)[-1]
        if ma is not None and closes[-1] > ma:
            above += 1
        momentums.append((closes[-1] / closes[-1 - config.momentum_days] - 1) * 100)

    if n == 0:
        return {"ready": False, "regime": None, "breadth_pct": None, "avg_momentum_pct": None, "n": 0}

    breadth_pct = round(above / n * 100, 1)
    avg_momentum_pct = round(sum(momentums) / len(momentums), 2)

    if breadth_pct >= config.bull_breadth:
        regime = "과열" if avg_momentum_pct >= config.overheat_momentum else "강세"
    elif breadth_pct <= config.bear_breadth:
        regime = "조정" if avg_momentum_pct <= config.correction_momentum else "약세"
    else:
        regime = "중립"

    return {
        "ready": True,
        "regime": regime,
        "breadth_pct": breadth_pct,
        "avg_momentum_pct": avg_momentum_pct,
        "n": n,
    }
