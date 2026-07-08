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

from signal_desk.signals import flow as flow_mod
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
    weight_flow: float = 0.20  # 수급(외국인·기관 순매수) — KR만, 데이터 있을 때만 포함

    # 5단계 시그널 임계값(종합점수 범위 ~[-3,3]): 강력매수 ≥ 매수 ≥ (관망) ≥ 매도 ≥ 강력매도
    strong_buy_threshold: float = 2.0
    buy_threshold: float = 1.2
    sell_threshold: float = -1.2
    strong_sell_threshold: float = -2.0

    # 국면 적응: 1이면 약세·조정·거시 비우호 국면에서 매수 임계값을 자동 상향(regime.buy_threshold_bump).
    # 0이면 임계값 고정. (관리자 조정 필드 — signalcfg.FIELDS에 포함)
    regime_adaptive: float = 1.0

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
    flow_intensity: float | None = None  # 수급 강도(외국인·기관 순매수/거래대금, [-1,1])
    has_flow: bool = False
    event_risk: bool = False  # KB에서 최근 악재 이벤트 감지 — 매수 후보에서 제외(veto)
    event_note: str = ""
    event_severity: str = ""  # 악재 강도: critical(전량 청산)|serious(부분 청산)|''
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


def _downtrend_confirmed(
    closes: list[float], series: dict, i: int, config: SignalConfig
) -> bool:
    """확인된 하락추세(=떨어지는 칼) 판정. 종가가 MA20·MA60 아래이고 역배열(MA20<MA60)이면
    구조적 하락 국면으로 본다. 종가가 MA20를 회복하면(c>=MA20) 반등 신호로 간주해 해제한다.

    이 국면에선 낙폭과대(반등 기대)·저평가(싸 보임)가 계속 매수를 부추기지만 주가는 더 싸지고
    더 떨어진다(가치함정). 그래서 이 국면의 낙폭과대 매수기여를 무효화하고 종합 매수신호도
    관망으로 강등한다 — backtest/live 공통 게이트."""
    ma_s = series["ma_short"][i]
    ma_m = series["ma_mid"][i]
    if ma_s is None or ma_m is None:
        return False
    c = closes[i]
    return c < ma_s and c < ma_m and ma_s < ma_m


def _apply_trend_gate(
    combined: dict, closes: list[float], series: dict, i: int, config: SignalConfig
) -> dict:
    """확인된 하락추세에서 종합 매수신호를 관망으로 강등(떨어지는 칼 차단). 낙폭과대 기여는
    컴포넌트 단계(_price_only_components/evaluate)에서 이미 무효화됨."""
    if combined["kind"] in BUY_KINDS and _downtrend_confirmed(closes, series, i, config):
        combined["kind"] = HOLD
        combined["reasons"] = [*combined["reasons"],
                               "[추세] 하락추세 확인(종가<MA20<MA60) — 반등 전 매수 차단(관망)"]
    return combined


# 5단계 시그널 종류
STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL = "STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"
BUY_KINDS = (STRONG_BUY, BUY)
SELL_KINDS = (STRONG_SELL, SELL)
ACTIONABLE_KINDS = (STRONG_BUY, BUY, SELL, STRONG_SELL)


def is_buy(kind: str) -> bool:
    return kind in BUY_KINDS


def is_sell(kind: str) -> bool:
    return kind in SELL_KINDS


def classify(score: float, config: SignalConfig | None = None) -> str:
    """종합점수 → 5단계 시그널."""
    config = config or SignalConfig()
    if score >= config.strong_buy_threshold:
        return STRONG_BUY
    if score >= config.buy_threshold:
        return BUY
    if score <= config.strong_sell_threshold:
        return STRONG_SELL
    if score <= config.sell_threshold:
        return SELL
    return HOLD


def combine(components: list[tuple[float, float, list[str]]], config: SignalConfig | None = None) -> dict:
    """(정규화 점수[-1,1], 가중치, 근거) 컴포넌트 리스트를 가중평균해 결합.

    가중치 0인 컴포넌트는 가중평균에는 기여하지 않지만(사실상 제외와 동일), 근거 문구는
    그대로 reasons에 포함된다 — 예: "재무데이터 없음" 안내.
    """
    config = config or SignalConfig()

    weight_sum = sum(w for _, w, _ in components)
    weighted = sum(norm * w for norm, w, _ in components) / weight_sum if weight_sum else 0.0
    score = weighted * 3
    kind = classify(score, config)

    confidence = round(abs(2 * ind.sigmoid(score) - 1) * 100) / 100
    reasons = [r for _, _, rs in components for r in rs]

    return {"score": round(score, 2), "kind": kind, "confidence": confidence, "reasons": reasons}


def evaluate(
    universe: list[dict],
    prices: dict[str, list[float]],
    fundamentals: dict[str, dict] | None = None,
    config: SignalConfig | None = None,
    sentiment: dict[str, dict] | None = None,
    flows: dict[str, dict] | None = None,
) -> list[SignalResult]:
    """universe: [{ticker, name}], prices: ticker -> 종가 리스트(오래된→최신), fundamentals: ticker -> metrics.
    sentiment: ticker -> {score[-1,1], reasons} (KB 정성 팩터), flows: ticker -> {intensity,...} (수급 팩터, KR)."""
    config = config or SignalConfig()
    fundamentals = fundamentals or {}
    sentiment = sentiment or {}
    flows = flows or {}
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
        flow_norm, flow_weight, flow_reasons, flow_intensity, has_flow = flow_mod.component(
            flows.get(ticker), config.weight_flow
        )

        # 확인된 하락추세(떨어지는 칼)에서는 낙폭과대·저평가 매수기여를 무효화한다 — 싸고
        # 과매도여도 구조적 하락이면 계속 싸지고 떨어지는 가치함정. 종합 BUY도 아래서 관망 강등.
        i_last = len(closes) - 1
        if _downtrend_confirmed(closes, series, i_last, config):
            if rev_weight and rev_norm > 0:
                rev_norm, rev_weight = 0.0, 0.0
                rev_reasons = [*rev_reasons, "[추세] 하락추세 — 낙폭과대 매수신호 무효화"]
            if val_weight and val_norm > 0:
                val_norm, val_weight = 0.0, 0.0
                val_reasons = [*val_reasons, "[추세] 하락추세 — 저평가 매수기여 보류(가치함정 방지)"]

        # 정성(KB)은 점수 팩터가 아니라 '악재 이벤트 veto'로만 쓴다(백테스트상 점수 기여 미미 —
        # 대신 KB의 강점인 이벤트 리스크 회피에 집중). 감성 점수는 표시용으로만 보존.
        components = [
            (tech_score / 3.0, config.weight_technical, tech_reasons),
            (fund.score / 2.0 if fund.has_data else 0.0, config.weight_fundamental if fund.has_data else 0.0, fund.reasons),
            (val_norm, val_weight, val_reasons),
            (rev_norm, rev_weight, rev_reasons),
            (flow_norm, flow_weight, flow_reasons),  # 수급(외국인·기관) — 하락추세에도 유효(스마트머니 실매수 확인)
        ]
        combined = combine(components, config)
        _apply_trend_gate(combined, closes, series, i_last, config)

        entry = sentiment.get(ticker) or {}
        result = SignalResult(
            ticker=ticker, name=name, score=combined["score"], kind=combined["kind"],
            confidence=combined["confidence"], technical_score=round(tech_score, 2),
            fundamental_score=round(fund.score, 2), has_fundamental=fund.has_data,
            valuation_percentile=val_pct, has_valuation=has_valuation,
            reversion_score=round(rev_score_raw, 2), has_reversion=has_reversion,
            qualitative_score=qual_score, has_qualitative=has_qualitative,
            flow_intensity=round(flow_intensity, 3) if has_flow else None, has_flow=has_flow,
            event_risk=bool(entry.get("event_risk")), event_note=str(entry.get("event_note") or ""),
            event_severity=str(entry.get("event_severity") or ""),
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
    if rev_weight and rev_norm > 0 and _downtrend_confirmed(closes, series, i, config):
        rev_norm, rev_weight = 0.0, 0.0  # 하락추세 확인 시 낙폭과대 매수기여 무효화(떨어지는 칼)
        rev_reasons = [*rev_reasons, "[추세] 하락추세 — 낙폭과대 매수신호 무효화"]
    return [
        (tech_score / 3.0, config.weight_technical, tech_reasons),
        (rev_norm, rev_weight, rev_reasons),
    ]


def _fundamental_component(
    metrics: dict | None, config: SignalConfig
) -> tuple[float, float, list[str]]:
    """재무 metrics(ROE/부채/성장) → 컴포넌트. 데이터 없으면 가중치 0(제외). backtest의
    point-in-time 재무 반영에 쓰인다 — evaluate()의 인라인 계산과 동일 규칙(fnd.score)."""
    fund = fnd.score(metrics or {})
    if not fund.has_data:
        return 0.0, 0.0, fund.reasons
    return fund.score / 2.0, config.weight_fundamental, fund.reasons


def _pit_year(date_str: str, available_years: list[int]) -> int | None:
    """date_str('YYYY-MM-DD') 시점에 '이미 공시돼 알 수 있던' 가장 최근 사업연도.
    연간 사업보고서는 이듬해 3~4월 공시 → 4월 이후면 (연도-1), 이전이면 (연도-2)까지 가용."""
    y, m = int(date_str[:4]), int(date_str[5:7])
    known = y - 1 if m >= 4 else y - 2
    avail = [hy for hy in available_years if hy <= known]
    return max(avail) if avail else None


def _replay_components(
    closes: list[float], series: dict, i: int, config: SignalConfig,
    fund_metrics: dict | None = None,
) -> list[tuple[float, float, list[str]]]:
    """backtest 재현용 컴포넌트 — 가격기반(기술+낙폭과대)에 point-in-time 재무를 선택적으로 더한다.
    (저평가는 시점별 PER/PBR이 종목별 스케일 차이로 횡단면 비교가 왜곡돼 backtest에서 제외 — 기술/
    낙폭/재무는 종목 내 상대·절대값이라 유효.)"""
    comps = _price_only_components(closes, series, i, config)
    if fund_metrics is not None:
        comps.append(_fundamental_component(fund_metrics, config))
    return comps


def replay_signal_kinds(closes: list[float], config: SignalConfig | None = None) -> list[str]:
    """전 구간 매 시점 시그널 재현(backtest_summary와 동일 방법론) — 차트 구간 표시용."""
    config = config or SignalConfig()
    series = compute_indicator_series(closes, config)
    kinds = []
    for i in range(len(closes)):
        combined = combine(_price_only_components(closes, series, i, config), config)
        _apply_trend_gate(combined, closes, series, i, config)
        kinds.append(combined["kind"])
    return kinds


def signal_zones(
    dates: list[str], closes: list[float], config: SignalConfig | None = None
) -> list[dict]:
    """연속된 동일 시그널(BUY/SELL) 구간을 [{start,end,kind,reasons}]로 압축 — 차트 markArea/마커용.
    HOLD는 제외. reasons는 구간 시작(=시그널 전환) 시점의 가격기반 판단 근거(호버 설명용)."""
    config = config or SignalConfig()
    series = compute_indicator_series(closes, config)
    kinds, reasons_at, scores_at = [], [], []
    for k in range(len(closes)):
        combined = combine(_price_only_components(closes, series, k, config), config)
        _apply_trend_gate(combined, closes, series, k, config)
        kinds.append(combined["kind"])
        reasons_at.append(combined["reasons"])
        scores_at.append(combined["score"])
    zones = []
    i, n = 0, len(kinds)
    while i < n:
        if kinds[i] == "HOLD":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == kinds[i]:
            j += 1
        zones.append({"start": dates[i], "end": dates[j - 1], "kind": kinds[i],
                      "reasons": reasons_at[i], "score": scores_at[i]})
        i = j
    return zones


def _run_backtest(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig,
    dates_by_ticker: dict[str, list[str]] | None = None,
    fundamentals_history: dict[str, dict] | None = None,
    start_frac: float = 0.0, end_frac: float = 1.0,
) -> dict:
    """백테스트 코어 — 종가 계열로 과거 매 시점 시그널을 재현하고 이후 실현 수익률로 적중 검증.
    fundamentals_history가 주어지면 각 시점의 '그때 알 수 있던' 연간 재무(point-in-time)를 반영한다.
    start_frac/end_frac로 시계열 구간을 잘라 walk-forward에 재사용한다.

    진입가는 시그널 다음 날의 종가로 근사(시가 미보유 시 근사치). 반환: {by_kind_counts}.
    """
    horizons = config.backtest_horizons
    primary_h = horizons[0]
    by_kind = {k: {f"ret_{h}d": [] for h in horizons} for k in ACTIONABLE_KINDS}
    hits = {k: 0 for k in ACTIONABLE_KINDS}
    counted = {k: 0 for k in ACTIONABLE_KINDS}

    for ticker, closes in prices_by_ticker.items():
        n_all = len(closes)
        lo, hi = int(n_all * start_frac), int(n_all * end_frac)
        window = closes[lo:hi]
        if len(window) < 30:
            continue
        dates = (dates_by_ticker or {}).get(ticker)
        wdates = dates[lo:hi] if dates else None
        hist = (fundamentals_history or {}).get(ticker) or {}
        hist_years = sorted(int(y) for y in hist) if hist else []

        series = compute_indicator_series(window, config)
        for i in range(len(window) - 1):
            entry_idx = i + 1
            if entry_idx + primary_h >= len(window):
                continue
            fund_metrics = None
            if hist_years and wdates:
                py = _pit_year(wdates[i], hist_years)
                fund_metrics = hist.get(str(py)) if py else None
            combined = combine(_replay_components(window, series, i, config, fund_metrics), config)
            _apply_trend_gate(combined, window, series, i, config)
            kind = combined["kind"]
            if kind == HOLD:
                continue

            entry_price = window[entry_idx]
            ret_primary = (window[entry_idx + primary_h] - entry_price) / entry_price
            counted[kind] += 1
            if (ret_primary > config.backtest_hit_ret if is_buy(kind) else ret_primary < -config.backtest_hit_ret):
                hits[kind] += 1
            for h in horizons:
                if entry_idx + h < len(window):
                    by_kind[kind][f"ret_{h}d"].append((window[entry_idx + h] - entry_price) / entry_price)

    return {"by_kind": by_kind, "hits": hits, "counted": counted}


def _by_signal_rows(core: dict, horizons: tuple[int, ...]) -> list[dict]:
    rows = []
    for kind in ACTIONABLE_KINDS:
        n = core["counted"][kind]
        row = {"kind": kind, "n": n,
               "winrate": round(core["hits"][kind] / n * 100, 1) if n else None}
        for h in horizons:
            rets = core["by_kind"][kind][f"ret_{h}d"]
            row[f"avg_ret_{h}d"] = round(sum(rets) / len(rets) * 100, 2) if rets else None
        rows.append(row)
    return rows


def backtest_summary(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig | None = None,
    dates_by_ticker: dict[str, list[str]] | None = None,
    fundamentals_history: dict[str, dict] | None = None,
) -> dict:
    """시그널 적중률 성적표. fundamentals_history를 주면 point-in-time 재무까지 반영(method=pit_v3),
    아니면 가격기반(기술+낙폭과대)만(method=price_based_v2)."""
    config = config or SignalConfig()
    core = _run_backtest(prices_by_ticker, config, dates_by_ticker, fundamentals_history)
    return {
        "method": "pit_v3" if fundamentals_history else "price_based_v2",
        "hit_threshold_pct": config.backtest_hit_ret * 100,
        "by_signal": _by_signal_rows(core, config.backtest_horizons),
    }


# 팩터별 개별 백테스트를 위해 한 팩터만 남기고 나머지 가중치를 0으로
_FACTOR_WEIGHTS = {
    "technical": "weight_technical",
    "reversion": "weight_reversion",
    "fundamental": "weight_fundamental",
}


def factor_contribution(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig | None = None,
    dates_by_ticker: dict[str, list[str]] | None = None,
    fundamentals_history: dict[str, dict] | None = None,
) -> dict:
    """팩터별 기여도 — 각 팩터만 단독으로 켜고(나머지 가중치 0) 백테스트해, 어느 팩터가 매수
    적중률을 끌어올리는지 비교한다. '전체'(모든 팩터)도 함께 반환해 기준선을 준다."""
    from dataclasses import replace
    config = config or SignalConfig()
    zeroed = {w: 0.0 for w in _FACTOR_WEIGHTS.values()}
    factors = []
    # 전체(baseline)
    base_core = _run_backtest(prices_by_ticker, config, dates_by_ticker, fundamentals_history)
    factors.append({"factor": "all", "label": "전체", **_buy_stats(base_core)})
    for name, wfield in _FACTOR_WEIGHTS.items():
        # fundamental은 히스토리 없으면 스킵(단독으로 볼 게 없음)
        if name == "fundamental" and not fundamentals_history:
            continue
        cfg = replace(config, **{**zeroed, wfield: 1.0})
        hist = fundamentals_history if name == "fundamental" else None
        core = _run_backtest(prices_by_ticker, cfg, dates_by_ticker, hist)
        factors.append({"factor": name, "label": _FACTOR_LABEL[name], **_buy_stats(core)})
    return {"factors": factors, "primary_horizon": config.backtest_horizons[0]}


_FACTOR_LABEL = {"technical": "기술적", "reversion": "낙폭과대", "fundamental": "기본적(PIT)"}


def _buy_stats(core: dict) -> dict:
    """매수(강력매수+매수) 합산 적중률·표본·평균수익 — 팩터 비교 지표."""
    n = core["counted"]["BUY"] + core["counted"]["STRONG_BUY"]
    h = core["hits"]["BUY"] + core["hits"]["STRONG_BUY"]
    rets = core["by_kind"]["BUY"]["ret_5d"] + core["by_kind"]["STRONG_BUY"]["ret_5d"] \
        if "ret_5d" in core["by_kind"]["BUY"] else []
    return {
        "n": n,
        "winrate": round(h / n * 100, 1) if n else None,
        "avg_ret": round(sum(rets) / len(rets) * 100, 2) if rets else None,
    }


def walk_forward(
    prices_by_ticker: dict[str, list[float]], config: SignalConfig | None = None,
    dates_by_ticker: dict[str, list[str]] | None = None,
    fundamentals_history: dict[str, dict] | None = None,
    windows: int = 4,
) -> dict:
    """워크포워드 — 시계열을 windows개 구간으로 순차 분할해 각 구간에서 따로 백테스트한다.
    특정 구간에서만 잘 맞고 다른 구간에선 무너지는(과최적화·불안정) 시그널을 드러내기 위함이다.
    (우리 시그널은 학습 파라미터가 없어 별도 train은 없고, 구간별 out-of-sample 안정성 점검이다.)"""
    config = config or SignalConfig()
    segs = []
    for w in range(windows):
        core = _run_backtest(prices_by_ticker, config, dates_by_ticker, fundamentals_history,
                             start_frac=w / windows, end_frac=(w + 1) / windows)
        segs.append({"window": w + 1, **_buy_stats(core)})
    valid = [s["winrate"] for s in segs if s["winrate"] is not None]
    spread = round(max(valid) - min(valid), 1) if len(valid) >= 2 else None
    return {"windows": segs, "winrate_spread": spread}
