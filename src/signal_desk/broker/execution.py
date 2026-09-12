"""페이퍼 체결 비용 모델 — 기준가와 실제 가상 체결가/현금흐름을 분리한다."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from signal_desk import config


@dataclass(frozen=True)
class Fill:
    reference_price: float
    fill_price: float
    qty: int
    side: str
    gross_notional: float
    commission: float
    sell_tax: float
    regulatory_fee: float
    total_fees: float
    cash_change: float
    slippage_cost: float
    assumptions: dict

    def as_dict(self) -> dict:
        return asdict(self)


def calculate(reference_price: float, qty: int, side: str, market: str) -> Fill:
    """기준 현재가에 불리한 슬리피지와 시장별 비용을 적용한 체결.

    국내 기본 매도세 20bp는 2026 KOSPI(거래세 5bp+농특세 15bp)와 KOSDAQ(거래세 20bp)의
    공통값이다. 미국 SEC 0.206bp는 2026-04-04 이후 $20.60/$1m 공시값이며, FINRA TAF는
    매도 주당 $0.000195·주문당 상한 $9.79의 2026 값이다. 브로커 수수료와 spread/impact는
    계좌·종목별로 달라 환경변수 가정이며, 진짜 호가 데이터가 생길 때 대체할 예정이다.
    """
    if side not in {"buy", "sell"} or qty <= 0 or reference_price <= 0:
        raise ValueError("positive price/qty and buy/sell side required")
    slip_bps = config.paper_execution_bps(market, "SLIPPAGE", 5.0)
    commission_bps = config.paper_execution_bps(market, "COMMISSION", 0.0 if market == "us" else 1.5)
    fill_price = reference_price * (1 + slip_bps / 10_000 if side == "buy" else 1 - slip_bps / 10_000)
    gross = fill_price * qty
    commission = gross * commission_bps / 10_000
    sell_tax = gross * config.paper_execution_bps(market, "SELL_TAX", 0.0 if market == "us" else 20.0) / 10_000 if side == "sell" else 0.0
    regulatory = 0.0
    if side == "sell" and market == "us":
        regulatory += gross * config.paper_execution_bps("us", "SEC_SELL", 0.206) / 10_000
        regulatory += min(config.paper_us_finra_taf_cap(), qty * config.paper_us_finra_taf_per_share())
    fees = commission + sell_tax + regulatory
    cash_change = -(gross + fees) if side == "buy" else gross - fees
    return Fill(reference_price=round(reference_price, 8), fill_price=round(fill_price, 8), qty=qty, side=side,
                gross_notional=round(gross, 8), commission=round(commission, 8), sell_tax=round(sell_tax, 8),
                regulatory_fee=round(regulatory, 8), total_fees=round(fees, 8), cash_change=round(cash_change, 8),
                slippage_cost=round(abs(fill_price - reference_price) * qty, 8),
                assumptions={"slippage_bps": slip_bps, "commission_bps": commission_bps})
