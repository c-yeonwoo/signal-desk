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

import datetime
from dataclasses import dataclass, field

from signal_desk.signals import flow as flow_mod
from signal_desk.signals import fundamental as fnd
from signal_desk.signals import short as short_mod
from signal_desk.signals import indicators as ind
from signal_desk.signals import momentum as mom
from signal_desk.signals import quality as fscore
from signal_desk.signals import narrative as narr
from signal_desk.signals import qualitative as qual
from signal_desk.signals import reversion as rev
from signal_desk.signals import valuation as val
from signal_desk.signals.decision import Decision, decision_from_legacy, empty_decision


@dataclass
class SignalConfig:
    # H1(2026-08-03): harness 가격순위 ablation — technical=0 → 판별력 있음(p100, 최악위상>중위).
    # 횡단면 IC≈0인 타이밍 팩터라 랭킹 가중에서 제외. 게이트·차트·reasons 계산은 유지(가중만 0).
    weight_technical: float = 0.0
    weight_fundamental: float = 0.30
    weight_valuation: float = 0.15
    weight_reversion: float = 0.20
    # **점수에 들어가지 않는다.** `qual.component`가 돌려주는 정규화점수·가중치는 폐기되고
    # (`_ = qual_norm, qual_weight`) 표시용 `qualitative_score`·`has_qualitative`만 쓴다 —
    # KB 정성은 veto/shadow 층이고 `combine`에 미투입이라는 계약(CLAUDE.md·화면 카피)대로다.
    # 이름이 `weight_*`라 9.4%(0.15/1.60) 가중처럼 읽히는 것이 함정이었다: 2026-08-06 감사에서
    # 실제로 이 값을 근거로 "정성이 점수의 9.4%"라고 잘못 집계했다. A/B 실측 Δ=0.000(16종목).
    # `signalcfg.FIELDS`에 없어 사전등록·해시 대상도 아니다 — combine에 넣으려면 **먼저 FIELDS와
    # 사전등록에 넣어야** 한다(안 그러면 검증된 적 없는 파라미터가 판정에 섞인다).
    weight_qualitative: float = 0.15  # combine 미투입 — 표시·shadow 전용
    weight_flow: float = 0.20  # 수급(외국인·기관 순매수) — KR만, 데이터 있을 때만 포함
    weight_quality: float = 0.15  # 퀄리티(축약 F-Score) — 재무 건전성·개선. 데이터 있을 때만 포함
    weight_momentum: float = 0.30  # 중기 모멘텀(12-1개월) — 5.5년 실측상 가격기반 종합 IC의 주동인·과소가중
    #                                확인(0.20→0.30 상향, 어드민 승인). 가격만 필요, 백테스트 반영. 트래커 라이브 IC로 확정
    weight_short: float = 0.15  # 공매도 거래비중(KR) — 하방 리스크. 비중 높을 때만 음(-)으로 포함
    momentum_lookback: int = 252   # 약 12개월(거래일)
    momentum_skip: int = 21        # 직전 약 1개월 제외(단기 반전 노이즈 제거)
    momentum_scale: float = 0.5    # 수익률→스코어 스케일(+50%면 +1로 포화)

    # 5단계 시그널 임계값(종합점수 범위 ~[-3,3]): 강력매수 ≥ 매수 ≥ (관망) ≥ 매도 ≥ 강력매도
    strong_buy_threshold: float = 2.0
    buy_threshold: float = 1.2
    sell_threshold: float = -1.2
    strong_sell_threshold: float = -2.0

    # 매수권 선정 방식 — "rank"(횡단면 상위 분위) | "absolute"(절대 문턱)
    #
    # absolute는 문턱이 점수 분포 밖으로 나가면 후보가 **산술적으로** 0이 된다. 2026-07-26 진단:
    # PIT 스냅샷 2,000건(200종목×10거래일)의 최고점수 1.91 · p99 1.45인데 유효문턱이 2.0~2.4여서
    # 10거래일간 매수 1건. 8팩터를 가중평균 후 ×3 하는 구조상 2.0을 넘으려면 정규화 팩터 평균이
    # 0.67이어야 해서, 강세장에서도 거의 발생하지 않는다(strong_buy 2.0은 관측 0건).
    # rank는 같은 시장 안 상대 순위로 고른다. 다만 최소점수 미달이면 후보 0이 정상
    # (매일 약한 BUY를 찍으면 신뢰가 떨어진다). 매도는 절대 기준 유지.
    selection_mode: str = "rank"
    rank_top_pct: float = 3.0     # 시장 내 상위 N%를 매수권으로(200종목이면 6종목)
    # buy_threshold와 동기화 — 절대 BUY도 안 될 점수를 분위 승격하지 않는다
    rank_min_score: float = 1.2

    # 매수권에 요구하는 **데이터 커버리지** 하한(조건 미발동은 누락으로 안 셈 — `data_coverage`).
    # 재정규화 편향 때문에 팩터가 빠진 종목이 남은 팩터로 재정규화돼 극단 점수를 받는다.
    # 실측 200종목 분포로 정했다: p5 0.759 · p25 0.862 · 중위 1.000.
    #   0.75 → 4종목 차단(상위6 중 0자리) = 아무것도 안 막는다
    #   0.80 → 27종목 차단(상위6 중 3자리)  ← 채택
    #   0.90 → 94종목 차단(상위6 중 6자리) = 매수 0, 즉 0으로 나누기
    # 0이면 비활성. 점수는 그대로 내고 **매수권에서만** 뺀다 — 조용히 지우면 왜 없는지 모른다.
    min_data_coverage: float = 0.80

    # 하락추세 게이트(떨어지는 칼 차단) 사용 여부. 1=적용 · 0=미적용.
    # **설정으로 끌 수 있어야 사전등록·하네스로 검증할 수 있다** — "검사에 넣을 수 없는 파라미터는
    # 검증된 적이 없다". 실측 진단(2026-08-06): 게이트를 끄면 백분위 71.5 → 92.5, 대신 위상편차가
    # 73.2 → 109.4pp로 커졌다. 그 결과를 **보고 난 뒤**라 채택은 OOS 사전등록으로만 한다.
    trend_gate: float = 1.0

    # 국면 적응: 1이면 약세·조정·거시 비우호 국면에서 매수 임계값을 자동 상향(regime.buy_threshold_bump).
    # 0이면 임계값 고정. (관리자 조정 필드 — signalcfg.FIELDS에 포함)
    regime_adaptive: float = 1.0

    # 실적발표 임박 게이트: 발표 D-day까지 이 일수 이내면 신규 매수 신호를 관망으로 강등(바이너리
    # 이벤트 회피 — precision 우선). 0이면 비활성. 미 어닝 캘린더만 있어 사실상 US 전용.
    earnings_gate_days: int = 7

    # 급락 게이트(떨어지는 칼·당일): 모멘텀(12-1)은 최근 1개월을 빼고, 추세 게이트는 MA 역배열만
    # 봐서 폭등 직후 −18%에도 우선매수가 남을 수 있다(HD현대에너지솔루션 2026-07-27). 일·이틀
    # 급락이면 매수권·분위 승격을 막는다(gate_blocked). 0이면 해당 창 비활성.
    crash_gate_1d_pct: float = -8.0    # 1거래일 수익률(%) 이하
    crash_gate_2d_pct: float = -12.0   # 2거래일 누적 수익률(%) 이하

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
    quality_points: int | None = None  # 축약 F-Score(0~5, 재무 건전성·개선)
    has_quality: bool = False
    momentum_ret: float | None = None  # 12-1개월 수익률(중기 모멘텀)
    has_momentum: bool = False
    short_ratio: float | None = None  # 최근 공매도 거래비중(KR)
    has_short: bool = False
    event_risk: bool = False  # Decision.buy_blocked 별칭 — 매수 후보 제외(veto)
    event_note: str = ""
    event_severity: str = ""  # critical|serious|'' — Decision.severity 별칭
    decision: Decision = field(default_factory=empty_decision)
    earnings_date: str | None = None  # 다가오는 실적발표 예정일(YYYY-MM-DD, US)
    earnings_soon: bool = False  # 실적발표 임박(게이트 창 이내) — 신규 매수 보류
    rank: int | None = None        # 시장 내 점수 순위(1=최상위) — 횡단면 선정용
    rank_pct: float | None = None  # 시장 내 점수 상위 백분위(1.0=상위 1%) — 표시용
    rank_eligible: bool = False    # 매수권(상위 분위 + 최소점수 + 게이트 통과)
    gate_blocked: bool = False     # 추세·실적·급락·악재 게이트로 매수 보류(분위 안이어도 승격 금지)
    # ── 재정규화 편향 노출 (X2) ──
    # `weight_sum_ratio`: 점수가 **실제로 나눈** 가중합 / 전체 가중합. 낮을수록 남은 팩터로
    # 재정규화돼 점수가 극단으로 간다(실측 |점수| 1.070 vs 0.303).
    weight_sum_ratio: float | None = None
    # `data_coverage`: 조건 미발동(낙폭과대)을 뺀 데이터 커버리지. 게이트가 보는 값.
    data_coverage: float | None = None
    missing_factors: list[str] = field(default_factory=list)  # 데이터 없어 빠진 팩터(이름)
    low_coverage: bool = False      # 데이터 커버리지 미달로 매수권 제외
    # ── 게이트 투명화 (X3) ──
    # `gate_blocked` 불리언 하나로는 무엇이 막았는지 알 수 없었다. 화면이 근거 문구를
    # 문자열 파싱(`[추세]` 접두어)해서 뒤집어 맞추고 있었고, 그 매핑이 어디에도 검사되지 않았다.
    gates: list[str] = field(default_factory=list)          # 발동한 게이트 키(GATE_LABELS)
    gates_relaxed: list[str] = field(default_factory=list)  # 조건은 걸렸으나 완화된 게이트
    reasons: list[str] = field(default_factory=list)
    narrative: str = ""
    factor_scores: dict[str, float] = field(default_factory=dict)  # 팩터별 방향·강도 [-1,1] — 시각화용

    def __post_init__(self) -> None:
        # 테스트·수동 조립: decision 미지정 시 레거시 event_* 에서 복원
        if self.decision == empty_decision() and (self.event_risk or self.event_severity):
            self.decision = decision_from_legacy(
                event_risk=self.event_risk, event_severity=self.event_severity,
                event_note=self.event_note,
            )


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


def ret_pct_n(closes: list[float], i: int, n: int) -> float | None:
    """i 시점 기준 n거래일 수익률(%). 표본 부족이면 None."""
    if i - n < 0 or i >= len(closes) or closes[i - n] == 0:
        return None
    return (closes[i] / closes[i - n] - 1) * 100


def market_return_pct(prices: dict[str, list[float]], n: int = 20) -> float | None:
    """유니버스 n거래일 수익률의 중위값(%). 시장 자체가 내렸는지를 판정하는 기준선.

    이게 없으면 하락추세 게이트가 종목 고유 정보와 시장 전체 하락을 구분하지 못한다 —
    조정장에서는 200종목이 거의 다 역배열이라 게이트가 '전면 차단 스위치'로 작동한다.
    """
    rets = [r for closes in prices.values()
            if (r := ret_pct_n(closes, len(closes) - 1, n)) is not None]
    if not rets:
        return None
    rets.sort()
    m = len(rets) // 2
    return rets[m] if len(rets) % 2 else (rets[m - 1] + rets[m]) / 2


def _downtrend_blocking(
    closes: list[float], series: dict, i: int, config: SignalConfig,
    market_ret_20d: float | None = None,
) -> bool:
    """하락추세 게이트를 실제로 적용할지. 확인된 하락추세여도 **시장 대비 상대강도가 우위**면
    적용하지 않는다 — 시장이 −10%인데 이 종목이 −3%라면 역배열은 종목의 결함이 아니라
    시장 상태다. 상대 예외 없이는 조정장에 매수 후보가 0이 된다(2026-07-26 진단).
    market_ret_20d=None(백테스트·리플레이)이면 기존 절대 판정과 동일하게 동작한다."""
    if not _downtrend_confirmed(closes, series, i, config):
        return False
    if market_ret_20d is None or market_ret_20d >= 0:
        return True
    own = ret_pct_n(closes, i, 20)
    return not (own is not None and own > market_ret_20d)


# 매수 게이트 — 키와 표시명. `gate_blocked` 불리언 하나로는 무엇이 막았는지 알 수 없어서
# 화면이 근거 문구를 문자열 파싱하고 있었다(`[추세]`·`[급락]` 접두어). 구조로 남긴다.
GATE_LABELS = {
    "trend": "추세", "earnings": "실적", "crash": "급락", "event": "악재",
    "coverage": "데이터부족",
}


def _mark_gate(combined: dict, key: str, reason: str) -> None:
    """게이트 발동을 **구조로** 기록한다. 문구는 사람용, `gates`는 집계용."""
    combined["gated"] = True
    combined["gates"] = [*(combined.get("gates") or []), key]
    combined["reasons"] = [*combined["reasons"], reason]


def _apply_trend_gate(
    combined: dict, closes: list[float], series: dict, i: int, config: SignalConfig,
    market_ret_20d: float | None = None,
) -> dict:
    """확인된 하락추세에서 종합 매수신호를 관망으로 강등(떨어지는 칼 차단). 낙폭과대 기여는
    컴포넌트 단계(_price_only_components/evaluate)에서 이미 무효화됨.
    시장 대비 상대강도 우위면 게이트를 적용하지 않는다(_downtrend_blocking).

    **kind가 BUY가 아니어도 `gated`를 세운다.** 예전엔 여기서 early-return 해서, `rank_min_score`
    가 `buy_threshold` 보다 낮으면 그 사이 점수(classify → HOLD)가 `gate_blocked=False` 로 남고
    `apply_cross_sectional` 이 그걸 **STRONG_BUY로 승격**했다 — 실측 재현: 하락추세 확인 종목이
    점수 +0.80에 매수권을 받았고 근거 문구에 추세 언급조차 없었다. `_apply_crash_gate` 는 이미
    이 위험을 알고 HOLD여도 gated를 세우고 있었다(그 docstring 참고) — 게이트 3개가 정책이
    둘로 갈라져 있었다. 이제 셋 다 같은 규약이다.
    """
    if not float(getattr(config, "trend_gate", 1.0) or 0.0):
        return combined                            # 게이트 미적용(사전등록 D4 family)
    if _downtrend_blocking(closes, series, i, config, market_ret_20d):
        if combined["kind"] in BUY_KINDS:
            combined["kind"] = HOLD
        _mark_gate(combined, "trend",
                   "[추세] 하락추세 확인(종가<MA20<MA60) — 반등 전 매수 차단(관망)")
    elif _downtrend_confirmed(closes, series, i, config):
        # 완화도 기록한다. 예전엔 BUY일 때만 남겨서, 실측 74종목 중 7종목이 완화됐는데
        # 화면상 완화 발동은 0건이었다 — "있는지 모르는 완화"는 없는 것과 같다.
        combined["gate_relaxed"] = [*(combined.get("gate_relaxed") or []), "trend"]
        combined["reasons"] = [
            *combined["reasons"],
            f"[추세] 하락추세지만 시장(20일 {market_ret_20d:+.1f}%) 대비 상대강도 우위 — 게이트 완화"
            if market_ret_20d is not None else
            "[추세] 하락추세지만 게이트 완화 조건 충족",
        ]
    return combined


def _days_until(date_str: str | None, today: datetime.date) -> int | None:
    """date_str('YYYY-MM-DD')까지 남은 일수(음수=이미 지남). 파싱 실패 시 None."""
    if not date_str:
        return None
    try:
        return (datetime.date.fromisoformat(str(date_str)[:10]) - today).days
    except (TypeError, ValueError):
        return None


def _apply_earnings_gate(
    combined: dict, days_until: int | None, config: SignalConfig
) -> bool:
    """실적발표 임박(D-day까지 earnings_gate_days 이내) 종목의 신규 매수 신호를 관망으로 강등.
    실적은 바이너리 이벤트(발표 후 급등락)라 발표 전 진입은 동전던지기 — precision 우선으로 보류한다.
    매도·청산 신호는 그대로 둔다(발표 전 리스크 축소는 허용). 반환: 임박 창 이내 여부(표시용)."""
    window = config.earnings_gate_days
    if window <= 0 or days_until is None or not (0 <= days_until <= window):
        return False
    # 추세 게이트와 같은 이유로 kind와 무관하게 gated를 세운다 — 분위 승격이 우회하지 못하게.
    if combined["kind"] in BUY_KINDS:
        combined["kind"] = HOLD
    _mark_gate(combined, "earnings",
               f"[실적] {days_until}일 뒤 실적발표 예정 — 발표 전 신규 매수 보류(관망)")
    return True


def _crash_reason(closes: list[float], i: int, config: SignalConfig) -> str | None:
    """급락 게이트 사유 문구. 해당 없으면 None. 매도 신호는 호출측에서 건드리지 않는다."""
    # 0.0은 비활성 — `or 0` 쓰면 안 된다(0.0이 falsy).
    thr1 = float(config.crash_gate_1d_pct)
    thr2 = float(config.crash_gate_2d_pct)
    if thr1 >= 0 and thr2 >= 0:
        return None
    # 비율로 비교(×100 부동소수로 −8%가 −7.999…가 되는 경계 오차 회피)
    eps = 1e-12
    if thr1 < 0 and i >= 1 and closes[i - 1] > 0:
        r1 = closes[i] / closes[i - 1] - 1
        if r1 <= thr1 / 100 + eps:
            return f"[급락] 1일 {r1 * 100:+.1f}% — 단기 급락으로 신규 매수 보류(관망)"
    if thr2 < 0 and i >= 2 and closes[i - 2] > 0:
        r2 = closes[i] / closes[i - 2] - 1
        if r2 <= thr2 / 100 + eps:
            return f"[급락] 2일 {r2 * 100:+.1f}% — 단기 급락으로 신규 매수 보류(관망)"
    return None


def _apply_crash_gate(
    combined: dict, closes: list[float], i: int, config: SignalConfig
) -> bool:
    """단기 급락 시 신규 매수 보류. 이미 BUY면 HOLD로 강등하고, HOLD여도 gated=True로 두어
    이후 횡단면 분위 승격(apply_cross_sectional)이 급락 종목을 우선매수로 올리지 못하게 한다.
    매도·청산은 그대로. 반환: 게이트 적용 여부."""
    if is_sell(combined.get("kind") or HOLD):
        return False
    reason = _crash_reason(closes, i, config)
    if not reason:
        return False
    if combined["kind"] in BUY_KINDS:
        combined["kind"] = HOLD
    _mark_gate(combined, "crash", reason)
    return True


def _decision_from_entry(entry: dict | None) -> Decision:
    """sentiment_map 한 줄 → Decision. Decision 객체·dict·레거시 event_* 모두 허용."""
    from signal_desk.signals import decision as decmod
    entry = entry or {}
    dec_raw = entry.get("decision")
    if isinstance(dec_raw, decmod.Decision):
        return dec_raw
    if isinstance(dec_raw, dict) and dec_raw:
        return decmod.Decision(
            buy_blocked=bool(dec_raw.get("buy_blocked")),
            holding_action=dec_raw.get("holding_action") or "none",
            event_id=dec_raw.get("event_id"),
            severity=dec_raw.get("severity"),
            summary=str(dec_raw.get("summary") or ""),
            policy_version=str(dec_raw.get("policy_version") or decmod.POLICY_VERSION),
        )
    return decmod.decision_from_legacy(
        event_risk=bool(entry.get("event_risk")),
        event_severity=str(entry.get("event_severity") or ""),
        event_note=str(entry.get("event_note") or ""),
        event_id=entry.get("event_id"),
    )


def _apply_event_veto(combined: dict, dec: Decision) -> bool:
    """Decision.buy_blocked면 급락 게이트와 같이 BUY→HOLD + gated.

    예전엔 event_risk가 분위 승격만 막고(absolute 모드·표시) kind를 BUY로 남겨
    '매수 pill + 악재 경고' 충돌이 났다. 하드 강등으로 hold_tag 악재가 리스트에 보이게 한다.
    매도·청산 kind는 건드리지 않는다(보유 trim/exit는 bot이 holding_action으로 처리).
    """
    if not dec or not dec.buy_blocked:
        return False
    if is_sell(combined.get("kind") or HOLD):
        return False
    if combined["kind"] in BUY_KINDS:
        combined["kind"] = HOLD
    note = (dec.summary or "").strip()
    sev = (dec.severity or "").strip()
    head = note[:120] if note else (sev or "결정 이벤트")
    _mark_gate(combined, "event", f"[악재] {head} — 신규 매수 보류(관망)")
    return True


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


# 점수 팩터 → 가중치 필드. `combine`은 컴포넌트 이름을 모르므로 커버리지는 여기서 센다.
SCORE_WEIGHT_FIELDS = ("weight_technical", "weight_fundamental", "weight_valuation",
                       "weight_reversion", "weight_flow", "weight_quality",
                       "weight_momentum", "weight_short")
# **조건부 팩터** — 조건이 발동해야 가중치를 갖는다(`_reversion_component` 주석 참고).
# '계산 불가'와 '조건 미발동'은 나눠 센다: 낙폭과대는 급락 때만 발동해 평상시 발동률이 2.5%인데,
# 이걸 데이터 부족으로 세면 데이터를 아무리 더 받아도 커버리지가 영원히 안 오른다.
CONDITIONAL_FACTORS = ("reversion",)


def total_weight(config: SignalConfig | None = None) -> float:
    """점수 팩터 가중치의 전체 합 — 재정규화 비율의 분모."""
    config = config or SignalConfig()
    return sum(float(getattr(config, f, 0.0) or 0.0) for f in SCORE_WEIGHT_FIELDS)


def data_coverage(has: dict[str, bool], config: SignalConfig | None = None, *,
                  unavailable: tuple[str, ...] = ()) -> dict:
    """`{factor: 데이터있음}` → 데이터 커버리지. **조건 미발동은 누락으로 세지 않는다.**

    반환 `ratio`는 (셀 수 있는 가중합 − 데이터 없어서 빠진 가중합) / 셀 수 있는 가중합이다.
    `missing`은 이름으로 낸다 — "누락 2개"만 적으면 어느 팩터인지 몰라 조사가 안 된다.

    `unavailable`은 **그 실행이 원리적으로 볼 수 없는** 팩터다(하네스의 수급·공매도는 시계열
    이력 자체가 없다). 분자·분모에서 같이 빼야 한다 — 종목의 결함이 아닌 것을 종목에 물리면
    price6 하네스가 전 종목 커버리지 미달로 매수 0이 되고, 게이트는 검증 불가가 된다.
    조건부 팩터(`reversion`)를 빼는 것과 같은 이유다.
    """
    config = config or SignalConfig()
    skip = set(CONDITIONAL_FACTORS) | set(unavailable or ())
    countable = sum(float(getattr(config, f, 0.0) or 0.0) for f in SCORE_WEIGHT_FIELDS
                    if f.removeprefix("weight_") not in skip)
    missing, lost = [], 0.0
    for f in SCORE_WEIGHT_FIELDS:
        key = f.removeprefix("weight_")
        if key in skip or has.get(key, False):
            continue
        missing.append(key)
        lost += float(getattr(config, f, 0.0) or 0.0)
    return {"ratio": (round((countable - lost) / countable, 4) if countable else None),
            "missing": missing, "lost_weight": round(lost, 4),
            "countable_weight": round(countable, 4), "excluded": sorted(skip)}


def rank_slots(universe_n: int, top_pct: float) -> int:
    """상위 top_pct%가 몇 종목인지. 최소 1종목 — 유니버스가 작으면 반올림으로 0이 되어
    "상대적으로 가장 좋은 종목"조차 못 고르는 일이 생긴다(200종목 3% = 6종목)."""
    if universe_n <= 0:
        return 0
    return max(1, round(universe_n * top_pct / 100))


def apply_cross_sectional(results: list[SignalResult],
                          config: SignalConfig | None = None) -> list[SignalResult]:
    """횡단면 분위로 매수권을 정한다(절대 문턱 대체). results는 점수 내림차순 정렬 상태를 가정.

    모든 종목에 `rank_pct`(상위 백분위)를 채우고, **원 상위 k자리 안** · `rank_min_score`
    이상 · 게이트 미차단인 종목만 `rank_eligible`로 표시하고 kind를 BUY로 승격한다.
    승격은 매수권 표시일 뿐이고, 실제 매수 크기는 국면 익스포저(regime.target_exposure)가 정한다.

    상위가 게이트면 그 자리는 **공석** — k 밖 약한 종목으로 채우지 않는다.
    (예전엔 다음 순위가 올라와 매일 슬롯이 찼고, 최소점수 0.5와 겹쳐 신뢰가 떨어졌다.)

    분위를 봇 성향별로 더 좁힐 수 있게 `rank_pct`는 전 종목에 남긴다(넓히는 건 불가 — 엔진
    분위가 앱 전체의 '매수권' 정의다).
    """
    config = config or SignalConfig()
    if config.selection_mode != "rank" or not results:
        return results
    n = len(results)
    k = rank_slots(n, config.rank_top_pct)
    taken = 0
    for idx, r in enumerate(results):
        r.rank = idx + 1
        r.rank_pct = round((idx + 1) / n * 100, 2)
        in_window = r.rank <= k
        # 데이터 커버리지 미달 — 점수는 그대로 두고 매수권에서만 뺀다(X2).
        # 커버리지를 모르는 종목(None)은 막지 않는다: 재무 없는 시장에서 전부 차단되면
        # 그건 신중함이 아니라 0으로 나누기다.
        r.low_coverage = bool(config.min_data_coverage > 0
                              and r.data_coverage is not None
                              and r.data_coverage < config.min_data_coverage)
        eligible = (in_window
                    and taken < k
                    and r.score >= config.rank_min_score
                    and not r.gate_blocked
                    and not r.event_risk
                    and not r.low_coverage)
        r.rank_eligible = eligible
        if eligible:
            # 절대 classify가 이미 BUY여도 자리 순서로 다시 나눈다.
            # 예전엔 `if not is_buy`라서 절대 BUY(≥1.2)는 STRONG 승격이 스킵되고,
            # 그 아래 절대 HOLD(<1.2)만 우선매수 자리를 먹어 점수↔라벨이 뒤집혔다.
            strong_n = max(1, k // 3)
            new_kind = STRONG_BUY if taken < strong_n else BUY
            if r.kind != new_kind:
                r.reasons = [*r.reasons,
                             f"[선정] 시장 {n}종목 중 {r.rank}위(매수권 {k}자리·우선 {strong_n}) — "
                             f"절대 문턱이 아니라 같은 시장 안의 상대 순위로 고른 종목"]
            r.kind = new_kind
            taken += 1
        elif is_buy(r.kind):
            # 절대 문턱은 넘었지만 상대 순위·최소점수·게이트로 탈락 → 매수권 아님
            r.kind = HOLD
            why = (f"데이터 커버리지 {r.data_coverage:.0%} < {config.min_data_coverage:.0%}"
                   if r.low_coverage else f"매수권({k}자리) 밖")
            r.reasons = [*r.reasons, f"[선정] 시장 {n}종목 중 {r.rank}위 — {why}"]
        elif in_window and (r.gate_blocked or r.event_risk or r.low_coverage
                            or r.score < config.rank_min_score):
            # 창 안이지만 공석 — 왜 비었는지 남긴다(조용한 0 방지)
            if r.low_coverage:
                note = (f"데이터 커버리지 {r.data_coverage:.0%} < {config.min_data_coverage:.0%}"
                        f" — 없는 팩터: {', '.join(r.missing_factors) or '없음'}"
                        f" (남은 팩터로 재정규화돼 점수가 부풀려진다)")
            elif r.gate_blocked or r.event_risk:
                note = "게이트·악재로 자리 공석"
            else:
                note = f"최소점수 {config.rank_min_score:.1f} 미달로 자리 공석"
            r.reasons = [*r.reasons, f"[선정] 시장 {n}종목 중 {r.rank}위 — {note}"]
    return results


def selection_summary(results: list[SignalResult],
                      config: SignalConfig | None = None) -> dict:
    """매수권 선정 상태 요약 — UI·브리핑·진단이 같은 숫자를 쓰게 한다.

    `distribution`은 "문턱이 점수 분포 밖에 있는지"를 상시 확인하기 위한 값이다. 절대 문턱
    모드에서 max < buy_threshold면 매수는 산술적으로 불가능하다 — 그걸 화면에서 보이게 한다.
    """
    config = config or SignalConfig()
    scores = sorted((r.score for r in results), reverse=True)
    n = len(scores)

    def pct(p: float) -> float | None:
        if not n:
            return None
        return round(scores[min(n - 1, int(n * p / 100))], 2)

    eligible = [r for r in results if r.rank_eligible] if config.selection_mode == "rank" \
        else [r for r in results if is_buy(r.kind)]
    return {
        "mode": config.selection_mode,
        "universe": n,
        "rank_slots": rank_slots(n, config.rank_top_pct),
        "rank_top_pct": config.rank_top_pct,
        "rank_min_score": config.rank_min_score,
        "buy_threshold": config.buy_threshold,
        "eligible": len(eligible),
        "cutoff_score": round(min((r.score for r in eligible), default=0.0), 2) if eligible else None,
        "gate_blocked": sum(1 for r in results if r.gate_blocked),
        # 문턱은 넘었는데 창(rank_slots) 밖이라 못 산 종목을 세려면 이 수가 필요하다.
        # 2026-08-05 진단: 매수 0의 원인이 게이트인지 창인지 화면이 구분하지 못했다(둘 다
        # "정밀도 우선 · 고장 아님"으로 렌더). 실측은 문턱 통과 26 · 창 6자리 · 게이트 차단 11이었다.
        "over_threshold": sum(1 for r in results if r.score >= config.rank_min_score),
        "distribution": {"max": pct(0), "p90": pct(10), "p99": pct(1), "median": pct(50)},
        "threshold_above_max": bool(n and config.selection_mode == "absolute"
                                    and scores[0] < config.buy_threshold),
        # 커버리지 게이트(X2) — 문턱은 점수 분포처럼 **커버리지 분포와 함께** 봐야 한다.
        # 새 절대 문턱을 만들 때 그 값이 분포의 어디인지 화면에 안 내면 다음 사람이 다시 튜닝한다.
        "coverage": _coverage_summary(results, config),
        # 게이트 투명화(X3) — `gate_blocked 17` 하나로는 무엇이 매수 0을 만들었는지 모른다.
        # 실측: 추세 17 · 급락 1이고 **상위 6자리 중 5자리가 추세**였다.
        "gates": _gate_summary(results, config),
    }


def _gate_summary(results: list[SignalResult], config: SignalConfig) -> dict:
    """게이트별 차단 수 — 전체 · 상위 k자리 · 완화 발동. 이름과 함께 낸다.

    **창 안 차단 수가 핵심이다.** 전체 17종목이 막혔어도 상위 6자리를 안 건드리면 매수 0의
    원인이 아니다. 실측은 반대였다 — 추세 게이트가 상위 6자리 중 5자리를 먹었다.
    """
    k = rank_slots(len(results), config.rank_top_pct)
    window = results[:k]                       # 점수 내림차순 가정
    keys = list(GATE_LABELS)
    counted = {"coverage": sum(1 for r in results if r.low_coverage)}
    win = {"coverage": sum(1 for r in window if r.low_coverage)}
    relaxed: dict[str, int] = {}
    for key in keys:
        if key == "coverage":
            continue
        counted[key] = sum(1 for r in results if key in (r.gates or ()))
        win[key] = sum(1 for r in window if key in (r.gates or ()))
        relaxed[key] = sum(1 for r in results if key in (r.gates_relaxed or ()))
    # 창 자리를 실제로 앗아간 게이트만 골라 정렬 — 매수 0의 원인 순서다.
    top_causes = sorted(((k2, v) for k2, v in win.items() if v), key=lambda kv: -kv[1])
    return {
        "labels": dict(GATE_LABELS),
        "blocked": {k2: v for k2, v in counted.items() if v},
        "blocked_in_window": {k2: v for k2, v in win.items() if v},
        "relaxed": {k2: v for k2, v in relaxed.items() if v},
        "window_slots": k,
        # 창 안에서 어떤 게이트도 안 걸렸는데 매수 0이면 원인은 게이트가 아니다(점수·창).
        "window_causes": [{"gate": k2, "label": GATE_LABELS.get(k2, k2), "slots": v}
                          for k2, v in top_causes],
    }


def _coverage_summary(results: list[SignalResult], config: SignalConfig) -> dict:
    """데이터 커버리지 분포 + 게이트가 실제로 막은 수. 어느 팩터가 몇 종목에서 빠졌는지도 낸다."""
    covs = sorted(r.data_coverage for r in results if r.data_coverage is not None)
    m = len(covs)

    def q(p: float) -> float | None:
        return round(covs[min(m - 1, int(m * p))], 3) if m else None

    missing: dict[str, int] = {}
    for r in results:
        for f in r.missing_factors or ():
            missing[f] = missing.get(f, 0) + 1
    k = rank_slots(len(results), config.rank_top_pct)
    window = results[:k]              # 점수 내림차순 가정 — 상위 k자리
    return {
        "min_required": config.min_data_coverage,
        "measured": m,
        "blocked": sum(1 for r in results if r.low_coverage),
        # 상위 k자리 중 몇 자리가 커버리지로 비었나 — 전체 차단 수보다 이게 매수 0을 설명한다.
        "blocked_in_window": sum(1 for r in window if r.low_coverage),
        "distribution": {"min": q(0.0), "p5": q(0.05), "p25": q(0.25),
                         "median": q(0.50), "max": (round(covs[-1], 3) if m else None)},
        "full_coverage": sum(1 for r in results if not (r.missing_factors or ())),
        # 조건부 팩터는 여기 안 센다 — 데이터를 더 받아도 안 오르는 값이라 섞으면 판정이 막힌다.
        "missing_by_factor": dict(sorted(missing.items(), key=lambda kv: -kv[1])),
        "conditional_factors": list(CONDITIONAL_FACTORS),
    }


def combine(components: list[tuple[float, float, list[str]]], config: SignalConfig | None = None) -> dict:
    """(정규화 점수[-1,1], 가중치, 근거) 컴포넌트 리스트를 가중평균해 결합.

    가중치 0인 컴포넌트는 가중평균에는 기여하지 않지만(사실상 제외와 동일), 근거 문구는
    그대로 reasons에 포함된다 — 예: "재무데이터 없음" 안내.

    ## 재정규화 편향 (2026-08-06 X2)

    분모가 **발동한 가중치의 합**이라, 팩터가 빠진 종목은 남은 팩터로 재정규화돼 극단 점수가
    나온다. 실측(200종목): `|점수|` 평균이 커버리지 순으로 1.070 / 0.805 / 0.638 / **0.303** —
    데이터가 가장 적은 종목이 가장 센 점수를 받고, 전 팩터를 가진 3종목은 최고 +0.43으로
    사실상 매수 불가였다. 상위 3위가 전부 5/8 팩터였고 유일한 매수권 종목도 그랬다.

    분모를 전체 가중합으로 바꾸면(=결측을 중립 0으로 반영) 편향은 사라지지만 **모든 종목의
    점수가 변한다.** 판별력이 `판정 불가`인 동안 점수 정의를 바꾸는 것은 곡선 맞추기라
    지금은 하지 않는다. 대신 (a) `weight_sum_ratio`를 산출물에 실어 노출하고,
    (b) 데이터 커버리지가 낮은 종목을 **매수권에서** 뺀다(점수는 그대로 낸다 — 조용히 지우면
    왜 없는지 모른다). 분모 교체는 하네스에 넣어 재는 것이 먼저다.
    """
    config = config or SignalConfig()

    weight_sum = sum(w for _, w, _ in components)
    weighted = sum(norm * w for norm, w, _ in components) / weight_sum if weight_sum else 0.0
    score = weighted * 3
    kind = classify(score, config)

    confidence = round(abs(2 * ind.sigmoid(score) - 1) * 100) / 100
    reasons = [r for _, _, rs in components for r in rs]

    total = total_weight(config)
    return {"score": round(score, 2), "kind": kind, "confidence": confidence, "reasons": reasons,
            "weight_sum": round(weight_sum, 4), "weight_total": round(total, 4),
            # 점수가 **실제로 나눈** 가중합의 비율. 1.0이면 전 팩터가 발동했다.
            "weight_sum_ratio": (round(weight_sum / total, 4) if total else None)}


def evaluate(
    universe: list[dict],
    prices: dict[str, list[float]],
    fundamentals: dict[str, dict] | None = None,
    config: SignalConfig | None = None,
    sentiment: dict[str, dict] | None = None,
    flows: dict[str, dict] | None = None,
    earnings_dates: dict[str, str] | None = None,
    today: datetime.date | None = None,
    shorts: dict[str, dict] | None = None,
) -> list[SignalResult]:
    """universe: [{ticker, name}], prices: ticker -> 종가 리스트(오래된→최신), fundamentals: ticker -> metrics.
    sentiment: ticker -> {score[-1,1], reasons} (KB 정성 팩터), flows: ticker -> {intensity,...} (수급 팩터, KR).
    earnings_dates: ticker -> 'YYYY-MM-DD' 실적발표 예정일(US) — 임박 시 신규 매수 게이트.
    shorts: ticker -> {short_ratio,...} (공매도 팩터, KR)."""
    config = config or SignalConfig()
    fundamentals = fundamentals or {}
    sentiment = sentiment or {}
    flows = flows or {}
    earnings_dates = earnings_dates or {}
    today = today or datetime.date.today()
    shorts = shorts or {}
    val_scores = val.scores(universe, fundamentals)
    # 시장 20일 수익 중위값 — 하락추세 게이트를 '종목 고유'와 '시장 전체'로 구분하는 기준선
    market_ret_20d = market_return_pct(prices, 20)
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
        # `_norm`·`_weight`를 **버린다는 것을 이름으로** 드러낸다 — 예전엔 `qual_norm`·`qual_weight`
        # 로 받아 놓고 아래에서 쓰지 않아, 읽는 사람이 combine에 들어간다고 착각했다.
        _unused_norm, _unused_weight, qual_reasons, qual_score, has_qualitative = qual.component(
            sentiment.get(ticker), config.weight_qualitative
        )
        del _unused_norm, _unused_weight
        flow_norm, flow_weight, flow_reasons, flow_intensity, has_flow = flow_mod.component(
            flows.get(ticker), config.weight_flow
        )
        ql_norm, ql_weight, ql_reasons, ql_points, has_quality = fscore.component(
            fundamentals.get(ticker), config.weight_quality
        )
        mom_norm, mom_weight, mom_reasons, mom_ret, has_momentum = mom.score_at(
            closes, len(closes) - 1, config
        )
        sh_norm, sh_weight, sh_reasons, sh_ratio, has_short = short_mod.component(
            shorts.get(ticker), config.weight_short
        )

        # 팩터별 방향·강도(정규화 [-1,1]) — 종합점수 구성에 실제 들어간 값. 프론트 팩터 시각화(레이더/
        # 막대)용. 하락추세 게이트가 아래에서 저평가·낙폭 매수기여를 무효화하기 '전' 원 강도를 담아
        # 두 종목의 진짜 팩터 상태를 보여준다(게이트로 보류된 사정은 reasons/whyHold가 별도 설명).
        factor_scores: dict[str, float] = {"technical": round(max(-1.0, min(1.0, tech_score / 3.0)), 3)}
        if fund.has_data:
            factor_scores["fundamental"] = round(max(-1.0, min(1.0, fund.score / 2.0)), 3)
        if has_valuation:
            factor_scores["valuation"] = round(max(-1.0, min(1.0, val_norm)), 3)
        if has_reversion:
            factor_scores["reversion"] = round(max(-1.0, min(1.0, rev_norm)), 3)
        if has_flow:
            factor_scores["flow"] = round(max(-1.0, min(1.0, flow_norm)), 3)
        if has_quality:
            factor_scores["quality"] = round(max(-1.0, min(1.0, ql_norm)), 3)
        if has_momentum:
            factor_scores["momentum"] = round(max(-1.0, min(1.0, mom_norm)), 3)
        if has_short:
            factor_scores["short"] = round(max(-1.0, min(1.0, sh_norm)), 3)

        # 확인된 하락추세(떨어지는 칼)에서는 낙폭과대·저평가 매수기여를 무효화한다 — 싸고
        # 과매도여도 구조적 하락이면 계속 싸지고 떨어지는 가치함정. 종합 BUY도 아래서 관망 강등.
        i_last = len(closes) - 1
        if _downtrend_blocking(closes, series, i_last, config, market_ret_20d):
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
            (ql_norm, ql_weight, ql_reasons),        # 퀄리티(축약 F-Score) — 재무 건전성·개선
            (mom_norm, mom_weight, mom_reasons),     # 중기 모멘텀(12-1개월)
            (sh_norm, sh_weight, sh_reasons),        # 공매도 거래비중(KR) — 하방 리스크 감점
        ]
        combined = combine(components, config)
        # 커버리지는 `has_*` 플래그를 아는 여기서 센다 — `combine`은 컴포넌트 이름을 모른다.
        cov = data_coverage({"technical": True, "fundamental": fund.has_data,
                            "valuation": has_valuation, "reversion": has_reversion,
                            "flow": has_flow, "quality": has_quality,
                            "momentum": has_momentum, "short": has_short}, config)
        _apply_trend_gate(combined, closes, series, i_last, config, market_ret_20d)
        edate = earnings_dates.get(ticker)
        earnings_soon = _apply_earnings_gate(combined, _days_until(edate, today), config)
        _apply_crash_gate(combined, closes, i_last, config)
        entry = sentiment.get(ticker) or {}
        dec = _decision_from_entry(entry)
        _apply_event_veto(combined, dec)
        result = SignalResult(
            ticker=ticker, name=name, score=combined["score"], kind=combined["kind"],
            confidence=combined["confidence"], technical_score=round(tech_score, 2),
            fundamental_score=round(fund.score, 2), has_fundamental=fund.has_data,
            valuation_percentile=val_pct, has_valuation=has_valuation,
            reversion_score=round(rev_score_raw, 2), has_reversion=has_reversion,
            qualitative_score=qual_score, has_qualitative=has_qualitative,
            flow_intensity=round(flow_intensity, 3) if has_flow else None, has_flow=has_flow,
            quality_points=ql_points, has_quality=has_quality,
            momentum_ret=mom_ret, has_momentum=has_momentum,
            short_ratio=round(sh_ratio, 4) if sh_ratio is not None else None, has_short=has_short,
            event_risk=dec.buy_blocked, event_note=dec.summary,
            event_severity=dec.severity or "",
            decision=dec,
            earnings_date=edate, earnings_soon=earnings_soon,
            gate_blocked=bool(combined.get("gated")),
            gates=list(combined.get("gates") or []),
            gates_relaxed=list(combined.get("gate_relaxed") or []),
            weight_sum_ratio=combined.get("weight_sum_ratio"),
            data_coverage=cov["ratio"],
            missing_factors=cov["missing"],
            reasons=combined["reasons"],
            factor_scores=factor_scores,
        )
        results.append(result)

    results.sort(key=lambda r: r.score, reverse=True)
    apply_cross_sectional(results, config)   # 매수권 = 횡단면 분위(rank 모드일 때만 개입)
    for r in results:
        r.narrative = narr.explain(r)        # 승격·강등이 반영된 kind 기준으로 설명 생성
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
    mom_norm, mom_weight, mom_reasons, _, _ = mom.score_at(closes[: i + 1], i, config)  # 모멘텀도 가격기반 → 백테스트 반영
    return [
        (tech_score / 3.0, config.weight_technical, tech_reasons),
        (rev_norm, rev_weight, rev_reasons),
        (mom_norm, mom_weight, mom_reasons),
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
        _apply_crash_gate(combined, closes, i, config)
        kinds.append(combined["kind"])
    return kinds


def chart_scores_and_zones(
    dates: list[str], closes: list[float], config: SignalConfig | None = None,
    stored: dict[str, dict] | None = None,
) -> tuple[list[float | None], list[dict]]:
    """차트용 점수 시계열 + BUY/SELL 구간을 **한 패스**로 계산(지표·combine 중복 제거).
    실측 스냅샷 우선, 없으면 가격기반 재현. 구간은 kind·출처가 같을 때만 병합."""
    config = config or SignalConfig()
    stored = stored or {}
    series = compute_indicator_series(closes, config)
    kinds, sources, reasons_at, scores = [], [], [], []
    for k in range(len(closes)):
        st = stored.get(dates[k])
        if st:  # 실측 우선
            kinds.append(st["kind"])
            sources.append("actual")
            reasons_at.append(["실측 — 당일 저장된 시그널(전 팩터)"])
            try:
                scores.append(float(st["score"]) if st.get("score") is not None else None)
            except (TypeError, ValueError):
                scores.append(None)
            continue
        combined = combine(_price_only_components(closes, series, k, config), config)
        _apply_trend_gate(combined, closes, series, k, config)
        _apply_crash_gate(combined, closes, k, config)
        kinds.append(combined["kind"])
        sources.append("replay")
        reasons_at.append(combined["reasons"])
        scores.append(combined.get("score"))
    zones = []
    i, n = 0, len(kinds)
    while i < n:
        if kinds[i] == "HOLD":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == kinds[i] and sources[j] == sources[i]:
            j += 1
        zones.append({"start": dates[i], "end": dates[j - 1], "kind": kinds[i],
                      "reasons": reasons_at[i], "score": scores[i], "actual": sources[i] == "actual"})
        i = j
    return scores, zones


def daily_signal_scores(
    dates: list[str], closes: list[float], config: SignalConfig | None = None,
    stored: dict[str, dict] | None = None,
) -> list[float | None]:
    """날짜별 종합점수 시계열(차트 참고용). chart_scores_and_zones 래퍼."""
    scores, _ = chart_scores_and_zones(dates, closes, config=config, stored=stored)
    return scores


def signal_zones(
    dates: list[str], closes: list[float], config: SignalConfig | None = None,
    stored: dict[str, dict] | None = None,
) -> list[dict]:
    """연속된 동일 시그널(BUY/SELL) 구간 — chart_scores_and_zones 래퍼."""
    _, zones = chart_scores_and_zones(dates, closes, config=config, stored=stored)
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
            _apply_crash_gate(combined, window, i, config)
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
