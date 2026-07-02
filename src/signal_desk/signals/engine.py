"""시그널 엔진 — 종합 분석(기술·기본·저평가·낙폭과대/단기과열)을 결합해 종목별 매수/매도
시그널을 산출한다.

가중치·임계값은 전부 `SignalConfig`에 모여 있다(하드코딩 금지 — CLAUDE.md 데이터 규칙).
brightdesk 3팩터(기술 0.35·기본 0.30·KB 0.35) 중 KB는 이번 범위 밖이라 뺐고, 대신 순수
가격/재무 데이터만으로 계산 가능한 저평가·낙폭과대 팩터 둘을 더해 4팩터로 확장했다
(정성적/시국/정세/산업사이클 등 LLM·뉴스데이터가 필요한 팩터는 BACKLOG #11·#17 — 별도 범위).

각 팩터는 (정규화 점수[-1,1], 가중치, 근거) 컴포넌트 하나로 `combine()`에 들어간다. 팩터
자체가 계산 불가능하거나(예: 재무데이터 없음, PER/PBR 미제공, 상장 이력 부족) 이번 시점에
할 말이 없으면(예: 낙폭과대/단기과열 조건 미충족 — 대부분의 평상시가 여기 해당) 가중치를
0으로 둬 사실상 제외하고 나머지 팩터끼리 재정규화한다(그레이스풀 폴백). 반대로 기술점수처럼
"매 시점 항상 계산되고 중립이면 0으로 반영"되는 팩터는 가중치를 그대로 둬 가중평균을
희석시킨다 — 이건 팩터 하나의 내부 서브스코어 합산 방식이라 여기선 해당 없음.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from signal_desk.signals import fundamental as fnd
from signal_desk.signals import indicators as ind
from signal_desk.signals import narrative as narr
from signal_desk.signals import qualitative as qual
from signal_desk.signals import reversion as rev
from signal_desk.signals import valuation as val


@dataclass
class SignalConfig:
    weight_technical: float = 0.35
    weight_fundamental: float = 0.30
    weight_valuation: float = 0.15
    weight_reversion: float = 0.20
    weight_qualitative: float = 0.15  # KB(뉴스·영상) 정성 — 데이터 있을 때만 포함(없으면 재정규화 제외)

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

    reversion: rev.ReversionConfig = field(default_factory=rev.ReversionConfig)

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
    valuation_percentile: float | None = None
    has_valuation: bool = False
    reversion_score: float = 0.0
    has_reversion: bool = False
    qualitative_score: float | None = None
    has_qualitative: bool = False
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


def _valuation_component(
    ticker: str, val_scores: dict[str, float], config: SignalConfig
) -> tuple[float, float, list[str], float | None, bool]:
    """저평가 percentile(0=가장 저평가, 100=가장 고평가)을 [-1,1]로 변환. PER/PBR이 둘 다
    없어 percentile 자체가 없는 종목은 가중치 0으로 완전히 제외한다."""
    percentile = val_scores.get(ticker)
    if percentile is None:
        return 0.0, 0.0, [], None, False
    norm = (50 - percentile) / 50
    zone = "저평가" if percentile <= 50 else "고평가"
    reasons = [f"[저평가] PER·PBR 상대순위 상위 {percentile:.0f}% — {zone} 구간"]
    return norm, config.weight_valuation, reasons, percentile, True


def _reversion_component(
    closes: list[float], rsi_series: list[float | None], config: SignalConfig
) -> tuple[float, float, list[str], float, bool]:
    """낙폭과대/단기과열 팩터. 평상시(급락·급등이 없을 때)가 대부분이라, 조건이 실제로
    발동했을 때만 가중치를 부여한다 — 항상 가중치를 유지한 채 0으로 반영하면 평상시에도
    매 종목 점수가 희석돼(가중치 0.20이 커서) 기술/기본 단독 시그널까지 약해진다. 상장 이력이
    짧아 계산 자체가 불가능한 경우도 같은 방식(가중치 0)으로 제외한다."""
    rev_cfg = config.reversion
    if len(closes) <= rev_cfg.lookback_days:
        return 0.0, 0.0, [], 0.0, False
    rev_score, reasons = rev.score(closes, rsi_series, rev_cfg)
    if not reasons:
        return 0.0, 0.0, [], 0.0, False
    return rev_score / rev_cfg.max_score, config.weight_reversion, reasons, rev_score, True


def combine(components: list[tuple[float, float, list[str]]], config: SignalConfig | None = None) -> dict:
    """(정규화 점수[-1,1], 가중치, 근거) 컴포넌트 리스트를 가중평균해 결합.

    가중치 0인 컴포넌트는 가중평균에는 기여하지 않지만(사실상 제외와 동일), 근거 문구는
    그대로 reasons에 포함된다 — 예: "재무데이터 없음" 안내.
    """
    config = config or SignalConfig()

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
    reasons = [r for _, _, rs in components for r in rs]

    return {"score": round(score, 2), "kind": kind, "confidence": confidence, "reasons": reasons}


def evaluate(
    universe: list[dict],
    prices: dict[str, list[float]],
    fundamentals: dict[str, dict] | None = None,
    config: SignalConfig | None = None,
    sentiment: dict[str, dict] | None = None,
) -> list[SignalResult]:
    """universe: [{ticker, name}], prices: ticker -> 종가 리스트(오래된→최신), fundamentals: ticker -> metrics.
    sentiment: ticker -> {score[-1,1], reasons} (KB 정성 팩터, 있는 종목만 반영)."""
    config = config or SignalConfig()
    fundamentals = fundamentals or {}
    sentiment = sentiment or {}
    val_scores = val.scores(universe, fundamentals)
    results: list[SignalResult] = []

    for item in universe:
        ticker, name = item["ticker"], item["name"]
        closes = prices.get(ticker)
        if not closes:
            continue
        series = compute_indicator_series(closes, config)
        tech_score, tech_reasons = technical_score_at(closes, series, len(closes) - 1, config)
        fund = fnd.score(fundamentals.get(ticker, {}))
        val_norm, val_weight, val_reasons, val_pct, has_valuation = _valuation_component(
            ticker, val_scores, config
        )
        rev_norm, rev_weight, rev_reasons, rev_score_raw, has_reversion = _reversion_component(
            closes, series["rsi"], config
        )
        qual_norm, qual_weight, qual_reasons, qual_score, has_qualitative = qual.component(
            sentiment.get(ticker), config.weight_qualitative
        )

        components = [
            (tech_score / 3.0, config.weight_technical, tech_reasons),
            (fund.score / 2.0 if fund.has_data else 0.0, config.weight_fundamental if fund.has_data else 0.0, fund.reasons),
            (val_norm, val_weight, val_reasons),
            (rev_norm, rev_weight, rev_reasons),
            (qual_norm, qual_weight, qual_reasons),
        ]
        combined = combine(components, config)

        result = SignalResult(
            ticker=ticker, name=name, score=combined["score"], kind=combined["kind"],
            confidence=combined["confidence"], technical_score=round(tech_score, 2),
            fundamental_score=round(fund.score, 2), has_fundamental=fund.has_data,
            valuation_percentile=val_pct, has_valuation=has_valuation,
            reversion_score=round(rev_score_raw, 2), has_reversion=has_reversion,
            qualitative_score=qual_score, has_qualitative=has_qualitative,
            reasons=combined["reasons"],
        )
        result.narrative = narr.explain(result)
        results.append(result)

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _price_only_components(
    closes: list[float], series: dict, i: int, config: SignalConfig
) -> list[tuple[float, float, list[str]]]:
    """과거 시점 재현(replay/backtest)용 — 기술+낙폭과대는 순수 가격 데이터만으로 그 시점
    기준 재계산 가능하지만, 기본/저평가는 시점별 재무 스냅샷이 없어 범위 밖(TODO)."""
    tech_score, tech_reasons = technical_score_at(closes, series, i, config)
    rev_norm, rev_weight, rev_reasons, _, _ = _reversion_component(
        closes[: i + 1], series["rsi"][: i + 1], config
    )
    return [
        (tech_score / 3.0, config.weight_technical, tech_reasons),
        (rev_norm, rev_weight, rev_reasons),
    ]


def replay_signal_kinds(closes: list[float], config: SignalConfig | None = None) -> list[str]:
    """전 구간 매 시점 시그널 재현(backtest_summary와 동일 방법론) — 차트 구간 표시용."""
    config = config or SignalConfig()
    series = compute_indicator_series(closes, config)
    kinds = []
    for i in range(len(closes)):
        combined = combine(_price_only_components(closes, series, i, config), config)
        kinds.append(combined["kind"])
    return kinds


def signal_zones(
    dates: list[str], closes: list[float], config: SignalConfig | None = None
) -> list[dict]:
    """연속된 동일 시그널(BUY/SELL) 구간을 [{start,end,kind,reasons}]로 압축 — 차트 markArea/마커용.
    HOLD는 제외. reasons는 구간 시작(=시그널 전환) 시점의 가격기반 판단 근거(호버 설명용)."""
    config = config or SignalConfig()
    series = compute_indicator_series(closes, config)
    kinds, reasons_at = [], []
    for k in range(len(closes)):
        combined = combine(_price_only_components(closes, series, k, config), config)
        kinds.append(combined["kind"])
        reasons_at.append(combined["reasons"])
    zones = []
    i, n = 0, len(kinds)
    while i < n:
        if kinds[i] == "HOLD":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == kinds[i]:
            j += 1
        zones.append({"start": dates[i], "end": dates[j - 1], "kind": kinds[i], "reasons": reasons_at[i]})
        i = j
    return zones


def backtest_summary(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig | None = None
) -> dict:
    """가격 기반 팩터(기술+낙폭과대) 백테스트 — 종가 계열만으로 과거 매 시점의 시그널을
    재현하고 이후 실현 수익률로 적중 여부를 검증한다. 기본/저평가는 시점별 재무 스냅샷이
    없어 과거 재현이 더 복잡해 범위 밖(TODO).

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
            combined = combine(_price_only_components(closes, series, i, config), config)
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
        "method": "price_based_v2",
        "hit_threshold_pct": config.backtest_hit_ret * 100,
        "by_signal": by_signal,
    }
