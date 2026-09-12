"""실시간 청산과 사후 검증이 공유하는 작은 execution twin.

시그널의 방향 정확도와 실제 수익은 별개다. 특히 5분 틱으로 작동하는 손절·익절·트레일링은
일봉 한 개로 재현하면 결과가 바뀐다. 이 모듈은 한 가격 관측에서의 peak 갱신과 리스크 우선순위를
한 곳에 두고, 같은 함수를 라이브 봇과 장중 가격 원장 재생에 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from signal_desk.signals import risk


@dataclass(frozen=True)
class QuoteTick:
    ts: int
    price: float


@dataclass(frozen=True)
class RiskStep:
    price: float
    peak: float
    reason: str | None


@dataclass(frozen=True)
class ReplayResult:
    exit_tick: QuoteTick | None
    reason: str | None
    peak: float
    observed: int


def evaluate_quote(entry_price: float, price: float, previous_peak: float,
                   config: risk.RiskConfig | None = None) -> RiskStep:
    """가격 한 건으로 peak와 청산 판정을 갱신한다.

    유효하지 않은 입력은 명시적으로 거절한다. 라이브 코드가 0원/빈 시세를 정상 가격으로
    간주하여 포지션을 잘못 닫는 일을 막고, 재생기에도 같은 제약을 준다.
    """
    if entry_price <= 0 or price <= 0:
        raise ValueError("entry_price and price must be positive")
    peak = max(entry_price, previous_peak, price)
    return RiskStep(price=price, peak=peak, reason=risk.check_exit(entry_price, price, peak, config))


def replay(entry_price: float, ticks: Iterable[QuoteTick], *, initial_peak: float | None = None,
           config: risk.RiskConfig | None = None) -> ReplayResult:
    """저장한 시간순 틱에서 라이브와 똑같은 첫 청산 시점만 재생한다."""
    peak = max(entry_price, initial_peak or entry_price)
    observed = 0
    for tick in ticks:
        step = evaluate_quote(entry_price, float(tick.price), peak, config)
        peak, observed = step.peak, observed + 1
        if step.reason:
            return ReplayResult(exit_tick=tick, reason=step.reason, peak=peak, observed=observed)
    return ReplayResult(exit_tick=None, reason=None, peak=peak, observed=observed)
