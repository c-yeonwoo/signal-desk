"""기본적분석 스코어링 — brightdesk `fundamentals.server.ts`의 정확한 임계값을 이식.

점수 범위 [-2, +2]. 재무데이터가 전혀 없으면(has_data=False) engine이 이 컴포넌트를
가중치 계산에서 제외한다 — DART_API_KEY 미설정 시 그레이스풀 폴백.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FundamentalResult:
    score: float
    has_data: bool
    reasons: list[str] = field(default_factory=list)


def score(metrics: dict, *, include_growth: bool = True) -> FundamentalResult:
    """metrics: per, pbr, roe, revenue_growth(%), debt_ratio(%), dividend_yield(%) — 모두 optional.

    `include_growth=False` 면 매출성장 항목을 뺀다 — 성장을 **독립 팩터**로 분리해 재는
    실험에서 이중계상을 막기 위한 스위치다(`signals/growth.py`). 기본은 True이므로
    라이브·기존 판정의 점수는 한 자리도 바뀌지 않는다.
    """
    per = metrics.get("per")
    pbr = metrics.get("pbr")
    roe = metrics.get("roe")
    revenue_growth = metrics.get("revenue_growth")
    debt_ratio = metrics.get("debt_ratio")
    dividend_yield = metrics.get("dividend_yield")

    if all(v is None for v in (per, pbr, roe, revenue_growth, debt_ratio, dividend_yield)):
        return FundamentalResult(score=0.0, has_data=False, reasons=["재무데이터 없음"])

    total = 0.0
    reasons: list[str] = []

    if roe is not None:
        if roe >= 15:
            total += 1.0
            reasons.append(f"[기본] ROE {roe:.1f}% — 우수")
        elif roe >= 10:
            total += 0.5
            reasons.append(f"[기본] ROE {roe:.1f}% — 양호")
        elif roe < 5:
            total -= 0.5
            reasons.append(f"[기본] ROE {roe:.1f}% — 저조")

    if per is not None and per > 0:
        if per < 10:
            total += 0.7
            reasons.append(f"[기본] PER {per:.1f} — 저평가 구간")
        elif per < 15:
            total += 0.3
            reasons.append(f"[기본] PER {per:.1f} — 양호")
        elif per > 25:
            total -= 0.5
            reasons.append(f"[기본] PER {per:.1f} — 고평가 우려")

    if pbr is not None and pbr > 0:
        if pbr < 1:
            total += 0.5
            reasons.append(f"[기본] PBR {pbr:.2f} — 자산가치 대비 저평가")
        elif pbr > 3:
            total -= 0.3
            reasons.append(f"[기본] PBR {pbr:.2f} — 고평가 우려")

    if revenue_growth is not None and include_growth:
        if revenue_growth > 15:
            total += 0.7
            reasons.append(f"[기본] 매출성장 {revenue_growth:.1f}% — 고성장")
        elif revenue_growth > 5:
            total += 0.3
            reasons.append(f"[기본] 매출성장 {revenue_growth:.1f}% — 성장세")
        elif revenue_growth < 0:
            total -= 0.5
            reasons.append(f"[기본] 매출성장 {revenue_growth:.1f}% — 역성장")

    if debt_ratio is not None and debt_ratio > 200:
        total -= 0.5
        reasons.append(f"[기본] 부채비율 {debt_ratio:.0f}% — 과다")

    if dividend_yield is not None and dividend_yield >= 3:
        total += 0.2
        reasons.append(f"[기본] 배당수익률 {dividend_yield:.1f}% — 양호")

    total = max(-2.0, min(2.0, total))
    return FundamentalResult(score=total, has_data=True, reasons=reasons)
