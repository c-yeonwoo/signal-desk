"""리스크 엔진 (BACKLOG #8) — stop-loss/take-profit/trailing 청산 판정.

brightdesk `risk.server.ts`의 정확한 기본값(−7%/+15%/−5%)을 그대로 이식. 포지션(평단가·보유량)
추적 기능은 아직 이 리포에 없어서(#7 자동매매봇이 KIS 모의투자로 실제 붙을 때 같이 옴), 지금은
`indicators.py`/`fundamental.py`와 같은 패턴으로 순수 함수만 제공한다 — 포지션 모델이 생기면
바로 갖다 쓸 수 있게.

주의: 진입 이후 고점(peak_since_entry)은 고가(high) 데이터가 없어 종가로 근사한다(다른 곳에서도
쓰는 근사 패턴 — 고가 데이터를 저장하게 되면 더 정확해짐).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    stop_loss_pct: float = -0.07
    take_profit_pct: float = 0.15
    trailing_from_peak_pct: float = -0.05
    # 트레일링은 **이익을 지키는 장치**다 — 손실 구간에서 나가는 것은 손절의 몫이다.
    #
    # 2026-09-06 진단: 진입 시 `peak = 진입가`(`bot.run_once` 가 그렇게 넣는다)이고 트레일링
    # 폭이 손절 폭보다 좁다(안정 -4 vs -5 · 균형 -5 vs -7 · 공격 -7 vs -10). 그래서 주가가
    # 한 번도 오르지 않아도 **트레일링 발동가가 늘 손절가보다 위**에 있고, 5분틱으로 연속
    # 관측하면 트레일링이 항상 먼저 닿는다. 실측이 그대로였다 — 레퍼런스 3봇 최근 20거래의
    # 매도 사유가 **100% TRAILING**(STOP_LOSS 0건 · TAKE_PROFIT 0건)이었다.
    #
    # 즉 파라미터가 셋인데 실제로 작동하는 규칙은 하나였고, 그 하나는 "진입가 대비 -4%에서
    # 자른다"였다. 국내 2026 일간 σ가 4.6%라 그건 **0.9σ** — 하루치 변동보다 좁다.
    # 12.4%의 종목-일이 하루 만에 그 폭을 넘는다.
    #
    # True면 트레일링은 손익분기 이상에서만 발동한다. 그러면 셋이 각자 일한다:
    #   손실 구간 → 손절(-5/-7/-10) · 이익 구간 → 트레일링(고점 되돌림) · 목표 도달 → 익절
    # 새 숫자를 만들지 않는다 — 이미 있는 세 값이 설계대로 동작하게만 한다.
    trailing_protects_gains_only: bool = True


def peak_since_entry(closes: list[float], entry_idx: int) -> float:
    """진입 시점(entry_idx, 포함) 이후 현재까지의 최고 종가."""
    return max(closes[entry_idx:])


def check_exit(
    avg_price: float, last_close: float, peak: float, config: RiskConfig | None = None
) -> str | None:
    """포지션 청산 판정. avg_price=평단가, last_close=현재가, peak=진입 이후 고점.

    brightdesk와 동일한 우선순위(손절 → 익절 → 트레일링)로 체크한다. 반환: 'STOP_LOSS' |
    'TAKE_PROFIT' | 'TRAILING' | None(청산 신호 없음).
    """
    config = config or RiskConfig()
    # round(): 부동소수점 오차로 정확히 -7.0%/+15.0% 경계값이 근소하게 어긋나
    # (예: 93/100-1 == -0.06999999999999995) 임계값을 못 넘는 걸 방지.
    pl = round(last_close / avg_price - 1, 6)
    if pl <= config.stop_loss_pct:
        return "STOP_LOSS"
    if pl >= config.take_profit_pct:
        return "TAKE_PROFIT"
    drawdown = round(last_close / peak - 1, 6)
    if drawdown <= config.trailing_from_peak_pct:
        # 손실 구간의 되돌림은 손절이 판정한다 — 여기서 나가면 트레일링이 손절을 덮어써
        # 손절·익절이 도달 불가능한 죽은 파라미터가 된다(위 주석 참고).
        if config.trailing_protects_gains_only and pl < 0:
            return None
        return "TRAILING"
    return None


def check_exit_from_series(
    closes: list[float], entry_idx: int, avg_price: float, config: RiskConfig | None = None
) -> str | None:
    """가격 시계열 + 진입 인덱스만으로 청산 판정(peak을 자동 계산하는 편의 함수)."""
    last_close = closes[-1]
    peak = peak_since_entry(closes, entry_idx)
    return check_exit(avg_price, last_close, peak, config)
