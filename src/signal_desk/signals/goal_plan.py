"""목표금액 달성 플랜 — 보유 + 월 적립 + 배당 재투자로 목표까지 얼마나 걸리는가.

`scenario.py`의 부트스트랩을 그대로 쓴다. **기대수익률을 가정하지 않는다** — 보유종목의 실제
과거 일간수익률을 복원추출해 경로를 만들고, 결과는 항상 분위(p10/p50/p90)로 낸다.

## 이 모듈이 지어내지 않는 것

- **기대수익률**: 입력이 아니다. 부트스트랩 표본이 전부다.
- **배당 성장**: 0으로 둔다(현재 DPS 유지 가정). 성장률을 넣으면 그건 예측이다.
- **환율·물가**: 반영하지 않는다. 통화별로 따로 계산한다(합치면 둘 다 거짓이 된다).

## 반드시 함께 읽어야 하는 한계

부트스트랩은 **그 보유종목의 과거를 물려받는다.** "시장이 이럴 것"이 아니라 "이 종목들이
과거처럼 계속하면"이다. 승자를 들고 있으면 경로도 낙관적으로 나온다 — 하네스에서 경계하는
생존편향과 같은 족속이고, 그래서 `basis`·`caveat`를 산출물에 싣는다.

## 왜 정확히 역산할 수 있나 (아핀 구조)

한 경로를 고정하면 자산은 **초기자산과 월적립액에 대해 1차식**이다:

    asset_T = A0 · g_T + c · s_T

배당 재투자를 넣어도 배당은 자산에 비례하므로 이 성질이 유지된다. 그래서 같은 난수 경로에서
`g`(초기자산 계수)와 `s`(적립 계수)를 함께 누적하면, "목표를 맞추려면 월 얼마가 필요한가"를
이분탐색 없이 **경로별로 정확히** 풀 수 있다: `c = (goal − A0·g) / s`.
"""

from __future__ import annotations

import numpy as np

from signal_desk.signals import scenario

_TRADING_DAYS_PER_MONTH = 21
_MIN_HISTORY = scenario._MIN_HISTORY

# 배당소득 원천징수. 국내 15.4%(소득세 14% + 지방소득세 1.4%) · 미국 15%(조세조약 한도).
# **세전 배당으로 재투자를 계산하면 경로가 낙관적으로 부풀려진다** — 기존 배당 플래너의
# `예상 연/월 배당`이 세전이었다. 금융소득종합과세(연 2천만원 초과)는 개인 상황이라 넣지 않고,
# 넣지 않았다는 사실을 산출물에 밝힌다.
DIV_TAX = {"KRW": 0.154, "USD": 0.15}


def _monthly_paths(daily: np.ndarray, exposure: float, months: int, sims: int,
                   *, div_yield_annual: float, tax: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """월별 (초기자산 계수 g, 월적립 계수 s) 경로. 둘 다 (sims, months+1) 형태.

    같은 난수 경로에서 두 계수를 함께 누적한다 — 따로 돌리면 서로 다른 시장을 가정하게 된다.
    배당은 **월말 자산에 비례**해 세후로 재투자한다. 시세 종가는 배당을 반영하지 않는
    가격 계열이므로(배당락 조정 없음) 배당수익률을 더하는 것이 이중계상이 아니다.
    """
    rng = np.random.default_rng(seed)
    g = np.ones((sims, months + 1))
    s = np.zeros((sims, months + 1))
    div_m = max(0.0, float(div_yield_annual)) / 12.0 * (1.0 - float(tax))
    gi = np.ones(sims)
    si = np.zeros(sims)
    for m in range(1, months + 1):
        idx = rng.integers(0, len(daily), size=(sims, _TRADING_DAYS_PER_MONTH))
        growth = np.prod(1.0 + daily[idx] * exposure, axis=1)
        gi = gi * growth
        si = si * growth + 1.0                    # 이 달 적립분은 이 달 수익률을 못 받는다
        # 배당 재투자 — 자산에 비례하므로 두 계수에 같은 배수가 걸린다(아핀 유지).
        gi = gi * (1.0 + div_m)
        si = si * (1.0 + div_m)
        g[:, m] = gi
        s[:, m] = si
    return g, s


def _first_reach(values: np.ndarray, goal: float) -> float | None:
    """분위 경로가 목표를 처음 넘는 월. 끝까지 못 넘으면 None."""
    hit = np.nonzero(values >= goal)[0]
    return int(hit[0]) if hit.size else None


def plan(holdings: list[dict], prices: dict[str, list[float]], *,
         goal_amount: float, months: int, monthly_contribution: float = 0.0,
         div_yield_annual: float = 0.0, currency: str = "KRW",
         style: str = "balanced", sims: int = 2000, seed: int = 1234) -> dict:
    """목표금액까지의 경로 + 부족분 역산.

    `months`는 계획 기간(개월). `div_yield_annual`은 **보유종목 실제 DPS로 계산한 값**을
    호출자가 넘긴다 — 이 모듈이 배당수익률을 추정하지 않는다.
    """
    daily, total = scenario._portfolio_returns(holdings, prices)
    if daily.size < _MIN_HISTORY or total <= 0:
        return {"ready": False,
                "reason": "경로를 만들 과거 시세가 있는 보유종목이 부족합니다 "
                          f"(최소 {_MIN_HISTORY}일)."}
    months = max(1, min(int(months), 600))
    goal = float(goal_amount)
    if goal <= 0:
        return {"ready": False, "reason": "목표금액을 0보다 크게 넣어야 합니다."}
    c = max(0.0, float(monthly_contribution))
    exp = scenario.EQUITY_EXPOSURE.get(style, 0.85)
    tax = DIV_TAX.get(currency, 0.0)
    g, s = _monthly_paths(daily, exp, months, sims, div_yield_annual=div_yield_annual,
                          tax=tax, seed=seed)
    asset = total * g + c * s                       # (sims, months+1)

    # 경로 팬 — 분위는 **월별로** 낸다(경로별 정렬이 아니라 각 시점의 분포).
    fan = []
    for m in range(0, months + 1, max(1, months // 24)):   # 최대 25점(화면 가독성)
        col = asset[:, m]
        fan.append({"month": m,
                    "p10": round(float(np.percentile(col, 10)), 2),
                    "p50": round(float(np.percentile(col, 50)), 2),
                    "p90": round(float(np.percentile(col, 90)), 2)})
    if fan[-1]["month"] != months:
        col = asset[:, months]
        fan.append({"month": months,
                    "p10": round(float(np.percentile(col, 10)), 2),
                    "p50": round(float(np.percentile(col, 50)), 2),
                    "p90": round(float(np.percentile(col, 90)), 2)})

    # 도달 시점 — 분위 경로가 목표를 처음 넘는 달. **단일 답을 내지 않는다.**
    q = {p: np.percentile(asset, p, axis=0) for p in (10, 50, 90)}
    reach = {f"p{p}": _first_reach(q[p], goal) for p in (10, 50, 90)}

    # 부족분 역산 — 경로별로 정확히 푼다(아핀 구조). `s`가 0이면(months=0) 계산 불가.
    need = {}
    s_end, g_end = s[:, months], g[:, months]
    with np.errstate(divide="ignore", invalid="ignore"):
        c_req = np.where(s_end > 0, (goal - total * g_end) / s_end, np.inf)
    c_req = np.maximum(c_req, 0.0)
    for p in (10, 50, 90):
        # p10 경로(나쁜 쪽)를 맞추려면 **더 많이** 넣어야 하므로 분위가 뒤집힌다:
        # 자산 p10 ↔ 필요적립 p90. 헷갈리기 쉬워 이름으로 못박는다.
        v = float(np.percentile(c_req, 100 - p))
        need[f"p{p}"] = None if not np.isfinite(v) else round(v, 2)
    gap = {f"p{p}": (None if need[f"p{p}"] is None else round(need[f"p{p}"] - c, 2))
           for p in (10, 50, 90)}

    end = asset[:, months]
    return {
        "ready": True,
        "currency": currency,
        "goal_amount": round(goal, 2),
        "months": months,
        "monthly_contribution": round(c, 2),
        "current_value": round(total, 2),
        "style": style, "exposure": exp, "sims": sims,
        "div_yield_annual_pct": round(float(div_yield_annual) * 100, 3),
        "div_tax_pct": round(tax * 100, 1),
        # 목표 달성 확률 — 기간 말 기준. 경로 중간에 넘었다가 내려온 것은 포함하지 않는다
        # (자산은 팔 때 확정되므로 '기간 말'이 정직한 기준이다).
        "success_pct": round(float((end >= goal).mean()) * 100, 1),
        "terminal": {f"p{p}": round(float(np.percentile(end, p)), 2) for p in (10, 50, 90)},
        "reach_month": reach,
        # 목표를 맞추는 데 필요한 월 적립액 · 지금 대비 부족분(음수면 여유)
        "required_monthly": need,
        "gap_monthly": gap,
        "fan": fan,
        # **무엇을 근거로 만든 경로인지 밝힌다.** 이 문장이 없으면 '예측'으로 읽힌다.
        "basis": "보유종목의 과거 일간수익률 복원추출(부트스트랩) — 기대수익률을 가정하지 않음",
        "caveat": "이 경로는 **지금 보유한 종목의 과거를 물려받습니다** — 시장 전망이 아니라 "
                  "'이 종목들이 과거처럼 계속하면'입니다. 승자를 들고 있으면 경로도 낙관적으로 "
                  "나옵니다. 배당 성장·환율·물가는 반영하지 않고, 배당은 세후로 재투자하며 "
                  "금융소득종합과세는 넣지 않았습니다.",
    }


def facts(div_items: list[dict], currency: str, *, plan_result: dict | None = None) -> list[dict]:
    """검증이 필요 없는 사실만 제안한다 — 판별력이 판정 보류인 동안 '오를 종목'은 말하지 않는다.

    각 항목: {kind, text, severity}. `kind`는 화면이 문자열을 파싱하지 않게 하려는 것이다.
    """
    out: list[dict] = []
    items = [d for d in div_items if d.get("currency") == currency and (d.get("annual") or 0) > 0]
    total = sum(d["annual"] for d in items)

    # ① 배당 집중도 — 한 종목이 배당의 큰 몫이면 그 종목 삭감이 곧 계획 붕괴다.
    if total > 0 and items:
        top = max(items, key=lambda d: d["annual"])
        share = top["annual"] / total * 100
        if share >= 35:
            out.append({"kind": "div_concentration", "severity": "warn",
                        "text": f"{top['name']} 하나가 배당의 {share:.0f}% — 이 종목이 배당을 줄이면 "
                                f"계획이 그만큼 밀립니다"})

    # ② 지급월 공백 — 월 현금흐름이 목표면 빈 달이 곧 결손이다.
    if items:
        months_paid: set[int] = set()
        unknown = 0
        for d in items:
            ms = d.get("div_months") or []
            if ms:
                months_paid.update(int(m) for m in ms)
            else:
                unknown += 1
        empty = [m for m in range(1, 13) if m not in months_paid]
        if empty and unknown == 0:
            out.append({"kind": "payout_gap", "severity": "info",
                        "text": f"배당이 없는 달: {', '.join(str(m) + '월' for m in empty)} "
                                f"— 그 달 현금흐름은 0입니다"})
        elif unknown:
            # 0의 이유 — '공백이 없다'와 '지급월을 모른다'는 다르다.
            out.append({"kind": "payout_unknown", "severity": "info",
                        "text": f"{unknown}종목은 지급월 정보가 없어 공백을 셀 수 없습니다"})

    # ③ 목표 대비 부족분 — 계산 결과를 그대로 문장으로. 추천이 아니라 산술이다.
    # **단위를 붙인다.** `월 9,242 더 넣으면` 은 원인지 달러인지 알 수 없다.
    money = (lambda v: f"${v:,.0f}") if currency == "USD" else (lambda v: f"{v:,.0f}원")
    if plan_result and plan_result.get("ready"):
        gap = (plan_result.get("gap_monthly") or {}).get("p50")
        if gap is not None and gap > 0:
            out.append({"kind": "contribution_gap", "severity": "warn",
                        "text": f"지금 적립액으로는 중간 경로(p50)가 목표에 못 미칩니다 — "
                                f"월 {money(gap)} 더 넣으면 p50이 목표에 닿습니다"})
        elif gap is not None:
            out.append({"kind": "contribution_ok", "severity": "good",
                        "text": f"중간 경로(p50)는 목표를 넘습니다 — 월 {money(abs(gap))} 여유"})
        bad = (plan_result.get("gap_monthly") or {}).get("p10")
        if bad is not None and bad > 0:
            out.append({"kind": "contribution_gap_bad", "severity": "info",
                        "text": f"나쁜 경로(p10)까지 목표에 닿으려면 월 {money(bad)} 더 필요합니다"})
    return out
