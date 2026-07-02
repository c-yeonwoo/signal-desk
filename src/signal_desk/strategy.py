"""트레이딩 성향 프리셋 — 안정형/균형형/공격형이 봇 파라미터와 리스크 룰을 함께 정한다.

'보유종목 남발 없이 안정적이면서 적당한 수익'을 성향으로 조절한다:
- 안정형: 넓게 분산(종목 많이·비중 작게)·엄격한 매수 기준·타이트한 손절/익절
- 균형형: 기존 기본값
- 공격형: 소수 집중(종목 적게·비중 크게)·완화된 매수 기준·넓은 손절/큰 익절

리밸런싱(B)의 목표 종목수·비중 기준으로도 재사용한다.
"""

from __future__ import annotations

from signal_desk.signals import risk

STYLES = ("conservative", "balanced", "aggressive")
STYLE_LABEL = {"conservative": "안정형", "balanced": "균형형", "aggressive": "공격형"}
STYLE_DESC = {
    "conservative": "넓게 분산 · 엄격한 매수 · 타이트한 손절(변동성↓)",
    "balanced": "분산과 집중의 균형 · 표준 손익 규칙",
    "aggressive": "소수 집중 · 적극 매수 · 넓은 손절/큰 익절(변동성↑)",
}

PRESETS = {
    "conservative": {"max_positions": 12, "position_pct": 0.06, "min_buy_score": 1.9, "max_new_buys_per_run": 2,
                     "stop_loss_pct": -0.05, "take_profit_pct": 0.10, "trailing_from_peak_pct": -0.04},
    "balanced": {"max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.6, "max_new_buys_per_run": 2,
                 "stop_loss_pct": -0.07, "take_profit_pct": 0.15, "trailing_from_peak_pct": -0.05},
    "aggressive": {"max_positions": 6, "position_pct": 0.14, "min_buy_score": 1.3, "max_new_buys_per_run": 3,
                   "stop_loss_pct": -0.10, "take_profit_pct": 0.25, "trailing_from_peak_pct": -0.07},
}


def normalize(style: str) -> str:
    return style if style in PRESETS else "balanced"


def preset(style: str) -> dict:
    return PRESETS[normalize(style)]


def bot_params(style: str) -> dict:
    """봇 매수·보유 파라미터(bot_config 숫자 컬럼에 적용)."""
    p = preset(style)
    return {k: p[k] for k in ("max_positions", "position_pct", "min_buy_score", "max_new_buys_per_run")}


def risk_config(style: str) -> risk.RiskConfig:
    """성향별 손절/익절/트레일링 룰."""
    p = preset(style)
    return risk.RiskConfig(stop_loss_pct=p["stop_loss_pct"], take_profit_pct=p["take_profit_pct"],
                           trailing_from_peak_pct=p["trailing_from_peak_pct"])
