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

1. `random` — 같은 날짜, 같은 유니버스에서 **무작위로 k종목**을 뽑는 몬테카를로. 생존편향은
   전략과 똑같이 받으므로, 전략이 이 분포를 이기면 그건 편향이 아니라 **순위 판별력**이다.
   백분위(`vs_random.percentile`)가 사실상의 p-value다.
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
from dataclasses import dataclass, field

from signal_desk.signals import engine, regime as regime_mod
from signal_desk.signals.engine import SignalConfig


@dataclass
class HarnessConfig:
    top_pct: float = 3.0          # 매수권 분위(엔진 rank_top_pct와 같은 의미)
    min_score: float = 0.5        # 매수권 최소점수(엔진 rank_min_score)
    rebalance_days: int = 5       # 리밸런스 주기(거래일). 보유기간과 같다.
    cost_pct: float = 0.25        # 왕복 거래비용(수수료+세금+슬리피지) — 회전율에 비례 차감
    warmup: int = 130             # 지표가 안정되기 전 구간은 건너뛴다(MA120·모멘텀)
    use_exposure: bool = False    # 국면 익스포저 적용 여부(나머지는 현금, 무이자 가정)
    random_trials: int = 200      # 무작위 대조군 시행 수
    seed: int = 20260726
    invert_scores: bool = False   # 진단용 — 순위를 뒤집어도 되는지(체계적 음의 판별력 확인)
    phase_average: bool = True    # 리밸런스 시작일(위상)을 전부 돌려 평균 — 아래 주석 참고
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


def _score_series(panel: Panel, config: SignalConfig
                  ) -> tuple[dict[str, list[float | None]], dict[str, float]]:
    """종목별 전 구간 가격기반 점수 + 팩터 커버리지.

    라이브 엔진과 같은 `_price_only_components`·`combine`을 쓴다 — 백테스트가 별도 공식을 쓰면
    무엇을 검증한 건지 알 수 없다.

    커버리지를 함께 세는 이유: 모멘텀은 252거래일 이력을 요구하는데 우리 캐시는 그보다 짧을 수
    있다. 그러면 그 팩터는 대부분의 시점에서 **가중치 0으로 조용히 빠지고**, 결과 표에는
    "모멘텀 전략"이라고 적힌 빈 칸이 남는다. 커버리지가 낮은 팩터의 숫자는 읽지 말아야 한다.
    """
    scores: dict[str, list[float | None]] = {}
    seen = {"technical": 0, "reversion": 0, "momentum": 0}
    total = 0
    for ticker, row in panel.closes.items():
        vals = [v for v in row if v is not None]
        if len(vals) < 60:
            continue
        offset = len(row) - len(vals)          # 상장 이전 구간 길이
        series = engine.compute_indicator_series(vals, config)
        out: list[float | None] = [None] * len(row)
        for i in range(len(vals)):
            comps = engine._price_only_components(vals, series, i, config)
            for name, (_, w, _) in zip(("technical", "reversion", "momentum"), comps):
                if w:
                    seen[name] += 1
            total += 1
            out[offset + i] = engine.combine(comps, config)["score"]
        scores[ticker] = out
    coverage = {k: round(v / total * 100, 1) for k, v in seen.items()} if total else {}
    return scores, coverage


def _gated(panel: Panel, ticker: str, i: int, config: SignalConfig,
           market_ret: float | None, series_cache: dict) -> bool:
    """라이브와 같은 상대 추세 게이트. 시장 대비 상대강도 우위면 막지 않는다."""
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


def _period_return(panel: Panel, tickers: list[str], i: int, cfg: HarnessConfig) -> float:
    """i 다음 거래일 종가 진입 → rebalance_days 뒤 종가 청산. 동일가중 평균 수익률."""
    rets = []
    for t in tickers:
        row = panel.closes[t]
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


def _run_phase(panel: Panel, cfg: HarnessConfig, scores: dict, idxs: list[int],
               regimes: dict[int, str] | None, series_cache: dict, tie_rng: random.Random,
               ) -> dict:
    """한 위상(고정된 리밸런스 날짜 집합)에 대한 전략·벤치마크 시뮬레이션."""
    scfg = cfg.signal_config
    equity, bench, picks_log = [1.0], [1.0], []
    held: set[str] = set()
    per_period_ret: list[float] = []
    universe_by_date: dict[int, list[str]] = {}
    empty_periods = 0

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
        for t in ranked:
            if len(picks) >= k:
                break
            if scores[t][i] < cfg.min_score:
                continue
            if _gated(panel, t, i, scfg, market_ret, series_cache):
                continue
            picks.append(t)

        if picks:
            gross = _period_return(panel, picks, i, cfg)
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
            "bench_metrics": _metrics(bench, ppy), "universe_by_date": universe_by_date,
            "empty_periods": empty_periods, "periods": len(idxs),
            "win_rate_pct": round(sum(1 for r in per_period_ret if r > 0)
                                  / len(per_period_ret) * 100, 1) if per_period_ret else 0.0,
            "avg_picks": round(sum(p["picks"] for p in picks_log) / len(picks_log), 1)
            if picks_log else 0.0}


def run(panel: Panel, cfg: HarnessConfig | None = None,
        regimes: dict[int, str] | None = None) -> dict:
    """전략(횡단면 분위 top N%) + 무작위 대조군 + 동일가중 벤치마크를 같은 날짜축에서 비교.

    리밸런스 위상을 전부 돌려 평균 내고(`phase_average`), 위상 간 편차를 함께 낸다. 편차가
    초과수익보다 크면 그 초과수익은 규칙이 아니라 달력이 만든 것이므로 `verdict`가 판정 불가로
    떨어진다.

    regimes: {rebalance_index: 국면라벨}. use_exposure=True일 때만 쓰인다.
    """
    cfg = cfg or HarnessConfig()
    scores, coverage = _score_series(panel, cfg.signal_config)
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
        phase_idxs.append(idxs)
        runs.append(_run_phase(panel, cfg, scores, idxs, regimes, series_cache, tie_rng))
    if not runs:
        return {"ready": False, "reason": f"표본 부족 — 거래일 {len(panel)}일"}

    ppy = 252 / cfg.rebalance_days
    totals = [(r["equity"][-1] - 1) * 100 for r in runs]
    strat_total = sum(totals) / len(totals)
    spread = max(totals) - min(totals)
    rnd = _random_baseline(panel, cfg, phase_idxs, [r["universe_by_date"] for r in runs])
    better = sum(1 for r in rnd["totals"] if r < strat_total)
    percentile = round(better / len(rnd["totals"]) * 100, 1) if rnd["totals"] else None
    excess = strat_total - rnd["median"]

    warnings = [f"{name} 커버리지 {pct}% — 이력 부족으로 대부분의 시점에서 빠졌다. 이 팩터의 "
                f"결과는 읽지 말 것" for name, pct in coverage.items() if pct < 60]
    empty = sum(r["empty_periods"] for r in runs)
    if empty:
        warnings.append(f"{empty}/{sum(r['periods'] for r in runs)}기간은 매수 0건(현금) — "
                        f"수익률이 아니라 미참여로 나온 숫자다")
    verdict, why = _verdict(percentile, min(totals), max(totals), rnd["median"])

    return {
        "ready": True,
        "config": {"top_pct": cfg.top_pct, "hold_days": cfg.rebalance_days,
                   "cost_pct": cfg.cost_pct, "min_score": cfg.min_score,
                   "use_exposure": cfg.use_exposure, "phases": len(runs),
                   "periods": runs[0]["periods"],
                   "from": panel.dates[phase_idxs[0][0]], "to": panel.dates[phase_idxs[-1][-1]]},
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
        "warnings": warnings,
        "note": ("절대 수익률은 생존편향(유니버스=오늘 기준 상위 200)으로 부풀려져 있다. "
                 "판단은 vs_random.percentile로 — 무작위 대조군도 같은 편향을 받는다."),
    }


def _verdict(percentile: float | None, phase_min: float, phase_max: float,
             random_median: float) -> tuple[str, str]:
    """숫자를 행동으로 옮겨도 되는지의 판정. 기본값은 '판정 불가'다. (짧은 라벨, 사유).

    백분위만 보지 않고 **가장 나쁜 위상까지 무작위 중위를 이겼는지**를 함께 요구한다.
    평균만 보면 "운 좋은 리밸런스 달력 하나가 나머지를 끌어올린 결과"를 엣지로 오인한다.
    """
    if percentile is None:
        return "판정 불가", "대조군 없음"
    if percentile >= 95 and phase_min > random_median:
        return "판별력 있음", f"무작위 대비 상위 {100 - percentile:.0f}%, 최악 위상도 우위"
    if percentile <= 5 and phase_max < random_median:
        return "역판별력", "모든 위상에서 무작위보다 나쁘다 — 순위를 그대로 쓰면 안 된다"
    if phase_min <= random_median <= phase_max and (percentile >= 95 or percentile <= 5):
        return "판정 불가", (f"위상에 따라 무작위 중위를 넘기도 못 넘기도 한다"
                          f"({phase_min:+.0f}~{phase_max:+.0f}% vs {random_median:+.0f}%)")
    return "판정 불가", "무작위와 구분되지 않는다"


def _random_baseline(panel: Panel, cfg: HarnessConfig, phase_idxs: list[list[int]],
                     universes: list[dict[int, list[str]]]) -> dict:
    """같은 날짜·같은 유니버스에서 무작위 k종목을 뽑는 몬테카를로 — 생존편향 상쇄용 귀무분포.

    각 시행도 **전략과 똑같이 모든 위상을 돌려 평균**낸다. 전략만 위상 평균으로 분산을 줄이고
    대조군은 단일 위상으로 두면, 전략이 이기는 게 아니라 대조군만 흔들려서 이겨 보인다.
    """
    rng = random.Random(cfg.seed)
    totals: list[float] = []
    for _ in range(cfg.random_trials):
        per_phase = []
        for idxs, uni in zip(phase_idxs, universes):
            eq, held = 1.0, set()
            for i in idxs:
                avail = uni[i]
                k = engine.rank_slots(len(avail), cfg.top_pct)
                picks = rng.sample(avail, min(k, len(avail))) if avail else []
                if picks:             # 대조군도 전략과 같은 비용 규칙을 써야 비교가 성립한다
                    gross = _period_return(panel, picks, i, cfg)
                    turnover = 1.0 if not held else len(set(picks) - held) / len(picks)
                    eq *= (1 + gross - (cfg.cost_pct / 100) * turnover)
                held = set(picks)
            per_phase.append((eq - 1) * 100)
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
