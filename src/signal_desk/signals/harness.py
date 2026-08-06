"""포트폴리오 백테스트 하네스 — "적극적으로 거래하면 정말 버는가"에 숫자로 답하기 위한 도구.

## 왜 기존 백테스트로는 안 되나

`engine.backtest_summary`는 종목별 적중률을 센다. 매수권을 절대 문턱으로 판정하고, 같은 종목의
매일치 신호를 독립 표본처럼 세며(구간이 겹쳐 서로 의존), 거래비용도 포트폴리오도 없다.
2026-07-26에 도입한 **횡단면 분위 + 국면 익스포저**는 종목이 아니라 포트폴리오 수준의 규칙이라
그 방식으로는 검증 자체가 불가능하다.

## 편향을 없앨 수 없으면 상쇄한다

우리 유니버스(`universe.json`)는 **오늘 기준 시총 상위 200종목**이고, 과거 편입 이력은 공개
API에 없다(BACKLOG §0). 즉 생존편향을 제거할 방법이 없다. 그래서 절대 수익률은 어차피 부풀려진다
— 실제로 소박한 모멘텀 규칙도 이 데이터에서 연율 세 자릿수가 나온다.

대신 **같은 편향을 공유하는 대조군과 비교**한다:

1. `random` — **같은 시뮬레이터에 라벨을 치환한 점수**를 넣어 돌리는 몬테카를로. 생존편향뿐
   아니라 회전율·거래비용·게이트·보유 종목 수까지 전략과 같은 분포로 유지되므로, 남는 차이는
   점수의 정보량뿐이다. 백분위(`vs_random.percentile`)가 사실상의 p-value다.
   (매 기간 무작위 k종목을 새로 뽑는 대조군은 회전율이 늘 100%라 비용을 최대로 물어, 판별력이
   전혀 없는 전략도 백분위 100%로 만들어준다 — 실측으로 확인하고 폐기했다.)
2. `benchmark` — 같은 유니버스 동일가중 매수보유. 시장 수준(level) 효과를 상쇄한다.

절대 수익률은 보고하되 **판단 근거로 쓰지 않는다.** 판단은 무작위 대조군 대비 초과분으로 한다.

## 룩어헤드 차단

- t 시점 점수는 `closes[:t+1]`만 쓴다(`engine._price_only_components` 재사용 — 라이브 엔진과 같은 함수).
- 재무·저평가·수급·정성 팩터는 시점별 스냅샷이 없어 **제외**한다(있는 척하면 그게 룩어헤드다).
  따라서 여기서 검증되는 것은 가격기반 팩터(기술·낙폭과대·모멘텀)로 만든 순위의 판별력이다.
- 진입가는 신호 **다음 거래일 종가**. 비용은 왕복 `cost_pct`를 회전율에 비례해 차감.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from signal_desk.signals import engine, multiplicity, regime as regime_mod
from signal_desk.signals.engine import SignalConfig


@dataclass
class HarnessConfig:
    top_pct: float = 3.0          # 매수권 분위(엔진 rank_top_pct와 같은 의미)
    min_score: float = 1.2        # 매수권 최소점수(엔진 rank_min_score와 동기화)
    rebalance_days: int = 5       # 리밸런스 주기(거래일). 보유기간과 같다.
    cost_pct: float = 0.25        # 왕복 거래비용(수수료+세금+슬리피지) — 회전율에 비례 차감
    warmup: int = 130             # 지표가 안정되기 전 구간은 건너뛴다(MA120·모멘텀)
    use_exposure: bool = False    # 국면 익스포저 적용 여부(나머지는 현금, 무이자 가정)
    random_trials: int = 100      # 대조군 시행 수(시행마다 전 위상을 다시 시뮬레이션한다)
    seed: int = 20260726
    invert_scores: bool = False   # 진단용 — 순위를 뒤집어도 되는지(체계적 음의 판별력 확인)
    phase_average: bool = True    # 리밸런스 시작일(위상)을 전부 돌려 평균 — 아래 주석 참고
    shuffle_returns: bool = False # 누수 탐지용 — 점수와 수익률의 짝을 무작위로 어긋나게 한다
    min_periods: int = 30         # 이보다 표본이 적으면 어떤 결과도 판정하지 않는다
    signal_config: SignalConfig = field(default_factory=SignalConfig)


@dataclass
class Panel:
    """날짜축이 정렬된 종가 패널. closes[ticker][i]는 dates[i]의 종가(없으면 None)."""
    dates: list[str]
    closes: dict[str, list[float | None]]

    def __len__(self) -> int:
        return len(self.dates)


def build_panel(dated_closes: dict[str, tuple[list[str], list[float]]],
                tickers: set[str] | None = None) -> Panel:
    """{ticker: (dates, closes)} → 공통 날짜축 패널. 결측은 직전 값으로 채우되,
    **상장 이전(첫 데이터 이전)은 None으로 남긴다** — 0이나 첫 종가로 채우면 없던 종목을
    보유한 것처럼 계산된다.

    tickers를 주면 그 종목만 남긴다. **시장은 반드시 하나로 좁혀야 한다** — 국내·미국을 섞으면
    거래일 달력이 달라 휴장일이 직전 종가로 채워지고, 무엇보다 횡단면 순위가 서로 다른 시장의
    종목을 한 줄로 세우게 된다(우리가 검증하려는 규칙이 아니다).
    """
    if tickers is not None:
        dated_closes = {t: v for t, v in dated_closes.items() if t in tickers}
    all_dates = sorted({d for ds, _ in dated_closes.values() for d in ds})
    idx = {d: i for i, d in enumerate(all_dates)}
    out: dict[str, list[float | None]] = {}
    for ticker, (ds, cs) in dated_closes.items():
        row: list[float | None] = [None] * len(all_dates)
        for d, c in zip(ds, cs):
            row[idx[d]] = float(c)
        last: float | None = None
        for i, v in enumerate(row):
            if v is None:
                row[i] = last          # 거래정지·휴장 보정(상장 전이면 last=None 유지)
            else:
                last = v
        out[ticker] = row
    return Panel(dates=all_dates, closes=out)


def _computable(name: str, i: int, config: SignalConfig) -> bool:
    """i 시점에 이 팩터를 **계산할 이력이 있는가**(발동 여부와 무관).

    발동률과 계산가능률을 갈라놓는 이유: 낙폭과대는 급락이 있을 때만 가중치를 갖는 조건부
    팩터라 평상시 발동률이 몇 %에 머문다(설계대로다). 이걸 '이력 부족'으로 읽으면 데이터를
    아무리 더 받아도 영원히 판정이 막힌다. 이력 부족(모멘텀 252일)만 차단 사유여야 한다."""
    if name == "reversion":
        return i >= config.reversion.lookback_days
    if name == "momentum":
        return i >= config.momentum_lookback
    return True                                # technical — 워밍업 이후 항상 계산됨


# 가격 재계산 경로가 **원리적으로** 볼 수 없는 팩터(이력 자체가 없다). 종목의 결함이 아니므로
# 커버리지 분모에서 뺀다 — `data_coverage(unavailable=)` 주석 참고.
_PRICE_UNAVAILABLE = ("fundamental", "valuation", "flow", "quality", "short")
_PIT_UNAVAILABLE = ("flow", "short")


def _score_series(panel: Panel, config: SignalConfig
                  ) -> tuple[dict[str, list[float | None]], dict[str, float],
                             dict[str, float], dict[str, list[float | None]]]:
    """종목별 전 구간 가격기반 점수 + 팩터 계산가능률 + 발동률 + (종목·날짜)별 데이터 커버리지.

    라이브 엔진과 같은 `_price_only_components`·`combine`을 쓴다 — 백테스트가 별도 공식을 쓰면
    무엇을 검증한 건지 알 수 없다.

    커버리지를 함께 세는 이유: 모멘텀은 252거래일 이력을 요구하는데 우리 캐시는 그보다 짧을 수
    있다. 그러면 그 팩터는 대부분의 시점에서 **가중치 0으로 조용히 빠지고**, 결과 표에는
    "모멘텀 전략"이라고 적힌 빈 칸이 남는다. 커버리지가 낮은 팩터의 숫자는 읽지 말아야 한다.
    """
    scores: dict[str, list[float | None]] = {}
    cov: dict[str, list[float | None]] = {}
    names = ("technical", "reversion", "momentum")
    can = dict.fromkeys(names, 0)              # 계산 가능(이력 충분)
    fired = dict.fromkeys(names, 0)            # 실제 가중치가 붙음
    total = 0
    for ticker, row in panel.closes.items():
        vals = [v for v in row if v is not None]
        if len(vals) < 60:
            continue
        offset = len(row) - len(vals)          # 상장 이전 구간 길이
        series = engine.compute_indicator_series(vals, config)
        out: list[float | None] = [None] * len(row)
        cov[ticker] = [None] * len(row)
        for i in range(len(vals)):
            comps = engine._price_only_components(vals, series, i, config)
            for name, (_, w, _) in zip(names, comps):
                if w:
                    fired[name] += 1
                if _computable(name, i, config):
                    can[name] += 1
            total += 1
            out[offset + i] = engine.combine(comps, config)["score"]
            # 커버리지 게이트를 라이브와 대칭으로 걸려면 (종목·날짜)별 커버리지가 필요하다.
            # 가격 경로가 원리적으로 볼 수 없는 팩터는 분모에서 뺀다 — 안 빼면 전 종목이
            # 미달로 매수 0이 되고 게이트가 검증 불가가 된다.
            cov[ticker][offset + i] = engine.data_coverage(
                {"technical": True, "momentum": _computable("momentum", i, config)},
                config, unavailable=_PRICE_UNAVAILABLE)["ratio"]
        scores[ticker] = out
    if not total:
        return scores, {}, {}, cov
    pct = lambda d: {k: round(v / total * 100, 1) for k, v in d.items()}  # noqa: E731
    return scores, pct(can), pct(fired), cov


def scores_with_pit_fundamentals(
    panel: Panel, config: SignalConfig, hist: dict, *,
    shares: dict[str, float], universe: list[dict] | None = None,
    universe_at: "Callable[[str], set[str] | None] | None" = None,
    mktcap_anchors: dict | None = None, price_on: dict | None = None,
) -> tuple[dict[str, list[float | None]], dict[str, float], dict[str, float], dict,
           dict[str, list[float | None]]]:
    """가격 3팩터 + **시점별 재무** 3팩터 = 6팩터 점수 시계열.

    가격 재계산 하네스가 3팩터(사실상 모멘텀 단독)인 이유는 재무 이력이 없어서가 아니라
    **"언제부터 알 수 있었나"가 없어서**였다. `pit_fundamentals`가 그 매핑을 준다
    (FY Y → (Y+1)-04-01 부터 사용 가능, 법정기한 기반 보수적 규칙).

    재무가 없는 날짜는 점수를 **내지 않는다(None)**. 조용히 3팩터로 떨어뜨리면 표에는
    "6팩터"라고 적힌 3팩터 결과가 남는다 — 그게 이 파일이 반복해서 경계하는 실패다.
    비운 날짜는 `run`이 `empty_periods`로 세고 `effective_periods`에서 빼 준다.

    수급·공매도는 여기 없다. `flows.json`·`short.json`이 시계열이 아니라 현재값 스냅샷이라
    **백필이 원리적으로 불가능**하다 — 그래서 이것은 6팩터이고 8팩터가 아니다.

    `universe_at(date) -> set[ticker] | None` 을 주면 **그 날 유니버스 밖 종목은 점수를 내지 않는다**
    (생존편향 제거, N5). None 을 돌려주는 날짜는 스냅샷이 없는 구간이므로 아무 점수도 내지 않는다 —
    오늘 유니버스로 폴백하지 않는다(그러면 그 구간만 편향이 남고 결과가 뭘 재는지 불분명해진다).
    대조군(`_permuted_scores`)은 각 티커의 **자기 가용성을 유지**하므로 같은 제약을 공유한다.
    """
    from signal_desk.signals import pit_fundamentals as pf

    names = ("technical", "reversion", "momentum", "fundamental", "valuation", "quality")
    can = dict.fromkeys(names, 0)
    fired = dict.fromkeys(names, 0)
    total = 0

    prepared: dict[str, tuple[list[float], int, dict]] = {}
    for ticker, row in panel.closes.items():
        vals = [v for v in row if v is not None]
        if len(vals) < 60:
            continue
        prepared[ticker] = (vals, len(row) - len(vals),
                            engine.compute_indicator_series(vals, config))

    scores: dict[str, list[float | None]] = {t: [None] * len(panel) for t in prepared}
    cov: dict[str, list[float | None]] = {t: [None] * len(panel) for t in prepared}
    # 재무 기본값(연도별)은 사업연도가 바뀔 때만 다시 만든다. PER/PBR·저평가 percentile 은
    # 가격이 움직이므로 날짜마다 다시 계산한다.
    fy_cache: dict[int, dict[str, dict]] = {}
    first_date = None
    dates_with_fund = 0

    for i, date_str in enumerate(panel.dates):
        fy = pf.latest_fiscal_year(date_str)
        base = fy_cache.get(fy)
        if base is None:
            base = pf.metrics_at(hist, date_str, shares={}, price_at={})   # 가격 없이 재무+퀄리티만
            fy_cache[fy] = base
        if not base:
            continue                                  # 그 시점엔 알 수 있는 재무가 없다 → 점수 없음
        price_at = {t: prepared[t][0][i - prepared[t][1]]
                    for t in prepared
                    if 0 <= i - prepared[t][1] < len(prepared[t][0])}
        metrics: dict[str, dict] = {}
        for t, m in base.items():
            if t not in price_at:
                continue
            mm = dict(m)
            # 시가총액은 **시점 앵커**를 우선한다(월 스냅샷의 mktcap). 앵커가 없으면 주식수 근사로
            # 폴백하는데, 폐지·이탈 종목은 현재 시총이 없어 폴백도 안 된다 — 그러면 저평가가 빠지고
            # 재정규화 편향으로 극단 점수가 나온다(2026-08-05 실측: 매수권 6자리 중 4.62자리).
            mktcap = pf.mktcap_at(t, date_str, price_at[t], anchors=mktcap_anchors or {},
                                  price_on=price_on, shares=shares)
            if mktcap:
                ni, eq = mm.get("net_income"), mm.get("equity")
                if ni and ni > 0:
                    mm["per"] = round(mktcap / ni, 2)
                if eq and eq > 0:
                    mm["pbr"] = round(mktcap / eq, 2)
            metrics[t] = mm
        if not metrics:
            continue
        uni_set = universe_at(date_str) if universe_at is not None else None
        if universe_at is not None and not uni_set:
            continue                              # 스냅샷 없는 구간 → 점수 없음(폴백하지 않는다)
        if uni_set is not None:
            metrics = {t: m for t, m in metrics.items() if t in uni_set}
            if not metrics:
                continue
        val_scores = pf.valuation_scores_at(metrics, universe)
        if first_date is None:
            first_date = date_str
        dates_with_fund += 1
        for ticker, (vals, offset, series) in prepared.items():
            if uni_set is not None and ticker not in uni_set:
                continue                          # 그 날 유니버스 밖 — 후보가 아니다
            j = i - offset
            if j < 0 or j >= len(vals):
                continue
            comps = engine._price_only_components(vals, series, j, config)
            comps = comps + pf.components_at(ticker, metrics.get(ticker), val_scores, config)
            for name, (_, w, _) in zip(names, comps):
                if w:
                    fired[name] += 1
            for name in ("technical", "reversion", "momentum"):
                if _computable(name, j, config):
                    can[name] += 1
            # 재무 3팩터의 '계산 가능'은 그날 그 종목에 재무가 있었는가로 센다.
            m = metrics.get(ticker)
            if m:
                can["fundamental"] += 1
                if m.get("per") is not None and m.get("pbr") is not None:
                    can["valuation"] += 1
                if (m.get("quality") or {}).get("has"):
                    can["quality"] += 1
            total += 1
            scores[ticker][i] = engine.combine(comps, config)["score"]
            # 라이브와 **같은** 커버리지 게이트를 걸려면 (종목·날짜)별 커버리지가 필요하다.
            # 수급·공매도는 이 경로가 원리적으로 못 보므로 분모에서 뺀다.
            cov[ticker][i] = engine.data_coverage(
                {"technical": True, "momentum": _computable("momentum", j, config),
                 "fundamental": bool(m), "quality": bool((m or {}).get("quality", {}).get("has")),
                 "valuation": bool(m and m.get("per") is not None and m.get("pbr") is not None)},
                config, unavailable=_PIT_UNAVAILABLE)["ratio"]

    meta = {"fund_from": first_date, "fund_dates": dates_with_fund,
            "universe_mode": "pit" if universe_at is not None else "today",
            "fiscal_years": sorted(str(y) for y, v in fy_cache.items() if v),
            "note": ("수급·공매도는 시계열 이력이 없어 제외 — 6팩터다. "
                     "재무는 (FY+1)-04-01 부터 사용(법정기한 기반 보수적 규칙).")}
    if not total:
        return scores, {}, {}, meta, cov
    pct = lambda d: {k: round(v / total * 100, 1) for k, v in d.items()}  # noqa: E731
    return scores, pct(can), pct(fired), meta, cov


def _gated(panel: Panel, ticker: str, i: int, config: SignalConfig,
           market_ret: float | None, series_cache: dict) -> bool:
    """라이브와 같은 상대 추세 게이트. 시장 대비 상대강도 우위면 막지 않는다.

    `config.trend_gate == 0` 이면 적용하지 않는다 — 라이브 `_apply_trend_gate` 와 같은 스위치를
    본다. 하네스만 따로 끄는 플래그를 두면 그게 곧 "라이브와 다른 전략을 재는" 경로다.
    """
    if not float(getattr(config, "trend_gate", 1.0) or 0.0):
        return False
    row = panel.closes[ticker]
    vals = [v for v in row if v is not None]
    offset = len(row) - len(vals)
    j = i - offset
    if j < 0 or j >= len(vals):
        return True
    series = series_cache.get(ticker)
    if series is None:
        series = engine.compute_indicator_series(vals, config)
        series_cache[ticker] = series
    return engine._downtrend_blocking(vals, series, j, config, market_ret)


def _market_ret_at(panel: Panel, i: int, n: int = 20) -> float | None:
    """i 시점 유니버스 n일 수익률 중위값(%)."""
    rets = []
    for row in panel.closes.values():
        a, b = (row[i - n] if i - n >= 0 else None), row[i]
        if a and b:
            rets.append((b / a - 1) * 100)
    if not rets:
        return None
    rets.sort()
    m = len(rets) // 2
    return rets[m] if len(rets) % 2 else (rets[m - 1] + rets[m]) / 2


def _rebalance_indices(panel: Panel, cfg: HarnessConfig, phase: int = 0) -> list[int]:
    """리밸런스 날짜. phase는 시작일을 며칠 미루는지다.

    위상을 나눠 도는 이유(2026-07-26에 이걸로 데였다): 같은 데이터·같은 규칙인데 리밸런스를
    6거래일 늦게 시작했더니 5일 보유 누적이 -4.7% → +61.3%로 뒤집혔다. 27번 남짓한 리밸런스
    표본에서는 "언제 갈아타느냐"라는 우연이 전략 자체보다 결과를 크게 좌우한다. 위상 전부를
    돌려 평균 내고 **위상 간 편차를 함께 보고**해야 그 우연을 결과로 착각하지 않는다.
    """
    return list(range(cfg.warmup + phase, len(panel) - cfg.rebalance_days - 1,
                      cfg.rebalance_days))


def _period_return(panel: Panel, tickers: list[str], i: int, cfg: HarnessConfig,
                   alias: dict[str, str] | None = None) -> float:
    """i 다음 거래일 종가 진입 → rebalance_days 뒤 종가 청산. 동일가중 평균 수익률.

    alias는 누수 탐지용 — 종목의 점수는 그대로 두고 **수익률만 다른 종목 것으로 바꿔치기**한다.
    """
    rets = []
    for t in tickers:
        row = panel.closes[alias[t] if alias else t]
        entry, exit_ = row[i + 1], row[min(i + 1 + cfg.rebalance_days, len(panel) - 1)]
        if entry and exit_:
            rets.append(exit_ / entry - 1)
    return sum(rets) / len(rets) if rets else 0.0


def _metrics(equity: list[float], periods_per_year: float) -> dict:
    if len(equity) < 2:
        return {"total_ret_pct": 0.0, "cagr_pct": 0.0, "mdd_pct": 0.0, "sharpe": 0.0}
    total = equity[-1] / equity[0] - 1
    years = (len(equity) - 1) / periods_per_year
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [equity[k + 1] / equity[k] - 1 for k in range(len(equity) - 1)]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 0.0
    return {"total_ret_pct": round(total * 100, 1), "cagr_pct": round(cagr * 100, 1),
            "mdd_pct": round(mdd * 100, 1),
            "sharpe": round(mu / sd * math.sqrt(periods_per_year), 2) if sd else 0.0}


def _shuffled_alias(tickers: list[str], rng: random.Random) -> dict[str, str]:
    """점수와 수익률의 짝을 어긋나게 하는 치환. 누수 탐지의 핵심 도구다.

    점수가 미래 정보를 몰래 보고 있지 않다면, 짝을 무작위로 바꿨을 때 판별력은 반드시 사라진다.
    셔플하고도 `판별력 있음`이 나오면 그건 실력이 아니라 누수다. 사람이 코드를 읽어서 룩어헤드를
    찾는 것보다 이 검사 하나가 확실하다.
    """
    shuffled = tickers[:]
    rng.shuffle(shuffled)
    return dict(zip(tickers, shuffled))


def _weighted_factors(config: SignalConfig) -> set[str]:
    """이 설정에서 실제로 가중치가 걸린 가격기반 팩터 — 커버리지 차단 대상."""
    return {name for name, w in (("technical", config.weight_technical),
                                 ("reversion", config.weight_reversion),
                                 ("momentum", config.weight_momentum)) if w}


def _dsr_sample(runs: list[dict]) -> list[float]:
    """DSR의 표본 — **한 위상의 초과수익**(전략 − 벤치마크).

    두 선택 다 실측으로 정한 것이다(2026-08-06):

    1) **위상을 이어 붙이지 않는다.** 처음엔 5위상을 연결해 T=1093으로 썼더니 `sqrt(T−1)`이
       33.0이 되어(독립 219 기준 14.8) z가 2.2배 부풀고 **DSR 0.979 "유의"** 가 나왔다.
       같은 실행의 백분위는 71.5(판정 불가)였다 — 두 판정이 갈라지면 관대한 쪽이 읽힌다.
    2) **초과수익으로 잰다.** DSR은 `Sharpe > 0`을 검정하므로, 롱온리 전략은 상승장에서
       종목선택 능력이 없어도 Sharpe가 양수다. 벤치마크를 빼면 시장 베타가 빠지고
       "같은 유니버스를 그냥 사는 것보다 나은가"만 남는다 — 그게 물어볼 값어치가 있는 질문이다.
    """
    if not runs:
        return []
    a = runs[0].get("per_period_ret") or []
    b = runs[0].get("bench_per_period_ret") or []
    if len(a) != len(b):
        return []
    return [x - y for x, y in zip(a, b)]


def _run_phase(panel: Panel, cfg: HarnessConfig, scores: dict, idxs: list[int],
               regimes: dict[int, str] | None, series_cache: dict, tie_rng: random.Random,
               covers: dict[str, list[float | None]] | None = None) -> dict:
    """한 위상(고정된 리밸런스 날짜 집합)에 대한 전략·벤치마크 시뮬레이션.

    `covers`는 (종목·날짜)별 데이터 커버리지다. **대조군에도 같은 것을 넘긴다** — 라벨을
    치환해도 커버리지는 티커에 붙어 있어야 기계적 조건이 같고, 남는 차이가 점수의 정보량뿐이다.
    한쪽만 게이트를 걸면 그 차이가 판별력으로 둔갑한다(#326·#329에서 두 번 겪었다).
    """
    scfg = cfg.signal_config
    equity, bench, picks_log = [1.0], [1.0], []
    held: set[str] = set()
    per_period_ret: list[float] = []
    universe_by_date: dict[int, list[str]] = {}
    empty_periods = 0
    cov_blocked = trend_blocked = 0
    min_cov = float(getattr(scfg, "min_data_coverage", 0.0) or 0.0)

    for i in idxs:
        avail = [t for t, row in panel.closes.items()
                 if row[i] is not None and row[i + 1] is not None and scores.get(t)
                 and scores[t][i] is not None]
        universe_by_date[i] = avail
        k = engine.rank_slots(len(avail), cfg.top_pct)
        market_ret = _market_ret_at(panel, i)
        # 동점은 무작위로 가른다. 정렬만 하면 동점 구간이 유니버스 순서(=시총 순)로 정렬돼
        # "점수가 없을 때 대형주를 사는" 전략이 몰래 섞인다.
        shuffled = avail[:]
        tie_rng.shuffle(shuffled)
        ranked = sorted(shuffled, key=lambda t: scores[t][i], reverse=True)
        picks: list[str] = []
        # **후보는 상위 k자리뿐이다.** 예전엔 `ranked` 전체를 돌며 `len(picks) >= k` 에서 끊어,
        # 게이트로 막힌 자리를 **k 밖 다음 순위로 채웠다**. 라이브 `apply_cross_sectional` 은
        # 그 자리를 공석으로 둔다("k 밖 약한 종목으로 채우지 않는다"). 그래서 하네스의 게이트는
        # 사실상 재정렬이었고 보유 수를 줄이지 못했다 — 실측: 추세 게이트가 1076회 걸렸는데
        # 켜고 끈 결과의 백분위·매수0기간·평균보유수가 **완전히 같았다**(90.0 · 529 · 3.1).
        # 같은 날 라이브는 매수권 0/6자리였다. 게이트 없는 전략을 재고 있었던 것이다.
        for t in ranked[:k]:
            if scores[t][i] < cfg.min_score:
                continue
            if _gated(panel, t, i, scfg, market_ret, series_cache):
                trend_blocked += 1
                continue
            # 라이브 `apply_cross_sectional`과 같은 커버리지 게이트(X2). 커버리지를 모르면
            # 막지 않는다 — 전 종목 차단은 신중함이 아니라 0으로 나누기다.
            c = (covers or {}).get(t)
            cv = c[i] if (c and i < len(c)) else None
            if min_cov > 0 and cv is not None and cv < min_cov:
                cov_blocked += 1
                continue
            picks.append(t)

        if picks:
            alias = _shuffled_alias(avail, tie_rng) if cfg.shuffle_returns else None
            gross = _period_return(panel, picks, i, cfg, alias)
            if cfg.use_exposure and regimes is not None:
                exp = regime_mod.target_exposure({"regime": regimes.get(i)}, None)["exposure"]
                gross *= exp
            turnover = 1.0 if not held else len(set(picks) - held) / len(picks)
            net = gross - (cfg.cost_pct / 100) * turnover
        else:
            net = 0.0                 # 현금. 안 산 기간에 거래비용을 물리면 안 된다.
            empty_periods += 1
        held = set(picks)
        per_period_ret.append(net)
        equity.append(equity[-1] * (1 + net))
        bench.append(bench[-1] * (1 + _period_return(panel, avail, i, cfg)))
        picks_log.append({"date": panel.dates[i], "n_universe": len(avail),
                          "k": k, "picks": len(picks)})

    ppy = 252 / cfg.rebalance_days
    return {"equity": equity, "bench": bench, "metrics": _metrics(equity, ppy),
            # 기간별 순수익률 — Deflated Sharpe(L3)가 왜도·첨도를 여기서 재고, SPA(L2)가
            # 벤치마크와의 차이(loss differential)를 여기서 만든다. 요약값만으론 둘 다 못 한다.
            "per_period_ret": per_period_ret,
            "bench_per_period_ret": [bench[k + 1] / bench[k] - 1 for k in range(len(bench) - 1)],
            "bench_metrics": _metrics(bench, ppy), "universe_by_date": universe_by_date,
            "empty_periods": empty_periods, "periods": len(idxs),
            # 커버리지 게이트가 실제로 몇 번 후보를 걸렀나. 0이면 완화가 아무 것도 안 막은 것 —
            # "있는 것처럼 보이지만 효과 없는 게이트"를 여기서 드러낸다.
            "coverage_blocked": cov_blocked, "min_data_coverage": min_cov,
            # 게이트가 실제로 몇 번 후보를 걸렀나(X3). 0이면 하네스는 게이트 없는 전략을 잰 것이다.
            "trend_blocked": trend_blocked,
            "win_rate_pct": round(sum(1 for r in per_period_ret if r > 0)
                                  / len(per_period_ret) * 100, 1) if per_period_ret else 0.0,
            "avg_picks": round(sum(p["picks"] for p in picks_log) / len(picks_log), 1)
            if picks_log else 0.0}


def scores_from_pit(panel: Panel, history_rows: list[dict]
                    ) -> tuple[dict[str, list[float | None]], dict]:
    """PIT `signal_history` 점수 → 패널 날짜축. 스냅샷 없는 날은 None.

    가격 재계산 하네스는 fund/flow를 못 넣는다(룩어헤드). PIT 점수는 그날 라이브가 쓴
    전 팩터 합성이라, 스냅샷이 쌓인 구간에서만 그 순위의 포트폴리오 판별력을 잰다.
    """
    idx = {d: i for i, d in enumerate(panel.dates)}
    scores: dict[str, list[float | None]] = {t: [None] * len(panel) for t in panel.closes}
    filled = 0
    dates_hit: set[str] = set()
    for r in history_rows:
        t = str(r.get("ticker") or "")
        d = str(r.get("date") or "")
        if t not in scores or d not in idx:
            continue
        try:
            v = float(r["score"])
        except (TypeError, ValueError, KeyError):
            continue
        if not math.isfinite(v):
            continue
        scores[t][idx[d]] = v
        filled += 1
        dates_hit.add(d)
    meta = {
        "pit_cells": filled,
        "pit_dates": len(dates_hit),
        "pit_from": min(dates_hit) if dates_hit else None,
        "pit_to": max(dates_hit) if dates_hit else None,
        "coverage_pct": round(filled / max(1, len(panel) * max(1, len(panel.closes))) * 100, 2),
    }
    return scores, meta


def run(panel: Panel, cfg: HarnessConfig | None = None,
        regimes: dict[int, str] | None = None,
        scores: dict[str, list[float | None]] | None = None,
        score_source: str = "price",
        coverage: dict[str, float] | None = None,
        fired: dict[str, float] | None = None,
        covers: dict[str, list[float | None]] | None = None,
        n_trials: int | None = None,
        sr_variance: float | None = None) -> dict:
    """전략(횡단면 분위 top N%) + 무작위 대조군 + 동일가중 벤치마크를 같은 날짜축에서 비교.

    리밸런스 위상을 전부 돌려 평균 내고(`phase_average`), 위상 간 편차를 함께 낸다. 편차가
    초과수익보다 크면 그 초과수익은 규칙이 아니라 달력이 만든 것이므로 `verdict`가 판정 불가로
    떨어진다.

    regimes: {rebalance_index: 국면라벨}. use_exposure=True일 때만 쓰인다.
    scores: 외부 점수 패널(PIT 등). 주면 가격 재계산을 건너뛴다.
    score_source: \"price\" | \"pit\" — 결과에 기록(판독용).
    n_trials: 지금까지 돌려본 **서로 다른 설정 수**(L4 — `db.harness_trial_counts`). Deflated
        Sharpe가 이 수로 "고르기"를 보정한다. 하네스가 DB를 직접 읽지 않는 이유는 이 함수가 순수
        함수여야 검사에 넣을 수 있기 때문이다 — 호출자(`store.run_harness`)가 세어 넘긴다.
    sr_variance: 시도들 간 Sharpe 분산(실측). 없으면 이론값 1/(T−1)로 대체하고 어느 쪽인지 남긴다.
    covers: (종목·날짜)별 데이터 커버리지. 라이브 `apply_cross_sectional`과 같은 커버리지
      게이트를 걸기 위해 필요하다. **안 주면 게이트가 안 걸린다** — 그러면 하네스는 라이브가
      돌리지 않는 전략을 재는 것이므로 `coverage_blocked`를 결과에 실어 드러낸다.
    """
    cfg = cfg or HarnessConfig()
    if scores is None:
        scores, coverage, fired, covers = _score_series(panel, cfg.signal_config)
        score_source = "price"
    else:
        # 외부 점수(PIT 스냅샷·PIT 재무)도 커버리지를 받으면 차단 대상이 된다. 안 주면 빈 dict —
        # 그러면 `weak_factors`가 비어 "이름과 다른 전략을 측정한 것"을 못 잡는다.
        coverage, fired = dict(coverage or {}), dict(fired or {})
        score_source = score_source or "external"
    if cfg.invert_scores:
        scores = {t: [(-v if v is not None else None) for v in row] for t, row in scores.items()}
    phases = range(cfg.rebalance_days) if cfg.phase_average else [0]
    series_cache: dict = {}
    tie_rng = random.Random(cfg.seed)

    runs, phase_idxs = [], []
    for ph in phases:
        idxs = _rebalance_indices(panel, cfg, ph)
        if not idxs:
            continue
        # PIT 모드는 스냅샷 날짜에만 점수가 있다 — 그 날이 하나도 없는 위상은 건너뛴다.
        if score_source == "pit":
            idxs = [i for i in idxs
                    if any(scores.get(t) and scores[t][i] is not None for t in panel.closes)]
            if not idxs:
                continue
        phase_idxs.append(idxs)
        runs.append(_run_phase(panel, cfg, scores, idxs, regimes, series_cache, tie_rng, covers))
    if not runs:
        reason = f"표본 부족 — 거래일 {len(panel)}일"
        if score_source == "pit":
            reason = "PIT 스냅샷이 리밸런스 구간에 없음 — 마감 스냅샷이 더 쌓여야 한다"
        return {"ready": False, "reason": reason}

    ppy = 252 / cfg.rebalance_days
    totals = [(r["equity"][-1] - 1) * 100 for r in runs]
    strat_total = sum(totals) / len(totals)
    spread = max(totals) - min(totals)
    rnd = _null_distribution(panel, cfg, scores, phase_idxs, regimes, series_cache, covers)
    better = sum(1 for r in rnd["totals"] if r < strat_total)
    percentile = round(better / len(rnd["totals"]) * 100, 1) if rnd["totals"] else None
    excess = strat_total - rnd["median"]

    weighted = (_weighted_factors(cfg.signal_config)
                if score_source in ("price", "price6") else set())
    weak = [n for n, pct in coverage.items() if pct < 60 and n in weighted]
    warnings = [f"{name} 이력 커버리지 {coverage[name]}% — 대부분의 시점에서 계산조차 안 됐다. "
                f"이 팩터의 결과는 읽지 말 것" for name in weak]
    # 이력은 충분한데 거의 발동하지 않은 조건부 팩터 — 차단 사유는 아니지만, 그 팩터가 결과를
    # 설명한다고 읽으면 안 된다(사실상 다른 팩터 단독 전략이었다).
    warnings += [f"{n} 발동률 {fired[n]}% — 이력은 충분하나 조건이 거의 걸리지 않았다. "
                 f"이 결과를 {n} 팩터의 성적으로 읽지 말 것"
                 for n in weighted if n not in weak and fired.get(n, 0) < 10]
    empty = sum(r["empty_periods"] for r in runs)
    # 실효 기간 = 매수가 실제로 있던 리밸런스 횟수. 위상마다 다르므로 **최악 위상**을 쓴다 —
    # 이 파일이 수익률에 대해 이미 최악 위상을 요구하는 것과 같은 이유다(평균은 미참여를 가린다).
    eff_per_phase = [r["periods"] - r["empty_periods"] for r in runs]
    effective_periods = min(eff_per_phase) if eff_per_phase else 0
    if empty:
        warnings.append(f"{empty}/{sum(r['periods'] for r in runs)}기간은 매수 0건(현금) — "
                        f"수익률이 아니라 미참여로 나온 숫자다")
    if cfg.shuffle_returns:
        warnings.append("셔플 모드 — 점수와 수익률의 짝을 어긋나게 했다. "
                        "여기서 판별력이 나오면 그건 누수다")
    # 커버리지 게이트(X2)가 하네스에 실제로 걸렸는지 드러낸다. `covers`가 없으면 하네스는
    # 라이브가 돌리지 않는 전략을 잰 것이다 — 조용히 넘기면 무엇을 검증했는지 알 수 없다.
    min_cov = float(getattr(cfg.signal_config, "min_data_coverage", 0.0) or 0.0)
    cov_blocked = sum(r.get("coverage_blocked", 0) for r in runs)
    if min_cov > 0 and not covers:
        warnings.append(f"커버리지 게이트 {min_cov:.0%}가 설정돼 있는데 하네스에 커버리지 패널이 "
                        f"없어 걸리지 않았다 — 라이브와 다른 전략을 잰 결과다")
    elif min_cov > 0 and cov_blocked == 0:
        warnings.append(f"커버리지 게이트 {min_cov:.0%}가 후보를 한 번도 막지 않았다 — "
                        f"있는 것처럼 보이지만 효과가 없는 완화다")
    if score_source == "pit":
        warnings.append("PIT 점수 모드 — 스냅샷에 저장된 라이브 점수(fund/flow 포함)로 순위를 잰다. "
                        "스냅샷 구간 밖은 비어 있어 표본이 짧다")
    # PIT 완화(min(cfg.min_periods, 5))를 제거했다 — `periods`가 전체 리밸런스 횟수라 5든 30이든
    # 아무 것도 막지 못했고, 대신 실효 3~4기간의 결과가 판정으로 나갔다. 이제 실효 기간으로 센다.
    verdict, why = _verdict(percentile, min(totals), max(totals), rnd["median"],
                            periods=runs[0]["periods"], min_periods=cfg.min_periods,
                            effective_periods=effective_periods, weak_factors=weak)

    return {
        "ready": True,
        "score_source": score_source,
        "config": {"top_pct": cfg.top_pct, "hold_days": cfg.rebalance_days,
                   "cost_pct": cfg.cost_pct, "min_score": cfg.min_score,
                   "use_exposure": cfg.use_exposure, "phases": len(runs),
                   "periods": runs[0]["periods"],
                   "from": panel.dates[phase_idxs[0][0]], "to": panel.dates[phase_idxs[-1][-1]]},
        # 판정이 실제로 몇 기간의 결과인지. `periods`와 다르면 그 차이가 미참여(현금) 기간이다.
        "periods": runs[0]["periods"],
        "empty_periods": empty,
        "effective_periods": effective_periods,
        "strategy": {
            "total_ret_pct": round(strat_total, 1),
            "phase_spread_pp": round(spread, 1),
            "phase_min_pct": round(min(totals), 1), "phase_max_pct": round(max(totals), 1),
            "mdd_pct": round(sum(r["metrics"]["mdd_pct"] for r in runs) / len(runs), 1),
            "sharpe": round(sum(r["metrics"]["sharpe"] for r in runs) / len(runs), 2),
            "win_rate_pct": round(sum(r["win_rate_pct"] for r in runs) / len(runs), 1),
            "avg_picks": round(sum(r["avg_picks"] for r in runs) / len(runs), 1),
        },
        "benchmark": {"total_ret_pct": round(
            sum(r["bench_metrics"]["total_ret_pct"] for r in runs) / len(runs), 1)},
        "vs_random": {
            "trials": len(rnd["totals"]),
            "median_total_pct": rnd["median"],
            "p05_total_pct": rnd["p05"], "p95_total_pct": rnd["p95"],
            "percentile": percentile, "excess_pp": round(excess, 1),
        },
        "verdict": verdict,
        "verdict_why": why,
        "coverage_pct": coverage,
        "fired_pct": fired,
        # 커버리지 게이트(X2)가 라이브와 대칭으로 걸렸는지. `panel_given`이 False면 게이트가
        # 안 걸린 것이고, 그건 라이브와 다른 전략을 잰 결과다.
        "data_coverage_gate": {"min_required": min_cov, "blocked": cov_blocked,
                               "panel_given": bool(covers)},
        # 게이트별 차단 횟수(X3) — 합계 하나면 무엇이 매수 0을 만들었는지 알 수 없다.
        # 실측 라이브에서 추세 게이트가 상위 6자리 중 5자리를 먹었다.
        "gate_blocks": {"trend": sum(r.get("trend_blocked", 0) for r in runs),
                        "coverage": cov_blocked},
        # Deflated Sharpe(L3) — "시도 N회를 감안해도 이 Sharpe가 남는가". 백분위 판정을
        # **대체하지 않는다**; 고르기(다중검정)라는 별개 질문에 답한다.
        # SPA(L2)용 — **첫 위상**의 전략·벤치마크 기간 수익률. 위상마다 길이가 다를 수 있어
        # 조합 간 비교에는 하나를 고정해서 쓴다(같은 날짜축이 아니면 SPA가 성립하지 않는다).
        "phase0_rets": {"strategy": (runs[0].get("per_period_ret") or []),
                        "benchmark": (runs[0].get("bench_per_period_ret") or [])},
        # **초과수익 기준** DSR — 시장 베타를 뺀 뒤 "시도 N회를 감안해도 남는가".
        # 이 값이 유의해도 `verdict`는 바뀌지 않는다(판정은 백분위다) — 다른 질문에 대한 답이다.
        "dsr": {**multiplicity.deflated_sharpe(
            _dsr_sample(runs), n_trials=n_trials or 1,
            sr_variance=sr_variance, periods_per_year=252 / cfg.rebalance_days),
            "basis": "excess_over_benchmark", "phases_pooled": False,
            "note": ("초과수익(전략−벤치마크) 한 위상 기준. 판정(verdict)은 백분위로 하고 "
                     "DSR은 '고르기를 감안해도 남는가'만 답한다.")},
        "warnings": warnings,
        "note": ("절대 수익률은 생존편향(유니버스=오늘 기준 상위 200)으로 부풀려져 있다. "
                 "판단은 vs_random.percentile로 — 무작위 대조군도 같은 편향을 받는다."),
    }


def _verdict(percentile: float | None, phase_min: float, phase_max: float,
             random_median: float, *, periods: int = 10 ** 6, min_periods: int = 0,
             effective_periods: int | None = None,
             weak_factors: list[str] | None = None) -> tuple[str, str]:
    """숫자를 행동으로 옮겨도 되는지의 판정. 기본값은 '판정 불가'다. (짧은 라벨, 사유).

    백분위만 보지 않고 **가장 나쁜 위상까지 무작위 중위를 이겼는지**를 함께 요구한다.
    평균만 보면 "운 좋은 리밸런스 달력 하나가 나머지를 끌어올린 결과"를 엣지로 오인한다.

    표본·커버리지 미달은 경고가 아니라 **차단**이다. 경고로 두면 표 아래 회색 글씨로 밀려나고,
    숫자만 인용돼 돌아다닌다. 실제로 커버리지 5.9%짜리 모멘텀 결과를 그렇게 읽을 뻔했다.

    표본은 `periods`(전체 리밸런스 횟수)가 아니라 **`effective_periods`(매수가 실제로 있던 기간)**로
    센다. 2026-08-05 진단: PIT 점수가 없는 날은 후보가 비어 매수 0건이 되는데, 리밸런스 인덱스는
    가격 패널 전체에 깔리므로 hold=5면 218회쯤 된다. 실제 신호가 4기간뿐이어도 `218 >= min_periods`로
    통과했다. 미참여 기간을 표본으로 세면 "표본 218회"라는 문장이 거짓이 된다.
    """
    if percentile is None:
        return "판정 불가", "대조군 없음"
    eff = periods if effective_periods is None else effective_periods
    if eff < min_periods:
        if eff != periods:
            return "판정 불가", (f"실효 리밸런스 표본 {eff}회 < 최소 {min_periods}회 "
                              f"(전체 {periods}회 중 {periods - eff}회는 매수 0건)")
        return "판정 불가", f"리밸런스 표본 {periods}회 < 최소 {min_periods}회"
    if weak_factors and percentile >= 95:
        return "판정 불가", (f"{', '.join(weak_factors)} 커버리지 미달 — 이 결과는 이름과 다른 "
                          f"전략을 측정한 것이다")
    if percentile >= 95 and phase_min > random_median:
        return "판별력 있음", f"무작위 대비 상위 {100 - percentile:.0f}%, 최악 위상도 우위"
    if percentile <= 5 and phase_max < random_median:
        return "역판별력", "모든 위상에서 무작위보다 나쁘다 — 순위를 그대로 쓰면 안 된다"
    if phase_min <= random_median <= phase_max and (percentile >= 95 or percentile <= 5):
        return "판정 불가", (f"위상에 따라 무작위 중위를 넘기도 못 넘기도 한다"
                          f"({phase_min:+.0f}~{phase_max:+.0f}% vs {random_median:+.0f}%)")
    return "판정 불가", "무작위와 구분되지 않는다"


def _permuted_scores(scores: dict[str, list[float | None]], rng: random.Random) -> dict:
    """티커 라벨만 섞는다 — 점수의 시계열 구조는 그대로 두고 '누구의 점수인가'만 어긋나게 한다.

    귀무가설은 "점수에 미래 정보가 없다"이지 "점수가 매기간 흔들린다"가 아니다. 매 기간 새로
    k종목을 뽑는 대조군은 회전율이 항상 100%라 거래비용을 최대로 물지만, 점수 기반 전략은
    점수가 지속적이라 회전율이 낮아 비용을 덜 문다. 그러면 전략은 **순위 판별력이 전혀 없어도**
    비용 차이만으로 대조군을 이긴다(실측: 5년·10%·5일에서 셔플한 전략이 백분위 100%).
    라벨만 치환하면 지속성·회전율·게이트·매수 종목 수가 전략과 같은 분포로 유지된다.
    """
    keys = list(scores)
    src = keys[:]
    rng.shuffle(src)
    out: dict[str, list[float | None]] = {}
    for k, donor in zip(keys, src):
        mask, row = scores[k], scores[donor]
        # **자기 가용성(mask)은 그대로 두고 값만 기증자에게서 받는다.**
        # 2026-08-05: 시계열을 통째로 맞바꾸면 None 패턴까지 옮겨가 대조군이 **전략이 살 수 없던
        # 종목**을 살 수 있게 된다(시점별 유니버스·PIT 재무에서 종목마다 가용 날짜가 다르므로).
        # 이미 폐지돼 forward-fill 된 종목을 대조군만 담으면 그 0% 수익이 대조군을 끌어내려
        # 전략이 좋아 보인다 — 이 파일이 경계하는 "기계적 차이가 판별력으로 둔갑"이다.
        # 기증자가 그 날 값이 없으면 **직전 값**을 쓴다(대조군 점수는 정보가 없어야 하는 값이므로
        # 낡아도 무해하다). 이렇게 하면 날짜별 후보 수·지속성·회전율이 전략과 같게 유지된다.
        filled = _fill_both_ways(row)
        if all(v is None for v in filled):
            filled = _fill_both_ways(mask)      # 기증자가 통째로 비었으면 자기 값(가용성 우선)
        out[k] = [filled[i] if mask[i] is not None else None for i in range(len(mask))]
    return out


def _fill_both_ways(row: list[float | None]) -> list[float | None]:
    """앞뒤로 채운 사본. 대조군 점수는 **정보가 없어야 하는 값**이므로 낡거나 당겨써도 무해하다 —
    중요한 것은 그 종목이 그 날 후보였는지(가용성)를 전략과 똑같이 유지하는 것이다."""
    out = list(row)
    last: float | None = None
    for i, v in enumerate(out):
        if v is not None:
            last = v
        elif last is not None:
            out[i] = last
    nxt: float | None = None
    for i in range(len(out) - 1, -1, -1):
        if out[i] is not None:
            nxt = out[i]
        elif nxt is not None:
            out[i] = nxt
    return out


def _null_distribution(panel: Panel, cfg: HarnessConfig, scores: dict,
                       phase_idxs: list[list[int]], regimes: dict[int, str] | None,
                       series_cache: dict,
                       covers: dict[str, list[float | None]] | None = None) -> dict:
    """귀무분포 — 전략과 **똑같은 시뮬레이터**를 라벨 치환한 점수로 돌린다.

    대조군을 별도 코드 경로로 만들면 비용·게이트·보유 종목 수 같은 기계적 차이가 판별력으로
    둔갑한다. 같은 `_run_phase`를 쓰고 점수만 바꾸면 남는 차이는 점수의 정보량뿐이다.

    각 시행도 **전략과 똑같이 모든 위상을 돌려 평균**낸다. 전략만 위상 평균으로 분산을 줄이고
    대조군은 단일 위상으로 두면, 전략이 이기는 게 아니라 대조군만 흔들려서 이겨 보인다.

    `covers`도 전략과 **같은 것**을 넘긴다. 커버리지는 티커에 붙은 데이터 사실이라 라벨 치환과
    무관하다 — 대조군만 게이트를 안 걸면 대조군이 더 많이 살 수 있게 되고 그 차이가 판별력으로
    둔갑한다.
    """
    rng = random.Random(cfg.seed)
    totals: list[float] = []
    for _ in range(cfg.random_trials):
        perm = _permuted_scores(scores, rng)
        per_phase = []
        for idxs in phase_idxs:
            r = _run_phase(panel, cfg, perm, idxs, regimes, series_cache,
                           random.Random(cfg.seed), covers)
            per_phase.append((r["equity"][-1] - 1) * 100)
        totals.append(sum(per_phase) / len(per_phase))
    totals.sort()
    n = len(totals)
    return {"totals": totals,
            "median": round(totals[n // 2], 1) if n else 0.0,
            "p05": round(totals[int(n * 0.05)], 1) if n else 0.0,
            "p95": round(totals[min(n - 1, int(n * 0.95))], 1) if n else 0.0}


def regimes_at(panel: Panel, idxs: list[int]) -> dict[int, str]:
    """리밸런스 시점별 국면 라벨 — 그 시점까지의 종가만 사용(룩어헤드 차단)."""
    out = {}
    for i in idxs:
        prices = {t: [v for v in row[:i + 1] if v is not None] for t, row in panel.closes.items()}
        out[i] = regime_mod.classify({t: c for t, c in prices.items() if len(c) > 61}).get("regime")
    return out
