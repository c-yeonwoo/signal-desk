"""트레이딩 성향 프리셋 — 안정형/균형형/공격형이 봇 파라미터와 리스크 룰을 함께 정한다.

'보유종목 남발 없이 안정적이면서 적당한 수익'을 성향으로 조절한다:
- 안정형: 넓게 분산(종목 많이·비중 작게)·엄격한 매수 기준·타이트한 손절/익절
- 균형형: 기존 기본값
- 공격형: 소수 집중(종목 적게·비중 크게)·완화된 매수 기준·넓은 손절/큰 익절

리밸런싱(B)의 목표 종목수·비중 기준으로도 재사용한다.
"""

from __future__ import annotations

from signal_desk.signals import risk

STYLES = ("conservative", "balanced", "aggressive")
STYLE_LABEL = {"conservative": "안정형", "balanced": "균형형", "aggressive": "공격형"}
STYLE_DESC = {
    "conservative": "넓게 분산 · 엄격한 매수 · 타이트한 손절(변동성↓)",
    "balanced": "분산과 집중의 균형 · 표준 손익 규칙",
    "aggressive": "소수 집중 · 적극 매수 · 넓은 손절/큰 익절(변동성↑)",
}

# entry_tranches: 목표비중을 몇 회로 나눠 분할매수할지(라오어 분할매수 응용 — 진입 타이밍 리스크 분산)
# harvest_take_profit_pct: 횡보·약세 국면에서 '중간 실현'용 타이트 익절(추세 국면엔 위 take_profit + 트레일링 유지)
#
# ## 성향별 `rank_top_pct` 를 없앴다 (2026-08-07)
#
# 성향마다 매수권을 좁히던 값(안정 1.0 · 균형 2.0 · 공격 3.0)이 **`max_positions` 를 죽이고
# 설계 의도를 뒤집고 있었다.** 200종목 기준 실제 후보 자리:
#
#     안정형  창 2자리  (설계 의도: 12종목 넓게 분산)   ← 가장 집중
#     균형형  창 4자리
#     공격형  창 6자리  (설계 의도: 6종목 소수 집중)    ← 가장 분산
#
# `rank_top_pct` 가 먼저 막으므로 `max_positions`(12/10/6)는 한 번도 발동하지 않았다.
# 이제 **세 성향이 같은 후보(엔진 분위 = 창 6자리)를 보고 `position_pct` 로 갈린다**:
#
#     안정형  6자리 × 6%  = 투입 36% (현금 64%)
#     균형형  6자리 × 8%  = 투입 48%
#     공격형  6자리 × 14% = 투입 84%
#
# `안정형 = 변동성↓` 과 방향이 맞고, 화면의 매수권과 봇의 후보가 **하나의 정의**를 쓴다.
# min_buy_score는 절대문턱 모드에서만 쓰인다 — 분위 모드에서 절대값 1.9를 요구하면
# 관측 최고점수 1.91과 겹쳐 다시 매수 0건이 된다.
#
# **미검증 변경이다**: 판별력이 `판정 불가`인 동안 넣은 것이므로 `unproven` 으로 이력에 남는다.
PRESETS = {
    "conservative": {"max_positions": 12, "position_pct": 0.06, "min_buy_score": 1.9, "max_new_buys_per_run": 2,
                     "stop_loss_pct": -0.05, "take_profit_pct": 0.10, "trailing_from_peak_pct": -0.04,
                     "entry_tranches": 4, "harvest_take_profit_pct": 0.06},
    "balanced": {"max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.6, "max_new_buys_per_run": 2,
                 "stop_loss_pct": -0.07, "take_profit_pct": 0.15, "trailing_from_peak_pct": -0.05,
                 "entry_tranches": 3, "harvest_take_profit_pct": 0.09},
    "aggressive": {"max_positions": 6, "position_pct": 0.14, "min_buy_score": 1.3, "max_new_buys_per_run": 3,
                   "stop_loss_pct": -0.10, "take_profit_pct": 0.25, "trailing_from_peak_pct": -0.07,
                   "entry_tranches": 2, "harvest_take_profit_pct": 0.12},
}

# 추세 국면(여기선 익절을 넓게 두고 트레일링으로 수익 극대화). 그 외(횡보·약세·조정)는 중간 실현.
TRENDING_REGIMES = ("강세", "과열")

# 컨빅션 로테이션 — 약한 보유를 더 강한 후보로 교체. 기준·행동강령을 투자 성향별로 나눈다.
#   min_gap: (후보 최고점수 − 보유 최저점수) 격차가 이 이상일 때만 교체
#   min_hold_days: 최소 보유일(이전엔 교체 대상 제외 — 잦은 교체 방지)
#   max_loss_pct: 이보다 크게 손실 중인 보유는 교체 제외(손절선에 맡김 — 손실 확정 회피)
#   cooldown_days: 방금 판 종목 재매수 금지(핑퐁 방지)
#   max_per_run: 한 사이클 최대 교체 건수
#   only_cooled: True면 'BUY→HOLD로 식은' 보유만 청산 후보(아직 BUY면 순위 낮아도 유지)
#   when_slots_free: True면 자리가 남아도 현금이 부족할 때 약한 보유를 정리해 매수 자금을 마련(선제 교체)
ROTATION_PRESETS = {
    # 안정형: 웬만하면 유지. 식은(HOLD) 것만, 확실히 우월할 때만, 자리 꽉 찼을 때만.
    "conservative": {"min_gap": 1.2, "min_hold_days": 7, "max_loss_pct": -0.02,
                     "cooldown_days": 7, "max_per_run": 1, "only_cooled": True, "when_slots_free": False},
    # 균형형: 식은 것 우선이되 하위 BUY도 격차 크면 교체. 자리 꽉 찼을 때만.
    "balanced":     {"min_gap": 1.0, "min_hold_days": 5, "max_loss_pct": -0.03,
                     "cooldown_days": 5, "max_per_run": 1, "only_cooled": False, "when_slots_free": False},
    # 공격형: 자본을 계속 최강 후보로. 격차 작아도, 자주, 자리 없거나 현금 부족해도 선제 교체.
    "aggressive":   {"min_gap": 0.6, "min_hold_days": 3, "max_loss_pct": -0.05,
                     "cooldown_days": 3, "max_per_run": 2, "only_cooled": False, "when_slots_free": True},
}


def rotation_params(style: str = "balanced") -> dict:
    """투자 성향별 컨빅션 로테이션 기준·행동강령."""
    return dict(ROTATION_PRESETS[normalize(style)])


def entry_tranches(style: str) -> int:
    return int(preset(style)["entry_tranches"])


def rank_top_pct(style: str, engine_top_pct: float) -> float:
    """매수권 분위 — **엔진 값 하나만 쓴다.** 성향은 후보를 좁히지 않고 비중으로 갈린다.

    2026-08-07 이전에는 성향별로 더 좁혔는데, 그게 `max_positions` 를 죽이고 설계 의도를
    뒤집었다(안정형 창 2자리 = 가장 집중). 매수권 정의가 하나면 화면의 '매수권'과 봇의
    후보가 갈라질 수 없다 — 갈라짐은 이 리포가 네 번 밟은 병이다.

    `style` 은 시그니처 호환을 위해 남긴다(호출처가 넘긴다) — 값은 쓰지 않는다.
    """
    return float(engine_top_pct)


def normalize(style: str) -> str:
    return style if style in PRESETS else "balanced"


def preset(style: str) -> dict:
    return PRESETS[normalize(style)]


def bot_params(style: str) -> dict:
    """봇 매수·보유 파라미터(bot_config 숫자 컬럼에 적용)."""
    p = preset(style)
    return {k: p[k] for k in ("max_positions", "position_pct", "min_buy_score", "max_new_buys_per_run")}


def risk_config(style: str, regime: str | None = None) -> risk.RiskConfig:
    """성향별 손절/익절/트레일링 룰. 횡보·약세 국면(비추세)이면 '중간 실현'용 타이트 익절 적용
    (라오어 응용) — 추세 국면(강세·과열)에선 넓은 익절 + 트레일링으로 수익을 끝까지."""
    p = preset(style)
    tp = p["take_profit_pct"]
    if regime is not None and regime not in TRENDING_REGIMES:
        tp = p["harvest_take_profit_pct"]  # 횡보/약세 → 빨리 실현
    return risk.RiskConfig(stop_loss_pct=p["stop_loss_pct"], take_profit_pct=tp,
                           trailing_from_peak_pct=p["trailing_from_peak_pct"])


# ─────────────────────────────────────────────────────────────────────────────
# 미검증 변경 기록 (2026-08-07)
#
# 이 파일의 프리셋 변경은 **소스 편집**이라 관리자 UI의 판정 게이트(`prereg.change_allowed`)를
# 지나지 않는다. 그 게이트가 경고한 우회로가 바로 이것이다 — "순수하게 잠그면 소스를 직접
# 편집하는 우회로가 생기고 그 변경은 이력에 남지 않는다"(H1이 그랬다).
#
# 그래서 부팅 시 **한 번** 설정 이력에 남긴다. 관리자 화면의 미검증 배너가 이걸 읽는다.
# 매 부팅 중복 기록하지 않도록 kv 가드를 둔다 — 배너가 같은 항목으로 도배되면 안 읽힌다.
_UNPROVEN_KEY = "strategy_unproven:style-breadth-2026-08-07"


def record_unproven_change() -> bool:
    """성향별 매수권 좁히기 제거를 설정 이력에 1회 기록. 이미 있으면 False."""
    from signal_desk import db, signalcfg
    if db.kv_get(_UNPROVEN_KEY):
        return False
    signalcfg.append_history({
        "ts": __import__("time").time(),
        "source": "strategy.py (소스 편집 · 관리자 UI 아님)",
        "unproven": True,
        "reason": ("성향별 rank_top_pct(1.0/2.0/3.0) 제거 — 그 값이 max_positions(12/10/6)를 "
                   "죽이고 설계 의도를 뒤집었다(안정형 창 2자리 = 가장 집중, 공격형 6자리 = "
                   "가장 분산). 이제 엔진 분위 하나가 매수권을 정하고 성향은 position_pct로 "
                   "갈린다(투입 36%/48%/84%)."),
        "before": {"conservative.rank_top_pct": 1.0, "balanced.rank_top_pct": 2.0,
                   "aggressive.rank_top_pct": 3.0},
        "after": {"rank_top_pct": "엔진 값 하나만 사용(성향별 좁히기 없음)"},
        # 판별력이 `판정 불가`인 동안 넣은 변경이라는 사실을 함께 남긴다.
        "verdict_at_change": "판정 불가(실효 표본 미달) — 측정으로 정당화된 변경이 아니다",
    })
    db.kv_set(_UNPROVEN_KEY, {"recorded": True})
    return True
