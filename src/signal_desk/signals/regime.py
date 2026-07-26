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


# 국면·거시가 비우호일 때 매수 임계값에 더할 가산량(점수 스케일 ~[-3,3] 기준).
# **selection_mode="absolute"에서만 쓰인다** — rank 모드에서는 국면이 문턱이 아니라 익스포저를
# 조절한다(target_exposure). 문턱 상향은 나쁜 시장에서 후보를 0으로 만들어 학습을 멈춘다.
_REGIME_BUMP = {"조정": 0.8, "약세": 0.4, "중립": 0.0, "강세": 0.0, "과열": 0.0}
_MACRO_UNFAVORABLE_BUMP = 0.3
_FLOW_SELL_BUMP = 0.3          # 시장 전체 외국인·기관 20일 순매도 시 가산
_FLOW_STRONG_SELL_JO = -5.0    # 이보다 큰 순매도(조원)면 강한 이탈로 보고 더 크게 가산
_FLOW_STRONG_SELL_BUMP = 0.5


def buy_threshold_bump(regime_result: dict | None, macro_result: dict | None,
                       flow_result: dict | None = None) -> dict:
    """약세·조정 국면 / 거시 비우호 / 시장 전체 외국인·기관 순매도일 때 매수 임계값에 더할 가산량과
    사유를 반환. 매수 문턱만 올린다(청산·매도 기준은 불변 — 하락 방어는 억제하지 않음).

    반환: {bump: float, reasons: [str]}. 우호적·중립이면 bump=0. engine/config가 아니라 여기
    (국면 판정 로직 옆)에 두어 봇·API가 동일한 규칙을 공유한다.
    """
    bump = 0.0
    reasons: list[str] = []
    reg = (regime_result or {}).get("regime")
    r_bump = _REGIME_BUMP.get(reg, 0.0)
    if r_bump:
        bump += r_bump
        reasons.append(f"{reg} 국면 — 매수 기준 +{r_bump:.1f}")
    if (macro_result or {}).get("bias") == "비우호":
        bump += _MACRO_UNFAVORABLE_BUMP
        reasons.append(f"거시 비우호 — 매수 기준 +{_MACRO_UNFAVORABLE_BUMP:.1f}")
    fb = market_flow_bias(flow_result)
    if fb.get("available") and fb.get("bias") == "순매도":
        net = fb["smart_net_20d"]
        f_bump = _FLOW_STRONG_SELL_BUMP if net <= _FLOW_STRONG_SELL_JO else _FLOW_SELL_BUMP
        bump += f_bump
        reasons.append(f"외국인·기관 20일 순매도 {net:+.1f}조 — 매수 기준 +{f_bump:.1f}")
    return {"bump": round(bump, 2), "reasons": reasons}


# ── 국면 = '얼마나 살까'(크기) ─────────────────────────────────────────────
# 국면으로 매수 문턱을 올리면(buy_threshold_bump) 나쁜 시장에서 후보가 **0**이 된다. 자격 × 0은
# 손실도 학습도 없는 상태이고, 실측 track record가 영원히 안 쌓인다(2026-07-26 진단: 10거래일
# 매수 1건 → 표본 20건까지 약 10개월). 그래서 국면은 자격이 아니라 총 익스포저를 정한다.
# 심각도 순서는 기존 _REGIME_BUMP와 동일하게 유지한다(조정 = 급락 중이라 약세보다 무겁다).
_REGIME_EXPOSURE = {"강세": 1.0, "과열": 0.6, "중립": 0.7, "약세": 0.4, "조정": 0.2}
_MACRO_UNFAVORABLE_MULT = 0.8
_FLOW_SELL_MULT = 0.8
_FLOW_STRONG_SELL_MULT = 0.6
# 하한 — 0으로 내리지 않는다. 아무것도 사지 않으면 그 국면에서 무엇이 통하는지 배울 수 없다.
EXPOSURE_FLOOR = 0.15


def target_exposure(regime_result: dict | None, macro_result: dict | None,
                    flow_result: dict | None = None) -> dict:
    """총 익스포저 목표(0<x≤1)와 사유. 봇은 (총평가액 × exposure)를 투자 상한으로 쓴다.

    반환: {exposure, reasons[], regime}. 판정 불가(국면 없음)면 중립값을 쓴다 —
    모르는 상태를 '전액 투자'로도 '전액 현금'으로도 번역하지 않는다.
    """
    reg = (regime_result or {}).get("regime")
    exp = _REGIME_EXPOSURE.get(reg, 0.7)
    reasons: list[str] = []
    if reg:
        reasons.append(f"{reg} 국면 — 기준 익스포저 {exp * 100:.0f}%")
    else:
        reasons.append("국면 판정 없음 — 중립 익스포저 70%")
    if (macro_result or {}).get("bias") == "비우호":
        exp *= _MACRO_UNFAVORABLE_MULT
        reasons.append(f"거시 비우호 — ×{_MACRO_UNFAVORABLE_MULT}")
    fb = market_flow_bias(flow_result)
    if fb.get("available") and fb.get("bias") == "순매도":
        net = fb["smart_net_20d"]
        mult = _FLOW_STRONG_SELL_MULT if net <= _FLOW_STRONG_SELL_JO else _FLOW_SELL_MULT
        exp *= mult
        reasons.append(f"외국인·기관 20일 순매도 {net:+.1f}조 — ×{mult}")
    exp = max(EXPOSURE_FLOOR, min(1.0, exp))
    return {"exposure": round(exp, 3), "reasons": reasons, "regime": reg}


# 시장 전체(KOSPI) 외국인+기관 20일 순매수 누적(조원)이 이 값 이하/이상이면 순매도/순매수세로 본다.
_FLOW_SELL_JO = -2.0
_FLOW_BUY_JO = 2.0


def market_flow_bias(flow_result: dict | None, market: str = "KOSPI") -> dict:
    """토스 시장전체 수급(외국인·기관 순매수 누적) → 국면 보조 신호. pykrx 종목별 수급이 죽어
    그 대체로 '시장 전체' 스마트머니 방향만 본다. smart_net_20d(조원) 부호·크기로 라벨링.

    반환: {available, bias('순매수'|'중립'|'순매도'|None), smart_net_20d, foreign/inst_net_20d, as_of}.
    """
    mf = (flow_result or {}).get(market) if flow_result else None
    net = (mf or {}).get("smart_net_20d")
    if net is None:
        return {"available": False, "bias": None}
    bias = "순매도" if net <= _FLOW_SELL_JO else "순매수" if net >= _FLOW_BUY_JO else "중립"
    return {"available": True, "bias": bias, "smart_net_20d": net,
            "foreign_net_20d": mf.get("foreign_net_20d"), "inst_net_20d": mf.get("inst_net_20d"),
            "as_of": mf.get("as_of")}
