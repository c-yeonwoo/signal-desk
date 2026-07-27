"""횡단면 분위 선정 + 상대 추세 게이트.

2026-07-26 진단: PIT 스냅샷 2,000건(200종목 × 10거래일)의 최고점수가 1.91인데 유효 매수문턱이
2.0~2.4여서 매수 시그널이 1건이었다. 문턱이 점수 분포 밖에 있으면 매수는 판단이 아니라 산수로
0이 된다. 그래서 매수권을 '절대 문턱'에서 '같은 시장 안의 상대 순위'로 바꿨다.
매도는 절대 기준 그대로 — 청산은 억제하지 않는다는 기존 원칙 유지.
"""

from signal_desk.signals import engine
from signal_desk.signals.engine import SignalConfig, SignalResult


def _sig(ticker, score, **over):
    base = dict(ticker=ticker, name=ticker, score=score, kind="HOLD", confidence=0.5,
                technical_score=0.0, fundamental_score=0.0, has_fundamental=False)
    base.update(over)
    return SignalResult(**base)


def _ranked(scores, **cfg_over):
    cfg = SignalConfig(**{"selection_mode": "rank", **cfg_over})
    rs = sorted([_sig(f"T{i}", s) for i, s in enumerate(scores)],
                key=lambda r: r.score, reverse=True)
    return engine.apply_cross_sectional(rs, cfg), cfg


def test_rank_slots_never_zero():
    """유니버스가 작으면 반올림으로 0종목이 되어 '상대적으로 가장 좋은 종목'조차 못 고른다."""
    assert engine.rank_slots(200, 3.0) == 6
    assert engine.rank_slots(10, 3.0) == 1
    assert engine.rank_slots(1, 3.0) == 1
    assert engine.rank_slots(0, 3.0) == 0


def test_buys_relative_best_when_everything_below_absolute_threshold():
    """관측된 실제 분포(최고 1.9 · 중위 0.3)에서 절대문턱 1.2+는 후보를 거의 못 만든다."""
    scores = [1.9, 1.7, 1.4, 1.4, 1.3, 1.1] + [0.3] * 194
    results, cfg = _ranked(scores)
    eligible = [r for r in results if r.rank_eligible]
    assert len(eligible) == 6                      # 200종목 × 3%
    assert all(engine.is_buy(r.kind) for r in eligible)
    assert eligible[0].rank == 1 and eligible[0].kind == "STRONG_BUY"
    assert "상대 순위" in " ".join(eligible[-1].reasons)


def test_absolute_mode_can_produce_zero_candidates():
    """비교군 — 같은 분포를 절대문턱 2.0으로 보면 매수권이 0이다(지금까지의 동작)."""
    scores = [1.9, 1.7, 1.4] + [0.3] * 197
    rs = sorted([_sig(f"T{i}", s, kind=engine.classify(s, SignalConfig(buy_threshold=2.0)))
                 for i, s in enumerate(scores)], key=lambda r: r.score, reverse=True)
    cfg = SignalConfig(selection_mode="absolute", buy_threshold=2.0)
    engine.apply_cross_sectional(rs, cfg)          # absolute 모드에선 개입하지 않음
    assert [r for r in rs if r.rank_eligible] == []
    summary = engine.selection_summary(rs, cfg)
    assert summary["eligible"] == 0 and summary["threshold_above_max"] is True
    assert summary["distribution"]["max"] == 1.9


def test_min_score_floor_blocks_least_bad_in_crash():
    """폭락장에서 전 종목이 음수면 상위 3%여도 사지 않는다 — '최악 중 최선' 방지."""
    results, _ = _ranked([-0.2, -0.4, -0.6] + [-1.0] * 97)
    assert [r for r in results if r.rank_eligible] == []


def test_gated_ticker_does_not_consume_a_slot():
    """추세·실적 게이트로 막힌 종목은 매수권이 아니고, 그 자리를 비워두지도 않는다 —
    다음 순위가 올라온다. veto 몇 건이 조용히 후보 수를 깎으면 원래 버그와 같은 결과가 된다."""
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=50.0)   # 2종목 → 1자리
    rs = [_sig("A", 2.0, gate_blocked=True), _sig("B", 1.5)]
    engine.apply_cross_sectional(rs, cfg)
    assert rs[0].rank_eligible is False and rs[0].kind == "HOLD"
    assert rs[1].rank_eligible is True and engine.is_buy(rs[1].kind)


def test_event_veto_still_wins_and_next_rank_fills_in():
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=50.0)
    rs = [_sig("A", 2.0, event_risk=True), _sig("B", 1.5)]
    engine.apply_cross_sectional(rs, cfg)
    assert rs[0].rank_eligible is False and rs[1].rank_eligible is True


def test_absolute_buy_outside_rank_is_demoted():
    """모드는 하나만 유효해야 한다 — 절대문턱은 넘었지만 상대 순위 밖이면 매수권 아님."""
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=10.0)
    rs = sorted([_sig(f"T{i}", 2.0 - i * 0.01, kind="BUY") for i in range(20)],
                key=lambda r: r.score, reverse=True)
    engine.apply_cross_sectional(rs, cfg)
    assert engine.is_buy(rs[0].kind) and rs[0].rank == 1
    assert rs[-1].kind == "HOLD" and "매수권" in rs[-1].reasons[-1]


def test_absolute_buy_still_gets_strong_slot_by_rank():
    """절대 classify가 이미 BUY여도 매수권 앞자리는 우선매수여야 한다.

    버그 재현(US 스크린샷): 게이트로 상위가 빠지면 ≥1.2 종목은 BUY로 남고,
    그 아래 <1.2만 STRONG_BUY로 승격돼 점수↔라벨이 뒤집혔다."""
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=3.0, rank_min_score=0.0)
    named = [
        ("ACGL", 1.41, True), ("FOX", 1.32, False), ("FOXA", 1.31, False),
        ("SOLV", 1.26, False), ("UHS", 1.22, True), ("EG", 1.20, True),
        ("HIG", 1.12, False), ("F", 1.04, False),
    ]
    pad = [(f"P{i}", 0.50 - i * 0.001, False) for i in range(492)]
    rs = []
    for t, s, g in named + pad:
        rs.append(_sig(t, s, kind=engine.classify(s, cfg), gate_blocked=g))
    rs = sorted(rs, key=lambda r: r.score, reverse=True)
    engine.apply_cross_sectional(rs, cfg)
    by = {r.ticker: r for r in rs}
    assert by["ACGL"].kind == "HOLD" and by["ACGL"].rank_eligible is False
    # 게이트로 빠진 자리를 채운 상위 적격 5자리(k=15의 앞 1/3) = 우선매수
    assert by["FOX"].kind == "STRONG_BUY" and by["FOX"].rank_eligible
    assert by["FOXA"].kind == "STRONG_BUY"
    assert by["SOLV"].kind == "STRONG_BUY"
    assert by["HIG"].kind == "STRONG_BUY"   # 절대 점수 <1.2여도 적격 4번째 → 우선
    assert by["F"].kind == "STRONG_BUY"
    # 뒤집힘 금지: 매수권 안에서 더 낮은 점수가 더 강한 kind면 안 된다
    # (고치기 전엔 FOX=BUY · HIG=STRONG_BUY 로 여기가 실패했다)
    elig = [r for r in rs if r.rank_eligible]
    strength = {"STRONG_BUY": 2, "BUY": 1}
    for a, b in zip(elig, elig[1:]):
        assert strength[a.kind] >= strength[b.kind], (a.ticker, a.kind, b.ticker, b.kind)
    assert by["FOX"].kind == "STRONG_BUY" and by["HIG"].kind == "STRONG_BUY"


def test_sell_side_untouched_by_rank():
    """매도는 절대 기준 그대로 — 하락장에서 청산을 억제하지 않는다."""
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=50.0)
    rs = [_sig("A", 1.5), _sig("B", -1.5, kind="SELL")]
    engine.apply_cross_sectional(rs, cfg)
    assert rs[1].kind == "SELL" and rs[1].rank_eligible is False


# ── 상대 추세 게이트 ──────────────────────────────────────────────────────
def _falling(n=140, daily=-0.002):
    """완만한 하락 시계열 — 종가<MA20<MA60(역배열)이 성립한다."""
    px, p = [], 100.0
    for _ in range(n):
        px.append(p)
        p *= (1 + daily)
    return px


def test_downtrend_gate_applies_when_stock_is_weaker_than_market():
    closes = _falling()
    cfg = SignalConfig()
    series = engine.compute_indicator_series(closes, cfg)
    i = len(closes) - 1
    own = engine.ret_pct_n(closes, i, 20)
    assert engine._downtrend_confirmed(closes, series, i, cfg) is True
    # 시장은 이 종목보다 덜 빠졌다 → 종목 고유 약세 → 게이트 적용
    assert engine._downtrend_blocking(closes, series, i, cfg, own + 1.0) is True


def test_downtrend_gate_relaxed_when_relatively_strong():
    """시장이 −10%인데 이 종목이 −3%면 역배열은 종목의 결함이 아니라 시장 상태다.
    상대 예외가 없으면 조정장에 200종목이 거의 다 역배열이어서 게이트가 전면 차단 스위치가 된다."""
    closes = _falling()
    cfg = SignalConfig()
    series = engine.compute_indicator_series(closes, cfg)
    i = len(closes) - 1
    own = engine.ret_pct_n(closes, i, 20)
    assert engine._downtrend_blocking(closes, series, i, cfg, own - 1.0) is False
    combined = {"kind": "BUY", "reasons": [], "score": 1.5, "confidence": 0.5}
    engine._apply_trend_gate(combined, closes, series, i, cfg, own - 1.0)
    assert combined["kind"] == "BUY" and "상대강도 우위" in combined["reasons"][-1]


def test_gate_unchanged_when_market_context_missing():
    """백테스트·리플레이 경로(시장 맥락 없음)는 기존 절대 판정을 그대로 쓴다."""
    closes = _falling()
    cfg = SignalConfig()
    series = engine.compute_indicator_series(closes, cfg)
    i = len(closes) - 1
    assert engine._downtrend_blocking(closes, series, i, cfg, None) is True
    # 시장이 오르는 중이면 역배열은 종목 고유 정보 → 게이트 유지
    assert engine._downtrend_blocking(closes, series, i, cfg, 5.0) is True


def test_evaluate_produces_buy_candidates_in_a_falling_market():
    """엔드투엔드 — 전 종목이 하락 중이어도 상대적으로 나은 종목은 매수권에 들어온다.
    (예전 동작: 추세 게이트가 전 종목을 HOLD로 강등 → 매수권 0)"""
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(20)]
    # T0이 가장 덜 빠지고, 뒤로 갈수록 더 빠진다
    prices = {f"T{i}": _falling(daily=-0.001 - i * 0.0005) for i in range(20)}
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=10.0)
    results = engine.evaluate(universe, prices, config=cfg)
    picks = [r for r in results if r.rank_eligible]
    assert picks, "하락장에서도 상대 상위는 매수권에 있어야 한다"
    assert picks[0].ticker == "T0"                    # 가장 덜 빠진 종목
    assert all(engine.is_buy(r.kind) for r in picks)
    s = engine.selection_summary(results, cfg)
    assert s["mode"] == "rank" and s["eligible"] == len(picks) == s["rank_slots"]


def test_market_return_is_median_not_mean():
    """한 종목의 급등이 시장 기준선을 끌어올리면 게이트가 통째로 풀린다 → 중위값을 쓴다."""
    prices = {"A": [100.0] * 20 + [100.0], "B": [100.0] * 20 + [90.0],
              "C": [100.0] * 20 + [400.0]}
    assert engine.market_return_pct(prices, 20) == 0.0
    assert engine.market_return_pct({}, 20) is None
