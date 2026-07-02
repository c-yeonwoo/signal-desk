"""낙폭과대 반등 / 단기과열 조정 팩터 — 순수 가격 데이터만으로 계산(LLM·추가 데이터 불필요).

기존 RSI 과매도/과매수(순간값)와 다르게, N일 누적 수익률로 "짧은 기간에 얼마나 급하게
움직였는지"를 함께 봐서 단순 변동성과 구분한다: 급락+과매도 조합만 반등 기대로, 급등+과매수
조합만 조정 우려로 잡는다. BACKLOG #14(통합 후보뷰의 "낙폭과대"/"눌림목" 유형)와 개념은
겹치지만, 여기서는 종합 시그널에 바로 반영되는 팩터 하나로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReversionConfig:
    lookback_days: int = 10
    crash_threshold: float = -0.15  # N일 누적 -15% 이하
    rally_threshold: float = 0.20  # N일 누적 +20% 이상
    rsi_oversold_confirm: float = 35
    rsi_overbought_confirm: float = 70
    max_score: float = 1.5  # technical의 RSI 서브스코어(±1.5)와 같은 스케일


def score(
    closes: list[float], rsi_series: list[float | None], config: ReversionConfig | None = None
) -> tuple[float, list[str]]:
    """범위 [-max_score, +max_score]. 조건 미충족이면 0.0, 근거 없음."""
    config = config or ReversionConfig()
    n = config.lookback_days
    if len(closes) <= n:
        return 0.0, []

    recent_ret = closes[-1] / closes[-1 - n] - 1
    rsi = rsi_series[-1] if rsi_series else None
    if rsi is None:
        return 0.0, []

    if recent_ret <= config.crash_threshold and rsi < config.rsi_oversold_confirm:
        magnitude = min(config.max_score, abs(recent_ret / config.crash_threshold) * config.max_score * 0.5)
        return magnitude, [
            f"[낙폭과대] 최근 {n}일 {recent_ret * 100:.1f}% 급락 + RSI {rsi:.1f} 과매도 — 반등 기대"
        ]
    if recent_ret >= config.rally_threshold and rsi > config.rsi_overbought_confirm:
        magnitude = min(config.max_score, (recent_ret / config.rally_threshold) * config.max_score * 0.5)
        return -magnitude, [
            f"[단기과열] 최근 {n}일 +{recent_ret * 100:.1f}% 급등 + RSI {rsi:.1f} 과매수 — 조정 우려"
        ]
    return 0.0, []
