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

# entry_tranches: 목표비중을 몇 회로 나눠 분할매수할지(라오어 분할매수 응용 — 진입 타이밍 리스크 분산)
# harvest_take_profit_pct: 횡보·약세 국면에서 '중간 실현'용 타이트 익절(추세 국면엔 위 take_profit + 트레일링 유지)
PRESETS = {
    "conservative": {"max_positions": 12, "position_pct": 0.06, "min_buy_score": 1.9, "max_new_buys_per_run": 2,
                     "stop_loss_pct": -0.05, "take_profit_pct": 0.10, "trailing_from_peak_pct": -0.04,
                     "entry_tranches": 4, "harvest_take_profit_pct": 0.06},
    "balanced": {"max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.6, "max_new_buys_per_run": 2,
                 "stop_loss_pct": -0.07, "take_profit_pct": 0.15, "trailing_from_peak_pct": -0.05,
                 "entry_tranches": 3, "harvest_take_profit_pct": 0.09},
    "aggressive": {"max_positions": 6, "position_pct": 0.14, "min_buy_score": 1.3, "max_new_buys_per_run": 3,
                   "stop_loss_pct": -0.10, "take_profit_pct": 0.25, "trailing_from_peak_pct": -0.07,
                   "entry_tranches": 2, "harvest_take_profit_pct": 0.12},
}

# 추세 국면(여기선 익절을 넓게 두고 트레일링으로 수익 극대화). 그 외(횡보·약세·조정)는 중간 실현.
TRENDING_REGIMES = ("강세", "과열")


def entry_tranches(style: str) -> int:
    return int(preset(style)["entry_tranches"])


def normalize(style: str) -> str:
    return style if style in PRESETS else "balanced"


def preset(style: str) -> dict:
    return PRESETS[normalize(style)]


def bot_params(style: str) -> dict:
    """봇 매수·보유 파라미터(bot_config 숫자 컬럼에 적용)."""
    p = preset(style)
    return {k: p[k] for k in ("max_positions", "position_pct", "min_buy_score", "max_new_buys_per_run")}


def risk_config(style: str, regime: str | None = None) -> risk.RiskConfig:
    """성향별 손절/익절/트레일링 룰. 횡보·약세 국면(비추세)이면 '중간 실현'용 타이트 익절 적용
    (라오어 응용) — 추세 국면(강세·과열)에선 넓은 익절 + 트레일링으로 수익을 끝까지."""
    p = preset(style)
    tp = p["take_profit_pct"]
    if regime is not None and regime not in TRENDING_REGIMES:
        tp = p["harvest_take_profit_pct"]  # 횡보/약세 → 빨리 실현
    return risk.RiskConfig(stop_loss_pct=p["stop_loss_pct"], take_profit_pct=tp,
                           trailing_from_peak_pct=p["trailing_from_peak_pct"])
