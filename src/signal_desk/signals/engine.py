"""시그널 엔진 — 기술+기본 스코어를 결합해 종목별 매수/매도 시그널을 산출.

가중치·임계값은 전부 `SignalConfig`에 모여 있다(하드코딩 금지 — CLAUDE.md 데이터 규칙).
brightdesk 3팩터(기술 0.35·기본 0.30·KB 0.35) 중 KB는 이번 범위 밖이라, 사용 가능한
컴포넌트만 남겨 가중치를 재정규화하는 방식으로 일반화했다 — 재무데이터가 없는 종목도
기술점수만으로 시그널이 계속 나온다(그레이스풀 폴백).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from signal_desk.signals import fundamental as fnd
from signal_desk.signals import indicators as ind
from signal_desk.signals import narrative as narr


@dataclass
class SignalConfig:
    weight_technical: float = 0.35
    weight_fundamental: float = 0.30

    buy_threshold: float = 1.2
    sell_threshold: float = -1.2

    rsi_period: int = 14
    rsi_oversold: float = 30
    rsi_overbought: float = 70
    rsi_weak: float = 45
    rsi_strong: float = 55

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    ma_short: int = 20
    ma_mid: int = 60
    ma_long: int = 120

    backtest_hit_ret: float = 0.005  # 0.5%
    backtest_horizons: tuple[int, ...] = (5, 20)


@dataclass
class SignalResult:
    ticker: str
    name: str
    score: float
    kind: str  # BUY | SELL | HOLD
    confidence: float
    technical_score: float
    fundamental_score: float
    has_fundamental: bool
    reasons: list[str] = field(default_factory=list)
    narrative: str = ""


def compute_indicator_series(closes: list[float], config: SignalConfig | None = None) -> dict:
    """ma_long(MA120)은 정배열/역배열 판정(technical_score_at)엔 안 쓰이지만(brightdesk 원 공식이
    MA20/60 크로스오버만 사용), 차트에 추세 참고선으로 보여주기 위해 계산은 해 둔다."""
    config = config or SignalConfig()
    return {
        "rsi": ind.rsi(closes, config.rsi_period),
        "macd": ind.macd(closes, config.macd_fast, config.macd_slow, config.macd_signal),
        "ma_short": ind.sma(closes, config.ma_short),
        "ma_mid": ind.sma(closes, config.ma_mid),
        "ma_long": ind.sma(closes, config.ma_long),
    }


def technical_score_at(
    closes: list[float], series: dict, i: int, config: SignalConfig | None = None
) -> tuple[float, list[str]]:
    """지정 인덱스 i 시점(과거 리플레이 포함)의 기술 스코어. 범위 [-3, +3]."""
    config = config or SignalConfig()
    score = 0.0
    reasons: list[str] = []

    rsi_v = series["rsi"][i]
    if rsi_v is not None:
        if rsi_v < config.rsi_oversold:
            score += 1.5
            reasons.append(f"[기술] RSI {rsi_v:.1f} — 과매도")
        elif rsi_v > config.rsi_overbought:
            score -= 1.5
            reasons.append(f"[기술] RSI {rsi_v:.1f} — 과매수")
        elif rsi_v < config.rsi_weak:
            score += 0.3
            reasons.append(f"[기술] RSI {rsi_v:.1f} — 약세권")
        elif rsi_v > config.rsi_strong:
            score -= 0.3
            reasons.append(f"[기술] RSI {rsi_v:.1f} — 강세권")

    hist = series["macd"]["histogram"]
    cur = hist[i]
    prev = hist[i - 1] if i > 0 else None
    if cur is not None and prev is not None and prev <= 0 < cur:
        score += 1.0
        reasons.append("[기술] MACD 골든크로스")
    elif cur is not None and prev is not None and prev >= 0 > cur:
        score -= 1.0
        reasons.append("[기술] MACD 데드크로스")
    elif cur is not None and cur > 0:
        score += 0.2
        reasons.append("[기술] MACD 히스토그램 양전환 유지")
    elif cur is not None and cur < 0:
        score -= 0.2
        reasons.append("[기술] MACD 히스토그램 음전환 유지")

    ma_short = series["ma_short"][i]
    ma_mid = series["ma_mid"][i]
    close = closes[i]
    if ma_short is not None and ma_mid is not None:
        if ma_short > ma_mid and close > ma_short:
            score += 0.5
            reasons.append("[기술] 정배열 상승추세")
        elif ma_short < ma_mid and close < ma_short:
            score -= 0.5
            reasons.append("[기술] 역배열 하락추세")

    return score, reasons


def combine(
    technical_score: float,
    technical_reasons: list[str],
    fundamental: fnd.FundamentalResult,
    config: SignalConfig | None = None,
) -> dict:
    """기술/기본 컴포넌트를 사용 가능한 것만 재정규화해 결합. 반환: score/kind/confidence/reasons."""
    config = config or SignalConfig()

    components = [(technical_score / 3.0, config.weight_technical, technical_reasons)]
    if fundamental.has_data:
        components.append((fundamental.score / 2.0, config.weight_fundamental, fundamental.reasons))

    weight_sum = sum(w for _, w, _ in components)
    weighted = sum(norm * w for norm, w, _ in components) / weight_sum if weight_sum else 0.0
    score = weighted * 3

    if score >= config.buy_threshold:
        kind = "BUY"
    elif score <= config.sell_threshold:
        kind = "SELL"
    else:
        kind = "HOLD"

    confidence = round(abs(2 * ind.sigmoid(score) - 1) * 100) / 100
    reasons = technical_reasons + fundamental.reasons

    return {"score": round(score, 2), "kind": kind, "confidence": confidence, "reasons": reasons}


def evaluate(
    universe: list[dict],
    prices: dict[str, list[float]],
    fundamentals: dict[str, dict] | None = None,
    config: SignalConfig | None = None,
) -> list[SignalResult]:
    """universe: [{ticker, name}], prices: ticker -> 종가 리스트(오래된→최신), fundamentals: ticker -> metrics."""
    config = config or SignalConfig()
    fundamentals = fundamentals or {}
    results: list[SignalResult] = []

    for item in universe:
        ticker, name = item["ticker"], item["name"]
        closes = prices.get(ticker)
        if not closes:
            continue
        series = compute_indicator_series(closes, config)
        tech_score, tech_reasons = technical_score_at(closes, series, len(closes) - 1, config)
        fund = fnd.score(fundamentals.get(ticker, {}))
        combined = combine(tech_score, tech_reasons, fund, config)

        result = SignalResult(
            ticker=ticker, name=name, score=combined["score"], kind=combined["kind"],
            confidence=combined["confidence"], technical_score=round(tech_score, 2),
            fundamental_score=round(fund.score, 2), has_fundamental=fund.has_data,
            reasons=combined["reasons"],
        )
        result.narrative = narr.explain(result)
        results.append(result)

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def replay_signal_kinds(closes: list[float], config: SignalConfig | None = None) -> list[str]:
    """전 구간 매 시점 기술 시그널 재현(backtest_summary와 동일 방법론) — 차트 구간 표시용."""
    config = config or SignalConfig()
    series = compute_indicator_series(closes, config)
    kinds = []
    for i in range(len(closes)):
        tech_score, tech_reasons = technical_score_at(closes, series, i, config)
        combined = combine(tech_score, tech_reasons, fnd.score({}), config)
        kinds.append(combined["kind"])
    return kinds


def signal_zones(
    dates: list[str], closes: list[float], config: SignalConfig | None = None
) -> list[dict]:
    """연속된 동일 시그널(BUY/SELL) 구간을 [{start,end,kind}]로 압축 — 차트 markArea용. HOLD는 제외."""
    kinds = replay_signal_kinds(closes, config)
    zones = []
    i, n = 0, len(kinds)
    while i < n:
        if kinds[i] == "HOLD":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == kinds[i]:
            j += 1
        zones.append({"start": dates[i], "end": dates[j - 1], "kind": kinds[i]})
        i = j
    return zones


def backtest_summary(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig | None = None
) -> dict:
    """기술점수 단독 백테스트(1차 버전) — 종가 계열만으로 과거 매 시점의 시그널을 재현하고
    이후 실현 수익률로 적중 여부를 검증한다. 재무데이터는 과거 시점 재현이 더 복잡해 범위 밖.

    진입가는 시그널 다음 날의 종가로 근사한다(시가 데이터가 없는 경우의 근사치 — 실제 시가를
    구하면 더 정확해지므로 TODO로 남겨둔다).
    """
    config = config or SignalConfig()
    horizons = config.backtest_horizons
    by_kind: dict[str, dict[str, list[float]]] = {
        "BUY": {f"ret_{h}d": [] for h in horizons},
        "SELL": {f"ret_{h}d": [] for h in horizons},
    }
    hits: dict[str, int] = {"BUY": 0, "SELL": 0}
    counted: dict[str, int] = {"BUY": 0, "SELL": 0}

    for closes in prices_by_ticker.values():
        if len(closes) < 30:
            continue
        series = compute_indicator_series(closes, config)
        for i in range(len(closes) - 1):
            entry_idx = i + 1  # 시그널 다음날 종가로 진입 근사
            tech_score, tech_reasons = technical_score_at(closes, series, i, config)
            combined = combine(tech_score, tech_reasons, fnd.score({}), config)
            kind = combined["kind"]
            if kind == "HOLD":
                continue

            entry_price = closes[entry_idx]
            primary_h = horizons[0]
            if entry_idx + primary_h >= len(closes):
                continue
            ret_primary = (closes[entry_idx + primary_h] - entry_price) / entry_price
            counted[kind] += 1
            hit = ret_primary > config.backtest_hit_ret if kind == "BUY" else ret_primary < -config.backtest_hit_ret
            if hit:
                hits[kind] += 1

            for h in horizons:
                if entry_idx + h < len(closes):
                    ret_h = (closes[entry_idx + h] - entry_price) / entry_price
                    by_kind[kind][f"ret_{h}d"].append(ret_h)

    by_signal = []
    for kind in ("BUY", "SELL"):
        n = counted[kind]
        row = {
            "kind": kind,
            "n": n,
            "winrate": round(hits[kind] / n * 100, 1) if n else None,
        }
        for h in horizons:
            rets = by_kind[kind][f"ret_{h}d"]
            row[f"avg_ret_{h}d"] = round(sum(rets) / len(rets) * 100, 2) if rets else None
        by_signal.append(row)

    return {
        "method": "technical_only_v1",
        "hit_threshold_pct": config.backtest_hit_ret * 100,
        "by_signal": by_signal,
    }
