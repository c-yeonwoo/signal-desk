"""시점별(point-in-time) 재무 — 백테스트가 '그 날 알 수 있던' 재무만 쓰게 만든다.

## 왜 필요한가

가격 재계산 하네스는 `technical·reversion·momentum` 세 팩터만 잰다
(`harness._score_series`). 나머지를 뺀 이유는 `engine._price_only_components`의 주석 그대로다 —
"기본/저평가는 시점별 재무 스냅샷이 없어 범위 밖". 그래서 `harness_last.json`의
`fired_pct = {technical 0.0, reversion 2.4, momentum 78.4}`, 즉 실질적으로 **모멘텀 단독 랭킹**의
성적이 "8팩터 시그널의 판별력"으로 읽히고 있었다.

그런데 `fundamentals_history.json`에 **연도별 재무가 이미 있다**(199종목 × 2023~2025:
`roe·debt_ratio·revenue_growth·net_income·equity`). 없던 것은 데이터가 아니라
**"언제부터 알 수 있었나"** 였다.

## 공시 시점 규칙 — 왜 (Y+1)-04-01 인가

FY Y 사업보고서는 사업연도 종료 후 90일 내(=이듬해 3월 말) 제출이 법정기한이다.
`{'2024': …}` 를 2024-01-01부터 알았던 것처럼 쓰면 **최대 15개월 룩어헤드**다.

DART 공시목록 API에 실제 접수일(`rcept_dt`)이 있어(`ingest/dart.py:174`) 정밀 복원도 가능하지만,
그건 종목×연도만큼 API를 더 때려야 하고 키가 필요하다. 대신 **법정기한 기반 보수적 규칙**을 쓴다:

    FY Y 재무는 (Y+1)-04-01 부터 사용 가능

실제 공시는 대개 3월 중하순이므로 이 규칙은 **항상 실제보다 늦게** 열어준다 —
틀리는 방향이 안전한 쪽이다(룩어헤드를 만들지 않는다). 대가는 며칠~2주의 정보 지연이고,
그건 5일 보유 전략에서 판별력을 과소평가하게 만들 뿐 과대평가하지 않는다.

## 시가총액 근사

PER/PBR은 시점 시가총액이 필요한데 발행주식수 이력이 없다. 현재 시총과 현재가로 주식수를 역산해
**일정하다고 가정**한다: `shares ≈ mktcap_now / price_now`, `mktcap(t) ≈ shares × price(t)`.

액면분할·증자가 있으면 그 구간이 틀린다. 다만 (a) 라벨 치환 대조군이 **같은 근사를 공유**하므로
백분위 비교는 유효하고, (b) 틀리는 방향이 종목별로 무작위라 순위에 체계적 편향을 주지 않는다.
이 근사가 싫으면 `us_fundamentals` 처럼 발행주식수 이력을 따로 받아야 한다 — 지금은 없다.

## 남는 한계 (이 모듈이 해결하지 않는 것)

- **수급·공매도는 백필 불가.** `flows.json`·`short.json`은 시계열이 아니라 현재값 스냅샷 1개다.
  그래서 이 경로로 만들 수 있는 것은 **6팩터**이고 8팩터가 아니다(가중 0.35가 빠진다).
- **유니버스 생존편향은 별개 문제.** 유니버스가 "오늘 기준 시총 상위 200"인 것은 그대로다
  (BACKLOG §0 PIT 유니버스). 대조군이 같은 편향을 받으므로 백분위는 유효하지만 절대 수익률은 못 쓴다.
- 재무 이력이 3년치라 **2024-04 이전 구간은 재무가 없다** → 그 구간은 점수를 내지 않는다(None).
  조용히 3팩터로 떨어지는 것보다 아예 비우는 편이 정직하다 —
  하네스가 그 기간을 `empty_periods`로 세고 `effective_periods`에서 빼 준다.
"""

from __future__ import annotations

from signal_desk.signals import quality as fscore
from signal_desk.signals import valuation as val

# 사업보고서 법정기한(사업연도 종료 후 90일) + 여유 → 4월 1일부터 "알 수 있었다"고 본다.
DISCLOSURE_MONTH = 4


def latest_fiscal_year(date_str: str) -> int:
    """`date_str`(YYYY-MM-DD) 시점에 **알 수 있던** 가장 최근 사업연도.

    2026-08-05 → 2025 (FY2025는 2026-03에 공시) · 2026-03-15 → 2024 (FY2025는 아직)
    """
    y, m = int(date_str[:4]), int(date_str[5:7])
    return y - 1 if m >= DISCLOSURE_MONTH else y - 2


def shares_estimate(fundamentals_now: dict, price_now: dict[str, float]) -> dict[str, float]:
    """현재 시총 ÷ 현재가 → 발행주식수 근사. 둘 중 하나라도 없으면 제외."""
    out: dict[str, float] = {}
    for t, m in (fundamentals_now or {}).items():
        mc, px = (m or {}).get("mktcap"), price_now.get(t)
        if mc and px and px > 0:
            out[t] = mc / px
    return out


def metrics_at(hist: dict, date_str: str, *, shares: dict[str, float],
               price_at: dict[str, float]) -> dict[str, dict]:
    """그 날짜에 알 수 있던 재무 + 그 날 가격으로 계산한 PER/PBR + 축약 F-Score.

    반환: `{ticker: metrics}` — `engine._fundamental_component` · `fscore.component` ·
    `val.scores` 가 그대로 먹을 수 있는 모양. 재무가 없는 종목은 아예 넣지 않는다.
    """
    fy = latest_fiscal_year(date_str)
    cur_y, prev_y = str(fy), str(fy - 1)
    out: dict[str, dict] = {}
    for t, years in (hist or {}).items():
        cur = (years or {}).get(cur_y)
        if not cur:
            continue
        m = dict(cur)
        # 퀄리티는 당해·전년 비교다 — 전년이 없으면 fscore가 has=False로 돌려준다(가중 0).
        m["quality"] = fscore.evaluate(cur, (years or {}).get(prev_y) or {})
        px, sh = price_at.get(t), shares.get(t)
        if px and sh:
            mktcap = px * sh
            ni, eq = m.get("net_income"), m.get("equity")
            if ni and ni > 0:
                m["per"] = round(mktcap / ni, 2)
            if eq and eq > 0:
                m["pbr"] = round(mktcap / eq, 2)
        out[t] = m
    return out


def components_at(ticker: str, metrics: dict | None, val_scores: dict[str, float], config
                  ) -> list[tuple[float, float, list[str]]]:
    """PIT 재무에서 나오는 3컴포넌트 — 재무 · 저평가 · 퀄리티.

    라이브 `evaluate`와 **같은 함수**를 쓴다(`_fundamental_component` · `_valuation_component` ·
    `fscore.component`). 백테스트가 별도 공식을 쓰면 무엇을 검증한 건지 알 수 없다.
    """
    from signal_desk.signals import engine

    fund_norm, fund_w, fund_reasons = engine._fundamental_component(metrics, config)
    val_norm, val_w, val_reasons, _, _ = engine._valuation_component(ticker, val_scores, config)
    ql_norm, ql_w, ql_reasons, _, _ = fscore.component(metrics, config.weight_quality)
    return [(fund_norm, fund_w, fund_reasons),
            (val_norm, val_w, val_reasons),
            (ql_norm, ql_w, ql_reasons)]


def valuation_scores_at(metrics: dict[str, dict], universe: list[dict] | None = None
                        ) -> dict[str, float]:
    """그 날짜 횡단면 저평가 percentile(섹터 중립). 라이브와 같은 `val.scores`."""
    return val.scores(universe or [], metrics)
