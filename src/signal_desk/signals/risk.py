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
    # ── 변동성 스케일 ──────────────────────────────────────────────────────
    # 위 세 값은 **고정 퍼센트**다. 그런데 같은 −4%가 시장마다 다른 뜻이다:
    #
    #     2026-01~07 일간 σ    미국 2.51%   국내 4.57%   (1.8배)
    #     트레일링 −4%가 몇 σ   미국 1.6σ    국내 0.9σ
    #     하루에 −4% 초과 하락   미국 3.78%   국내 12.42%  (종목-일 비율)
    #
    # 실측이 그 차이를 그대로 보여줬다(2026-07-08~09-04, 같은 엔진·같은 규칙):
    #     미국  안정 −0.80%p · 균형 **+0.24%p** · 공격 **+3.04%p**  (초과수익)
    #     국내  안정 −5.26%p · 균형 −10.19%p · 공격 −10.57%p
    #
    # 즉 규칙이 통하는지는 폭이 **몇 σ냐**에 달렸고, 퍼센트로 적어 두면 그 값이 시장에 따라
    # 조용히 바뀐다. 이 리포가 정규화에서 이미 겪은 병이다 — "척도가 비율로 의미 있으면
    # max로 나누고, 절대값이 무의미하면 평균 대비 고정 감도로 환산한다".
    #
    # `sigma`(그 종목의 일간 실현변동성)를 주면 폭을 σ 배수로 해석한다. 배수는 새로 고른
    # 값이 아니라 **현재 퍼센트 ÷ 미국 일간 σ(2.51%)** 다 — 미국에서 하던 것을 그대로
    # 두고 국내가 같은 위험을 지게 하는 환산이다.
    sigma: float | None = None
    stop_loss_sigma: float | None = None
    take_profit_sigma: float | None = None
    trailing_sigma: float | None = None

    def effective(self) -> "RiskConfig":
        """σ가 있으면 σ 배수로 환산한 폭을, 없으면 고정 퍼센트를 그대로 쓴다.

        **모르면 바꾸지 않는다** — σ를 못 재는 종목(상장 직후·거래정지)에서 폭을 0으로
        만들거나 무한대로 벌리면 그건 규칙이 아니라 0으로 나누기다.
        """
        if not self.sigma or self.sigma <= 0:
            return self
        def _w(mult: float | None, fixed: float) -> float:
            return -abs(mult) * self.sigma if mult else fixed
        def _wp(mult: float | None, fixed: float) -> float:
            return abs(mult) * self.sigma if mult else fixed
        return RiskConfig(
            stop_loss_pct=_w(self.stop_loss_sigma, self.stop_loss_pct),
            take_profit_pct=_wp(self.take_profit_sigma, self.take_profit_pct),
            trailing_from_peak_pct=_w(self.trailing_sigma, self.trailing_from_peak_pct),
            trailing_protects_gains_only=self.trailing_protects_gains_only,
        )


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
    config = (config or RiskConfig()).effective()
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
