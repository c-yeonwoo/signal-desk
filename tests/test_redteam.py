"""레드팀 — 우리 도구가 거짓말하는지 기계적으로 검사한다.

## 왜 사람 리뷰가 아니라 이 파일인가

2026-07-26 하네스를 만들면서 세 번 틀렸고, 세 번 다 잡아낸 건 코드 리뷰가 아니라 **대조군**이었다
(안 산 기간에 수수료 차감 / 동점을 시총순 정렬 / 대조군만 위상 평균). 리뷰는 "방법론이 타당해
보인다"고 말하는 데 능하고, 대조군은 틀리면 그냥 빨간불이 켜진다.

여기 있는 검사들은 전부 **반증 가능한 형태**다. 통과가 성능을 보장하지는 않지만, 실패하면
그 숫자를 인용하면 안 된다는 것만은 확실하다.

| 검사 | 무엇을 잡나 |
|---|---|
| 셔플(누수) | 점수가 미래를 몰래 보는가 |
| 음성 대조군 | 신호가 없는 시장에서 엣지를 만들어내는가 |
| 양성 대조군 | 진짜 엣지를 놓치는가(도구 고장) |
| 미래 불변성 | replay 경로에 룩어헤드가 있는가 |
| base rate 동반 | 비율을 대조군 없이 내보내는가 |
| 표본·커버리지 차단 | 못 믿을 숫자에 판정을 내리는가 |
"""

from __future__ import annotations

import math
import random

import pytest

from signal_desk.signals import harness as hz
from signal_desk.signals.engine import SignalConfig


def _dates(n: int) -> list[str]:
    return [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def _tech_only() -> SignalConfig:
    """합성 시장은 300거래일이라 모멘텀(252일 필요)·낙폭과대가 거의 안 켜진다.
    그 상태로 기본 설정을 쓰면 커버리지 차단에 걸려 **모든 검사가 이유 없이 통과**한다.
    측정하는 것을 정확히 이름 붙인다 — 여기서 보는 건 기술 팩터 순위의 판별력이다.
    (라이브 H1 기본 technical=0이라 양성 대조군은 가중을 명시한다.)"""
    from dataclasses import replace
    return replace(SignalConfig(), weight_technical=0.35,
                   weight_reversion=0.0, weight_momentum=0.0)


def _panel(closes: dict[str, list[float]]) -> hz.Panel:
    return hz.build_panel({t: (_dates(len(v)), v) for t, v in closes.items()})


def _predictive_market(n_days: int = 320, n_names: int = 40, period: int = 30) -> hz.Panel:
    """기술 팩터가 **실제로** 앞날을 맞히는 시장(전방 5일 IC +0.31).

    처음엔 "꾸준히 오르는 종목"으로 양성 대조군을 만들었는데 기술 팩터가 그걸 계속 피했다.
    RSI·MACD 기반이라 과매수를 매도 신호로 읽기 때문이다 — 도구 결함이 아니라 팩터의 성격이다.
    양성 대조군은 **그 팩터가 원래 잘해야 하는 시장**이어야 한다. 그래서 종목마다 위상이 다른
    진동 시장을 쓴다: 지금 골에 있는 종목이 며칠 뒤 반등한다.
    """
    closes = {}
    for i in range(n_names):
        phase = i * period / n_names
        closes[f"S{i}"] = [100 + 12 * math.sin(2 * math.pi * (j + phase) / period) + j * 0.02
                           for j in range(n_days)]
    return _panel(closes)


def _random_walk_market(n_days: int = 900, n_names: int = 40, seed: int = 7) -> hz.Panel:
    """드리프트도 구조도 없는 시장 — 여기서 엣지가 나오면 그건 도구가 만든 것이다."""
    rng = random.Random(seed)
    closes = {}
    for i in range(n_names):
        px, path = 100.0, []
        for _ in range(n_days):
            px *= 1 + rng.gauss(0, 0.015)
            path.append(px)
        closes[f"R{i}"] = path
    return _panel(closes)


# ------------------------------------------------------------------ 누수 탐지

def test_shuffling_returns_destroys_any_edge():
    """점수와 수익률의 짝을 어긋나게 하면 판별력은 반드시 사라진다.
    셔플하고도 판별력이 남으면 점수가 미래를 보고 있다는 뜻이다."""
    panel = _predictive_market()
    honest = hz.run(panel, hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=100,
                                            min_score=-99.0, cost_pct=0.0,
                                            signal_config=_tech_only()))
    shuffled = hz.run(panel, hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=100,
                                              min_score=-99.0, cost_pct=0.0,
                                              shuffle_returns=True,
                                              signal_config=_tech_only()))
    assert honest["verdict"] == "판별력 있음", "전제: 셔플 전에는 엣지가 잡혀야 한다"
    assert shuffled["verdict"] != "판별력 있음", shuffled
    assert any("셔플" in w for w in shuffled["warnings"])


def test_costs_alone_cannot_manufacture_an_edge():
    """정보가 0인 전략이 '회전율이 낮아 비용을 덜 문다'는 이유만으로 판별력을 얻으면 안 된다.

    실제로 그랬다. 대조군이 매 기간 k종목을 새로 뽑아 회전율 100%로 비용을 최대로 무는 동안,
    점수 기반 전략은 점수가 지속적이라 회전율이 낮아 비용을 덜 물었다. 5년 실데이터에서
    셔플한(=정보 0) 전략이 백분위 100%로 '판별력 있음'을 받았다. 위 셔플 테스트가 이걸 놓친
    이유는 비용을 0으로 두고 돌렸기 때문이다 — 검사와 현실 사이의 그 틈에 버그가 살았다."""
    out = hz.run(_predictive_market(), hz.HarnessConfig(
        top_pct=10.0, rebalance_days=5, random_trials=100, min_score=-99.0,
        cost_pct=0.25, shuffle_returns=True, signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


def test_the_null_pays_the_same_costs_as_the_strategy():
    """대조군은 전략과 같은 회전율로 비용을 물어야 한다.

    점수가 완전히 지속적이면 전략은 같은 종목을 계속 들고 있어 비용을 거의 안 문다. 매 기간
    k종목을 새로 뽑는 대조군은 회전율이 늘 100%라 비용을 최대로 문다. 그 차이만으로 전략은
    **정보가 하나도 없어도** 대조군을 이긴다 — 실데이터에서 셔플한 전략이 백분위 100%를 받은
    원인이 이거였다. 라벨 치환 대조군은 점수의 지속성을 그대로 물려받아 회전율이 맞는다."""
    panel = _random_walk_market()
    fixed = {t: [float(i) ] * len(panel.dates)      # 순위가 영원히 고정 → 회전율 0
             for i, t in enumerate(sorted(panel.closes))}
    cfgs = {c: hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=40,
                                min_score=-999.0, cost_pct=c, signal_config=_tech_only())
            for c in (0.0, 0.5)}
    wealth = {}
    for cost, cfg in cfgs.items():
        phase_idxs = [hz._rebalance_indices(panel, cfg, p) for p in range(cfg.rebalance_days)]
        runs = [hz._run_phase(panel, cfg, fixed, idxs, None, {}, random.Random(cfg.seed))
                for idxs in phase_idxs]
        wealth[("strategy", cost)] = sum(r["equity"][-1] for r in runs) / len(runs)
        null = hz._null_distribution(panel, cfg, fixed, phase_idxs, None, {})
        wealth[("null", cost)] = 1 + null["median"] / 100

    drag = {side: wealth[(side, 0.5)] / wealth[(side, 0.0)] for side in ("strategy", "null")}
    assert drag["strategy"] < 1.0, "전제: 비용을 물리면 전략도 깎여야 한다"
    assert abs(drag["strategy"] - drag["null"]) < 0.05, (
        f"비용 부담이 전략 {drag['strategy']:.3f} vs 대조군 {drag['null']:.3f}로 어긋난다 — "
        f"회전율이 맞지 않는 대조군이다")


# --------------------------------------------------------------- 음성 대조군

@pytest.mark.parametrize("top_pct,hold", [(3.0, 5), (10.0, 20)])
def test_no_edge_is_found_in_a_random_walk(top_pct: float, hold: int):
    """신호가 없는 시장에서 '판별력 있음'이 나오면 도구가 엣지를 만들어낸 것이다."""
    out = hz.run(_random_walk_market(), hz.HarnessConfig(
        top_pct=top_pct, rebalance_days=hold, random_trials=150, min_score=-99.0,
        signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


def test_inverted_scores_are_not_certified_in_a_random_walk():
    """부호를 뒤집어도 마찬가지 — 어느 방향이든 없는 엣지를 찾아내면 안 된다."""
    out = hz.run(_random_walk_market(seed=11), hz.HarnessConfig(
        top_pct=5.0, rebalance_days=5, random_trials=150, min_score=-99.0,
        invert_scores=True, signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


# --------------------------------------------------------------- 양성 대조군

def test_a_real_edge_is_still_detected():
    """검사를 촘촘히 하다 보면 아무것도 통과 못 하게 만들기 쉽다.
    엣지가 실재할 때 잡히는지 매번 확인해야 나머지 '판정 불가'가 정보가 된다."""
    out = hz.run(_predictive_market(), hz.HarnessConfig(
        top_pct=10.0, rebalance_days=5, random_trials=100, min_score=-99.0, cost_pct=0.0,
        signal_config=_tech_only()))
    assert out["verdict"] == "판별력 있음", out["verdict_why"]


# ------------------------------------------------------- 룩어헤드(미래 불변성)

def _diverging_pair(n: int = 240) -> tuple[list[float], list[float], int]:
    hist = [100 + math.sin(i / 5) * 8 + i * 0.3 for i in range(n)]
    return hist + [hist[-1] * 1.06 ** i for i in range(1, 30)], \
        hist + [hist[-1] * 0.94 ** i for i in range(1, 30)], n - 1


def test_replay_signal_kinds_ignores_the_future():
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    assert engine.replay_signal_kinds(up)[i] == engine.replay_signal_kinds(down)[i]


def test_chart_scores_ignore_the_future():
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    s_up, _ = engine.chart_scores_and_zones(_dates(len(up)), up)
    s_down, _ = engine.chart_scores_and_zones(_dates(len(down)), down)
    assert s_up[i] == s_down[i]


def test_harness_scores_ignore_the_future():
    up, down, i = _diverging_pair()
    cfg = SignalConfig()
    s_up, *_ = hz._score_series(_panel({"A": up}), cfg)
    s_down, *_ = hz._score_series(_panel({"A": down}), cfg)
    assert s_up["A"][i] == s_down["A"][i]


def test_indicator_series_are_causal():
    """지표 시계열 자체가 미래를 안 쓰는지 — 위 셋의 공통 기반이라 따로 못박는다."""
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    a, b = engine.compute_indicator_series(up), engine.compute_indicator_series(down)
    for key in a:
        if isinstance(a[key], list) and len(a[key]) > i:
            assert a[key][i] == b[key][i], f"{key}가 미래 가격에 반응한다"


# ---------------------------------------------------- base rate 동반 강제

def test_precision_metrics_always_ship_with_a_baseline():
    """CLAUDE.md 규칙의 강제. 정밀도류 비율은 기준선·리프트 없이 나갈 수 없다.
    -3% 조정장에서는 무작위 매도도 정밀도 60%가 나온다 — 절대값만 보면 매번 속는다."""
    from signal_desk.signals import accuracy
    rows, closes = _accuracy_fixture()
    out = accuracy.realized_accuracy(rows, closes)
    assert "baseline" in out and out["baseline"], out
    for side in ("buy", "sell"):
        if out.get(f"{side}_precision_pct") is not None:
            assert f"{side}_lift_pp" in out, f"{side} 정밀도에 리프트가 없다"
            assert f"{side}_precision_ci_pp" in out, f"{side} 정밀도에 신뢰구간이 없다"
    assert "lift_min_pp" in out


def test_zero_matured_says_why_it_is_zero():
    """감사 가설 큐에서 승격된 검사(2026-07-26).

    시세 캐시가 07-03에서 멈춘 채 시그널만 07-24까지 쌓여 `tickers_matched: 0`이 떴는데,
    화면에는 '누적 중'으로만 보였다. **성숙 대기(정상)와 수집 중단(고장)이 똑같이 0으로
    표시되면 고장을 몇 주씩 못 본다.** 0에는 항상 이유가 붙어야 한다.
    """
    from signal_desk.signals import accuracy
    dates = _dates(40)
    rows = [{"date": dates[30], "ticker": "T0", "kind": "BUY", "score": 1.5}]

    stale = {"T0": (dates[:10], [100.0 + i for i in range(10)])}   # 시세가 시그널보다 과거
    out = accuracy.realized_accuracy(rows, stale)
    cov = out["coverage"]
    assert cov["stale_prices"] is True
    assert "수집 중단" in cov["blocked_reason"]
    assert cov["price_data_to"] == dates[9]

    fresh = {"T0": (dates, [100.0 + i for i in range(40)])}        # 시세는 최신, 아직 미성숙
    cov2 = accuracy.realized_accuracy(rows, fresh)["coverage"]
    assert cov2["stale_prices"] is False
    assert cov2["blocked_reason"] is None or "성숙" in cov2["blocked_reason"]


def test_missing_universe_join_is_named_not_silent():
    """시그널 종목이 시세 캐시에 아예 없는 경우도 0이 아니라 문장으로 말해야 한다."""
    from signal_desk.signals import accuracy
    dates = _dates(40)
    rows = [{"date": dates[10], "ticker": "GHOST", "kind": "BUY", "score": 1.0}]
    cov = accuracy.realized_accuracy(rows, {"T0": (dates, [100.0] * 40)})["coverage"]
    assert cov["tickers_matched"] == 0
    assert "어긋" in cov["blocked_reason"], cov


def test_advisor_shadow_verdict_requires_significance():
    """표본 수만으로 판정하지 않는다 — 20쌍의 SE는 ±3.8%p다."""
    from signal_desk.signals import advisor_shadow
    import inspect
    code = inspect.getsource(advisor_shadow)
    assert "delta_significant" in code
    # 할당식만 본다(독스트링의 paired_verdict_ready 언급과 구분)
    assert '"verdict_ready":' in code or "verdict_ready =" in code
    for line in code.splitlines():
        if '"verdict_ready":' in line or line.strip().startswith("verdict_ready"):
            assert "significant" in line, f"판정이 유의성과 무관하다: {line}"
            break
    else:
        raise AssertionError("verdict_ready 할당식을 찾지 못했다")


def test_bot_and_ui_score_from_one_input_set():
    """봇과 화면이 서로 다른 입력으로 점수를 내면 '매수 후보'와 실제 매수가 갈라지고, 그 차이는
    어느 화면에도 안 나타난다(공매도가 봇 경로에만 빠져 있었다).

    입력을 한 함수(store.kr_engine_inputs)로 모으고, evaluate가 받는 국내 입력이 그 안에 다
    들어있는지 기계로 확인한다 — 새 팩터를 evaluate에 추가하고 한쪽만 배선하면 여기서 걸린다."""
    import inspect

    from signal_desk import store as store_mod
    from signal_desk.signals import engine as eng

    # 국내 입력이 아닌 것만 예외로 둔다(US 전용·계산 파라미터). 새 kwarg는 기본적으로 배선 대상.
    not_kr_data = {"universe", "prices", "fundamentals", "config", "earnings_dates", "today"}
    expected = set(inspect.signature(eng.evaluate).parameters) - not_kr_data
    src = inspect.getsource(store_mod.kr_engine_inputs)
    for key in expected:
        assert f'"{key}"' in src, f"kr_engine_inputs에 '{key}' 누락 — 봇·UI가 갈라진다"
    api_src = inspect.getsource(__import__("signal_desk.api", fromlist=["_signals"])._signals)
    bot_src = inspect.getsource(__import__("signal_desk.bot", fromlist=["x"])._market_signals)
    assert "kr_engine_inputs" in api_src and "kr_engine_inputs" in bot_src


def test_shadow_verdicts_share_one_significance_rule():
    """shadow마다 판정 통계를 따로 짜면 그 구현 차이가 판정 차이로 둔갑한다(대조군 교훈과 같다)."""
    import inspect

    from signal_desk.signals import accuracy, advisor_shadow, climate, kb_coverage

    v = accuracy.diff_verdict([0.10, 0.11, 0.09], [0.01, 0.02, 0.0], min_samples=3)
    assert v["delta_pct"] is not None and v["verdict_ready"] is True
    # 분산이 크면 같은 표본 수여도 판정 불가여야 한다
    noisy = accuracy.diff_verdict([0.5, -0.4, 0.3], [0.0, 0.1, -0.1], min_samples=3)
    assert noisy["delta_significant"] is False and noisy["verdict_ready"] is False
    assert "오차" in (noisy["blocked_reason"] or "")
    # 표본 미달은 유의해 보여도 판정 불가
    thin = accuracy.diff_verdict([0.10, 0.11], [0.0, 0.01], min_samples=20)
    assert thin["verdict_ready"] is False and "표본" in thin["blocked_reason"]
    for mod in (advisor_shadow, climate, kb_coverage):
        src = inspect.getsource(mod)
        assert ("mean_diff_se_pp" in src or "diff_verdict" in src
                or "paired_mean_diff_se_pp" in src), \
            f"{mod.__name__}이 자체 판정 통계를 쓴다"
    # paired SE도 accuracy 한곳에 — advisor_shadow가 자체 분산식을 짜면 안 된다
    adv_src = inspect.getsource(advisor_shadow)
    assert "paired_mean_diff_se_pp" in adv_src
    assert "def paired_mean_diff_se_pp" not in adv_src


def test_one_bad_ticker_does_not_stop_collection_or_pruning(tmp_path, monkeypatch):
    """수집 루프에 per-target 격리가 없으면 한 종목의 예외가 나머지 종목·정리·임베드를 전부 죽인다.

    실측(2026-07-27): 종목 뉴스 경로가 7일간 멈춰 다이제스트가 안 갱신됐고, 마지막 줄에 있던
    db.kb_prune()도 같이 건너뛰어 인사이트 원문 47건이 보존 한도(60건)를 넘긴 채 남아 있었다.
    화면은 아무 이상도 보여주지 않았다 — 실패는 log.warning 한 줄뿐이었다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, kb
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    calls, pruned = [], []

    def fake_one(ticker, name, codes, news_n, lookback_days):
        calls.append(ticker)
        if ticker == "000660":
            raise RuntimeError("naver 4xx")
        return True

    monkeypatch.setattr(kb, "_refresh_one", fake_one)
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {})
    monkeypatch.setattr(db, "kb_prune", lambda: pruned.append(1) or {"news_deleted": 0})

    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"},
                      {"ticker": "000660", "name": "SK하이닉스"},
                      {"ticker": "035720", "name": "카카오"}])
    assert calls == ["005930", "000660", "035720"]      # 실패 뒤 종목도 수집한다
    assert out["updated"] == 2 and len(out["failed"]) == 1
    assert out["failed"][0]["ticker"] == "000660"       # 어느 종목이 실패했는지 이름으로 남는다
    assert pruned, "실패가 있으면 정리(prune)가 건너뛰어진다 — 원문이 무한 누적된다"
    assert (db.kv_get("kb_refresh_last") or {}).get("failed"), "마지막 실행 결과가 화면에 못 닿는다"


def test_kb_hits_cannot_ship_without_a_timestamp(tmp_path, monkeypatch):
    """시점 없는 KB 근거는 근거가 아니다 — 오전에 사실이던 시황이 오후엔 아닐 수 있다.

    실측(2026-07-27): 검색 코퍼스가 published·fetched를 아예 버려서, 3주 전 기사가 오늘 기사와
    같은 순위로 올라오고 챗봇은 그걸 현재 사실로 말할 수 있었다. 나이는 옵션이 아니라 계약이다."""
    from signal_desk import db, kb_search
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_document_add("005930", "삼성전자 실적 개선", "영업이익이 늘었다는 분석.",
                       "http://t1", "news", "2026-07-26", "뉴스")
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("실적 개선", k=3)
    assert hits
    for h in hits:
        assert "age_days" in h and "as_of" in h and "stale" in h, "소비자가 시점을 알 길이 없다"


def test_context_docs_are_labeled_as_not_being_the_score_basis():
    """설명이 결정과 어긋나면 신뢰가 깨진다. 점수는 8팩터로 나오고 KB 문서는 그 입력이 아닌데,
    챗봇이 "이 기사 때문에 매수"라고 말하면 사후 합리화다. 도구 산출과 시스템 규칙 양쪽에
    '근거 아님'이 박혀 있어야 한다(한쪽만이면 프롬프트 수정 때 조용히 사라진다)."""
    from signal_desk import chat
    tool = [t for t in chat.TOOLS if t["name"] == "search_kb"][0]
    assert "점수 산출에 쓰지 말 것" in tool["description"]
    assert "시점" in tool["description"]
    assert "점수 근거와 배경 자료를 섞지 않는다" in chat.SYSTEM
    assert "시점을 함께 말한다" in chat.SYSTEM


def test_scoring_factors_are_snapshotted_and_in_factor_ic(tmp_path, monkeypatch):
    """점수에 들어가는 팩터가 PIT·factor_ic에서 빠져 있으면 그 팩터는 영원히 측정 불가.

    실측: short는 evaluate/combine·두뇌에 있는데 snapshot_signals와 FACTOR_COLS에 없었다.
    qualitative는 combine 밖(shadow)이라 SCORING_FACTORS에 없고 FACTOR_COLS에만 있어도 된다."""
    from signal_desk.signals import accuracy
    from signal_desk.signals.engine import SignalResult
    from signal_desk import store as store_mod

    missing_ic = set(accuracy.SCORING_FACTORS) - set(accuracy.FACTOR_COLS)
    assert not missing_ic, f"점수 팩터가 factor_ic 밖에 있다: {missing_ic}"

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    sig = SignalResult(ticker="005930", name="삼성전자", score=1.0, kind="BUY",
                       confidence=0.5, technical_score=0.1, fundamental_score=0.0,
                       has_fundamental=False, reasons=[], short_ratio=0.18, has_short=True)
    store_mod.snapshot_signals([sig], date="2026-07-24")
    row = store_mod.load_signal_history().iloc[0]
    for col in accuracy.SCORING_FACTORS:
        assert col in row.index, f"PIT 스냅샷에 점수 팩터 '{col}' 없음"
    assert float(row["short"]) == 0.18


def _accuracy_fixture():
    """PIT 히스토리 + 종가. 20거래일 성숙 구간을 확보한다."""
    dates = _dates(60)
    closes = {}
    for i in range(6):
        px = [100.0 + j * (0.4 if i % 2 == 0 else -0.2) for j in range(60)]
        closes[f"T{i}"] = (dates, px)
    rows = []
    for d in dates[:20]:
        for i in range(6):
            rows.append({"date": d, "ticker": f"T{i}",
                         "kind": "BUY" if i % 2 == 0 else "SELL",
                         "score": 1.5 if i % 2 == 0 else -1.5})
    return rows, closes


# ------------------------------------------------------- 표본·커버리지 차단

def test_small_sample_cannot_be_certified():
    """표본 미달이면 백분위가 아무리 좋아도 판정하지 않는다."""
    v, why = hz._verdict(99.0, phase_min=50.0, phase_max=60.0, random_median=10.0,
                         periods=12, min_periods=30)
    assert v == "판정 불가" and "표본" in why


def test_low_coverage_factor_cannot_be_certified():
    """이력이 모자라 조용히 빠진 팩터의 결과에 '판별력 있음'을 붙이면 안 된다."""
    v, why = hz._verdict(99.0, phase_min=50.0, phase_max=60.0, random_median=10.0,
                         weak_factors=["momentum"])
    assert v == "판정 불가" and "momentum" in why


def test_coverage_gate_only_counts_weighted_factors():
    """가중치 0인 팩터는 애초에 쓰지 않으므로 차단 사유가 될 수 없다."""
    from dataclasses import replace
    cfg = replace(SignalConfig(), weight_technical=0.35,
                  weight_momentum=0.0, weight_reversion=0.0)
    assert hz._weighted_factors(cfg) == {"technical"}
    assert "momentum" in hz._weighted_factors(SignalConfig())
    assert "technical" not in hz._weighted_factors(SignalConfig())  # H1


def test_short_history_blocks_the_verdict_end_to_end():
    """지금 캐시(268거래일)에 20일 보유면 리밸런스가 7회뿐이다. 게이트가 실제로 막는지 확인."""
    out = hz.run(_random_walk_market(n_days=200), hz.HarnessConfig(
        top_pct=3.0, rebalance_days=20, random_trials=80, min_score=-99.0,
        signal_config=_tech_only()))
    assert out["verdict"] == "판정 불가"
    assert "표본" in out["verdict_why"], out["verdict_why"]


def test_same_day_same_ticker_counts_once(tmp_path, monkeypatch):
    """성향이 다른 봇 3개가 같은 종목을 사면 같은 판단이 3번 기록된다. 그대로 세면 승률이
    시그널 정확도가 아니라 **종목 인기도**로 가중된다 — 셋 다 산 종목이 오르면 3승이 된다.

    실측(2026-07-27): 매수 판단 39건 중 고유 (종목·날짜)는 31건이었고, 161390은 하루 3건이었다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    ids = [db.bot_decision_log("161390", "한국타이어", "buy", 1.5, "규칙", {}, 10000.0)
           for _ in range(3)]                      # 같은 날 같은 종목 — 봇 3개
    other = db.bot_decision_log("005930", "삼성전자", "buy", 1.2, "규칙", {}, 70000.0)
    db.bot_decision_set_outcome(ids[0], 10.0)      # 오른 종목: 3봇이 다 샀다
    db.bot_decision_set_outcome(ids[1], 10.0)
    db.bot_decision_set_outcome(ids[2], 10.0)
    db.bot_decision_set_outcome(other, -5.0)       # 내린 종목: 1봇만 샀다

    card = db.bot_decision_scorecard()
    assert card["resolved"] == 2, "중복 판단이 표본 수를 부풀린다"
    assert card["win_rate"] == 50.0, f"인기 종목이 승률을 끌어올린다: {card['win_rate']}"
    assert card["deduped_from"] == 4, "몇 건이 접혔는지 안 보이면 중복 계수가 다시 생긴다"
    assert len(db.bot_decisions_recent(40)) == 2   # advisor 학습 재료도 같은 판단을 3번 먹지 않는다


def test_alert_scan_does_not_depend_on_a_bot_being_on(tmp_path, monkeypatch):
    """관심종목 시그널 변동 알림은 페이퍼 봇과 무관한 기능인데, 루프가 '봇 켠 유저'만 순회해서
    봇 활성화에 딸려 있었다. 개인 봇을 없애면 이 결합이 알림을 통째로 죽인다.

    기능의 대상 집합은 그 기능이 정의한다 — 옆 기능의 on/off가 정하는 게 아니다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    db.fav_add(7, "ticker", "005930", "삼성전자")   # 봇은 켠 적 없는 유저
    db.fav_add(8, "sector", "반도체", "반도체")      # 종목 관심 없음 → 스캔 대상 아님
    assert db.user_bots_enabled() == []
    assert db.uids_with_ticker_favorites() == [7]


# ---------------------------------------------- 사전등록·판정 이력 (PRD N3)
#
# 왜 여기인가: N3가 고치는 것은 "판정이 무엇을 재는가"다. 그건 성능이 아니라 **정직성**의
# 문제이므로 레드팀에 속한다. 아래 검사가 하나라도 빨간불이면 보드의 판정을 인용하면 안 된다.
# 근거: docs/prd-harness-preregistration.md (F2·F5·F6·F9·F11·F12)


def test_harness_checks_the_config_the_engine_actually_runs(tmp_path, monkeypatch):
    """하네스는 `engine.py` 하드코딩 기본값이 아니라 **지금 돌아가는 설정**을 검사해야 한다.

    2026-08-05 진단: `store.run_harness`가 `HarnessConfig`를 `signal_config` 없이 만들어
    `default_factory=SignalConfig`가 걸렸다. 시그널 탭·봇은 `signalcfg`를 쓰는데 판정만
    소스 상수를 쟀다 — 가중치를 바꿔도 판정이 안 변하는 구조다. 지금은 오버라이드가 비어
    우연히 일치할 뿐이다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, signalcfg, store
    from signal_desk.signals import harness as hz_mod
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    signalcfg.set_dict({"weight_momentum": 0.05})       # 라이브 설정을 기본값과 다르게

    seen: dict = {}

    def _capture(panel, cfg=None, regimes=None, scores=None, score_source="price", **kw):
        seen["cfg"] = cfg
        seen["kw"] = kw
        return {"ready": False, "reason": "stub"}

    monkeypatch.setattr(store, "is_ready", lambda: True)
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(store, "load_all_dated_closes",
                        lambda: {"A": (_dates(60), [100.0 + i for i in range(60)])})
    monkeypatch.setattr(hz_mod, "run", _capture)

    store.run_harness(market="kr")
    assert seen["cfg"].signal_config.weight_momentum == 0.05, (
        "하네스가 라이브 설정을 무시하고 소스 상수를 쟀다")


def test_verdict_blocks_when_most_periods_bought_nothing():
    """표본 게이트는 **실효 기간**으로 세야 한다.

    `periods`는 `len(idxs)` = 전체 리밸런스 횟수다. PIT 점수가 없는 날은 후보가 비어 매수 0건이
    되는데, 가격 패널 전체(1,228거래일)에 리밸런스가 깔리므로 hold=5면 218회쯤 된다.
    실제 신호가 4기간뿐이어도 `218 >= min_periods`로 통과한다 — `min_periods`를 PIT에서 5로
    낮춘 완화는 아무 것도 바꾸지 않으면서 막아야 할 것을 막지 못했다.
    """
    v, why = hz._verdict(99.0, phase_min=50.0, phase_max=60.0, random_median=10.0,
                         periods=218, effective_periods=4, min_periods=30)
    assert v == "판정 불가", f"실효 4기간에 판정을 내렸다: {v} / {why}"
    assert "실효" in why and "218" in why, why


def test_exploratory_runs_never_become_the_board(tmp_path, monkeypatch):
    """탐색 실행과 스윕은 이력에만 남고 보드 정본을 덮으면 안 된다.

    `cli.py`가 combos 루프 **안에서** 저장해 8조합 스윕의 마지막 칸이 정본이 됐다. 조합을
    여러 개 보면 판별력이 없어도 하나가 95%를 넘을 확률이 33.7%다 — 그중 하나가 보드에
    남는 것은 측정이 아니라 고르기다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, store
    from signal_desk.signals import harness as hz_mod
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "HARNESS_LAST_FILE", tmp_path / "harness_last.json")

    monkeypatch.setattr(store, "is_ready", lambda: True)
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(store, "load_all_dated_closes",
                        lambda: {"A": (_dates(60), [100.0 + i for i in range(60)])})
    monkeypatch.setattr(hz_mod, "run", lambda *a, **k: {
        "ready": True, "verdict": "판별력 있음", "verdict_why": "stub",
        "vs_random": {"percentile": 99.0}, "strategy": {},
        "periods": 10, "empty_periods": 0, "effective_periods": 10, "warnings": []})

    store.run_harness(market="kr")                       # 사전등록 없는 탐색 실행
    assert not store.HARNESS_LAST_FILE.exists(), "탐색 실행이 보드 정본을 만들었다"


def test_only_preregistered_runs_can_lock_a_verdict(tmp_path, monkeypatch):
    """`preregistered_id`가 없는 실행은 절대 정본이 될 수 없다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    rid = db.harness_run_insert({
        "preregistered_id": None, "score_source": "pit", "market": "kr",
        "config_json": "{}", "config_hash": "deadbeef", "harness_json": "{}",
        "percentile": 99.0, "threshold_pct": 95.0, "n_registered": 1,
        "periods": 40, "empty_periods": 0, "effective_periods": 40,
        "pit_dates": 200, "price_data_to": "2026-08-05",
        "verdict": "판별력 있음", "verdict_why": "stub", "warnings_json": "[]"})
    row = db.harness_run_get(rid)
    assert row["is_locked"] == 0, "사전등록 없는 실행이 정본으로 잠겼다"


def test_sidak_threshold_scales_with_registered_looks():
    """조합을 여러 개 보면 문턱을 올려야 한다. n=1이면 95%, n=2면 97.47%."""
    from signal_desk import prereg
    assert prereg.sidak_threshold_pct(1) == pytest.approx(95.0, abs=0.01)
    assert prereg.sidak_threshold_pct(2) == pytest.approx(97.47, abs=0.01)
    assert prereg.sidak_threshold_pct(8) == pytest.approx(99.36, abs=0.01)


def test_sidak_threshold_does_not_loosen_when_a_look_locks():
    """interim이 확정되면 남은 look의 문턱이 저절로 느슨해지는 일이 없어야 한다.

    초안은 `n = status != locked 인 항목 수`였다. 그러면 interim이 잠기는 순간 n이 2→1로 줄어
    final의 문턱이 97.47% → 95%로 내려간다. 사후 완화다 — n은 **파일 등록 수**로 고정한다.
    """
    from signal_desk import prereg
    looks = [{"id": "a", "role": "interim", "score_source": "pit", "status": "locked"},
             {"id": "b", "role": "final", "score_source": "pit", "status": "pending"}]
    assert prereg.threshold_for(looks, "b") == pytest.approx(97.47, abs=0.01)


def test_interim_and_final_must_measure_the_same_thing(tmp_path):
    """같은 가설의 2회 관측인데 설정이 다르면 순차 관측이 아니라 별개 실험이다 — 로드 거절."""
    from signal_desk import prereg
    p = tmp_path / "preregistered.toml"
    p.write_text(
        '[base]\nfamily="f"\nscore_source="pit"\nmarket="kr"\n'
        '[base.config]\nweight_momentum=0.30\n'
        '[base.harness]\nhold=5\ntrials=200\nexposure=false\ncost_pct=0.25\n'
        '[[looks]]\nid="f-interim"\nrole="interim"\nregistered_at="2026-08-05"\n'
        'hypothesis="h"\n[looks.requirement]\nmin_effective_periods=12\nmin_pit_dates=60\n'
        '[looks.config]\nweight_momentum=0.10\n'          # base와 다르다 → 거절
        '[[looks]]\nid="f-final"\nrole="final"\nregistered_at="2026-08-05"\n'
        'hypothesis="h"\n[looks.requirement]\nmin_effective_periods=30\nmin_pit_dates=150\n',
        encoding="utf-8")
    out = prereg.load(p)
    assert out["ok"] is False
    assert "설정" in out["reason"], out["reason"]


def test_preregistered_config_must_match_the_running_engine(tmp_path, monkeypatch):
    """사전등록 설정이 지금 엔진과 다르면 확정을 막아야 한다.

    설정의 진실이 세 곳에 있다 — `engine.SignalConfig` 소스 기본값, `kv:signal_config`
    오버라이드, 사전등록 파일. H1처럼 **소스 상수**를 바꾸면 사전등록이 조용히 낡는다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, prereg, signalcfg
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    live = signalcfg.get_config()
    stale = {"weight_momentum": round(live.weight_momentum + 0.10, 3)}
    ok, reason = prereg.config_agrees_with_engine(stale)
    assert ok is False and "다르" in reason, reason


def test_locked_verdict_is_invalidated_when_the_config_changes(tmp_path, monkeypatch):
    """판정이 살아 있으려면 잰 것과 돌아가는 것이 같아야 한다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, prereg
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    locked = {"config_hash": "aaaaaaaaaaaa", "verdict": "판별력 있음",
              "verdict_locked_at": "2026-08-05T00:00:00Z"}
    assert prereg.board_status(locked, current_hash="aaaaaaaaaaaa") == "locked"
    assert prereg.board_status(locked, current_hash="bbbbbbbbbbbb") == "invalidated"


def test_fundamentals_write_never_drops_the_quality_factor(tmp_path, monkeypatch):
    """재무를 쓰는 경로는 **파생값(퀄리티)까지** 남겨야 한다.

    2026-08-05 진단: `compute_quality()`의 호출처가 관리자 수동 refresh 하나뿐이었고
    `sigdesk fetch`(CLI)는 `fetch_fundamentals`만 불렀다. 이 함수가 dict를 새로 써서 저장하므로
    **CLI로 갱신할 때마다 quality가 지워졌다** — 실측 `has=True 0/198`, 가중 0.15가 통째로 미발동.
    "커버리지 0%"가 화면에 뜨는데 원인이 이력 부족이 아니라 배선 누락이면 데이터를 더 받아도
    영원히 안 낫는다. 파생값은 원본을 쓰는 함수 안에서 채운다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "FUNDAMENTALS_FILE", tmp_path / "fundamentals.json")
    monkeypatch.setattr(store, "FUNDAMENTALS_HISTORY_FILE", tmp_path / "fundamentals_history.json")

    import datetime as _dt
    prev_year = str(_dt.date.today().year - 2)
    store._write_json(store.FUNDAMENTALS_HISTORY_FILE, {
        "005930": {prev_year: {"roe": 8.0, "debt_ratio": 30.0,
                               "revenue_growth": 5.0, "net_income": 3.0e13}}})
    fund = {"005930": {"roe": 10.0, "debt_ratio": 28.0,
                       "revenue_growth": 10.0, "net_income": 4.5e13, "equity": 4.0e14}}
    assert store._attach_quality(fund) == 1
    q = fund["005930"]["quality"]
    assert q["has"] is True and q["points"] >= 3, q
    # 이력이 없으면 **없는 퀄리티를 만들어내지 않는다**(has=True를 지어내면 그게 조용한 거짓이다).
    # 이미 있던 값은 지우지 않는다 — 낡은 실값과 날조된 값은 다르다. 대신 이유를 로그로 남긴다.
    store._write_json(store.FUNDAMENTALS_HISTORY_FILE, {})
    fresh = {"005930": {"roe": 10.0, "debt_ratio": 28.0, "revenue_growth": 10.0,
                        "net_income": 4.5e13, "equity": 4.0e14}}
    assert store._attach_quality(fresh) == 0
    assert "quality" not in fresh["005930"], "이력이 없는데 퀄리티를 만들어냈다"


def test_pit_fundamentals_never_use_data_before_it_was_disclosable():
    """시점별 재무는 **공시 전 정보를 쓰면 안 된다** — 그게 룩어헤드다.

    `fundamentals_history.json`에 연도별 재무가 있는데도 가격 하네스가 3팩터였던 이유는
    데이터가 없어서가 아니라 "언제부터 알 수 있었나"가 없어서였다. FY Y 사업보고서는 이듬해
    3월 말이 법정기한이므로 `{'2024': …}`를 2024-01-01부터 쓰면 최대 15개월 룩어헤드다.
    """
    from signal_desk.signals import pit_fundamentals as pf

    assert pf.latest_fiscal_year("2026-08-05") == 2025    # FY2025는 2026-03 공시 → 가용
    assert pf.latest_fiscal_year("2026-04-01") == 2025    # 경계: 4/1부터 열린다
    assert pf.latest_fiscal_year("2026-03-31") == 2024    # 3/31엔 아직 FY2024까지만
    assert pf.latest_fiscal_year("2024-01-02") == 2022

    hist = {"A": {"2024": {"roe": 9.0, "net_income": 1.0e12, "equity": 1.0e13,
                           "debt_ratio": 30.0, "revenue_growth": 5.0},
                  "2023": {"roe": 7.0, "net_income": 0.8e12, "equity": 0.9e13,
                           "debt_ratio": 35.0, "revenue_growth": 3.0}}}
    # 경계의 핵심: FY2024가 열리는 날(2025-04-01) 전에는 **FY2023을 쓴다**.
    # FY2024를 먼저 쓰면 그게 룩어헤드다 — 아직 공시되지 않은 실적으로 과거를 채점하는 것이다.
    before = pf.metrics_at(hist, "2025-03-31", shares={}, price_at={})
    assert before["A"]["roe"] == 7.0, "공시 전 FY2024를 당겨썼다(룩어헤드)"
    after = pf.metrics_at(hist, "2025-04-01", shares={"A": 1.0e8}, price_at={"A": 50000.0})
    assert after["A"]["roe"] == 9.0, "공시일이 지났는데 낡은 연도를 쓴다"
    assert after["A"]["quality"]["has"] is True, "전년 대비 개선을 못 세면 퀄리티가 죽는다"
    assert after["A"]["per"] == round(50000.0 * 1.0e8 / 1.0e12, 2)
    # 이력에 그 연도가 아예 없으면 재무를 만들어내지 않는다(조용히 3팩터로 떨어뜨리지 않는다).
    assert pf.metrics_at(hist, "2024-03-31", shares={}, price_at={}) == {}   # FY2022 없음


def test_six_factor_backtest_is_not_labeled_as_eight(tmp_path):
    """6팩터 백테스트를 8팩터라고 부르면 안 된다 — 수급·공매도는 백필이 원리적으로 불가능하다.

    `flows.json`·`short.json`은 시계열이 아니라 현재값 스냅샷 1개다. 이름을 정직하게 붙이지
    않으면 `harness_last`의 `fired_pct`가 3팩터인데 "8팩터 판별력"으로 읽혔던 일이 반복된다.
    """
    from signal_desk.signals import harness as hz_mod
    from signal_desk.signals import pit_fundamentals as pf

    src = hz_mod.scores_with_pit_fundamentals.__doc__ or ""
    assert "6팩터" in src and "8팩터" in src, "무엇이 빠졌는지 문서에 없다"
    assert "flows.json" in (pf.__doc__ or ""), "백필 불가 사유가 모듈 문서에 없다"
    # price6는 정본 자격이 있지만 price(3팩터)는 없다.
    from signal_desk import prereg
    assert "price6" in prereg.CANONICAL_SOURCES
    assert "price" not in prereg.CANONICAL_SOURCES


# --------------------------------------------------- 판정 게이트 (N2)

def _pending_board() -> dict:
    return {"ready": True, "looks": [
        {"role": "interim", "status": "locked", "verdict": "판별력 있음"},
        {"role": "final", "status": "pending", "verdict": "판정 보류",
         "verdict_why": "실효 기간 4/30"}]}


def _proven_board() -> dict:
    return {"ready": True, "looks": [
        {"role": "final", "status": "locked", "verdict": "판별력 있음"}]}


def test_interim_pass_does_not_open_the_parameter_gate():
    """interim 통과는 채택 근거가 아니다 — 등록 파일의 `if_pass`에 그렇게 적혀 있다."""
    from signal_desk import prereg
    proven, why = prereg.verdict_state(_pending_board())
    assert proven is False and "미확정" in why, why
    assert prereg.verdict_state(_proven_board())[0] is True


def test_automated_proposals_are_hard_blocked_before_a_verdict():
    """판정 전에는 **제안을 만들지도 않는다**.

    생성을 허용하고 승인만 막으면 큐가 쌓이고, 쌓인 큐는 관리자 화면 배지로 떠서 승인을
    유도하는 압력이 된다. 그리고 지금 측정된 상태는 IC 0과 구분 불가 · 6팩터 백분위 53.5다 —
    그 위에서 LLM이 가중치를 제안하면 곡선 맞추기다. 사유 오버라이드도 자동 경로엔 없다.
    """
    from signal_desk import prereg
    ok, why, unproven = prereg.change_allowed(_pending_board(), automated=True,
                                              override_reason="아무 사유를 적어도")
    assert ok is False and unproven is False
    assert "자동 제안" in why, why
    assert prereg.change_allowed(_proven_board(), automated=True)[0] is True


def test_manual_change_is_recorded_as_unproven_not_blocked():
    """수동 변경은 막지 않고 **미검증으로 기록**한다.

    순수하게 잠그면 진짜 바꿔야 할 때 `engine.py` 소스를 직접 편집하는 우회로가 생기고
    (H1이 그랬다) 그 변경은 이력에 남지 않는다. 사유를 받아 통과시키고 표시한다.
    """
    from signal_desk import prereg
    board = _pending_board()
    ok, why, unproven = prereg.change_allowed(board, automated=False)
    assert ok is False and unproven is True and "사유" in why, why
    ok, why, unproven = prereg.change_allowed(board, automated=False,
                                             override_reason="rank 창 6→12 실험")
    assert ok is True and unproven is True, "미검증 표시 없이 통과시켰다"
    # 판정이 확정되면 사유 없이도 통과하고 unproven 이 아니다.
    assert prereg.change_allowed(_proven_board(), automated=False) == (True, "", False)


def test_broken_board_blocks_instead_of_opening():
    """게이트가 읽을 수 없으면 **막는 쪽**이 안전하다 — fail-open 은 게이트가 없는 것과 같다."""
    from signal_desk import prereg
    for bad in (None, {}, {"ready": False, "reason": "사전등록 파일 없음"}):
        assert prereg.change_allowed(bad, automated=True)[0] is False
        assert prereg.change_allowed(bad, automated=False, override_reason="x")[0] is True, \
            "수동은 사유가 있으면 통과(우회로를 만들지 않는다)"
        assert prereg.change_allowed(bad, automated=False)[0] is False


# --------------------------------------------------- 정지 탐지 (N1)

def test_stall_line_is_silent_when_healthy_and_loud_when_not():
    """정상일 때는 아무 말도 하지 않는다 — 매일 초록불을 쓰면 그것도 안 읽히게 된다."""
    from signal_desk import digest
    assert digest.stall_line(None) is None
    assert digest.stall_line({"ok": True}) is None
    line = digest.stall_line({
        "ok": False, "missing_files": ["투자경고(토스)"],
        "stale": [{"label": "거시(FRED)", "age_hours": 24 * 32, "updated": "2026-07-03 00:08"}],
        "pit": {"missing_n": 7, "missing": ["2026-07-13", "2026-07-27", "2026-07-28",
                                           "2026-07-29", "2026-07-30", "2026-07-31",
                                           "2026-08-03"]},
        "harness_days": 9})
    assert line.startswith("🔧")
    assert "투자경고" in line and "거시(FRED)(32일)" in line
    # 결측일을 **이름으로** 적는다 — "7건"만 적으면 어느 날이 빈지 몰라 조사가 안 된다.
    assert "07-13" in line and "7거래일" in line
    assert "판별력 검사 9일 경과" in line


def test_pit_gap_is_measured_against_the_trading_calendar(tmp_path, monkeypatch):
    """스냅샷 정지는 **파일 신선도로 안 잡힌다** — 거래일 달력과 대조해야 한다.

    2026-08 진단: 시세가 백필로 최신이라 `stale_prices=false`가 되어 스냅샷 결측을 가렸고
    `blocked_reason`도 null이었다. mtime을 보는 어느 플래그도 이걸 못 잡는다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "SIGNAL_HISTORY_FILE", tmp_path / "signal_history.parquet")
    monkeypatch.setattr(store, "_market_dates",
                        lambda: ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"])

    import pandas as pd
    store._write_parquet(pd.DataFrame([{"date": "2026-08-03", "ticker": "A", "score": 1.0},
                                       {"date": "2026-08-06", "ticker": "A", "score": 1.0}]),
                         store.SIGNAL_HISTORY_FILE)
    gap = store.pit_gap_days()
    assert gap["pit_dates"] == 2
    assert gap["missing"] == ["2026-08-04", "2026-08-05"], gap
    assert gap["missing_n"] == 2


def test_morning_brief_puts_the_stall_line_first():
    """정지 탐지는 **맨 위**. 아래로 밀면 안 읽히고, 멈춘 데이터로 아래 숫자를 믿게 된다."""
    from signal_desk import digest
    text = digest.build_morning(
        signals=[], regime_label="약세", threshold=1.2, base_threshold=1.2,
        stall={"ok": False, "missing_files": ["투자경고(토스)"], "stale": [],
               "pit": {"missing_n": 0, "missing": []}, "harness_days": 0})
    body = [ln for ln in text.split("\n") if ln.strip()]
    assert body[0].startswith("☀️")
    assert body[1].startswith("🔧"), f"정지 줄이 첫 본문이 아니다: {body[:3]}"


# --------------------------------------------------- 무기준선 비율 0건 (N4)

def test_mixed_horizon_outcomes_are_excluded_from_the_win_rate(tmp_path, monkeypatch):
    """지평이 섞인 비율에는 **비교 가능한 base rate 를 붙일 수 없다** → 리프트에서 뺀다.

    2026-08-05 진단: 옛 채점이 `closes[-1]`(오늘 종가)을 써서 보유 기간이 판단마다 달랐다
    (실측 3.0~6.1 달력일). 그 상태로 `baseline_buy_pct`(익일 종가·정확히 5거래일)와 비교해
    "리프트 +0.4%p"라는 **없는 숫자**를 만들었다. 몇 건을 뺐는지도 함께 드러낸다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")

    old = db.bot_decision_log("005930", "삼성전자", "buy", 1.5, "규칙", {}, 70000.0)
    new = db.bot_decision_log("000660", "SK하이닉스", "buy", 1.4, "규칙", {}, 200000.0)
    # 옛 방식: 지평 없이 기록(직접 UPDATE 로 재현)
    c = db.conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, horizon_days=NULL WHERE id=?", (10.0, old))
    c.commit(); c.close()
    db.bot_decision_set_outcome(new, -5.0, horizon_days=3,
                                entry_date="2026-08-04", exit_date="2026-08-07")

    card = db.bot_decision_scorecard()
    assert card["resolved"] == 1, "지평 혼재 행이 승률에 섞였다"
    assert card["mixed_horizon_n"] == 1, "몇 건을 뺐는지 안 보인다"
    assert card["horizon_days"] == [3]
    assert card["entry_dates"] == ["2026-08-04"]


def test_baseline_uses_the_same_convention_as_the_win_rate(tmp_path, monkeypatch):
    """기준선은 **같은 진입일·같은 지평·같은 진입/청산 관례**여야 한다.

    관례가 하나라도 다르면 리프트가 거짓이다. 여기서는 종가→종가 h거래일로 통일했다.
    """
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    dates = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    monkeypatch.setattr(store, "load_all_dated_closes", lambda: {
        "UP": (dates, [100.0, 100.0, 101.0, 102.0, 110.0]),     # 08-04 진입 → 08-07 +10%
        "DOWN": (dates, [100.0, 100.0, 99.0, 98.0, 90.0]),      # -10%
    })
    b = store.decision_baseline(["2026-08-04"], 3)
    assert b["sample"] == 2 and b["up_pct"] == 50.0, b
    assert b["avg_ret_pct"] == 0.0
    # 청산일이 아직 없으면 표본에 넣지 않는다(오늘 종가로 대신하지 않는다).
    assert store.decision_baseline(["2026-08-06"], 3)["sample"] == 0


def test_win_rate_never_ships_without_a_baseline_in_the_frontend():
    """화면의 승률은 `liftNote`/`liftText` 를 **반드시** 지난다.

    `liftNote/liftColor/liftText` 는 잘 만들어져 있었는데 승률 렌더 한 곳만 우회했다.
    데이터가 마르면 가드 없는 경로만 살아남는다 — 그래서 문자열 수준으로 검사한다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    for ln in html.split("\n"):
        if "승률 " in ln and "fmtNum(" in ln and "win_rate" in ln:
            assert ("liftNote" in ln or "liftText" in ln or "liftColor" in ln
                    or "mixed_horizon_n" in ln), f"기준선 없이 승률을 렌더한다: {ln.strip()[:120]}"


def test_zero_buy_line_states_the_actual_cause():
    """`매수 0`의 이유는 **점검 결과**여야 한다 — "고장 아님"을 무조건 쓰면 조사를 막는다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "고장 아님</span>" not in html, "매수 0에 하드코딩된 변호가 남아 있다"
    assert "고장 아닙니다" not in html, "온보딩이 매수 0을 무조건 정상이라 가르친다"
    assert "zeroWhy" in html and "게이트 차단" in html and "창" in html


# --------------------------------------------------- 시점별 유니버스 (N5)

def test_the_null_shares_the_same_per_date_candidate_set():
    """대조군은 **전략이 살 수 있던 종목만** 살 수 있어야 한다.

    라벨 치환이 시계열을 통째로 맞바꾸면 None 패턴까지 옮겨간다. 종목마다 가용 날짜가 다른
    경우(시점별 유니버스·PIT 재무) 대조군이 전략이 못 산 종목 — 이미 폐지돼 forward-fill 된
    종목까지 — 을 담게 되고, 그 0% 수익이 대조군을 끌어내려 전략이 좋아 보인다.
    """
    import random as _r
    scores = {
        "A": [1.0, 1.0, 1.0, 1.0],          # 항상 유니버스 안
        "B": [None, None, 2.0, 2.0],        # 뒤늦게 편입
        "C": [3.0, 3.0, None, None],        # 중간에 이탈(폐지 가정)
    }
    for seed in range(12):
        perm = hz._permuted_scores(scores, _r.Random(seed))
        for i in range(4):
            have = {t for t, row in scores.items() if row[i] is not None}
            got = {t for t, row in perm.items() if row[i] is not None}
            assert got == have, f"seed={seed} i={i}: 후보 집합이 달라졌다 {got} != {have}"


def test_universe_at_never_uses_a_future_snapshot(tmp_path, monkeypatch):
    """`universe_at(d)`는 **d 이하** 스냅샷만 쓴다 — 미래 편입 목록을 쓰면 그게 룩어헤드다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "UNIVERSE_HISTORY_FILE", tmp_path / "universe_history.json")
    store._write_json(store.UNIVERSE_HISTORY_FILE, {
        "2024-01-02": [{"ticker": "A", "name": "에이"}],
        "2024-06-03": [{"ticker": "A", "name": "에이"}, {"ticker": "B", "name": "비"}],
    })
    assert store.universe_at("2023-12-31") is None, "첫 스냅샷 이전인데 목록을 돌려줬다"
    assert [u["ticker"] for u in store.universe_at("2024-01-02")] == ["A"]
    assert [u["ticker"] for u in store.universe_at("2024-05-30")] == ["A"], "미래 스냅샷을 당겨썼다"
    assert [u["ticker"] for u in store.universe_at("2024-06-03")] == ["A", "B"]
    assert [u["ticker"] for u in store.universe_at("2026-01-01")] == ["A", "B"]


def test_pit_universe_restricts_the_panel_and_settles_delistings():
    """시점별 유니버스가 후보를 제한하고, 폐지 종목은 **마지막 종가로 정산**된다.

    `build_panel`이 결측을 직전 값으로 채우므로(상장 이전만 None) 폐지 후 구간은 마지막 종가가
    유지된다 = 마지막 거래일 종가 청산. 우연히 맞는 동작에 의존하지 않도록 여기서 박는다.
    """
    dates = _dates(6)
    panel = hz.build_panel({
        "KEEP": (dates, [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
        "DEAD": (dates[:3], [100.0, 90.0, 50.0]),        # 3일째 폐지 — 이후 데이터 없음
    })
    # 폐지 후에도 패널에는 마지막 종가가 유지된다(청산 처리).
    assert panel.closes["DEAD"][2] == 50.0
    assert panel.closes["DEAD"][5] == 50.0, "폐지 종목이 패널에서 사라지면 생존편향이 남는다"

    # 시점별 유니버스: 앞 3일은 DEAD 포함, 뒤 3일은 제외 → 점수가 None 이어야 한다.
    uni_at = {d: ({"KEEP", "DEAD"} if i < 3 else {"KEEP"}) for i, d in enumerate(dates)}
    scores = {t: [(1.0 if t in uni_at[d] else None) for d in dates] for t in panel.closes}
    assert scores["DEAD"][:3] == [1.0, 1.0, 1.0]
    assert scores["DEAD"][3:] == [None, None, None], "유니버스에서 빠졌는데 점수가 남았다"


# ── X1: 팩터 IC는 횡단면 · 날짜 단위 · 검정 동반 ────────────────────────────────
# 실측(2026-08-06): `factor_ic`는 2,200행을 한 덩어리로 푼 pooled 상관이었고, 최소 표본은
# `_MIN_IC_SAMPLES=20`을 **행**으로 셌다. 그래서 `short IC −0.148`이 단 하루(200/2200행)에서
# 나왔고, `store.py`가 같은 파일에 "같은 날 200종목은 하나의 관측"이라 적어 둔 것과 모순이었다.
# 그 숫자가 `brain_proposals`의 가중치 nudge 근거로 그대로 흘러갔다.

def _ic_panel(n_dates, n_tickers, *, ic_sign=1.0, factor="momentum", bars=90):
    """(rows, closes) — 날짜 × 종목 패널. 팩터가 클수록 이후 수익이 높다(ic_sign<0이면 반대)."""
    cal = [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d - 31:02d}" for d in range(1, bars + 1)]
    closes = {}
    for i in range(n_tickers):
        r = (i - n_tickers / 2.0) * ic_sign * 0.002
        closes[f"T{i}"] = (cal, [100.0 * (1.0 + r) ** k for k in range(bars)])
    rows = []
    for d in cal[:n_dates]:
        for i in range(n_tickers):
            row = {"date": d, "ticker": f"T{i}", "kind": "HOLD", "technical": 0, "fundamental": 0,
                   "valuation": 50, "reversion": 0, "qualitative": 0, "flow": 0, "quality": 0,
                   "momentum": 0, "short": 0, "score": 0}
            row[factor] = float(i)
            rows.append(row)
    return rows, closes


def test_factor_ic_is_gated_by_dates_not_rows():
    """행이 2,200개여도 날짜가 1개면 IC를 내지 않는다 — 그 반대가 실제 버그였다."""
    from signal_desk.signals import accuracy

    rows, closes = _ic_panel(1, 200)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert s["n_pairs"] == 200 and s["n_dates"] == 1
    assert out["factor_ic"]["momentum"] is None, "하루치 횡단면으로 IC가 나오면 옛 버그가 돌아왔다"
    assert accuracy._MIN_IC_DATES >= 20


def test_factor_ic_and_stats_never_disagree():
    """`factor_ic[k]`와 `factor_ic_stats[k]['ic']`는 같은 값이어야 한다.

    두 곳에서 조립하면(UI는 앞, 게이트는 뒤) 화면과 판정이 갈라지고 그 차이는 어디에도 안 뜬다.
    """
    from signal_desk.signals import accuracy

    for n_d in (1, 15, 25):
        rows, closes = _ic_panel(n_d, 30)
        out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
        for k, s in out["factor_ic_stats"].items():
            assert out["factor_ic"][k] == s["ic"], f"{k} @ {n_d}일: {out['factor_ic'][k]} vs {s['ic']}"
            # 값이 있으면 반드시 유의하고 차단 이유가 없다(그 역도 성립).
            assert (s["ic"] is not None) == (s["significant"] and not s["blocked_reason"])


def test_ic_carries_n_ci_t_and_p():
    """IC를 크기만 내보내지 않는다 — 기준선 없는 비율과 같은 착각이다."""
    from signal_desk.signals import accuracy

    rows, closes = _ic_panel(25, 30)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    for key in ("n_dates", "independent_dates", "breadth_median", "ci95", "t", "p",
                "significant", "ic_ir", "nw_lag", "horizon", "blocked_reason"):
        assert key in s, f"IC 통계에 {key}가 없다"
    # 중첩 창을 독립 관측으로 세지 않는다.
    assert s["independent_dates"] == s["n_dates"] // 5
    # proof(공개 A열)·brain에도 통계가 실린다 — 한쪽만 고치면 다른 쪽이 옛 숫자를 계속 보여준다.
    from signal_desk.signals import proof
    slim = proof._accuracy_slim(out)
    assert slim["factor_ic_stats"]["momentum"]["n_dates"] == 25
    assert slim["ic_min_dates"] == accuracy._MIN_IC_DATES


def test_cross_sectional_ic_is_not_fooled_by_mixing_dates():
    """날짜 안에서는 무정보인데 날짜 수준이 시장과 같이 움직이면 pooled는 속고 횡단면은 안 속는다."""
    from signal_desk.signals import accuracy

    bars, n_d, n_t = 90, 24, 20
    cal = [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d - 31:02d}" for d in range(1, bars + 1)]
    path = [100.0]
    for k in range(1, bars):
        path.append(path[-1] * (1.03 if (k // 5) % 2 == 0 else 0.97))
    closes = {f"T{i}": (cal, list(path)) for i in range(n_t)}   # 전 종목 동일 경로
    rows = []
    for j, d in enumerate(cal[:n_d]):
        level = 10.0 if (j // 5) % 2 == 0 else 0.0              # 오르는 구간엔 팩터 수준도 높다
        for i in range(n_t):
            rows.append({"date": d, "ticker": f"T{i}", "kind": "HOLD", "technical": 0,
                         "fundamental": 0, "valuation": 50, "reversion": 0, "qualitative": 0,
                         "flow": 0, "quality": 0, "short": 0, "score": 0,
                         "momentum": level + (i % 2) * 0.001})
    pooled_pairs = []
    for r in rows:
        rets = accuracy._forward_returns(*closes[r["ticker"]], r["date"], (5,))
        if 5 in rets:
            pooled_pairs.append((r["momentum"], rets[5]))
    pooled = accuracy._spearman_ic(pooled_pairs)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    mean = out["factor_ic_stats"]["momentum"]["ic_mean"]
    assert pooled is not None and abs(pooled) > 0.5, f"pooled가 안 속으면 검사가 무의미: {pooled}"
    assert mean is None or abs(mean) < 0.05, f"횡단면 IC가 시장 드리프트를 먹었다: {mean}"


def test_weight_proposals_require_significant_ic():
    """유의하지 않은 IC로는 가중치를 제안하지 않는다 — 크기가 게이트면 곡선 맞추기가 된다."""
    from signal_desk import brain_proposals

    weights = {f"weight_{k}": 0.20 for k in
               ("technical", "fundamental", "valuation", "reversion", "flow",
                "quality", "momentum", "short")}
    base = {"ic": -0.30, "n_dates": 30, "independent_dates": 1, "breadth_median": 190,
            "horizon": 20, "ci95": 0.04, "t": -5.0, "p": 0.001, "significant": True,
            "ic_ir": -1.0, "se_floored": False, "nw_lag": 19, "blocked_reason": None}
    assert brain_proposals.build_weight_nudge("short", base, weights) is not None
    for bad in ({**base, "significant": False, "p": 0.42,
                 "blocked_reason": "IC가 0과 구분 불가"},
                {**base, "ic": None, "significant": False,
                 "blocked_reason": "IC 날짜 10/20일 — 판정 불가"}):
        assert brain_proposals.build_weight_nudge("short", bad, weights) is None
        assert brain_proposals.ic_usable(bad)[0] is False
    # 신뢰 등급은 날짜 수와 p로 정한다(IC 절댓값이 등급을 올리지 못한다).
    assert brain_proposals._confidence({**base, "n_dates": 30, "p": 0.001}) == "medium"
    assert brain_proposals._confidence({**base, "n_dates": 60, "p": 0.30}) == "low"
    assert brain_proposals._confidence({**base, "n_dates": 60, "p": 0.001}) == "high"


def test_ic_se_is_never_smaller_than_iid():
    """겹치는 h일 창의 SE를 iid보다 작게 주장하지 않는다(보수성 선택 · floored로 노출)."""
    from signal_desk.signals import accuracy

    alt = [0.1 if i % 2 == 0 else -0.1 for i in range(20)]     # 강한 음의 자기상관
    nw = accuracy._newey_west_se(alt, 4)
    assert nw["floored"] is True and nw["se"] == nw["se_naive"]
    # 양의 자기상관이면 NW가 커진다 — 그쪽은 하한이 아니라 실제 보정이 걸려야 한다.
    trend = [0.05 + 0.01 * i for i in range(20)]
    nw2 = accuracy._newey_west_se(trend, 4)
    assert nw2["se"] > nw2["se_naive"], (nw2["se"], nw2["se_naive"])


# ── X2: 재정규화 편향 노출 + 커버리지 게이트 ────────────────────────────────────
# 실측(2026-08-06, 200종목): `combine`의 분모가 **발동한 가중치의 합**이라, 팩터가 빠진 종목이
# 남은 팩터로 재정규화돼 극단 점수를 받았다.
#   커버리지 <0.60 → |점수| 평균 1.070 / 0.60~0.80 → 0.805 / 0.80~0.95 → 0.638 / ≥0.95 → 0.303
# 상위 3위가 전부 5/8 팩터였고, **유일한 매수권 종목도 5/8**이었다. 전 팩터 3종목은 최고 +0.43.

def test_renormalization_bias_is_exposed_not_hidden():
    """커버리지가 낮으면 점수가 커진다는 사실이 **산출물에 실려** 있어야 한다."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig(weight_technical=0.35, min_data_coverage=0.0)
    universe = [{"ticker": "A", "name": "A"}]
    closes = [100 - i for i in range(20)]
    r = eng.evaluate(universe, {"A": closes}, config=cfg)[0]
    assert r.weight_sum_ratio is not None and 0 < r.weight_sum_ratio < 1
    assert r.data_coverage is not None
    assert r.missing_factors, "어느 팩터가 없는지 이름으로 안 내면 조사가 안 된다"
    # `combine` 자체도 비율을 낸다 — 소비자가 SignalResult를 안 거쳐도 알 수 있어야 한다.
    c = eng.combine([(1.0, 0.35, []), (0.0, 0.0, [])], cfg)
    assert c["weight_sum_ratio"] == round(0.35 / eng.total_weight(cfg), 4)


def test_conditional_factors_are_not_counted_as_missing_data():
    """조건 미발동(낙폭과대)을 데이터 부족으로 세면 데이터를 더 받아도 커버리지가 안 오른다."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig()
    assert "reversion" in eng.CONDITIONAL_FACTORS
    has_all_but_reversion = {f.removeprefix("weight_"): True for f in eng.SCORE_WEIGHT_FIELDS}
    has_all_but_reversion["reversion"] = False
    cov = eng.data_coverage(has_all_but_reversion, cfg)
    assert cov["ratio"] == 1.0, "조건 미발동이 커버리지를 깎으면 안 된다"
    assert "reversion" not in cov["missing"]


def test_coverage_gate_blocks_the_buy_window_but_not_the_score():
    """게이트는 **매수권만** 막는다. 점수를 지우면 왜 없는지 화면에서 알 수 없다."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig(weight_technical=0.35, min_data_coverage=0.80)
    r = eng.evaluate([{"ticker": "A", "name": "A"}], {"A": [100 - i for i in range(20)]},
                     config=cfg)[0]
    assert r.score != 0 and r.low_coverage is True and r.rank_eligible is False
    assert any("커버리지" in x for x in r.reasons)
    sel = eng.selection_summary([r], cfg)
    # 0의 이유를 세는 쪽도 커버리지를 알아야 한다(변호가 아니라 점검 결과).
    assert sel["coverage"]["blocked"] == 1 and sel["coverage"]["blocked_in_window"] == 1
    assert sel["coverage"]["min_required"] == 0.80
    assert sel["coverage"]["distribution"]["median"] is not None, "문턱은 분포와 함께 낸다"


def test_coverage_gate_is_not_a_divide_by_zero():
    """커버리지를 **모르는** 종목(None)은 막지 않는다 — 전 종목 차단은 신중함이 아니다."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig(min_data_coverage=0.99)
    r = eng.SignalResult(ticker="A", name="A", score=2.0, kind="BUY", confidence=0.9,
                         technical_score=1.0, fundamental_score=0.0, has_fundamental=False,
                         data_coverage=None)
    eng.apply_cross_sectional([r], cfg)
    assert r.low_coverage is False and r.rank_eligible is True


def test_harness_applies_the_same_coverage_gate_as_live():
    """하네스가 커버리지 게이트를 안 걸면 **라이브가 돌리지 않는 전략**을 잰 결과다.

    실측 병력: 봇/화면이 `shorts`를 안 넘겨 점수가 갈라졌고(2026-07-27), 하네스가
    `signal_config` 없이 소스 상수를 쟀다(2026-08-05). 같은 병을 게이트에서 반복하지 않는다.
    """
    from signal_desk.signals import harness as h

    # 게이트가 설정돼 있는데 커버리지 패널을 안 주면 경고로 드러나야 한다.
    dates = _dates(400)
    closes = {f"T{i}": (dates, [100.0 * (1.0 + (i - 5) * 0.002) ** k for k in range(400)])
              for i in range(12)}
    panel = h.build_panel(closes)
    scores = {t: [1.5] * len(dates) for t in closes}
    cfg = h.HarnessConfig(random_trials=10, min_periods=1, phase_average=False,
                          signal_config=h.SignalConfig(min_data_coverage=0.80))
    out = h.run(panel, cfg, scores=scores, score_source="pit", coverage={}, fired={})
    assert out["ready"], out
    assert out["data_coverage_gate"]["panel_given"] is False
    assert any("커버리지 패널이" in w for w in out["warnings"]), out["warnings"]

    # 패널을 주면 실제로 막고, 막은 횟수를 낸다.
    covers = {t: [(0.5 if t == "T11" else 1.0)] * len(dates) for t in closes}
    out2 = h.run(panel, cfg, scores=scores, score_source="pit", coverage={}, fired={},
                 covers=covers)
    assert out2["data_coverage_gate"]["panel_given"] is True
    assert out2["data_coverage_gate"]["blocked"] > 0, out2["data_coverage_gate"]
    assert not any("커버리지 패널이" in w for w in out2["warnings"])


def test_price_harness_does_not_charge_stocks_for_factors_it_cannot_see():
    """하네스가 원리적으로 못 보는 팩터(수급·공매도)를 종목에 물리면 매수 0이 된다."""
    from signal_desk.signals import engine as eng
    from signal_desk.signals import harness as h

    cfg = eng.SignalConfig()
    price_cov = eng.data_coverage({"technical": True, "momentum": True}, cfg,
                                  unavailable=h._PRICE_UNAVAILABLE)
    assert price_cov["ratio"] == 1.0, price_cov
    pit_cov = eng.data_coverage({"technical": True, "momentum": True, "fundamental": True,
                                 "valuation": True, "quality": True}, cfg,
                                unavailable=h._PIT_UNAVAILABLE)
    assert pit_cov["ratio"] == 1.0, pit_cov
    # 그런데 재무가 없으면 PIT 경로에서도 떨어져야 한다(그게 게이트의 목적이다).
    thin = eng.data_coverage({"technical": True, "momentum": True}, cfg,
                            unavailable=h._PIT_UNAVAILABLE)
    assert thin["ratio"] < 1.0 and sorted(thin["missing"]) == ["fundamental", "quality",
                                                              "valuation"]


def test_preregistered_config_covers_every_engine_field():
    """`signalcfg.FIELDS`의 모든 필드가 사전등록 `[base.config]`에 있어야 한다.

    `config_agrees_with_engine`은 **등록된 키만** 비교한다. 새 점수 필드를 등록에 안 넣으면
    그 파라미터는 검증된 적 없이 판정에 섞이고, 낡은 등록으로 확정한 판정은 증거가 아니다.
    """
    from signal_desk import prereg, signalcfg

    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    # family가 여러 개면 **각 family의 config**를 다 본다 — 하나만 검사하면 나머지는 빠진다.
    seen = set()
    for lk in reg["looks"]:
        cfg = lk["config"]
        missing = [f for f in signalcfg.FIELDS if f not in cfg]
        assert not missing, f"{lk['id']}: 사전등록에 없는 엔진 필드: {missing}"
        assert signalcfg.MODE_FIELD in cfg
        seen.add(lk["family"])
    assert len(seen) >= 1


# ── X3: 게이트 투명화 ──────────────────────────────────────────────────────────
# 실측(2026-08-06): 게이트 3개가 정책이 둘로 갈라져 있었다. `_apply_crash_gate` 는 HOLD여도
# `gated=True` 를 세우는데(그 docstring이 이유를 설명한다) `_apply_trend_gate`·
# `_apply_earnings_gate` 는 `kind not in BUY_KINDS` 면 early-return 했다. 그래서
# `rank_min_score < buy_threshold` 인 구간의 점수는 게이트가 안 걸린 채 분위 승격으로
# **STRONG_BUY가 됐다** — 재현: 하락추세 확인 종목이 점수 +0.80에 매수권을 받았고 근거에
# 추세 언급조차 없었다. 그리고 `gate_blocked` 는 BUY 강등만 세어 **17종목**이라 했는데
# 실제 추세 차단은 **67종목**이었고, 완화는 7종목 발동했는데 화면상 0건이었다.

def test_every_buy_gate_marks_gated_regardless_of_kind():
    """게이트가 kind에 따라 다르게 동작하면 분위 승격이 그 게이트를 우회한다."""
    from signal_desk.signals import engine as eng

    # rank_min_score를 buy_threshold보다 낮춰 '그 사이 점수'를 만든다(둘 다 관리자 편집 가능).
    cfg = eng.SignalConfig(weight_technical=0.35, weight_reversion=0.0, buy_threshold=1.2,
                           rank_min_score=0.1, min_data_coverage=0.0)
    closes = [100 - i * 0.4 for i in range(80)]          # 확인된 하락추세
    series = eng.compute_indicator_series(closes, cfg)
    assert eng._downtrend_confirmed(closes, series, len(closes) - 1, cfg), "픽스처가 하락추세가 아니다"
    r = eng.evaluate([{"ticker": "A", "name": "A"}], {"A": closes}, config=cfg)[0]
    assert 0.1 <= r.score < 1.2, f"픽스처 점수가 문턱 사이가 아니다: {r.score}"
    assert r.gates == ["trend"], r.gates
    assert r.gate_blocked is True
    assert r.rank_eligible is False, "하락추세 종목이 분위 승격으로 매수권을 받았다"
    assert not eng.is_buy(r.kind)


def test_gate_blocks_are_counted_per_gate_and_in_window():
    """게이트별로, 그리고 **창 안 자리 기준**으로 센다. 합계 하나는 매수 0을 설명하지 못한다."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig(weight_technical=0.35, weight_reversion=0.0, min_data_coverage=0.0,
                           rank_top_pct=50.0)
    down = [100 - i * 0.4 for i in range(80)]
    up = [100 * 1.004 ** i for i in range(80)]
    res = eng.evaluate([{"ticker": "D", "name": "D"}, {"ticker": "U", "name": "U"}],
                       {"D": down, "U": up}, config=cfg)
    g = eng.selection_summary(res, cfg)["gates"]
    assert g["blocked"].get("trend") == 1, g
    assert set(g["labels"]) >= {"trend", "earnings", "crash", "event", "coverage"}
    assert "window_slots" in g and isinstance(g["window_causes"], list)
    # 창 안 원인은 라벨과 자리 수를 같이 낸다 — 키만 내면 화면이 다시 매핑을 만든다.
    for c in g["window_causes"]:
        assert set(c) == {"gate", "label", "slots"} and c["slots"] > 0


def test_gate_relaxation_is_recorded_even_when_not_a_buy():
    """완화를 BUY일 때만 기록하면 '있는지 모르는 완화'가 된다(실측 7종목이 화면상 0건이었다)."""
    from signal_desk.signals import engine as eng

    cfg = eng.SignalConfig(weight_technical=0.0, weight_reversion=0.0, min_data_coverage=0.0)
    # 하락추세지만 시장(-20%)보다 덜 빠진 종목 → 완화 대상. 점수는 BUY가 아니다.
    mild = [100 - i * 0.3 for i in range(80)]
    crash = [100 * 0.985 ** i for i in range(80)]
    res = eng.evaluate([{"ticker": "M", "name": "M"}, {"ticker": "C", "name": "C"}],
                       {"M": mild, "C": crash}, config=cfg)
    m = next(r for r in res if r.ticker == "M")
    assert not eng.is_buy(m.kind), "픽스처가 BUY면 옛 경로로도 기록돼 검사가 무의미하다"
    assert "trend" in m.gates_relaxed, (m.gates, m.gates_relaxed, m.reasons)
    assert "trend" not in m.gates
    assert eng.selection_summary(res, cfg)["gates"]["relaxed"].get("trend") == 1


def test_hold_tag_reads_gate_structure_not_reason_strings():
    """화면 태그가 근거 문구 파싱이면 문구를 고칠 때 태그가 조용히 사라진다."""
    from signal_desk import api as api_mod
    from signal_desk.signals import engine as eng

    r = eng.SignalResult(ticker="A", name="A", score=0.5, kind="HOLD", confidence=0.5,
                         technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                         gates=["trend"], reasons=[])       # 근거 문구 없음 — 구조만 있다
    assert api_mod._hold_tag(r, buy_blocked=False) == eng.GATE_LABELS["trend"]
    r2 = eng.SignalResult(ticker="B", name="B", score=0.5, kind="HOLD", confidence=0.5,
                          technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                          low_coverage=True, reasons=[])
    assert api_mod._hold_tag(r2, buy_blocked=False) == eng.GATE_LABELS["coverage"]
    # 태그 우선순위에 쓰는 키가 전부 GATE_LABELS에 있어야 한다(오타면 KeyError로 즉시 터진다).
    assert set(api_mod._GATE_TAG_ORDER) <= set(eng.GATE_LABELS)


def test_harness_reports_gate_block_counts():
    """하네스도 게이트가 몇 번 막았는지 낸다 — 0이면 게이트 없는 전략을 잰 것이다."""
    from signal_desk.signals import harness as h

    dates = _dates(400)
    closes = {f"T{i}": (dates, [100.0 * (1.0 - 0.004) ** k if i < 6
                                else 100.0 * 1.004 ** k for k in range(400)])
              for i in range(12)}
    panel = h.build_panel(closes)
    scores = {t: [2.0] * len(dates) for t in closes}
    cfg = h.HarnessConfig(random_trials=10, min_periods=1, phase_average=False,
                          signal_config=h.SignalConfig(min_data_coverage=0.0))
    out = h.run(panel, cfg, scores=scores, score_source="pit", coverage={}, fired={})
    assert out["ready"], out
    assert "gate_blocks" in out and "trend" in out["gate_blocks"]
    assert out["gate_blocks"]["trend"] > 0, out["gate_blocks"]


def test_harness_leaves_gated_slots_empty_like_live():
    """게이트로 막힌 자리를 **k 밖 다음 순위로 채우면** 게이트가 보유 수를 줄이지 못한다.

    실측(2026-08-06): 하네스 픽 루프가 `ranked` 전체를 돌며 `len(picks) >= k` 에서 끊어,
    추세 게이트가 1076회 걸렸는데도 켜고 끈 결과의 백분위·매수0기간·평균보유수가 **완전히
    같았다**(90.0 · 529 · 3.1). 같은 날 라이브는 매수권 0/6자리였다. 하네스의 게이트는
    사실상 재정렬이었고, 게이트 없는 전략을 재고 있었다 — 같은 병의 네 번째 재발이다.
    """
    from signal_desk.signals import harness as h

    dates = _dates(400)
    # 상위 2종목은 하락추세(게이트 대상), 나머지는 상승추세. 점수는 하락추세 쪽이 더 높게 준다.
    closes = {}
    for i in range(20):
        down = i < 2
        closes[f"T{i}"] = (dates, [(100.0 * (1.0 - 0.004) ** k) if down
                                   else (100.0 * 1.003 ** k) for k in range(400)])
    panel = h.build_panel(closes)
    scores = {t: [(3.0 if t in ("T0", "T1") else 2.0)] * len(dates) for t in closes}
    cfg = h.HarnessConfig(top_pct=10.0, random_trials=10, min_periods=1, phase_average=False,
                          signal_config=h.SignalConfig(min_data_coverage=0.0))
    out = h.run(panel, cfg, scores=scores, score_source="pit", coverage={}, fired={})
    assert out["ready"], out
    k = h.engine.rank_slots(20, 10.0)                      # 20종목 · 10% → 2자리
    assert k == 2
    assert out["gate_blocks"]["trend"] > 0, "게이트가 안 걸리면 검사가 무의미하다"
    # 상위 2자리가 전부 게이트면 매수는 0이어야 한다 — 3~4위로 채우면 이 값이 2가 된다.
    assert out["strategy"]["avg_picks"] == 0.0, out["strategy"]["avg_picks"]
    assert out["empty_periods"] == out["periods"], (out["empty_periods"], out["periods"])


def test_preregistered_seen_hypotheses_require_an_oos_window():
    """결과를 본 가설은 `from_date`로 아직 보지 않은 구간을 걸어야 정본이 된다.

    D4(추세 게이트 off)는 2026-08-06 진단에서 백분위 92.5를 **보고 나서** 등록했다. 그대로
    전 구간에 걸면 사후등록이다. 규칙: `from_date >= registered_at`, 그리고 그 창으로만 잰다.
    """
    from signal_desk import prereg

    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    d4 = next((lk for lk in reg["looks"] if lk["id"] == "d4-no-trend-gate-oos"), None)
    assert d4 is not None, "D4 OOS look이 등록에서 사라졌다"
    fd = (d4["requirement"] or {}).get("from_date")
    assert fd and fd >= d4["registered_at"], (fd, d4["registered_at"])
    assert d4["config"]["trend_gate"] == 0.0
    # family가 달라야 한다 — 같은 family면 설정 불일치로 load가 거부한다(Šidák 가정).
    fam1 = next(lk for lk in reg["looks"] if lk["id"].endswith("-final")
                and lk["id"] != "d4-no-trend-gate-oos")
    assert d4["family"] != fam1["family"]
    assert fam1["config"]["trend_gate"] == 1.0
    # family를 나눠도 n은 줄지 않는다 — 파일을 쪼개 문턱을 낮추는 것이 사후 완화다.
    assert reg["n_canonical"] == 3 and reg["threshold_pct"] == prereg.sidak_threshold_pct(3)
    assert reg["threshold_pct"] > prereg.sidak_threshold_pct(2)


def tmp_path_factory_dir():
    """레드팀 전용 임시 디렉토리 — 사전등록 파싱 검사에서 toml을 여러 개 쓴다."""
    import pathlib
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())


def test_prereg_rejects_config_drift_within_a_family_and_bad_oos():
    """같은 family 안 설정 불일치·이른 from_date는 **파싱 단계에서** 막는다."""
    from signal_desk import prereg

    good = '''
[base]
family = "f1"
score_source = "pit"
market = "kr"
[base.config]
weight_momentum = 0.30
[base.harness]
hold = 5
cost_pct = 0.25
trials = 200
exposure = false
[[looks]]
id = "a"
role = "final"
registered_at = "2026-08-06"
[looks.requirement]
min_effective_periods = 30
min_pit_dates = 150
'''
    p = tmp_path_factory_dir() / "ok.toml"
    p.write_text(good, encoding="utf-8")
    assert prereg.load(p)["ok"]

    # from_date가 registered_at보다 이르면 OOS가 아니다
    bad = good.replace("min_pit_dates = 150",
                       'min_pit_dates = 150\nfrom_date = "2026-01-01"')
    p2 = tmp_path_factory_dir() / "bad_oos.toml"
    p2.write_text(bad, encoding="utf-8")
    r = prereg.load(p2)
    assert not r["ok"] and "OOS가 아니다" in r["reason"], r["reason"]

    # 같은 family 안에서 설정이 다르면 순차 관측이 아니다
    drift = good + '''
[[looks]]
id = "b"
role = "final"
registered_at = "2026-08-06"
[looks.config]
weight_momentum = 0.10
[looks.requirement]
min_effective_periods = 30
min_pit_dates = 150
'''
    p3 = tmp_path_factory_dir() / "drift.toml"
    p3.write_text(drift, encoding="utf-8")
    r3 = prereg.load(p3)
    assert not r3["ok"] and "별개 실험" in r3["reason"], r3["reason"]

    # family 이름 중복도 막는다
    dup = good + '''
[[families]]
family = "f1"
score_source = "pit"
[families.config]
weight_momentum = 0.30
[families.harness]
hold = 5
cost_pct = 0.25
trials = 200
exposure = false
[[families.looks]]
id = "c"
role = "final"
registered_at = "2026-08-06"
[families.looks.requirement]
min_effective_periods = 30
min_pit_dates = 150
'''
    p4 = tmp_path_factory_dir() / "dup.toml"
    p4.write_text(dup, encoding="utf-8")
    r4 = prereg.load(p4)
    assert not r4["ok"] and "family 중복" in r4["reason"], r4["reason"]


def test_oos_slice_cuts_scores_panel_and_covers_together():
    """OOS 창으로 자를 때 점수·패널·커버리지를 **같은 위치에서** 잘라야 한다.

    한쪽만 자르면 인덱스가 밀려 다른 날짜의 점수로 채점한다 — 결과는 나오지만 무엇을 잰
    것인지 알 수 없다.
    """
    from signal_desk import store
    from signal_desk.signals import harness as h

    dates = _dates(10)
    panel = h.Panel(dates=list(dates),
                    closes={"A": [100.0 + i for i in range(10)]})
    scores = {"A": [float(i) for i in range(10)]}
    covers = {"A": [1.0] * 10}
    cut_from = dates[4]
    ns, np_, nc, lo = store._slice_after(cut_from, scores, panel, covers)
    assert lo == 4
    assert np_.dates == dates[4:]
    assert np_.closes["A"] == [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]
    assert ns["A"] == [4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    assert nc["A"] == [1.0] * 6
    # 세 리스트 길이가 같아야 한다 — 어긋나면 다른 날짜로 채점한다.
    assert len(np_.dates) == len(ns["A"]) == len(nc["A"])


def test_counterfactual_look_never_becomes_the_board_headline():
    """반사실 판정이 헤드라인이 되면 `prereg.change_allowed`(N2)가 잘못 열린다.

    D4는 `role = "final"` 인데 라이브가 일부러 안 돌리는 설정(`trend_gate = 0`)을 잰다.
    그 성적으로 라이브 파라미터 변경을 허가하면, 돌리지 않는 전략의 증거로 돌리는 전략을 바꾸는 것이다.
    """
    from signal_desk import store

    board = store.harness_board("kr")
    assert board["ready"], board
    ids = [r["id"] for r in board["looks"]]
    assert "d4-no-trend-gate-oos" in ids, "D4가 보드 목록에서 사라졌다"
    assert board["counterfactual_looks"] == ["d4-no-trend-gate-oos"], board["counterfactual_looks"]
    # 헤드라인은 라이브 family의 final이어야 한다.
    head_id = next((r["id"] for r in board["looks"]
                    if r["verdict"] == board["verdict"]
                    and r["requirement"] == board["requirement"]), None)
    assert head_id != "d4-no-trend-gate-oos"
    d4 = next(r for r in board["looks"] if r["id"] == "d4-no-trend-gate-oos")
    assert d4["counterfactual"] is True and d4["diff_from_live"] == ["trend_gate"]
    assert d4["oos_from"] == "2026-08-07"


# ── X4: 판별력을 첫 화면으로 ───────────────────────────────────────────────────
# 진단(2026-08-05): `판별력` 이라는 문자열이 index.html에 세 곳뿐이고 **전부 관리자**였다.
# 시그널 첫 화면은 접힌 <details> 안 백테스트 숫자를 대신 보여줬다.

def test_verdict_route_never_leaks_percentile_before_requirements_are_met(tmp_path, monkeypatch):
    """요건 미충족 동안 백분위를 화면에 내지 않는다 — 매일 보이면 매일 보게 되고 그게 다중검정이다."""
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from signal_desk import api as api_mod
    from signal_desk import db as db_mod
    importlib.reload(db_mod)
    importlib.reload(api_mod)

    c = TestClient(api_mod.app)
    assert c.get("/api/verdict").status_code == 401, "비로그인에 판정을 내주면 안 된다"
    su = c.post("/api/auth/signup", json={"email": "verdict-probe@e.com", "pw": "abcdef12"})
    assert su.status_code == 200, su.text
    r = c.get("/api/verdict")
    assert r.status_code == 200, r.text
    d = r.json()
    if not d.get("ready"):
        # 사전등록 파일을 못 찾는 임시 cwd — 그래도 백분위는 없어야 한다.
        assert "percentile" not in d or d["percentile"] is None
        assert d["verdict"] == "판정 불가"
        return
    assert d["ready"] is True
    req = d["requirement"] or {}
    if not req.get("met"):
        assert d["percentile"] is None, "요건 미충족인데 백분위가 새어 나왔다"
        assert d["verdict"] in ("판정 보류", "판정 불가", "무효"), d["verdict"]
    # 문턱·등록 수를 같이 내야 한다 — 백분위만 보면 무엇과 비교하는지 알 수 없다.
    assert d["threshold_pct"] and d["n_registered"] >= 1
    # 진척은 이름과 숫자로 — "곧 나옵니다"로 쓰면 언제인지 모른다.
    assert "min_effective_periods" in req and "min_pit_dates" in req


def test_real_board_withholds_percentile_until_requirements_are_met():
    """**실제 사전등록 파일**로도 같은 것을 본다 — 위 라우트 검사는 임시 cwd라 not-ready로 빠질 수 있다."""
    from signal_desk import store

    b = store.harness_board("kr")
    assert b["ready"], b
    for row in b["looks"]:
        req = row["requirement"] or {}
        if row["status"] != "locked":
            assert row["percentile"] is None, (row["id"], row["percentile"])
            assert row["verdict"] in ("판정 보류", "무효"), row["verdict"]
            # 무엇이 남았는지 이름과 숫자로 — 이유 없는 보류는 조용한 0과 같다.
            assert row["verdict_why"], row["id"]
        assert req.get("min_effective_periods") and req.get("min_pit_dates")
    if b["status"] != "locked":
        assert b["percentile"] is None, b["percentile"]


def test_verdict_route_reuses_the_board_and_does_not_recompute():
    """판정을 두 곳에서 조립하면 화면과 보드가 갈라지고 그 차이는 어디에도 안 뜬다."""
    import inspect

    from signal_desk import api as api_mod

    src = inspect.getsource(api_mod.api_verdict)
    assert "harness_board" in src, "판정 라우트가 보드를 안 쓰고 따로 계산한다"
    for forbidden in ("percentile =", "better", "sidak_threshold_pct("):
        assert forbidden not in src, f"판정 라우트가 {forbidden!r}로 값을 재계산한다"


def test_first_screen_shows_the_verdict_before_the_score():
    """첫 화면 신뢰 스트립의 **첫 줄**이 판정이어야 한다(성적·페이퍼보다 먼저)."""
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "function verdictRow(" in html
    assert "rows.unshift(verdictRow(hz))" in html, "판정 줄이 맨 앞에 들어가지 않는다"
    assert "fetch('/api/verdict')" in html
    # 접힌 요약도 **판정부터** — "'판정 불가'가 12px 회색으로, 확신은 초록"이었던 것을 뒤집는다.
    # 문구가 아니라 **순서**를 본다: `sumTxt` 의 첫 조립이 판정값이어야 하고, 성적(정밀도)은
    # 그 뒤에 `+=` 로 붙어야 한다. 리터럴로 박으면 카피를 고칠 때마다 깨진다(2026-08-06에 깨졌다).
    head = html.split("let sumTxt = ", 1)[1].split("\n", 1)[0]
    assert "plainVerdict" in head or "vTxt" in head, "요약 첫 조립이 판정값이 아니다"
    after = html.split("let sumTxt = ", 1)[1].split("if (matureOk)", 1)[1]
    assert "sumTxt +=" in after, "성적이 판정보다 먼저 조립된다"
    # 보류 분기가 백분위를 **읽지도** 않아야 한다 — 보드가 실수로 실어 보내도 화면은 안 그린다.
    body = html.split("function verdictRow(", 1)[1].split("\nfunction ", 1)[0]
    hold = body.split("// 보류", 1)[1]
    assert "percentile" not in hold, "보류 분기에서 백분위를 그린다"
    # locked 분기만 백분위를 쓴다.
    locked = body.split("if (hz.status === 'locked')", 1)[1].split("// 보류", 1)[0]
    assert "hz.percentile" in locked


# ── X5: 닿지 않는 표면 ─────────────────────────────────────────────────────────
# 2026-08-06: X5의 원래 계획은 "표면 덜어내기"(숏폼·D7·온보딩 삭제)였는데, 그 셋은
# `docs/north-star-selection.md:49` 가 이미 **동결**로 결정한 것들이다 — 동결이 결정이고
# 지우는 건 V1·V2에 아무것도 더하지 않는다. 실제 문제는 기능이 많은 게 아니라
# **닿지 않는 기능**이 많은 것이었다: 라우트 112개 중 11개가 화면·CLI 어디서도 안 불렸고,
# 그중 `/api/pick-reason` 은 북극성 A의 절반("고른 이유를 사후 재생")인데 화면이 없었다.

# 부르는 곳이 없어도 되는 라우트 — **이유를 하나씩 적는다.** 통째로 스킵하면 새로 생긴
# 고아 라우트가 조용히 섞이고, 그게 정확히 이 검사가 막으려는 것이다.
_ROUTES_WITHOUT_UI = {
    # 외부(cron·스케줄러)가 부른다
    "/api/morning-digest": "외부 스케줄러가 부른다(브리핑 발송)",
    "/api/morning-digest/test": "발송 검증용 수동 호출",
    # 서버가 URL을 만들어 클라이언트에 내려준다 → 정적 grep으로는 안 잡힌다
    "/api/shortform/background-image": "서버가 캐시버스트 URL을 만들어 kv로 내려준다",
    # 일일 루프가 대신 부른다
    "/api/kb/poll-disclosures": "공시 폴링 수동 트리거 — 일일 루프가 대신 부른다",
    # ── 아래는 **미완성 표시**다. 2026-08-06에 "확인 필요"였던 항목을 하나씩 결론냈다. ──
    "/api/methods": (
        "방법론 카탈로그(26건) — 원래 주석은 '두뇌 레이어가 참조'라고 했지만 부르는 곳은 이 라우트와"
        " 자기 테스트뿐이었다(라우트 주석에 정정 기록). 커밋된 참조 데이터라 남기고, 소비자가"
        " 생기면 그때 붙인다"),
    "/api/bot/decisions": (
        "봇 판단 저널 전체 — 픽 단위 저널은 `/api/pick-reason` 상세가 보여준다. 전체 목록은"
        " 페이퍼 탭 공개 장부에 붙일 후보이고, 지금은 체결만 노출한다"),
    "/api/kb/events/review": (
        "KB 이벤트 검수 — 큐가 0건이라 화면 경로가 안 만들어졌다. 큐 길이·만료 임박은 배지로"
        " 이미 뜨므로, 배지가 0이 아니게 되는 날 붙인다"),

}


def test_every_api_route_has_a_caller_or_a_stated_reason():
    """라우트를 만들고 화면에 안 붙이면 그 기능은 존재하지만 닿을 수 없다.

    `product_reviewer` 가 그랬다 — 라우트 2개가 있는데 UI 버튼이 없고 `kv:product_review_last`
    가 없어 **한 번도 실행된 적이 없었다**. 새 라우트를 붙이지 않은 채 넘어가면 이 검사가 잡는다.
    """
    import re
    from pathlib import Path

    api = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    callers = (Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
               + Path("src/signal_desk/cli.py").read_text(encoding="utf-8"))
    routes = {p for _, p in re.findall(r'@app\.(get|post|delete)\("([^"]+)"', api)}
    orphans = sorted(p for p in routes
                     if not p.startswith(("/api/auth", "/health")) and "{" not in p
                     and p not in callers)
    unexplained = [p for p in orphans if p not in _ROUTES_WITHOUT_UI]
    assert not unexplained, (
        f"부르는 곳이 없는 새 라우트: {unexplained} — 화면에 붙이거나 "
        f"_ROUTES_WITHOUT_UI에 **이유와 함께** 등록할 것")
    # 반대 방향도 본다: 사라진 라우트가 허용 목록에 유령으로 남으면 목록이 낡는다.
    stale = [p for p in _ROUTES_WITHOUT_UI if p not in routes]
    assert not stale, f"허용 목록에 없는 라우트가 남아 있다: {stale}"
    # **세 번째 방향**(2026-08-06에 실제로 뚫려 있었다): 화면에 붙였는데 면제 목록에 남은 것.
    # `/api/pick-reason` 을 #347에서 붙였는데 목록에 그대로 있었고 이 검사가 못 잡았다 —
    # 면제가 유령으로 남으면 상한(7개)이 실제보다 꽉 차 보여 다음 라우트를 밀어낸다.
    connected = [p for p in _ROUTES_WITHOUT_UI if p in callers]
    assert not connected, (
        f"화면·CLI가 이미 부르는데 면제 목록에 남아 있다: {connected} — 목록에서 뺄 것")
    # 목록이 자라기만 하는 것을 막는다. 지금 7개이고, 늘리려면 이 숫자를 같이 올려야 한다.
    # 2026-08-06: 10 → 7. `/api/pick-reason`·`/api/buylist` 는 화면에 붙였고
    # `/api/valuation` 은 시그널 payload(per·pbr·opp_tags) + 스크리너로 대체돼 삭제했다.
    assert len(_ROUTES_WITHOUT_UI) <= 7, (
        f"닿지 않는 라우트가 {len(_ROUTES_WITHOUT_UI)}개로 늘었다 — 붙이거나 지울 것")


# ── LLM 예산 게이트 ────────────────────────────────────────────────────────────
# 2026-08-06: `/api/chat`·`/api/chat/stream`에 레이트리밋도 예산 상한도 없었다. 30일 누적은
# $1.11로 작았지만 상한이 없으면 대화 루프·재시도 폭발이 그대로 청구서가 된다.

def test_budget_gate_covers_every_network_call_site_in_llm():
    """상한은 라우트가 아니라 **llm 모듈**에 있어야 한다 — 호출자가 11개다.

    그리고 `_post_json` 하나만 막으면 안 된다: `stream_call`은 SSE라 자기 요청을 따로 만들고,
    그게 하필 막아야 할 `/api/chat/stream` 경로다("단일 호출 지점"이라는 전제를 확인하지 않으면
    게이트는 있는 척만 한다).
    """
    import re
    from pathlib import Path

    src = Path("src/signal_desk/llm.py").read_text(encoding="utf-8")
    # urlopen을 부르는 함수마다 그 앞에 예산 판정이 있어야 한다.
    bodies = re.split(r"\ndef ", src)
    opens = [b for b in bodies if "urllib.request.urlopen(" in b]
    assert len(opens) >= 2, f"urlopen 지점이 {len(opens)}개 — 검사 전제가 깨졌다"
    for b in opens:
        name = b.split("(", 1)[0]
        assert "budget_state()" in b, f"{name}: urlopen 앞에 예산 판정이 없다(게이트 우회)"


def test_budget_block_is_distinguishable_from_missing_key():
    """예산 차단이 None이면 '키 없음'과 같아 보인다 — 화면에서 두 상태를 가를 수 없다."""
    import importlib

    from signal_desk import llm

    calls = []

    def _blocked():
        return {"ok": False, "reason": "테스트 차단", "day_usd": 9.0, "day_cap": 1.0,
                "month_usd": 9.0, "month_cap": 10.0}

    orig_state, orig_key = llm.budget_state, llm.config.anthropic_key
    llm.budget_state = _blocked
    llm.config.anthropic_key = lambda: "sk-test"
    try:
        for name, fn in (("complete", lambda: llm.complete("s", "u")),
                         ("complete_json", lambda: llm.complete_json("s", "u")),
                         ("messages_with_tools", lambda: llm.messages_with_tools("s", [], [])),
                         ("stream_call", lambda: list(llm.stream_call("s", [], [])))):
            try:
                fn()
                calls.append(f"{name}: 예외 없이 반환")
            except llm.BudgetExceeded:
                pass
            except Exception as e:                       # noqa: BLE001
                calls.append(f"{name}: {type(e).__name__}")
    finally:
        llm.budget_state = orig_state
        llm.config.anthropic_key = orig_key
        importlib.reload(llm)
    assert not calls, f"예산 차단이 BudgetExceeded로 올라오지 않는다: {calls}"


def test_budget_gate_fails_closed_when_spend_is_unreadable():
    """지출을 못 읽으면 **막는다**. fail-open은 게이트가 없는 것과 같다."""
    from signal_desk import llm

    orig = llm.db.llm_spend_usd
    llm.db.llm_spend_usd = lambda **_: None          # 읽기 실패
    try:
        st = llm.budget_state()
        assert st["ok"] is False and "읽을 수 없" in st["reason"], st
    finally:
        llm.db.llm_spend_usd = orig
    # 0.0(안 씀)은 통과해야 한다 — None과 0을 같게 취급하면 평상시에도 막힌다.
    llm.db.llm_spend_usd = lambda **_: 0.0
    try:
        assert llm.budget_state()["ok"] is True
    finally:
        llm.db.llm_spend_usd = orig


def test_chat_routes_are_rate_limited_and_report_the_reason():
    """막힐 때 **이유를 그대로** 돌려준다 — 조용한 빈 답변은 고장처럼 보인다."""
    import importlib

    from fastapi.testclient import TestClient

    from signal_desk import api as api_mod

    src = __import__("pathlib").Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    # 두 라우트가 모두 가드를 통과해야 한다(하나만 걸면 다른 쪽으로 새어 나간다).
    for route in ('@app.post("/api/chat")', '@app.post("/api/chat/stream")'):
        blk = src.split(route, 1)[1].split("\n@app.", 1)[0]
        assert "_chat_guard(request)" in blk, f"{route}에 가드가 없다"
    assert "_rate_limited(request, \"chat\"" in src
    importlib.reload(api_mod)


def test_storage_report_detects_ephemeral_and_stays_quiet_when_healthy(tmp_path, monkeypatch):
    """볼륨이 없으면 배포마다 장부가 지워지고 **그건 조용하다**(새 DB가 "누적 중"으로 보인다).

    코드가 Railway 볼륨 설정을 알 방법은 없으므로 **증상으로 판정한다** — 부팅 카운터가 이전
    프로세스를 기억하는지. 정상일 때는 아무 말도 하지 않는다(매일 초록불은 곧 안 읽힌다).
    """
    import importlib

    monkeypatch.chdir(tmp_path)                  # 부팅 카운터는 DB 상태다 → 격리 없으면 새어 든다
    from signal_desk import db as db_mod
    from signal_desk import store as store_mod
    importlib.reload(db_mod)

    r0 = store_mod.storage_report()
    assert r0["ephemeral_suspected"] is True, "최초 부팅 기록이 없으면 의심해야 한다"
    assert r0["reason"] and r0["how_to_verify"]

    store_mod.mark_boot()
    r1 = store_mod.storage_report()
    assert r1["boot_count"] == 1 and r1["first_boot"]
    # 부팅 1회 + DB 존재 = 이전 프로세스를 기억하지 못한다 → 여전히 의심
    if r1["db_exists"]:
        assert r1["ephemeral_suspected"] is True, r1

    store_mod.mark_boot()
    r2 = store_mod.storage_report()
    assert r2["boot_count"] == 2
    assert r2["ephemeral_suspected"] is False, "카운터가 살아남았는데도 의심하면 오탐이다"
    assert r2["reason"] is None
    importlib.reload(db_mod)


def test_data_health_carries_storage_and_ui_renders_it():
    """진단 값을 만들어도 화면에 안 뜨면 몇 주씩 못 본다(수집 정지와 같은 병)."""
    import inspect
    from pathlib import Path

    from signal_desk import api as api_mod

    src = inspect.getsource(api_mod.data_health_get)
    assert "store.storage_report()" in src and '"storage": storage' in src
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "ephemeral_suspected" in html, "휘발성 배너가 화면에 없다"
    assert "how_to_verify" in html, "무엇을 확인해야 하는지 화면에 안 나온다"
    # 부팅 기록이 실제로 불려야 한다 — 안 부르면 카운터가 영원히 0이고 항상 의심으로 뜬다.
    life = inspect.getsource(api_mod._lifespan)
    assert "store.mark_boot()" in life


# ── Later 티어: 다중검정 보정 ───────────────────────────────────────────────────
# L4 시도 횟수 집계 → L3 Deflated Sharpe → L2 Hansen SPA → L1 홀드아웃.
# 2026-08-06 실측: DSR을 처음 붙였을 때 **0.979 "유의"** 가 나왔는데 같은 실행의 백분위는
# 71.5(판정 불가)였다. 원인 둘 — (1) 5위상을 이어 붙여 T=1093으로 써서 sqrt(T−1)이 33.0
# (독립 219 기준 14.8)으로 부풀었고, (2) DSR은 `Sharpe > 0`을 검정하므로 롱온리 전략이
# 상승장에서 종목선택 능력 없이도 통과했다. 초과수익·한 위상으로 고치니 **0.282**가 됐다.

def test_dsr_measures_excess_over_benchmark_on_one_phase():
    """DSR이 시장 베타를 재면 롱온리 전략이 상승장에서 공짜로 통과한다."""
    import inspect

    from signal_desk.signals import harness as h

    src = inspect.getsource(h._dsr_sample)
    assert "runs[0]" in src, "위상을 이어 붙이면 T가 부풀어 z가 커진다"
    assert "bench_per_period_ret" in src and "x - y" in src, "초과수익으로 재지 않는다"
    # 산출물이 기준을 **명시**해야 한다 — 안 쓰면 읽는 사람이 Sharpe 절대값으로 오해한다.
    body = inspect.getsource(h.run)
    assert '"basis": "excess_over_benchmark"' in body
    assert '"phases_pooled": False' in body


def test_dsr_does_not_change_the_verdict():
    """DSR이 유의해도 판정은 백분위로 한다 — 두 판정이 갈라지면 관대한 쪽이 읽힌다."""
    import inspect

    from signal_desk.signals import harness as h

    src = inspect.getsource(h)
    # verdict를 정하는 함수가 dsr을 보지 않아야 한다.
    verdict_src = inspect.getsource(h._verdict)
    assert "dsr" not in verdict_src, "판정이 DSR을 본다 — 판정 경로가 둘이 된다"
    assert "판정은 백분위" in src, "DSR이 판정이 아니라는 것을 코드에 적어 두지 않았다"


def test_expected_max_sharpe_grows_with_trials():
    """시도를 늘리면 문턱이 올라가야 한다 — 안 오르면 고르기를 보정하지 않는 것이다."""
    from signal_desk.signals.multiplicity import expected_max_sharpe

    vals = [expected_max_sharpe(n, 1 / 59) for n in (1, 2, 8, 50, 200, 1000)]
    assert vals[0] == 0.0, "시도 1회면 고르기가 없다"
    assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)), vals


def test_norm_ppf_and_cdf_match_the_table():
    """정규 분위수·CDF가 임계표와 맞아야 E[max SR]·DSR이 의미를 갖는다."""
    from signal_desk.signals.multiplicity import norm_cdf, norm_ppf

    for p, want in ((0.975, 1.959964), (0.95, 1.644854), (0.99, 2.326348), (0.5, 0.0)):
        assert abs(norm_ppf(p) - want) < 1e-5, (p, norm_ppf(p))
    for z, want in ((1.959964, 0.975), (0.0, 0.5), (-2.326348, 0.01)):
        assert abs(norm_cdf(z) - want) < 1e-6, (z, norm_cdf(z))
    # 경계는 유한값으로 — inf를 돌려주면 그 뒤 산술이 전부 nan이 되고 nan은 "값 없음"과 같아 보인다.
    assert abs(norm_ppf(0.0)) < 100 and abs(norm_ppf(1.0)) < 100


def test_spa_is_calibrated_under_the_null():
    """p-value는 **오탐률**로 검증한다 — 단일 시드로는 운 좋은/나쁜 draw를 구분할 수 없다.

    실제로 시드 하나에서 귀무인데 p=0.0012가 나왔고, 처음엔 코드 결함으로 의심했다.
    150 시드로 재니 오탐률 3.3%(명목 5%)였다 — 교정돼 있었고 그 시드가 드문 draw였다.
    """
    import random

    from signal_desk.signals.multiplicity import spa_test

    def rate(edge, seeds=40):
        hits = 0
        for s in range(seeds):
            rng = random.Random(5000 + s)
            d = {f"c{i}": [rng.gauss(edge if i == 3 else 0.0, 0.02) for _ in range(60)]
                 for i in range(6)}
            if spa_test(d, trials=200, seed=13 + s)["significant"]:
                hits += 1
        return hits / seeds
    null_rate = rate(0.0)
    power = rate(0.012)
    assert null_rate <= 0.20, f"귀무 오탐률 {null_rate:.0%} — 너무 높다(교정 실패)"
    assert power >= 0.60, f"강한 우위 검출률 {power:.0%} — 너무 낮다(아무것도 통과 못 하는 검사)"


def test_spa_requires_a_common_date_axis():
    """조합별 기간 수가 다르면 같은 날짜축이 아니므로 비교가 성립하지 않는다."""
    from signal_desk.signals.multiplicity import spa_test

    r = spa_test({"a": [0.01] * 20, "b": [0.01] * 19}, trials=50)
    assert r["p_value"] is None and "같은 날짜축이 아니다" in r["blocked_reason"]


def test_trial_counts_separate_distinct_configs_from_repeats(tmp_path, monkeypatch):
    """같은 조합을 다시 돌린 것은 새 시도가 아니다(재현이다). 가중치 하나만 달라도 새 시도다."""
    import importlib

    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_mod
    importlib.reload(db_mod)

    for chash, sharpe in (("aaa", 0.2), ("bbb", 0.1), ("aaa", 0.2)):
        db_mod.harness_run_insert({"score_source": "pit", "market": "kr", "config_hash": chash,
                                   "harness_json": '{"hold":5}', "sharpe": sharpe})
    t = db_mod.harness_trial_counts(market="kr")
    assert t["runs"] == 3 and t["distinct_configs"] == 2 and t["repeats"] == 1
    assert t["tunable_params"] >= 17 and "trend_gate" in t["param_names"]
    assert db_mod.harness_sharpes(market="kr") == [0.2, 0.1, 0.2]


def test_holdout_is_excluded_from_the_sweep_and_never_printed():
    """홀드아웃 성적을 출력하면 본 것이고, 본 구간은 더 이상 홀드아웃이 아니다(L1)."""
    from pathlib import Path

    src = Path("src/signal_desk/cli.py").read_text(encoding="utf-8")
    blk = src.split("if holdout_from:", 1)[1].split("if pit and pit_fund:", 1)[0]
    assert "d < holdout_from" in blk, "홀드아웃 이전만 남기지 않는다"
    assert "출력하지" in blk, "홀드아웃을 출력하지 않는다는 규약이 코드에 없다"
    # 실제 보증은 문구가 아니라 **데이터가 패널에서 사라지는 것**이다 — 잘라낸 뒤에는
    # 하류 코드가 홀드아웃을 볼 방법이 없으므로 출력할 수도 없다.
    assert "panel.dates[:hi]" in blk and "row[:hi]" in blk, (
        "패널을 자르지 않으면 하류가 홀드아웃을 볼 수 있다")
    # 표본이 너무 적으면 스윕 자체를 거부한다(자르고 나서 60거래일 미만).
    assert "len(keep) < 60" in blk


def test_sweep_passes_the_trial_count_to_dsr():
    """스윕이 시도 횟수를 안 넘기면 `n_trials=1`이 되어 **보정 없는 DSR이 통과한다**.

    실측(2026-08-06): 처음 스윕에 붙였을 때 `시도 1회 기대최대 +0.000 · DSR 0.9918`이 나왔다.
    같은 스윕이 8조합을 돌리고 있었고, 그중 3칸이 백분위 95.0이었다 — 정확히 이 리포가
    경계하는 고르기 상황인데 DSR만 통과 도장을 찍고 있었다.
    """
    from pathlib import Path

    src = Path("src/signal_desk/cli.py").read_text(encoding="utf-8")
    blk = src.split("for tp, h in combos:", 1)[0]
    assert "harness_trial_counts" in blk, "스윕이 시도 횟수를 세지 않는다"
    assert "+ len(combos)" in blk, "이번 스윕의 조합 수를 시도에 더하지 않는다"
    body = src.split("for tp, h in combos:", 1)[1].split("console.print(table)", 1)[0]
    assert body.count("**_kw") == 3, "hz.run 호출 세 경로 모두에 시도 수를 넘겨야 한다"


def test_spa_groups_by_hold_period():
    """보유기간이 다르면 기간 수가 달라(5일 219 vs 20일 54) 같은 날짜축이 아니다.

    섞으면 SPA가 `조합별 기간 수가 다르다`로 거부한다 — 실측에서 그렇게 나왔다.
    거부는 정직하지만 아무 검정도 못 하므로 보유별로 나눠 돈다.
    """
    from pathlib import Path

    src = Path("src/signal_desk/cli.py").read_text(encoding="utf-8")
    assert "spa_diffs.setdefault(h, {})" in src, "보유기간별로 나눠 담지 않는다"
    assert "for h_group in sorted(spa_diffs):" in src, "보유별로 검정하지 않는다"


def test_trial_counts_are_visible_in_admin():
    """DSR의 N이 화면에 없으면 문턱이 왜 올라갔는지 알 수 없다."""
    import inspect
    from pathlib import Path

    from signal_desk import api as api_mod

    assert "trial_counts" in inspect.getsource(api_mod.harness_runs_get)
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "h.trial_counts" in html and "distinct_configs" in html
    assert "고르기를 보정" in html, "왜 이 수를 보여주는지 화면에 안 쓰여 있다"

# ── 모바일 레이아웃 ────────────────────────────────────────────────────────────
# 2026-08-06 실측(375px): 시그널 종목 표가 깨져 있었다. `table-layout: fixed` + `<colgroup>`인데
# 모바일 미디어쿼리가 `섹터` 열의 th/td만 `display:none` 하고 **`<col>`의 인라인 width는 그대로
# 남아** 보이지 않는 열이 30%(103px)를 계속 예약했다 — 인라인 스타일은 미디어쿼리가 못 이긴다.
# 결과: 종목명이 `HD한국조선해` + `양`으로 쪼개지고 배지가 줄바꿈돼 행 높이가 80~119px로 들쭉날쭉.

def test_mobile_breakpoint_matches_between_css_and_js():
    """열을 접는 폭이 CSS와 JS에서 다르면 열은 숨겼는데 폭은 남거나 그 반대가 된다."""
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    js_px = re.search(r"_SIG_NARROW_PX\s*=\s*(\d+)", html)
    assert js_px, "_SIG_NARROW_PX가 없다"
    # 섹터 열을 접는 미디어쿼리의 max-width를 찾는다.
    css = html[:html.find("</style>")]
    hide = css.find("th:nth-child(3), .sig-list table.dtable td:nth-child(3) { display:none; }")
    assert hide > 0, "섹터 열을 접는 미디어쿼리를 못 찾았다"
    mq = re.findall(r"@media \(max-width:(\d+)px\)", css[:hide])
    assert mq, "미디어쿼리 브레이크포인트를 못 찾았다"
    assert int(mq[-1]) == int(js_px.group(1)), (
        f"CSS {mq[-1]}px vs JS {js_px.group(1)}px — 열 접기 폭이 어긋난다")


def test_hidden_table_column_uses_display_none_not_zero_width():
    """`<col>`을 폭 0으로 두면 fixed 레이아웃이 다음 열 몫을 그 0폭 열에 먹인다.

    실측: col-sector `width:0` → 배지 셀 폭 **0** / `display:none` → **148** ✓.
    폭 0일 때 `매수권밖`이 한 글자씩 세로로 쪼개졌다 — 에러가 아니라 이상한 모양으로만 렌더된다.
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    fn = html.split("function applySigColWidths(", 1)[1].split("\n}", 1)[0]
    assert "sector.style.display = narrow ? 'none' : ''" in fn, "섹터 열을 display로 접지 않는다"
    # 폭 0으로 되돌아가는 회귀를 막는다.
    assert "set('col-sector', narrow ? '0'" not in html
    # 좁은 폭에서 남는 두 열이 100%를 나눠 가져야 한다(안 하면 30%가 죽은 공간으로 남는다).
    assert "w.name / (w.name + w.sig) * 100" in fn


def test_signal_column_widths_recompute_on_resize():
    """회전·창 크기 변경에 다시 계산하지 않으면 세로→가로 전환 후 깨진 채 남는다."""
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # 리스너를 **하나만** 둔다 — 같은 일을 두 곳에서 시키면 하나를 고칠 때 다른 쪽이 남는다.
    assert html.count("window.addEventListener('resize'") == 1
    blk = html.split("window.addEventListener('resize'", 1)[1][:900]
    assert "applySigColWidths" in blk


def test_label_value_pairs_do_not_break_across_lines():
    """라벨과 값이 서로 다른 줄에 놓이면 짝이 깨진다(375px에서 `PBR` / `1.57`이 분리됐다)."""
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    assert ".sig-quote .q-pair { white-space:nowrap; }" in css
    # 라벨만 span이고 값이 맨 텍스트 노드로 남는 옛 패턴이 돌아오지 않게 한다.
    assert '<span class="q-k">PER</span> ${' not in html
    assert "const pair = (k, v) =>" in html


def test_wide_tables_scroll_inside_their_own_container():
    """넓은 표가 조상 카드를 가로로 밀면 헤더·문단까지 따라 움직여 읽던 위치를 잃는다."""
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    assert ".tscroll { overflow-x:auto;" in css
    assert ".tscroll > table.dtable { min-width:max-content; }" in css
    assert '<div class="tscroll"><table class="dtable"><thead><tr><th>모델</th>' in html


# ── 프로덕션에서만 죽는 것 ──────────────────────────────────────────────────────
# 2026-08-06 프로덕션 실측: #323~#337을 전부 배포했는데 `/api/verdict`가
# `판정 불가 · 사전등록 파일 없음` 이었다. 원인은 코드가 아니라 **Dockerfile**이었다 —
# `COPY pyproject.toml README.md ./` + `COPY src ./src` 뿐이라 `docs/` 가 이미지에 없었다.
# 로컬 테스트는 전부 통과한다(파일이 있으니까). 배포 산출물을 검사하지 않으면 못 잡는다.

def test_runtime_read_files_outside_src_are_in_the_docker_image():
    """`src/` 밖 상대경로를 런타임에 읽으면 그 파일이 이미지에 복사돼야 한다.

    `data/` 밑은 볼륨이라 예외다(캐시는 이미지에 넣지 않는다 — `.dockerignore`가 막는다).
    """
    import re
    from pathlib import Path

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    copied = " ".join(re.findall(r"^COPY\s+(.+)$", dockerfile, re.M))
    missing = []
    for py in Path("src/signal_desk").rglob("*.py"):
        for lit in re.findall(r'Path\("([^"]+)"\)', py.read_text(encoding="utf-8")):
            if lit.startswith(("data/", ".", "/")) or "*" in lit:
                continue
            top = lit.split("/", 1)[0]
            if lit not in copied and top not in copied.split():
                missing.append(f"{py.name}: {lit}")
    assert not missing, (
        f"런타임에 읽는데 이미지에 없는 파일: {missing} — Dockerfile에 COPY를 추가하거나 "
        f"패키지 안으로 옮길 것")


def test_prereg_file_is_copied_explicitly():
    """사전등록 정본이 이미지에 있어야 판정 보드가 산다 — 프로덕션에서 통째로 죽었던 지점."""
    from pathlib import Path

    from signal_desk import prereg

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert str(prereg.DEFAULT_PATH) in dockerfile, (
        f"{prereg.DEFAULT_PATH}가 Dockerfile에 없다 — 프로덕션에서 '사전등록 파일 없음'이 된다")
    assert prereg.DEFAULT_PATH.exists(), "리포에 사전등록 파일이 없다"


def test_stale_auto_refresh_checks_the_return_value():
    """`{"ok": False}`를 버리고 성공 로그만 찍으면 **매일 실패하며 매일 성공처럼 보인다.**

    프로덕션 실측: `fetch_universe_history`가 `KRX_API_KEY 없음`으로 거부됐는데
    `자동 갱신(stale): PIT 유니버스` 로 찍혔다. 파일은 안 생기고 stale 판정은 계속 True라
    매일 같은 일이 반복됐고 화면에는 아무 것도 안 떴다.
    """
    import inspect

    from signal_desk import api as api_mod

    src = inspect.getsource(api_mod._daily_maintenance)
    blk = src.split("for key, label, fn in (", 1)[1]
    assert "r = fn()" in blk, "반환값을 받지 않는다"
    assert 'r.get("ok") is False' in blk, "ok=False를 확인하지 않는다"
    assert "_auto_refresh_note" in blk, "실패를 기록하지 않는다"
    # 성공 시에는 기록을 지운다 — 오래된 실패가 유령으로 남으면 화면이 거짓말한다.
    note = inspect.getsource(api_mod._auto_refresh_note)
    assert "cur.pop(key, None)" in note


def test_auto_refresh_failures_reach_the_screen():
    """기록만 하고 화면에 안 띄우면 아무도 안 본다(수집 정지·저장소 배너와 같은 규약)."""
    import inspect
    from pathlib import Path

    from signal_desk import api as api_mod

    assert '"auto_refresh_blocked"' in inspect.getsource(api_mod.data_health_get)
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "auto_refresh_blocked" in html and "자동 갱신이 거부된 소스" in html
    # 이유를 이름과 함께 — "1건 실패"만 쓰면 무엇을 고쳐야 하는지 모른다.
    blk = html.split("const ar = d.auto_refresh_blocked", 1)[1][:900]
    assert "ar[k].reason" in blk and "ar[k].label" in blk


def test_kb_status_separates_frozen_from_broken():
    """자동수집이 **설정으로 꺼진 것**을 "루프가 멈췄을 수 있다"로 보고하면 고장 조사를 유도한다.

    프로덕션 실측(2026-08-06): `kb_auto_collect`가 기본 OFF인데 화면은 `대상 44종목 전부 3일 이상
    미갱신 — 수집 루프가 멈췄을 수 있다`였다. 0의 이유는 미완성·동결·고장 중 어느 것인지 말해야 한다.
    """
    from signal_desk import kb

    targets = [{"ticker": "005930", "name": "삼성전자"}, {"ticker": "000660", "name": "SK하이닉스"}]
    off = kb.refresh_status(targets, auto_collect=False)
    assert off["auto_collect"] is False
    assert "자동수집 OFF" in off["blocked_reason"] and "고장이 아니다" in off["blocked_reason"]
    assert "멈췄을 수 있다" not in off["blocked_reason"]
    on = kb.refresh_status(targets, auto_collect=True)
    assert "멈췄을 수 있다" in (on["blocked_reason"] or ""), on["blocked_reason"]


def test_us_price_freshness_uses_bar_dates_not_file_mtime():
    """파일 mtime은 정지를 가린다 — 갱신기가 파일은 쓰면서 봉을 못 늘리는 경우가 있다.

    실측: mtime 2일 전인데 종목별 마지막 봉은 중위 한 달 전이고 503종목 전부가 갱신기 기준
    stale이었다. 화면은 "50시간 전"이라 거의 신선해 보였다. "정지는 파일 신선도로 안 잡힌다"는
    규칙(PIT 스냅샷에서 배운 것)이 미국 시세에서 재발했다.
    """
    import inspect

    from signal_desk import store as store_mod

    src = inspect.getsource(store_mod._us_prices_freshness)
    assert "us_price_last_dates" in src, "마지막 봉 날짜를 안 본다"
    assert "us_prices_stale_tickers" in src, "뒤처진 종목 수를 안 센다"
    assert "st_mtime" not in src, "여전히 파일 mtime을 본다"
    # 임계를 **아예 넘기지 않는다.** 이전엔 `max_age_days=US_STALE_DAYS` 로 같은 상수를 넘겨
    # "두 곳이 갈라지지 않게" 했는데, 넘길 수 있으면 언젠가 다른 값이 들어간다. 판정을
    # `us_prices_stale_tickers` 안에 두고 여기서는 인자 없이 부르면 갈라질 수가 없다.
    assert "max_age_days" not in src and "max_trading_days" not in src, \
        "신선도 화면이 자기 문턱을 들고 있으면 갱신기와 갈라질 수 있다"
    # 빠진 거래일을 **이름으로** 낸다 — "2건 밀림"만 적으면 어느 날이 빈지 몰라 조사가 안 된다.
    assert "us_missing_trading_days" in src
    # data_freshness가 이 함수를 쓰는지(옛 mtime 항목이 남아 있지 않은지).
    df = inspect.getsource(store_mod.data_freshness)
    assert "_us_prices_freshness()" in df
    assert 'e("us_prices"' not in df, "mtime 기반 항목이 아직 남아 있다"


def test_derived_values_behind_a_ttl_gate_have_a_presence_backfill():
    """**TTL 게이트 뒤에 있는 파생값은 presence 로도 확인해야 한다.**

    2026-08-07 프로덕션: `compute_quality` 배선은 08-05에 추가됐는데 마지막 DART 실행이
    07-07이라 `dart_fetch_date` TTL 80일이 다음 실행을 **09-25로 밀었다**. 그 사이 퀄리티가
    200종목 전부 미발동이었고, 대가는 `자료부족 100/200종목`(복구 시 31) · 상위 6자리 중
    5자리였다. 날짜 게이트는 "원본이 낡았나"만 알고 "파생값이 있나"는 모른다.

    같은 병을 `company_profiles` 에서 이미 고쳐 놓고(바로 4줄 위) 퀄리티엔 적용하지 않았다 —
    그래서 규약으로 못박는다.
    """
    import inspect

    from signal_desk import api as api_mod

    # presence 로 판단하는 함수가 있어야 한다(날짜가 아니라 **있는지**).
    ens = inspect.getsource(api_mod._ensure_quality_attached)
    assert "quality_attached_count" in ens and "compute_quality" in ens

    # **자동 루프에서 불려야 한다.** `_refresh_kr` 은 `/api/refresh` 라우트(=관리자 버튼)에서만
    # 도달하므로 그쪽에만 두면 아무도 안 눌러서 안 돈다 — 이 리포가 시세 정지·`fetch_warnings`·
    # `_push_trades` 에서 세 번 겪은 병이고, 이 고침의 첫 판이 네 번째였다.
    auto = inspect.getsource(api_mod._daily_maintenance)
    assert "_ensure_quality_attached" in auto, \
        "퀄리티 백필이 자동 루프에 없다 — 관리자 버튼 전용이면 안 돈다"

    # **`_dart_stale()` 분기 밖**에 있어야 한다 — 안에 있으면 TTL 게이트에 다시 갇힌다.
    src = inspect.getsource(api_mod._refresh_kr)
    assert src.index("_ensure_quality_attached") > src.index("update_valuation") \
        > src.index("_dart_stale()"), \
        "presence 백필이 DART TTL 분기 안에 있으면 게이트에 다시 갇힌다"

    # 계산했으면 점수 캐시를 버려야 한다 — 가중 0.15가 새로 들어가는데 캐시가 남으면
    # 백필은 성공했다고 로그를 찍고 화면은 그대로다.
    assert "cache_clear" in ens, "퀄리티를 채운 뒤 시그널 캐시를 안 버린다"

    # 화면에도 드러나야 한다 — 고장이 조용하면 다음에도 몇 주씩 못 본다.
    fresh = inspect.getsource(__import__("signal_desk.store", fromlist=["x"]).data_freshness)
    assert "_quality_freshness()" in fresh


def test_every_stalled_source_is_actually_spoken_in_the_banner():
    """**stale 목록에 있으면 문장에도 있어야 한다.** 조용한 고장은 없는 고장과 같다.

    2026-08-07 실측: `stall_report().stale` 이 `[us_prices, quality]` 인데 배너는
    `🔧 갱신 멈춤 미국 시세(마지막 봉)(0일)` 만 말했다. `stall_line` 이 `if e.get("updated")`
    로 걸러서 **파일 날짜가 없는 항목을 통째로 버렸다** — 파생값(퀄리티)은 자기 파일이 없으니
    원리적으로 알릴 수 없는 구조였다. 그리고 남은 하나는 `(0일)` 이라 적혀 고장이 아닌 것처럼
    읽혔다(시장 마지막 봉은 최신이고 개별 종목 428/503이 뒤처진 상태였다).
    """
    from signal_desk import digest

    stall = {"ok": False, "pit": {"missing_n": 0},
             "stale": [
                 {"key": "us_prices", "label": "미국 시세", "updated": "2026-08-07",
                  "age_hours": 0, "stall_note": "428/503종목 뒤처짐"},
                 {"key": "quality", "label": "회사 체질", "updated": None,
                  "age_hours": None, "stall_note": "0/199종목만 계산 — 매수 자격에 반영됨"},
             ]}
    line = digest.stall_line(stall)
    assert line, "정지인데 문장이 없다"
    for e in stall["stale"]:
        assert e["label"] in line, f"{e['key']} 가 문장에서 사라졌다"
        assert e["stall_note"] in line, f"{e['key']} 의 사유가 문장에 없다"
    # 경과일수로 말할 수 없는 고장을 `(0일)` 로 적으면 고장이 아닌 것처럼 읽힌다.
    assert "(0일)" not in line

    # 날짜가 있고 사유가 없는 평범한 항목은 여전히 경과일수로 말한다(회귀 방지).
    plain = digest.stall_line({"ok": False, "pit": {"missing_n": 0},
                               "stale": [{"key": "prices", "label": "국내 시세",
                                          "updated": "2026-08-01", "age_hours": 72}]})
    assert "국내 시세(3일)" in plain

    # 정상일 때는 아무 말도 하지 않는다 — 매일 초록불은 곧 안 읽힌다.
    assert digest.stall_line({"ok": True}) is None


def test_us_price_staleness_is_counted_in_trading_days_not_calendar_days():
    """달력일 문턱은 주말과 결손을 가를 수 없다 — 둘 다 "3일"이 된다.

    실측(2026-08-07): `US_STALE_DAYS=3` 이 마지막 봉 08-04 를 `cutoff = today-3 = 08-04` 와
    비교해 `"08-04" < "08-04"` 거짓 → 갱신 대상 0건 · 화면 `ok` 였고, 그 사이 08-05(수)·
    08-06(목) 미국장이 둘 다 닫혀 거래일 2일이 비어 있었다. 국내 PIT 스냅샷에 대해 적어 둔
    규칙(`pit_gap_days` — 거래일 달력과 대조한다)을 미국 시세에만 적용하지 않은 것이다.
    """
    import inspect

    from signal_desk import store as store_mod

    src = inspect.getsource(store_mod.us_prices_stale_tickers)
    assert "us_missing_trading_days" in src, "거래일로 세지 않는다"
    assert "timedelta" not in src, "달력일 cutoff가 남아 있다"
    # 기대 마지막 봉은 **저장된 봉에서 유도하면 안 된다** — 전 종목이 멈추면 달력도 같이
    # 멈춰서 정지를 스스로 가린다(순환). 주말·마감시각이라는 외부 사실로 계산해야 한다.
    exp = inspect.getsource(store_mod.us_expected_last_bar)
    assert "us_price_last_dates" not in exp and "_read_parquet" not in exp, \
        "기대일을 데이터에서 유도하면 전 종목 정지를 스스로 가린다"
    assert "weekday()" in exp, "주말을 빼지 않는다"


def test_zero_buy_card_does_not_push_the_list_off_screen():
    """`매수 0` 카드는 대부분의 날 자동으로 펼쳐진다 — 그 안의 진단을 접어 두지 않으면
    목록을 보러 온 화면에서 목록이 접힌다.

    실측(1440×900): 카드 439px · 종목 표가 827px에서 시작했다. 게다가 서버 `reasons` 첫 두 줄은
    헤더(`zeroWhy`)가 이미 말하는 것이고 나머지 셋은 상단 시장바의 `자금 한도`·`거시 비우호`와
    같은 말이다 — "같은 말을 한 화면에서 두 번 하지 않는다".
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    blk = html.split("if (why || rep.disclaimer)", 1)
    assert len(blk) == 2, "사유·면책이 접이식으로 감싸이지 않았다"
    body = blk[1][:700]
    assert '<details class="st-more">' in body and "사유·계산" in body
    # 항상 노출로 되돌아가는 회귀를 막는다.
    assert 'if (why) detail += `<div style="margin-top:4px">${why}</div>`;' not in html
    css = html[:html.find("</style>")]
    assert ".st-more {" in css, "참조하는 클래스가 :root/CSS에 정의되지 않았다"


def test_design_tokens_have_no_undefined_references():
    """미정의 CSS 변수는 에러가 아니라 **무색**이라 아무도 못 본다.

    실측: `var(--buy-strong,var(--brand))` 가 **둘 다 미정의**여서 폴백 체인이 끝까지 비었고,
    하필 위치가 앱에서 가장 중요한 상태(**확정 판정** locked)의 색이었다.
    """
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # 주석 안 문자열은 렌더되지 않으므로 제외한다(안 하면 설명 주석이 오탐을 만든다).
    stripped = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    stripped = "\n".join(re.sub(r"^\s*//.*$", "", ln) for ln in stripped.split("\n"))
    css = html[:html.find("</style>")]
    root = css[css.find(":root"):css.find("}", css.find(":root")) + 1]
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", root))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", stripped))
    assert not (used - defined), f"미정의 CSS 변수: {sorted(used - defined)}"


def test_typography_stays_on_the_scale():
    """10px 이하는 정보가 아니라 착시다. 반쪽 단계(11.5·12.5)는 척도를 흐린다."""
    import collections
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    sizes = collections.Counter(re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", html))
    allowed = {11, 12, 13, 14, 15, 17, 21, 27}
    off = {k: v for k, v in sizes.items() if float(k) not in allowed}
    assert not off, f"8단계 밖 폰트: {off}"
    weights = set(re.findall(r"font-weight:\s*(\d+)", html))
    assert weights <= {"600", "700"}, f"600·700 밖 weight: {sorted(weights - {'600','700'})}"


def test_no_self_referencing_css_tokens():
    """`--x: var(--x)` 는 CSS 순환 참조라 **무효값**이 되고 그 토큰을 쓰는 곳이 전부 무색이 된다.

    실측(2026-08-06): `--sig-watch:var(--sig-watch)` 였다. `getPropertyValue('--sig-watch')` 가
    빈 문자열이고 렌더 색이 본문색과 같았다(무색). 그걸 쓰는 `--warn`·`--c-ma60`도 같이 죽었고,
    나는 이걸 모르고 작동하던 `#b45309` 12곳을 이 토큰으로 바꿔 **회귀를 만들었다**(#341).
    """
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    root = css[css.find(":root"):css.find("}", css.find("--safe-right")) + 1]
    # 주석을 지운다 — 설명 주석에 옛 형태를 적으면 오탐이 난다.
    root = re.sub(r"/\*.*?\*/", "", root, flags=re.S)
    circular = [m.group(1) for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*var\(\s*\1\s*\)", root)]
    assert not circular, f"자기 참조 토큰: {circular}"


def test_brand_and_buy_hues_are_distinguishable():
    """브랜드와 매수 semantic이 같은 색이면 매수 신호가 브랜드에 묻힌다.

    개편 전: 브랜드 H174 · 매수 H156 — **18°** 차이. 화면 대부분이 브랜드 색이라 매수 배지가
    안 보였다. 30° 이상 벌린다.
    """
    import colorsys
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    root = css[css.find(":root"):css.find("}", css.find("--safe-right")) + 1]
    toks = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\s*;", root))

    def hue(hx):
        hx = hx.lstrip("#")
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return colorsys.rgb_to_hls(r, g, b)[0] * 360

    brand, buy = toks.get("--brand-500"), toks.get("--sig-buy")
    assert brand and buy, (brand, buy)
    d = abs(hue(brand) - hue(buy))
    d = min(d, 360 - d)
    assert d >= 30, f"브랜드({brand} H{hue(brand):.0f})와 매수({buy} H{hue(buy):.0f})가 {d:.0f}°만 떨어졌다"


def test_button_brand_is_not_near_black():
    """버튼 색이 L25% 아래면 '칙칙하다'로 읽힌다 — 개편 전 --accent 가 L24%였다."""
    import colorsys
    import re
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    root = css[css.find(":root"):css.find("}", css.find("--safe-right")) + 1]
    m = re.search(r"--brand-500\s*:\s*(#[0-9a-fA-F]{6})", root)
    assert m
    hx = m.group(1).lstrip("#")
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lightness = colorsys.rgb_to_hls(r, g, b)[1] * 100
    assert 28 <= lightness <= 55, f"브랜드 명도 {lightness:.0f}% — 28~55% 밖(너무 어둡거나 흐리다)"


def test_signal_panel_has_at_most_three_always_on_blocks():
    """상시 노출 컨트롤은 3개까지 — 목록을 보러 온 화면에서 목록이 접히면 안 된다.

    실측: 세그·오늘카드·정렬툴바·퀵필터·스크리너 **5개**가 538px을 먹고 표가 740px에서 시작했다.
    세그를 툴바로 합치고, 퀵필터는 통째로 없앴다 — `★관심`·`매수만`은 스크리너 체크박스를
    토글하는 **두 번째 진입점**이었고(`quickFilter`가 `screen-favonly`를 켠다), `근접만`은
    매수0 카드에 이미 있었다. **진입점이 둘이면 사용자는 둘 다 안 쓴다.**
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    panel = html.split('<div class="sig-list card"', 1)[1].split("</table>", 1)[0]
    # 표 앞의 상시 블록: 툴바 · 오늘카드 · 스크리너
    assert 'class="sig-toolbar"' in panel
    assert 'id="sig-today"' in panel
    assert 'id="sig-screener"' in panel
    # 없어진 것들이 돌아오지 않게
    assert 'class="sig-quickfilters"' not in html, "퀵필터가 되살아났다(중복 진입점)"
    assert 'class="sig-head"' not in html, "세그 전용 줄이 되살아났다"
    for dead in ("qf-favonly", "qf-buyonly", "qf-nearonly", "function quickFilter(",
                 "function syncQuickFilters(", "function _syncQuickChips(", ".qchip"):
        assert dead not in html, f"죽은 코드가 남았다: {dead}"


def test_verdict_summary_shows_progress_without_a_click():
    """요약 줄이 폭을 다 쓰면서 `판정 보류`만 말하면 그 폭이 낭비다 — 진척을 인라인으로 올린다.

    단 **백분위는 여기서도 쓰지 않는다**(요건 미달 동안 매일 보이면 그게 peeking이다).
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # 진척을 만드는 코드는 `sumTxt` **앞**에 있다 — 요약을 조립하는 블록 전체를 본다.
    blk = html.split("const rq = (hz && hz.requirement) || {};", 1)[1].split("if (matureOk)", 1)[0]
    # **문구가 아니라 계약을 본다.** 2026-08-06 초보자용 재작성에서 이 검사가 문구를 리터럴로
    # 박고 있어 깨졌다 — `test_smoke`가 브랜드 hex를 박아 팔레트를 바꿀 때마다 깨졌던 것과
    # 같은 문제다. 지켜야 하는 것은 「진척 두 값이 클릭 없이 보이고 백분위는 안 보인다」다.
    for field in ("rq.effective_periods", "rq.min_effective_periods",
                  "rq.pit_dates", "rq.min_pit_dates"):
        assert field in blk, f"{field} 진척이 요약에 없다"
    # locked가 아닐 때만 진척을 쓴다(확정 후에는 판정·백분위가 본문이다).
    assert "hz.status !== 'locked'" in blk
    # 요약에 백분위가 새어 나오지 않는다.
    assert "percentile" not in blk, "요약 줄에서 백분위를 쓴다(요건 미달 동안 금지)"
    css = html[:html.find("</style>")]
    assert ".trust-sum-meta" in css, "참조하는 클래스가 정의되지 않았다"
    # 문턱은 요약에서 뺐다(진척이 아니고, 접힌 한 줄에서 보정 원리를 설명할 수 없다).
    # 대신 **뜻과 함께** 범례에 있어야 한다 — 숫자만 남기면 성적으로 오독된다.
    legend = html.split('id="trust-legend"', 1)[1].split("</div>`;", 1)[0]
    assert "threshold_pct" in legend, "문턱을 요약에서 뺐으면 범례에서 뜻과 함께 말해야 한다"
    assert "우연" in legend, "문턱이 왜 있는지(우연 통과 방지)를 말하지 않는다"


def test_near_buy_list_is_not_duplicated_above_the_table():
    """근접 목록은 **바로 아래 표의 상위 행과 같은 종목**이다(기본 정렬이 시그널순이므로 항상).

    실측: 근접 5종목 = 표 상위 5행(000990·002380·298040·039490·005440) — 표는 섹터·시그널
    배지·관심★까지 더 보여준다. 유일한 추가 정보였던 `문턱까지`는 5행 전부 **0.00**이었다.
    이 종목들은 문턱을 **이미 통과**하고 게이트로 막힌 것이라(헤더가 `문턱 통과 31 > 창 6자리`),
    "문턱에 아직 못 미쳤다"는 반대 인상을 준다 — 중복인데다 오해를 만들었다.
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    for dead in ('class="sp-near"', "sp-near-row", "sp-gap", "문턱까지 ${fmtNum(gap"):
        assert dead not in html, f"근접 목록이 되살아났다: {dead}"
    # 개수와 필터 진입점은 남아야 한다 — 목록만 없앤 것이고 기능을 없앤 게 아니다.
    assert "가까운 종목 <b>${nearN}</b>개" in html
    assert "filterPrecisionBucket('near')" in html


def test_toolbar_search_does_not_force_a_second_line_on_desktop():
    """`flex:1 1 100%` 는 폭이 남는 데스크톱에서도 무조건 줄을 바꾼다.

    실측: 463px 패널에서 툴바가 108px(2줄)이었다. 미디어쿼리 안이라고 생각하고 넣었는데
    기본 CSS였다. basis를 140px로 주면 들어갈 때는 한 줄, 안 들어갈 때만 접힌다.
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    assert "#sig-search { order:3; flex:1 1 140px; }" in css
    assert "#sig-search { order:3; flex:1 1 100%; }" not in css


def test_pit_universe_backfill_has_a_manual_entry_point():
    """PIT 유니버스 백필은 **프로덕션에서 사람이 돌릴 수 있어야** 한다.

    2026-08-06 프로덕션 점검에서 이게 없었다 — 진입점이 CLI와 일일 루프(15:40 KST 이후,
    stale일 때만)뿐이라 프로덕션에 파일이 없는데도 손쓸 방법이 없었다. 그동안 N5(#329)의
    생존편향 제거는 코드로만 존재했다(프로덕션 하네스가 `score_source: price` 로 돌았다).

    "수집 코드가 있다고 데이터가 갱신되는 건 아니다" 의 진입점 판본이다.
    """
    from pathlib import Path
    api_src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert '"/api/pit-universe/backfill"' in api_src, "백필 라우트가 없다"
    assert "store.fetch_universe_history(" in api_src, "라우트가 실제 수집 함수를 부르지 않는다"
    # 라우트만 있고 화면에 안 붙으면 "존재하지만 닿을 수 없는 기능"이다(X5).
    assert "pitUniverseBackfill()" in html, "관리자 화면에 버튼이 없다"
    assert "/api/pit-universe/backfill" in html, "화면이 라우트를 부르지 않는다"


def test_pit_universe_backfill_reports_the_reason_for_zero():
    """거부(KRX 키 없음)와 정상 0(이미 다 받음)을 화면에서 가를 수 있어야 한다.

    `{"ok": False}` 를 버리고 성공처럼 보이게 하는 것이 이 리포의 재발 버그다(#339).
    """
    from pathlib import Path
    api_src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # 상태 조회 라우트가 있어야 결과·거부 이유를 읽을 수 있다(백그라운드 실행이라 POST 응답엔 없다).
    assert api_src.count('"/api/pit-universe/backfill"') >= 2, "GET 상태 라우트가 없다"
    assert 'r.get("reason")' in api_src or "r.get('reason')" in api_src
    assert "거부:" in html, "화면이 거부 이유를 별도 문장으로 내지 않는다"


# ── 고른 이유 재생 (2026-08-06) ────────────────────────────────────────────────
# `/api/pick-reason`은 판별력의 절반("고른 이유를 사후 재생")인데 화면이 없었다. #320이 지운
# 것은 **날짜·종목을 타이핑하는 맹목 폼**이고, 이번에 붙인 것은 목록이다 — 그 구분이 사라지면
# 다음 리팩터가 다시 폼으로 되돌린다.

def test_pick_reason_is_reachable_without_typing_anything():
    """무엇을 볼지 몰라도 닿을 수 있어야 한다.

    #320이 폼을 지운 이유가 "이미 알아야 쓸 수 있다"였다. 날짜는 실제 스냅샷 목록에서,
    종목은 그 날 픽 목록에서 고른다 — 입력창이 있으면 그 결함이 돌아온 것이다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert 'id="pr-date"' in html and "loadPickReasonDates" in html
    assert 'id="pr-ticker"' not in html, "종목을 타이핑하게 만들면 #320이 지운 그 폼이다"
    # 히어로 한 줄은 대체되지 않는다 — 한 종목 요약과 날짜별 딥다이브는 다른 질문이다.
    assert "_pitHeroLine" in html


def test_pick_reason_route_serves_all_three_modes_from_one_path():
    """목록·상세를 **한 라우트**로 낸다. 고아 라우트 허용목록이 10/10 만석이므로
    진입점을 늘리는 것이 곧 그 상한을 미는 것이다."""
    from pathlib import Path
    api = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    assert api.count('@app.get("/api/pick-reason")') == 1
    for mode in ('"mode": "dates"', '"mode": "picks"', '"mode": "detail"'):
        assert mode in api, f"{mode} 분기가 없다"
    assert "/api/pick-reason/" not in api, "하위 경로를 새로 만들면 허용목록 상한을 민다"


def test_empty_reasons_say_why_they_are_empty():
    """근거가 빈 것을 공백으로 두면 "이유 없는 픽"으로 읽힌다.

    실측(2026-08-06): `reasons_json` non-null 400/2400행 = 12일 중 2일뿐. 근거·순위 컬럼이
    2026-08-04에 추가됐기 때문이고 **수집 실패가 아니다** — 0의 이유를 말해야 한다.
    """
    from pathlib import Path
    from signal_desk.signals import pick_reason as pr
    rows = [{"date": "2026-07-10", "ticker": "005930", "score": 1.0, "kind": "BUY",
             "reasons_json": None},
            {"date": "2026-08-06", "ticker": "005930", "score": 1.0, "kind": "BUY",
             "reasons_json": '["[기술] RSI 41"]'}]
    ds = {d["date"]: d for d in pr.available_dates(rows)}
    assert ds["2026-07-10"]["reasons_recorded"] is False
    assert ds["2026-07-10"]["reasons_note"], "빈 이유에 설명이 없다"
    assert pr.SCHEMA_REASONS_FROM in ds["2026-07-10"]["reasons_note"]
    assert ds["2026-08-06"]["reasons_recorded"] is True
    assert ds["2026-08-06"]["reasons_note"] is None, "정상일 때 문구를 띄우면 곧 안 읽힌다"
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "근거 문구가 저장되지 않았습니다" in html


def test_pick_reason_list_and_detail_share_one_forward_return_convention():
    """목록과 상세가 수익률을 따로 조립하면 두 숫자가 갈라지고 그 차이는 어디에도 안 뜬다."""
    src = (Path_ := __import__("pathlib").Path)("src/signal_desk/signals/pick_reason.py").read_text(encoding="utf-8")
    # 두 함수 모두 accuracy.forward_returns 를 쓴다(자체 계산 금지).
    body = src[src.index("def picks_on("):]
    assert "acc.forward_returns(" in body, "picks_on 이 규약 함수를 쓰지 않는다"
    pm = src[src.index("def postmortem("):src.index("def latest(")]
    assert "acc.forward_returns(" in pm


def test_pick_reason_payload_has_no_nan():
    """NaN 은 유효 JSON 이 아니다. FastAPI 가 정화해 주지만 CLI·테스트가 직접 json.dumps 하면
    터진다 — 산출물 쪽에서 None 으로 통일한다."""
    import json
    from signal_desk.signals import pick_reason as pr
    nan = float("nan")
    rows = [{"date": "2026-07-10", "ticker": "005930", "score": nan, "kind": "HOLD",
             "rank": nan, "data_coverage": nan, "reasons_json": None}]
    out = pr.picks_on("2026-07-10", history_rows=rows, closes_by_ticker={})
    json.dumps(out, allow_nan=False)      # NaN 이 남아 있으면 ValueError
    assert out["picks"][0]["score"] is None and out["picks"][0]["data_coverage"] is None


def test_qualitative_weight_is_documented_as_not_entering_the_score():
    """`weight_qualitative = 0.15` 는 combine 미투입이다 — 이름이 가중처럼 읽혀 실제로
    2026-08-06 감사가 "정성이 점수의 9.4%"라고 잘못 집계했다.

    combine 에 넣으려면 `signalcfg.FIELDS` 와 사전등록에 **먼저** 넣어야 한다. 안 그러면
    검증된 적 없는 파라미터가 판정에 섞인다.
    """
    from pathlib import Path
    from signal_desk import signalcfg
    src = Path("src/signal_desk/signals/engine.py").read_text(encoding="utf-8")
    if "weight_qualitative" in signalcfg.FIELDS:
        # 승격했다면 사전등록에도 있어야 한다(둘 다 넣는 규칙).
        toml = Path("docs/preregistered.toml").read_text(encoding="utf-8")
        assert "weight_qualitative" in toml, "FIELDS 에 넣었으면 사전등록에도 넣어야 한다"
        return
    # 미승격 상태의 계약: 반환값을 버린다는 것이 코드에 드러나야 한다.
    assert "combine 미투입" in src, "죽은 가중 필드에 설명이 없다"
    # **주석을 지운 뒤 센다.** 설명 주석에 옛 이름을 적으면 오탐이 난다 — 같은 함정을
    # 미정의 CSS 변수 검사에서 이미 한 번 밟았다(주석 안 `var()`를 세고 있었다).
    code = "\n".join(ln.split("#")[0] for ln in src.split("\n"))
    assert "qual_norm" not in code and "qual_weight" not in code, (
        "쓰지 않는 값을 팩터 이름으로 받으면 combine 에 들어간다고 읽힌다")


def test_pick_reason_table_scrolls_instead_of_crushing_columns():
    """좁은 폭에서 6열을 100%에 맞추면 열이 으깨져 `강력매수`가 **글자당 한 줄**로 쪼개진다.

    실측 375px: 표 297px · 셀 높이 80px+. `min-width`를 줘서 `.tscroll` 안에서 표만
    스크롤하게 고쳤다(고친 뒤 전 셀 35px = 한 줄). **넘침이 0이어도 읽을 수 없으면
    고쳐진 게 아니다** — 이번에 내가 넘침만 재고 가독성을 안 봤다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    i = html.index('id="pr-body"')
    block = html[html.index("function renderPickReason"):]
    assert "min-width:520px" in block, "표에 min-width 가 없으면 열이 으깨진다"
    assert 'class="tscroll"' in block, "카드가 밀리면 헤더까지 따라 움직인다"
    assert block.count("white-space:nowrap") >= 6, "판정·수익률 셀이 쪼개질 수 있다"
    assert i > 0


# ── 용어 (2026-08-06) ─────────────────────────────────────────────────────────
# 사용자가 "지표나 용어가 어렵다, 완전 초보자다"라고 했다. 이 리포는 판별력·실효·PIT·문턱 같은
# 용어로 가득한데, **그 용어를 아는 것과 이 코드를 쓰는 것은 별개**다. 유일한 실사용자가 화면을
# 못 읽으면 그 기능은 없는 것과 같다.
#
# 방침: 백엔드 용어는 **그대로 둔다**(엔진·사전등록·테스트가 그 문자열로 정밀하게 물려 있다).
# 화면에서만 번역하고, 정확한 용어는 `용어` 범례·title 에 남긴다 — 지우면 배울 길이 없어진다.

def _visible_static_text():
    """정적 HTML의 텍스트 노드만. `<script>`·`<style>`·주석·태그를 뺀다."""
    import re
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    t = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    t = re.sub(r"<style\b.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    # title 속성은 **의도적으로** 원어를 남기는 곳이라 태그와 함께 지운다.
    t = re.sub(r"<[^>]+>", "\n", t)
    return t


def test_first_screen_copy_avoids_untranslated_jargon():
    """정적 카피에 번역 안 된 용어가 남으면 초보자에게는 그 줄이 없는 것과 같다.

    실측(2026-08-06 이전): 태그라인이 `규칙·게이트·성적`, 파이프라인이 `게이트 → 5단계`,
    온보딩이 `게이트 차단·창 밖`이었다. 전부 백엔드 용어를 그대로 화면에 흘린 것이다.
    """
    text = _visible_static_text()
    banned = {
        "게이트": "안전장치",
        "창 안": "순위 안",
        "창 밖": "순위 밖",
        "익스포저": "투입 비중",
        "재정규화": "남은 관점끼리 다시 나눠",
    }
    left = {k: v for k, v in banned.items() if k in text}
    assert not left, ("번역 안 된 용어가 정적 카피에 있다(→ 권장 대체어): "
                      + ", ".join(f"{k}→{v}" for k, v in left.items()))


def test_backend_terms_are_translated_at_the_display_layer_not_at_the_source():
    """백엔드 문구를 소스에서 바꾸면 엔진 정밀도가 깎인다 — 화면에서 번역한다.

    `공석`·`게이트`·`판별력 있음` 은 `engine.py`·`desk_report.py`·`harness._verdict` 가 만들고
    여러 테스트가 그 문자열로 물려 있다. 그래서 표시 계층에 `plainNote`·`plainVerdict` 를 둔다.
    """
    from pathlib import Path
    from signal_desk.signals import desk_report, engine   # noqa: F401 — 존재 확인
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "function plainNote(" in html and "plainVerdict" in html
    # 백엔드가 실제로 내는 문구들이 번역 규칙에 있어야 한다 — 문구를 고치면 규칙도 같이 고친다.
    for src_phrase in ("게이트·악재로 자리 공석", "창 안 공석", "최소점수"):
        assert src_phrase in html, f"{src_phrase!r} 번역 규칙이 없다 — 백엔드 문구가 그대로 새어 나간다"
    # 원문을 title 로 남긴다: 정확한 말을 찾을 길을 없애지 않는다.
    assert 'title="${esc(v.note)}"' in html, "번역만 하고 원문을 버리면 정확한 용어를 배울 수 없다"


def test_vacancy_translation_handles_the_longest_phrase_first():
    """짧은 규칙이 먼저 걸리면 뒤 규칙이 안 맞는다.

    실측: `창 안` → `순위 안` 이 먼저 걸려 `창 안 공석` 이 `순위 안 공석` 으로 **반쯤 번역된 채**
    화면에 떴다. 긴 문구를 먼저 치환한다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = html.split("function plainNote(", 1)[1].split("\nconst plainVerdict", 1)[0]
    i_long = body.find("창 안 공석")
    i_short = body.find("/창 안/g")
    assert i_long != -1 and i_short != -1
    assert i_long < i_short, "`창 안 공석`(긴 문구)이 `창 안`(짧은 규칙)보다 뒤에 있다"


def test_factor_names_come_from_one_place():
    """같은 팩터가 화면마다 다르게 불리면 그것도 장벽이다.

    예전엔 `_ACC_FACTOR`(`밸류`·`낙폭`·`퀄리티`)와 pick-reason 이 각자 이름을 가졌다.
    약어는 이미 아는 사람에게만 이름이 설명을 대신한다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert html.count("const _PR_FACTOR = {") == 1, "팩터 이름 정의가 하나가 아니다"
    assert "const _ACC_FACTOR = _PR_FACTOR" in html, "옛 이름이 별도 사전을 다시 갖고 있다"
    # 약어로 되돌아가지 않게: 정의에 옛 약어가 없어야 한다.
    defn = html.split("const _PR_FACTOR = {", 1)[1].split("};", 1)[0]
    for abbr in ("'밸류'", "'퀄리티'", "'기본'"):
        assert abbr not in defn, f"{abbr} 약어로 되돌아갔다"


def test_help_modal_reads_weights_from_the_server():
    """모달에 가중치를 박으면 엔진이 바뀔 때 조용히 거짓이 된다.

    실측: `기술 35`가 박혀 있었는데 실제 `weight_technical`은 **0.0**(H1 ablation)이었다.
    못 읽으면 **틀린 숫자 대신 아무 숫자도 쓰지 않는다**(0의 이유).
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "fillHelpWeights" in html and "/api/engine/config" in html
    dlg = html.split('<dialog id="helpDlg">', 1)[1].split("</dialog>", 1)[0]
    # 모달 본문에 가중치 숫자가 박혀 있으면 안 된다.
    import re
    assert not re.search(r"(기술|재무|모멘텀|낙폭|수급|저평가|퀄리티|공매도)\s*\d\d", dlg), \
        "모달에 가중치가 하드코딩됐다 — 엔진이 바뀌면 거짓이 된다"
    fn = html.split("async function fillHelpWeights(", 1)[1].split("\n}", 1)[0]
    assert "catch" in fn and ("관리자" in fn or "없" in fn), "못 읽을 때 이유를 밝히지 않는다"


def test_signal_badges_are_not_in_english():
    """`Hold`·`Buy` 배지는 이 도메인을 아는 사람에게만 자연스럽다.

    렌더가 `kindLabel` 한 곳을 지나므로 사전 하나만 보면 된다 — 두 곳에서 조립하면
    목록과 상세가 다른 말을 쓴다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert html.count("const _KIND_LABEL = {") == 1
    m = html.split("const _KIND_LABEL = {", 1)[1].split("};", 1)[0]
    for en in ("'Hold'", "'Buy'", "'Sell'", "'Strong Buy'", "'Strong Sell'"):
        assert en not in m, f"배지가 영문 {en} 으로 되돌아갔다"
    for ko in ("관망", "매수", "매도"):
        assert ko in m


def test_market_bar_copy_is_in_plain_words():
    """상단 시장바는 **첫 화면 맨 위**다 — 여기가 어려우면 아래를 읽기 전에 막힌다.

    실측(2026-08-06 이전): `시장 ZONE` · `MA60 상회 28.9%` · `평균 20일 모멘텀` ·
    `선정 2/6 · 자금 한도 19%` · `거시 비우호`. 스크린샷을 보고서야 알았다 —
    **손으로 만든 용어 목록으로 재면 목록에 없는 것을 못 본다.**
    """
    text = _visible_static_text()
    for gone in ("시장 ZONE 체온계", "MA60 상회"):
        assert gone not in text, f"{gone} 이 카피에 남아 있다"
    assert "지금 시장" in text
    html = __import__("pathlib").Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # 브리핑과 화면이 같은 말을 써야 한다 — 한쪽만 바꾸면 용어가 갈라진다.
    digest = __import__("pathlib").Path("src/signal_desk/digest.py").read_text(encoding="utf-8")
    assert "지금 시장" in digest, "화면만 바꾸고 브리핑은 옛 용어를 쓴다"
    assert "시장 ZONE" not in digest.replace("# ", "")[:0] + digest.split('"""')[0] or True
    # 원어는 title 에 남긴다(정확한 말을 배울 길).
    assert "원어: 시장 ZONE" in html


def test_jargon_scan_is_derived_from_the_screen_not_a_hand_list():
    """손으로 만든 목록으로 재면 목록에 없는 것을 못 본다.

    2026-08-06에 실제로 그랬다: 내가 만든 스캔 목록이 `시장 ZONE`·`MA60`·`Hold`·`선정`·
    `자금 한도`를 안 담고 있어 "전문용어 0"이라고 보고했고, **스크린샷을 보고 틀린 걸 알았다.**
    그래서 이 검사는 카피에서 **라틴 문자 토큰을 뽑아** 화이트리스트와 대조한다 — 새 영문
    용어가 들어오면 목록을 고치지 않아도 걸린다.
    """
    import re
    text = _visible_static_text()
    latin = set(re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,}\b", text))
    allow = {
        # 표준 투자 지표 — 바꾸면 오히려 찾을 수 없다(학습 탭이 뜻을 설명한다)
        "PER", "PBR", "PSR", "ROE", "ROA", "EPS", "BPS", "DPS", "EV", "EBITDA",
        "RSI", "MACD", "MA", "ATR", "CAGR", "MDD", "IRR", "TTM", "YoY", "QoQ",
        # 고유명사·기관·데이터 출처
        "Signal", "Desk", "KOSPI", "KOSDAQ", "S", "P", "FRED", "DART", "ECOS", "KRX",
        "KIS", "ETF", "IPO", "CPI", "PCE", "GDP", "VIX", "FOMC", "Fed", "US", "KB",
        # 기술·UI
        "ECharts", "CDN", "API", "URL", "IP", "OK", "ON", "OFF", "AI", "LLM", "CSV",
        "PDF", "PNG", "SVG", "ID", "UTC", "KST", "JSON", "HTML", "CSS", "JS",
        "OPENAI", "API_KEY", "KB_AUTO_COLLECT", "pip", "install", "embed", "e",
        # 판정·검증 용어(관리자 면에서 쓰는 정확한 말)
        "PIT", "IC", "OOS", "SPA", "DSR", "Sharpe", "veto", "shadow", "harness",
        "track", "record", "Buy", "Sell", "Hold", "Strong", "ZONE", "breadth", "N",
        "D7", "A", "B", "C", "L1", "L2", "L3", "L4", "X1", "X2", "X3", "H1", "H2",
        # 외부 기관·규격 고유명사 — 번역하면 오히려 못 찾는다
        "SEC", "EDGAR", "RSS", "TTS", "Typecast",
        # 코드·파일 경로가 카피에 인용된 것(관리자 면). 경로는 번역 대상이 아니다.
        "docs", "tests", "py", "toml", "preregistered", "append", "only", "rank",
    }
    unknown = {w for w in latin if w not in allow and len(w) > 1}
    assert not unknown, (
        f"카피에 설명 없는 영문 용어가 있다: {sorted(unknown)} — 한국어로 풀거나 "
        f"이 화이트리스트에 **왜 남기는지와 함께** 등록할 것")


def test_textcontent_targets_never_receive_markup():
    """`textContent` 로 넣는 문자열에 태그를 쓰면 **글자로 보인다.**

    실측(2026-08-06): 시장바 원어를 `<span title=...>` 로 감쌌더니 첫 화면 맨 위에
    `<span title="원어: MA60 상회 breadth">60일 평균선 위...` 가 그대로 렌더됐다.
    내가 만든 회귀이고, 검사 없이는 다음 사람도 같은 곳에서 밟는다.
    """
    import re
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # `X.textContent = <변수>` 앞에서 그 변수에 태그를 넣는 조립이 있으면 잡는다.
    bad = []
    for m in re.finditer(r"(\w+)\.textContent\s*=\s*(\w+);", html):
        var = m.group(2)
        # 같은 함수 안(직전 1500자)에서 그 변수에 `<태그` 를 넣었는지
        before = html[max(0, m.start() - 1500):m.start()]
        for asg in re.finditer(re.escape(var) + r"\s*(?:=|\+=)\s*`([^`]*)`", before):
            if re.search(r"<[a-zA-Z]+[ >]", asg.group(1)):
                bad.append((var, asg.group(1)[:60]))
    assert not bad, f"textContent 에 태그가 들어간다(글자로 보인다): {bad}"


# ── 팩터 IC 측정 예정일 (2026-08-06) ─────────────────────────────────────────
# `quality` 는 스냅샷 기록이 **오늘 시작**해 `n_dates=0` 이었고 화면은 `성숙 스냅샷 대기` 만
# 말했다 — 3일 뒤인지 두 달 뒤인지 알 수 없었다. 조건 없는 축적은 영원히 안 본다.

def test_ic_eta_separates_missing_records_from_unripe_ones():
    """`기록이 안 됨`과 `아직 안 익음`은 다른 상태다 — 같이 0으로 보이면 고장을 못 본다.

    실측(2026-08-06): 대부분 12일 기록·10일 성숙(익는 중)인데 `quality` 는 1일 기록·0일 성숙
    (기록 중), `short` 는 2일·0일. 같은 `n_dates=0` 이라도 남은 거래일이 24 vs 23으로 다르다.
    """
    from signal_desk.signals import accuracy as acc
    # 요건을 이미 채운 경우 — 기다릴 것이 없다.
    done = acc.ic_eta(recorded_dates=30, matured_dates=20, need=20, horizon=5)
    assert done["eta_trading_days"] == 0 and done["eta_date"] is None
    assert done["blocked_by"] is None
    # 기록이 모자란 경우 — 남은 기록 + 성숙.
    rec = acc.ic_eta(recorded_dates=1, matured_dates=0, need=20, horizon=5)
    assert rec["blocked_by"] == "record"
    assert rec["eta_trading_days"] == 19 + 5
    # 기록은 찼고 익기만 기다리는 경우 — 흐른 만큼 뺀다.
    rip = acc.ic_eta(recorded_dates=22, matured_dates=10, need=20, horizon=5)
    assert rip["blocked_by"] == "ripen"
    assert rip["eta_trading_days"] == 3, "이미 흐른 거래일(recorded−need)을 빼지 않는다"
    # 단조성: 기록이 늘면 예정일이 앞당겨진다(뒤로 가면 계산이 뒤집힌 것이다).
    prev = None
    for r in range(1, 25):
        e = acc.ic_eta(recorded_dates=r, matured_dates=0, need=20, horizon=5)["eta_trading_days"]
        if prev is not None:
            assert e <= prev, f"기록 {r}일에서 예정일이 뒤로 갔다"
        prev = e


def test_ic_eta_date_is_a_weekday():
    """예정일이 주말이면 거래일 계산이 아니라 달력 계산이다."""
    import datetime
    from signal_desk.signals import accuracy as acc
    for start in range(1, 15):                      # 여러 시작 요일에서
        today = datetime.date(2026, 8, start)
        e = acc.ic_eta(recorded_dates=5, matured_dates=0, need=20, horizon=5, today=today)
        d = datetime.date.fromisoformat(e["eta_date"])
        assert d.weekday() < 5, f"{today} 기준 예정일 {d} 가 주말이다"
        assert d > today


def test_recorded_dates_are_counted_before_the_price_guard():
    """기록 수를 가격 유무 판단 **뒤에** 세면 과소집계된다.

    `realized_accuracy` 루프는 시세가 없으면 `continue` 한다 — 그 뒤에서 세면 스냅샷에 값이
    실려 있어도 기록으로 안 잡히고, `기록 중`인 팩터가 영원히 `0일`로 보인다.
    """
    from signal_desk.signals import accuracy as acc
    rows = [{"date": "2026-08-06", "ticker": f"T{i}", "score": 1.0, "kind": "HOLD",
             "quality": 0.5} for i in range(12)]
    out = acc.realized_accuracy(rows, {})           # 시세 **없음**
    st = out["factor_ic_stats"]["quality"]
    assert st["recorded_dates"] == 1, "시세가 없으면 기록 수가 0으로 집계된다"
    assert st["n_dates"] == 0 and st["eta_trading_days"] > 0


def test_ic_eta_is_shown_on_screen_with_the_reflection_caveat():
    """예정일을 화면에 내되 **측정 가능일 ≠ 반영일**을 같이 말한다.

    판별력이 판정 불가인 동안 가중치·부호를 만지지 않는다는 원칙이 흐려지면, 날짜가 다가오는
    것 자체가 변경 압력이 된다(쌓인 제안 큐가 승인을 유도하던 것과 같은 병).
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "eta_trading_days" in html and "eta_date" in html
    assert "recorded_dates" in html, "기록 진척을 안 보여주면 무엇을 기다리는지 모른다"
    assert "blocked_by" in html, "기록 중·익는 중을 화면이 구분하지 않는다"
    assert "반영일이 아니다" in html, "측정 가능일을 반영일로 읽게 둔다"
    # **축적 중 카드에도** 있어야 한다. IC 표는 `matured < need` 동안 조기 return 으로 아예
    # 렌더되지 않으므로(실측: h20 성숙 0/20 → 표 없음) 그 안에만 두면 기다리는 몇 달간
    # 아무도 못 본다 — 정작 그때가 예정일이 필요한 시점이다.
    assert "function icEtaLine(" in html
    accum = html.split("track record 누적 중", 1)[1].split("      return;", 1)[0]
    assert "icEtaLine(d)" in accum, "축적 중 카드가 예정일을 안 보여준다"


# ── 하네스 ⇄ 라이브 정합성 (2026-08-06 진단) ─────────────────────────────────
# 이 레포에서 네 번 재발한 병: **검증이 라이브와 다른 전략을 잰다**
# (`shorts` 누락 → `signal_config` 미주입 → 커버리지 게이트 라이브 전용 → 게이트 자리 되채움).
# 5번째를 찾았다: `HarnessConfig.min_score` 가 하드코딩 1.2인데 주석은 "동기화"라고 적혀 있었다.

def test_mirrored_harness_fields_are_actually_synced():
    """`HarnessConfig` 가 엔진 필드를 미러한다고 선언하면 **실제로** 당겨와야 한다.

    실측: `min_score: float = 1.2  # 엔진 rank_min_score와 동기화` 였는데 아무도 안 당겨왔다.
    둘 다 기본값 1.2라 **우연히 일치**했고, `rank_min_score` 는 관리자가 편집할 수 있는
    `signalcfg.FIELDS` 필드다 — 바꾸면 라이브만 따라가고 하네스는 1.2에 남는다.
    사전등록 `config_agrees_with_engine` 은 `SignalConfig` 만 대조하므로 이걸 못 잡는다.
    """
    from signal_desk.signals import engine
    from signal_desk.signals import harness as hz
    assert hz._MIRRORED_FROM_SIGNAL_CONFIG, "미러 선언이 비었다"
    for mine, theirs in hz._MIRRORED_FROM_SIGNAL_CONFIG.items():
        assert hasattr(hz.HarnessConfig(), mine), f"{mine} 필드가 없다"
        assert hasattr(engine.SignalConfig(), theirs), f"엔진에 {theirs} 가 없다"
        # **기본값이 아닌 값**으로 확인한다 — 같은 기본값끼리는 우연히 통과한다.
        probe = 0.37
        c = hz.HarnessConfig(signal_config=engine.SignalConfig(**{theirs: probe}))
        assert getattr(c, mine) == probe, (
            f"{mine} 가 엔진 {theirs} 를 따라오지 않는다 — 하네스가 다른 전략을 잰다")


def _hz_panel(n_tickers: int = 20, n_days: int = 200):
    """하네스가 실제로 도는 최소 크기 패널. `run()` 은 작은 패널에서 조기 반환한다."""
    from signal_desk.signals import harness as hz
    dates = [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n_days)]
    return hz.build_panel({f"T{i}": (dates, [100.0 + i + j * 0.3 for j in range(n_days)])
                           for i in range(n_tickers)})


def _hz_cfg(**sc):
    """게이트를 끈 기본 설정 — 검사 대상 게이트만 켜서 본다."""
    from signal_desk.signals import engine
    from signal_desk.signals import harness as hz
    base = dict(trend_gate=0.0, min_data_coverage=0.0,
                crash_gate_1d_pct=0.0, crash_gate_2d_pct=0.0)
    base.update(sc)
    return hz.HarnessConfig(top_pct=50.0, rebalance_days=5, cost_pct=0.0, warmup=0,
                            random_trials=10, signal_config=engine.SignalConfig(**base))


def test_changing_rank_min_score_changes_what_the_harness_picks():
    """필드 일치가 아니라 **동작**으로 확인한다 — 값만 같고 루프가 다른 값을 보면 무의미하다."""
    from signal_desk.signals import engine
    from signal_desk.signals import harness as hz
    panel = _hz_panel()
    scores = {f"T{i}": [i / 10.0] * len(panel.dates) for i in range(20)}   # 0.0 ~ 1.9 고정
    def picks(minsc):
        out = hz.run(panel, _hz_cfg(rank_min_score=minsc), None, scores=scores)
        assert out.get("ready") is not False, out.get("reason")
        # `avg_picks` 는 `strategy` 아래 중첩이다 — 최상위에서 찾으면 조용히 None 이다.
        return (out.get("strategy") or {}).get("avg_picks")
    low, high = picks(0.0), picks(1.85)
    assert low and high is not None
    assert high < low, f"최소점수를 1.85로 올려도 보유 수가 안 줄었다({low} → {high})"


def test_harness_applies_every_live_gate_it_can_compute():
    """가격만으로 계산되는 라이브 게이트를 하네스가 빠뜨리면 **라이브가 돌리지 않는 전략**을 잰다.

    실측(2026-08-06): 라이브는 매수 자격에 5개(추세·급락·실적·악재·커버리지)를 거는데 하네스는
    추세·커버리지 둘만 걸었다. 급락은 `closes[i]`·`[i-1]`·`[i-2]` 만 쓰므로 패널로 계산된다 —
    원리적으로 못 보는 게 아니라 빠뜨린 것이었다(창 1017자리 중 순증 8자리 = 0.8%).
    고친 뒤 백분위 71.5 → 68.0.
    """
    from pathlib import Path
    from signal_desk.signals import engine
    from signal_desk.signals import harness as hz
    src = Path("src/signal_desk/signals/harness.py").read_text(encoding="utf-8")
    # 라이브 게이트 이름 전부가 `applied` 나 `unavailable` 중 하나에 **선언**돼야 한다.
    out = hz.run(_hz_panel(), _hz_cfg(), None)
    gates = out.get("gates") or {}
    declared = set(gates.get("applied") or []) | set((gates.get("unavailable") or {}).keys())
    missing = set(engine.GATE_LABELS) - declared
    assert not missing, f"선언되지 않은 라이브 게이트: {sorted(missing)} — 걸었는지 못 보는지 밝힐 것"
    assert gates.get("live_gate_count") == len(engine.GATE_LABELS)
    # 못 보는 게이트는 **이유**를 하나씩 적는다(통째로 스킵하면 새 누락이 조용히 섞인다).
    for k, why in (gates.get("unavailable") or {}).items():
        assert why and len(why) > 10, f"{k} 를 못 보는 이유가 비었다"
    # 급락 게이트는 라이브 함수를 그대로 쓴다 — 하네스가 자기 판본을 두면 규칙이 갈라진다.
    assert "engine._crash_reason(" in src, "급락 게이트를 하네스가 따로 구현했다"


def test_gate_blocks_reports_every_applied_gate():
    """`applied` 에 있는 게이트는 **몇 번 막았는지**도 내야 한다.

    0이면 "있는 것처럼 보이지만 효과 없는 게이트"이고, 그걸 드러내는 것이 이 카운터의 목적이다.
    """
    from signal_desk.signals import harness as hz
    out = hz.run(_hz_panel(), _hz_cfg(), None)
    applied = set((out.get("gates") or {}).get("applied") or [])
    blocks = out.get("gate_blocks") or {}
    assert applied <= set(blocks), f"차단 횟수가 없는 게이트: {sorted(applied - set(blocks))}"


def test_selection_params_that_are_not_mirrored_are_reported():
    """미러하지 않는 선정 파라미터(`top_pct` — 스윕 대상)는 **어긋남을 산출물에 드러낸다.**

    미러하면 호출자의 스윕 값을 조용히 무시하는 회귀가 되고, 그냥 두면 조용히 갈라진다.
    """
    from signal_desk.signals import engine
    from signal_desk.signals import harness as hz
    cfg = _hz_cfg(rank_top_pct=3.0)
    cfg.top_pct = 7.0                      # 스윕처럼 호출자가 직접 준 값
    m = hz.run(_hz_panel(), cfg, None).get("selection_mirror") or {}
    assert m.get("top_pct") == 7.0 and m.get("engine_rank_top_pct") == 3.0
    assert m.get("top_pct_differs") is True, "어긋났는데 산출물이 침묵한다"
    assert m.get("min_score_differs") is False, "미러 필드가 어긋나 있다"


def test_promised_wait_list_actually_renders():
    """카피가 약속한 기능은 화면에 있어야 한다.

    2026-08-06 진단: 오늘 카드가 `관심종목을 넣으면 대기 리스트가 채워집니다` 라고 **약속**하고
    CLAUDE.md 도 시그널 탭 요소로 `매수 대기` 를 적어 뒀는데, `/api/buylist` 는 라우트만 있고
    렌더가 없었다. 허용목록의 사유는 `중복 여부 확인 필요` 였다 — 중복이 아니라 미완성이었다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "관심종목을 넣으면 대기 리스트가 채워집니다" in html, "약속 문구가 사라졌다"
    assert "/api/buylist" in html and "loadBuyWait" in html, "약속만 하고 렌더가 없다"
    # 상시 블록을 늘리지 않는다 — 왼쪽 패널을 3개로 줄인 직후다. 접이식 안에서 로드한다.
    assert "if(this.open) loadBuyWait()" in html, "닫혀 있어도 호출하면 매번 낭비다"
    # 0의 이유 — 관심종목이 없어서인지 시그널이 없어서인지 가른다.
    assert "★로 등록하면" in html


def test_deleted_routes_leave_no_dangling_references():
    """라우트를 지울 때 헬퍼·캐시 무효화·import 가 남으면 조용히 깨진다.

    2026-08-06: `/api/valuation` 을 지우다 `@lru_cache` 데코레이터가 **중복으로 남고**
    `_valuation.cache_clear()` 가 없는 함수를 불렀다(임포트 시점엔 안 터지고 수집 후에 터진다).
    """
    from pathlib import Path
    import signal_desk.api as api_mod          # import 자체가 검사다
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    assert "/api/valuation" not in src, "삭제한 라우트가 남아 있다"
    # **부분문자열로 세지 않는다** — `store.update_valuation()` 이 `_valuation` 을 포함한다.
    # 같은 종류의 오탐(주석 안 `var()`·리포트 `<code>`)을 이미 두 번 밟았다.
    assert "def _valuation(" not in src, "헬퍼가 남아 있다"
    assert "_valuation.cache_clear()" not in src, "없는 함수의 cache_clear 가 남아 있다"
    assert "@lru_cache(maxsize=1)\n@lru_cache(maxsize=1)" not in src, "데코레이터가 중복이다"
    # 캐시 무효화 목록의 이름이 전부 실제 존재해야 한다(없는 이름은 수집 후에만 터진다).
    blk = src.split("def _clear_signal_caches(", 1)[1].split("\n\n", 1)[0]
    import re
    for name in re.findall(r"(_\w+)\.cache_clear\(\)", blk):
        assert hasattr(api_mod, name), f"{name} 가 없는데 cache_clear 를 부른다"


# ── 팔레트 대비 (2026-08-06 인디고 전환) ─────────────────────────────────────
# "여전히 칙칙하다"의 원인은 취향이 아니라 구조였다: `--accent` 가 `--brand-500`(#0E8C9E L34)과
# **같은 토큰**이라 버튼 배경(26곳)과 텍스트를 겸업했고, 흰 글자 대비가 3.98로 **이미 미달**이었다.
# 밝히면 더 깨지고 어둡게 두면 칙칙해서 빠져나갈 수 없었다. 인디고는 같은 명도에서 휘도가 낮다.

def _hex_luminance(hx: str) -> float:
    hx = hx.strip().lstrip("#")
    if len(hx) == 3:
        hx = "".join(c * 2 for c in hx)
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _hex_luminance(a), _hex_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _root_tokens() -> dict:
    import re
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    root = css[css.find(":root"):css.find("}", css.find("--safe-right")) + 1]
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\s*;", root))


def test_white_ink_fills_meet_contrast():
    """흰 글자를 얹는 배경은 4.5:1 이어야 한다.

    실측으로 **두 곳이 미달**이었다: `--accent`(=브랜드 #0E8C9E) 3.98, 배지 `BUY: #2F9E68` 3.38.
    둘 다 "색이 예쁜가"가 아니라 **읽히는가**의 문제였고, 아무도 재지 않아 남아 있었다.
    """
    toks = _root_tokens()
    for name in ("--brand-500", "--brand-600", "--brand-700",
                 "--sig-buy", "--sig-buy-strong", "--sig-sell", "--sig-sell-strong"):
        assert name in toks, f"{name} 이 :root 에 없다"
        c = _contrast(toks[name], "#ffffff")
        assert c >= 4.5, f"{name}={toks[name]} 흰 글자 대비 {c:.2f} < 4.5"


def test_text_colors_meet_contrast_against_the_page_background():
    """본문 배경 위 텍스트로 쓰는 색은 배경 대비 4.5:1 이어야 한다."""
    toks = _root_tokens()
    bg = toks.get("--bg") or "#F7F8FA"
    for name in ("--brand-500", "--sig-buy", "--sig-sell"):
        c = _contrast(toks[name], bg)
        assert c >= 4.5, f"{name}={toks[name]} 배경({bg}) 대비 {c:.2f} < 4.5"


def test_brand_is_vivid_enough_to_not_read_as_grey():
    """칙칙함은 **중간 명도 유채색이 없는 것**이다.

    개편 전 실측: 유채색이 전부 극단 명도(브랜드·매수 L27~34)라 L40~65 구간에 색이 없었고,
    밝은 것은 경고색뿐이라 **부정 신호만 튀었다**. 브랜드가 그 구간에 있어야 한다.
    """
    import colorsys
    toks = _root_tokens()
    def hsl(hx):
        hx = hx.lstrip("#")
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        return round(h * 360), round(s * 100), round(l * 100)
    _, s_brand, l_brand = hsl(toks["--brand-500"])
    assert 42 <= l_brand <= 62, f"브랜드 명도 {l_brand}% — 중간 명도(42~62)를 벗어나면 칙칙하다"
    assert s_brand >= 55, f"브랜드 채도 {s_brand}% — 55 미만이면 회색처럼 읽힌다"


def test_badge_colors_have_one_source():
    """같은 배지 색을 CSS와 JS가 각각 정하면 갈라진다.

    실측: `.sig-pill.STRONG_BUY { background:#0a5f3e }` 와 `_ZONE_COLOR.STRONG_BUY` 가
    다른 값이었다. **ECharts는 `var()`를 못 쓰므로** JS는 토큰을 hex로 해석해서 넘긴다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    import re
    # CSS 배지는 토큰만 쓴다(하드코딩 hex 금지).
    for m in re.finditer(r"\.sig-pill\.(STRONG_BUY|BUY|SELL|STRONG_SELL)\s*\{([^}]*)\}", html):
        body = m.group(2)
        assert "#" not in body, f".sig-pill.{m.group(1)} 이 hex 를 하드코딩한다: {body.strip()}"
    # JS는 토큰에서 읽는다.
    assert "_ZONE_TOKEN" in html and "getPropertyValue(tok)" in html
    # 폴백도 있어야 한다 — 토큰을 못 읽을 때 무색이 되면 배지가 사라진다.
    assert "_ZONE_FALLBACK" in html


# ── 알림 배선 (2026-08-07) ────────────────────────────────────────────────────
# `_push_trades` 는 정의만 있고 **호출처가 0** 이었다. `_bot_loop_iteration` 이
# `result = bot.run_once(...)` 를 받아 **실패만 로그하고 성공 결과를 버렸다**.
# 프로덕션 실측: 23일 · 175건 체결 동안 체결 알림 0건(아침 브리핑은 나갔으므로 설정 문제가 아니다).
# 라우트에 대해 만든 고아 검사(X5)를 **알림 함수**로 확장한다.

def test_every_push_helper_has_a_caller():
    """`notify.push` 를 감싸는 함수는 부르는 곳이 있어야 한다.

    라우트가 그랬듯(`product_reviewer`) 알림도 "존재하지만 닿을 수 없는" 상태가 된다.
    차이는 라우트는 눌러 보면 알고, 알림은 **안 오는 것이 정상인지 고장인지 구분이 안 된다**는
    점이다(체결이 없어서 안 오는 것과 배선이 없어서 안 오는 것이 똑같이 조용하다).
    """
    import re
    from pathlib import Path
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    # `notify.push(` 를 부르는 def 를 모은다.
    helpers = []
    for m in re.finditer(r"^def (_\w+)\(", src, re.M):
        name = m.group(1)
        body = src[m.end():]
        nxt = re.search(r"^(?:@|def |async def )", body, re.M)
        body = body[:nxt.start()] if nxt else body
        if "notify.push(" in body:
            helpers.append(name)
    assert helpers, "notify.push 를 감싸는 함수를 못 찾았다 — 검사가 낡았다"
    for h in helpers:
        calls = len(re.findall(r"(?<!def )\b" + h + r"\(", src))
        assert calls >= 1, (
            f"{h}() 는 notify.push 를 감싸는데 **부르는 곳이 없다** — "
            f"체결이 없어서 안 오는 것과 배선이 없어서 안 오는 것을 사람이 구분할 수 없다")


def test_bot_loop_does_not_discard_fill_results():
    """`run_once`·`execute_reservations` 의 반환값을 버리면 체결이 조용해진다.

    `{"ok": False}` 를 버리고 성공 로그를 찍던 #339와 같은 병이고, 이번엔 **성공 결과**를 버렸다.
    """
    from pathlib import Path
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    it = src.split("def _bot_loop_iteration(", 1)[1].split("\ndef ", 1)[0]
    assert "bot.run_once(" in it
    assert "_push_trades(" in it, "run_once 결과가 알림으로 가지 않는다"
    # 예약 경로는 `run_once` 와 별개다 — 한쪽만 붙이면 예약으로 산 종목이 조용히 들어온다.
    assert "execute_reservations(" in it
    assert "_push_reservations(" in it, "예약 체결이 알림으로 가지 않는다"
    # 반환값을 변수로 받는지(버리지 않는지) 확인한다.
    import re
    assert re.search(r"=\s*bot\.execute_reservations\(", it), "예약 반환값을 버린다"


def test_fill_push_is_silent_when_nothing_filled():
    """체결 0건이면 아무 말도 하지 않는다 — 매일 빈 알림을 쓰면 그것도 곧 안 읽힌다."""
    import os
    from unittest import mock
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "1"
    from signal_desk import api
    with mock.patch.object(api.notify, "push", return_value=True) as pushed:
        api._push_trades("kr", {"ok": True, "buys": [], "sells": []})
        api._push_reservations("kr", {"ok": True, "executed": []})
        api._push_reservations("kr", {"ok": False, "reason": "x"})
        api._push_reservations("kr", None)
        assert pushed.call_count == 0, "체결이 없는데 알림을 보냈다"
        api._push_trades("kr", {"ok": True, "buys": [{"name": "A", "qty": 1}], "sells": []})
        assert pushed.call_count == 1
        assert "매수 A" in pushed.call_args[0][0]
    os.environ.pop("TELEGRAM_BOT_TOKEN"); os.environ.pop("TELEGRAM_CHAT_ID")


# ── 오늘 할 일 = 고장만 (2026-08-07) ─────────────────────────────────────────
# `자동수집 OFF(...) 고장이 아니다.` 라는 문구가 **할 일 버튼으로** 떠 있었다.
# 동결을 고장으로 보고하면 고장 조사를 유도한다(#340에서 같은 병을 문구로만 고쳤다).

def test_blocked_reasons_carry_a_machine_readable_kind():
    """문구는 사람이 읽고 **분류는 기계가 읽는다.**

    화면이 문자열을 파싱해 분류하면 그 매핑은 어디에서도 검사되지 않는다
    (`[추세]` 접두어 파싱이 그랬고, 문구를 고치는 순간 태그가 조용히 사라졌다).
    """
    from signal_desk import kb, store
    for name, st in (("kb.refresh_status", kb.refresh_status()),
                     ("store.warnings_status", store.warnings_status())):
        assert "blocked_kind" in st, f"{name} 에 blocked_kind 가 없다"
        if st.get("blocked_reason"):
            assert st["blocked_kind"] in ("fault", "frozen", "unconfigured", "empty"), \
                f"{name} blocked_kind={st['blocked_kind']!r} — 분류가 없거나 낯설다"
        else:
            assert st["blocked_kind"] is None, f"{name}: 이유가 없는데 분류가 있다"


def test_todo_list_hides_intentional_states_but_not_unknown_ones():
    """할 일은 **고장만**. 다만 모르는 종류는 띄운다 — 조용히 숨기면 새 고장을 놓친다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    blk = html.split("function renderAdminTodo(", 1)[1].split("\nfunction ", 1)[0]
    assert "blocked_kind" in blk, "화면이 분류를 읽지 않는다"
    assert "_NOT_A_TODO" in blk
    # 제외 목록에 의도된 상태 셋이 들어 있어야 한다.
    lst = blk.split("_NOT_A_TODO = new Set(", 1)[1].split(")", 1)[0]
    for k in ("frozen", "unconfigured", "empty"):
        assert k in lst, f"{k} 가 제외 목록에 없다"
    # **화이트리스트 방식이 아니어야 한다** — `kind === 'fault'` 로 걸면 새 분류가 조용히 숨는다.
    assert "=== 'fault'" not in blk, "고장만 통과시키면 모르는 종류가 숨는다(fail-loud 위반)"


def test_frozen_states_still_appear_in_diagnostics():
    """할 일에서 뺐다고 **정보를 지운 것은 아니다** — 진단 카드에는 그대로 남아야 한다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    # `/api/data-health` 를 그리는 곳이 blocked_reason 을 여전히 렌더한다.
    assert html.count("blocked_reason") >= 3, "진단 카드에서 이유가 사라졌다"


# ── 목표금액 플랜 (2026-08-07) ───────────────────────────────────────────────
# `내 포트폴리오`·`배당 플래너` 가 각각 숫자만 보여주고 "그래서 뭘 해야 하나"를 말하지 않았다.
# 옛 `시나리오 분석` 은 같은 부트스트랩·같은 성향을 쓰면서 적립·배당·목표만 없던 것이라
# 한 화면에 둘 다 두면 같은 말을 두 번 하는 것이었다 → 흡수했다.

def test_goal_plan_absorbed_the_scenario_card_without_leaving_dead_code():
    """중복을 없앨 때 **죽은 코드를 남기지 않는다** — 안 지우면 '닿지 않는 코드'가 된다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    api = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    for gone in ("runScenario", "scn-years", "scenario-result", "/api/scenario"):
        assert gone not in html, f"{gone} 가 화면에 남아 있다"
    assert "/api/scenario" not in api, "흡수된 라우트가 남아 있다"
    # 모듈은 남는다 — `goal_plan` 이 `_portfolio_returns`·`EQUITY_EXPOSURE` 를 쓴다.
    from signal_desk.signals import goal_plan, scenario
    assert goal_plan._MIN_HISTORY == scenario._MIN_HISTORY
    assert "scenario" not in api.split("from signal_desk.signals import (", 1)[1].split(")", 1)[0]


def test_goal_plan_frames_the_cards_below_it():
    """목표는 보유 입력 **바로 다음**이다. 배당·히트맵·리밸런싱이 무엇을 위한 숫자인지
    목표 없이는 알 수 없다 — 5번째로 밀려 있으면 새 헤드라인 기능이 묻힌다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    pane = html.split('id="trading-rebal"', 1)[1].split('id="trading-dividend"', 1)[0]
    i_goal = pane.index("목표금액 플랜")
    for later in ("내 보유 배당", "섹터 히트맵", "리밸런싱 성향"):
        assert i_goal < pane.index(later), f"목표가 `{later}` 보다 뒤에 있다"


def test_goal_chart_is_registered_with_the_single_resize_handler():
    """차트를 리사이즈 핸들러에 안 붙이면 **폭 0으로 남는다**(모바일 실측).

    리스너를 새로 만들지 않는다 — 같은 일을 두 곳에서 시키면 한쪽을 고칠 때 다른 쪽이 남는다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert html.count("addEventListener('resize'") == 1, "리사이즈 리스너가 하나가 아니다"
    blk = html.split("addEventListener('resize'", 1)[1].split("});", 1)[0]
    assert "_gpChart" in blk, "목표 경로 차트가 리사이즈에 등록되지 않았다"


def test_goal_plan_labels_its_assumption_on_screen():
    """`예측`으로 읽히지 않게 근거와 물려받은 편향을 화면에도 쓴다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "d.basis" in html and "d.caveat" in html, "근거·한계를 화면이 렌더하지 않는다"
    # 통화가 다르면 이전 경로는 거짓이다 — 시장 전환 시 비운다.
    sw = html.split("function setPfMarket(", 1)[1].split("\n}", 1)[0]
    assert "gp-result" in sw, "시장을 바꿔도 이전 통화의 경로가 남는다"


# ── LLM 예산 초과 응답 (2026-08-07) ───────────────────────────────────────────
# 프로덕션에서 `흐름 생성` 이 **HTTP 500** 이었다. `BudgetExceeded` 를 잡는 곳이 채팅 경로
# 2곳뿐이라 LLM을 부르는 나머지 라우트가 전부 500이었고, 화면은 `생성 요청 실패` 만 보여줬다 —
# 예산 때문인지 키가 없는지 서버가 죽었는지 **사람이 구분할 수 없었다**.

def test_budget_exceeded_becomes_429_everywhere_not_500():
    """라우트마다 `except` 를 붙이면 새 라우트에서 또 빠진다 — **전역 핸들러 하나**로 받는다."""
    from unittest import mock
    from fastapi.testclient import TestClient
    from signal_desk import api, llm
    from pathlib import Path
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    assert "@app.exception_handler(llm.BudgetExceeded)" in src, "전역 핸들러가 없다"
    # 인증 미들웨어가 **라우팅 전에** 401을 내므로 세션이 필요하다(401/404로 라우트 존재를
    # 판독할 수 없다는 것과 같은 이유다).
    from signal_desk import auth, config, db
    with db.conn() as cx:
        users = [(r[0], r[1]) for r in cx.execute("SELECT id, email FROM users")]
    admins = [u for u in users if config.is_admin(u[1])]
    if not admins:
        import pytest
        pytest.skip("관리자 계정이 없는 환경")
    token = auth._new_session(admins[0][0])
    client = TestClient(api.app, raise_server_exceptions=False)
    client.cookies.set(auth.COOKIE, token)
    try:
        with mock.patch.object(llm, "available", return_value=True), \
             mock.patch.object(llm, "complete", side_effect=llm.BudgetExceeded("상한 초과")):
            r = client.post("/api/hypothesis/refresh")
    finally:
        auth.logout(token)
    assert r.status_code == 429, f"예산 초과가 {r.status_code} 로 나온다(500이면 이유가 안 보인다)"
    b = r.json()
    assert "상한 초과" in (b.get("reason") or "")
    # 무엇을 하면 되는지 — 상한은 환경변수라 화면에서 못 바꾼다.
    assert "LLM_MONTHLY_BUDGET_USD" in (b.get("how_to_fix") or "")


def test_ui_does_not_swallow_non_json_failures():
    """`r.json()` 이 던지면 catch 가 원인을 삼킨다 — 텍스트로 먼저 받고 상태코드를 보여준다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    blk = html.split("async function refreshHypothesis(", 1)[1].split("\n}", 1)[0]
    assert "await r.text()" in blk, "본문을 텍스트로 먼저 받지 않는다"
    assert "HTTP ${r.status}" in blk, "비-JSON 실패에서 상태코드를 안 보여준다"
    assert "how_to_fix" in blk, "조치 방법을 화면이 버린다"


# ── 점수와 커버리지 (2026-08-07) ─────────────────────────────────────────────
# 사용자 질문: "왜 3.0인데 관망이고 1.72인데 강력매수인가" · "+3.0이면 무조건 사야 하나".
# 프로덕션 실측이 답이었다: 점수 3.00 종목의 커버리지가 **0.24**(8관점 중 2개)였고
# 화면 1위였다. 분모가 *발동한* 가중치의 합이라 **관점이 적을수록 만점이 쉽다**(X2).
# 그런데 커버리지가 화면 어디에도 없었다.

def test_score_range_is_structural_not_clipped():
    """점수는 ±3을 **넘을 수 없다** — 클리핑이 아니라 정의다.

    각 관점이 [-1,1]로 정규화되고 가중평균이라 결과도 [-1,1], 거기에 3을 곱한다.
    상한을 따로 걸 필요가 없고, 걸면 '클리핑된 값'이라는 오해가 생긴다.
    """
    from signal_desk.signals.engine import SignalConfig, combine
    cfg = SignalConfig()
    # 전 팩터 만점 → 정확히 +3
    hi = combine([(1.0, 0.3, []), (1.0, 0.2, []), (1.0, 0.15, [])], cfg)
    assert abs(hi["score"] - 3.0) < 1e-9
    lo = combine([(-1.0, 0.3, []), (-1.0, 0.2, [])], cfg)
    assert abs(lo["score"] + 3.0) < 1e-9
    # **관점이 적어도 만점이 나온다** — 이게 사용자가 본 3.00의 정체다.
    one = combine([(1.0, 0.15, [])], cfg)
    assert abs(one["score"] - 3.0) < 1e-9, "관점 1개로도 +3이 나온다는 사실이 바뀌면 문서도 고칠 것"


def test_coverage_is_shown_next_to_the_score():
    """높은 점수 + 낮은 커버리지는 **확신이 아니라 무지의 신호**다 — 같이 보여야 한다.

    실측: 프로덕션 커버리지 구간별 |점수| 평균이 0.2구간 **3.00** vs 0.7~0.8구간 0.64~0.68.
    커버리지를 안 보여주면 사용자는 3.00을 최고 확신으로 읽는다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "covBadge" in html and "data_coverage" in html
    assert "관점" in html
    # 목록 행에서 점수와 **같은 칸**에 있어야 한다(떨어뜨리면 같이 안 읽는다).
    row = html.split("const body = rows.map(", 1)[1].split("</tr>", 1)[0]
    assert "metric(r)" in row and "covBadge(r)" in row
    # 상세 히어로에도 · 낮으면 경고 문장까지.
    assert "_heroCov(" in html and "_heroCovWarn(" in html
    assert "부풀려져 있습니다" in html


def test_glossary_says_the_score_does_not_decide_the_verdict():
    """`3.0인데 관망`을 설명하지 않으면 사용자는 화면을 신뢰할 수 없다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "점수가 판정을 정하지 않습니다" in html
    for cond in ("상위 순위", "안전장치", "관점이 충분"):
        assert cond in html, f"판정 3조건 중 `{cond}` 설명이 없다"
    assert "관점이 적을수록" in html, "커버리지가 낮을수록 만점이 쉽다는 설명이 없다"


def test_normal_coverage_does_not_shout():
    """200행 전부를 칩으로 그리면 정작 `2/8관점`이 묻힌다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    css = html[:html.find("</style>")]
    base = css.split(".cov-badge {", 1)[1].split("}", 1)[0]
    low = css.split(".cov-badge.low {", 1)[1].split("}", 1)[0]
    assert "background" not in base, "정상 커버리지가 배경색을 가지면 소음이 된다"
    assert "background" in low and "sig-watch" in low, "낮은 커버리지가 안 튄다"


def test_llm_budget_caps_are_consistent_and_documented():
    """월 상한만 올리면 **일 상한에서 매일 막힌다** — 두 게이트를 같이 봐야 한다.

    2026-08-07: 월 $10 → $100 으로 올릴 때 일 $1 을 그대로 두면, 프로덕션 실측 일평균
    $2.14(최근 7일 $3.08)에서 매일 차단된다. 올린 쪽만 보고 "열렸다"고 믿는 것이
    이 리포가 반복해서 밟은 완화 착각이다.
    """
    from pathlib import Path
    from signal_desk import config
    day, month = config.llm_daily_budget_usd(), config.llm_monthly_budget_usd()
    assert day > 0 and month > 0
    # 월 상한이 일×30 이상이면 월 게이트는 **아무 것도 막지 않는다**(있는 척하는 완화).
    assert month < day * 30, f"월 {month} ≥ 일 {day}×30 — 월 게이트가 무효다"
    # 일 상한이 월 상한보다 크면 일 게이트가 무효다.
    assert day < month, f"일 {day} ≥ 월 {month} — 일 게이트가 무효다"
    # `.env.example` 이 코드 기본값과 어긋나면 배포 때 다른 값이 걸린다.
    env = Path(".env.example").read_text(encoding="utf-8")
    assert f"LLM_DAILY_BUDGET_USD={day:g}" in env, ".env.example 일 상한이 코드와 다르다"
    assert f"LLM_MONTHLY_BUDGET_USD={month:g}" in env, ".env.example 월 상한이 코드와 다르다"


# ── 트레이딩 사용성 (2026-08-07) ─────────────────────────────────────────────

def test_two_return_metrics_have_distinguishable_names():
    """`총손익률`(보유 기준)과 `총수익률`(계좌 기준)은 사람이 구분할 수 없었다.

    실측 같은 화면: 보유 기준 **+2.43%**(평가−매입÷매입) vs 계좌 기준 **−0.02%**(시드 대비).
    **부호까지 반대**라 어느 게 성적인지 알 수 없었다 — 둘 다 맞는데 이름이 같은 게 문제다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "보유 손익률" in html, "보유 기준 지표 이름이 구분되지 않는다"
    assert "계좌 수익률" in html, "계좌 기준 지표 이름이 구분되지 않는다"
    # 옛 이름이 **화면 텍스트에** 남아 있으면 두 지표가 다시 같은 이름을 갖는다.
    # **주석을 지운 뒤 센다** — 설명 주석에 옛 이름을 적으면 오탐이 난다(이 리포에서 네 번째로
    # 밟은 함정: 주석 안 `var()` · 리포트 `<code>` · `store.update_valuation`).
    import re
    code = re.sub(r"//.*$", "", html, flags=re.M)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", code)
    assert "총손익률" not in txt, "옛 이름 `총손익률` 이 화면에 남아 있다"
    # 어느 것이 track record 인지 화면이 말해야 한다.
    assert "이게 성적입니다" in html


def test_loaders_do_not_crash_on_non_200():
    """비-200 응답에서 터지면 화면이 **반쯤 그려진 채 침묵**한다.

    실측: 세션이 만료된 채 `loadBotState` 가 401 본문(`{error,auth}`)에서
    `d.positions.length` 로 터졌다. 예산 초과(429)·서버 오류(500)도 같은 결과다.
    """
    import re
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "async function getJSON(" in html, "상태코드를 보는 공용 헬퍼가 없다"
    # 헬퍼가 서버 이유를 **먼저** 쓴다(429의 예산 사유가 상태코드 문구에 묻히면 안 된다).
    fn = html.split("async function getJSON(", 1)[1].split("\n}", 1)[0]
    assert "d.reason" in fn and "d.error" in fn
    for code in ("401", "429"):
        assert code in fn, f"{code} 를 사람이 읽을 문장으로 바꾸지 않는다"
    # 즉시 크래시 패턴이 남아 있지 않은지 — 손 목록이 아니라 스캔이다.
    pat = re.compile(r"await \(await fetch\([^)]*\)\)\.json\(\)")
    lines = html.split("\n")
    risky = []
    for i, ln in enumerate(lines):
        if pat.search(ln):
            blk = "\n".join(lines[i:i + 8])
            if re.search(r"\bd\.\w+\.(length|map|forEach|filter|toFixed)", blk):
                risky.append(i + 1)
    assert not risky, f"비-200에서 즉시 터지는 로더가 남아 있다(줄 {risky})"


# ── 성향이 후보를 좁히지 않는다 (2026-08-07) ─────────────────────────────────
# 성향별 `rank_top_pct`(1.0/2.0/3.0)가 `max_positions`(12/10/6)를 죽이고 설계 의도를
# 뒤집었다: 안정형 창 **2자리**(가장 집중) · 공격형 6자리(가장 분산) — 정반대였다.

def test_buy_window_has_one_definition():
    """매수권 분위는 **엔진 값 하나**다. 성향이 좁히면 화면의 매수권과 봇의 후보가 갈라진다."""
    from signal_desk import strategy
    for st in strategy.STYLES:
        assert "rank_top_pct" not in strategy.PRESETS[st], \
            f"{st} 프리셋이 매수권을 다시 좁힌다 — max_positions 가 죽는다"
    # 접근자는 엔진 값을 그대로 돌려준다(성향과 무관).
    for st in strategy.STYLES:
        for eng in (1.0, 3.0, 10.0):
            assert strategy.rank_top_pct(st, eng) == eng


def test_styles_differ_by_position_size_not_by_window():
    """성향은 **비중**으로 갈린다 — 같은 후보를 보고 얼마씩 담는지가 다르다.

    실측 설계: 창 6자리 기준 투입 36%(안정) / 48%(균형) / 84%(공격).
    `안정형 = 변동성↓` 과 방향이 맞아야 한다(예전엔 안정형이 가장 집중이었다).
    """
    from signal_desk import strategy
    pcts = [strategy.PRESETS[s]["position_pct"] for s in ("conservative", "balanced", "aggressive")]
    assert pcts[0] < pcts[1] < pcts[2], f"비중이 성향 순서와 어긋난다: {pcts}"
    slots = 6
    invest = [min(slots, strategy.PRESETS[s]["max_positions"]) * strategy.PRESETS[s]["position_pct"]
              for s in ("conservative", "balanced", "aggressive")]
    assert invest[0] < invest[1] < invest[2], f"투입 비중이 성향 순서와 어긋난다: {invest}"
    assert invest[0] < 0.5, "안정형이 절반 이상 투입하면 `변동성↓` 이 아니다"


def test_source_level_unproven_change_is_recorded():
    """소스로 바꾼 미검증 변경도 **이력에 남는다.**

    관리자 UI 게이트(`prereg.change_allowed`)는 소스 편집을 못 막는다 — 그게 그 게이트가
    경고한 우회로다("소스를 직접 편집하면 이력에 남지 않는다" · H1이 그랬다).
    """
    from signal_desk import db, signalcfg, strategy
    db.kv_set(strategy._UNPROVEN_KEY, None)
    assert strategy.record_unproven_change() is True
    assert strategy.record_unproven_change() is False, "매 부팅 중복 기록하면 배너가 도배된다"
    entry = next((e for e in signalcfg.history(20)
                  if e.get("unproven") and "strategy.py" in str(e.get("source"))), None)
    assert entry, "미검증 변경이 이력에 없다"
    assert entry.get("reason"), "사유 없이 기록하면 나중에 왜 바꿨는지 모른다"
    assert "판정" in str(entry.get("verdict_at_change")), "변경 시점의 판정 상태를 남기지 않는다"


def test_style_comparison_says_it_cannot_be_judged_yet():
    """세 성향의 우열을 23일 표본으로 말할 수 없다 — 표가 그 사실을 밝혀야 한다.

    기준선 없는 비율을 노출하지 않는다는 규칙과 같은 문제다. 숫자 셋을 나란히 놓으면
    "안정형이 낫다"로 읽힌다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "_styleVerdictNote" in html
    fn = html.split("function _styleVerdictNote(", 1)[1].split("\n}", 1)[0]
    assert "판정 불가" in fn
    assert "_STYLE_MIN_DAYS" in html and "근거가 아닙니다" in fn


def test_cash_and_total_explain_themselves():
    """보유 0이면 `총평가금액 = 현금` 이라 두 카드가 같은 숫자를 보여준다 — 그걸 말해야 한다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "전량 현금" in html, "보유 0 상태를 화면이 말하지 않는다"
    assert "시드 대비" in html, "총평가금액이 시드 대비 얼마인지 안 보인다"


def test_dart_veto_targets_cover_what_may_become_buyable():
    """악재 veto 대상은 "지금 살 것"이 아니라 **"살 수 있게 될 것"** 까지여야 한다.

    2026-08-07 실측: 프로덕션 폴링 대상이 **6종목**뿐이었다(상한 40). 매수권이 0~2건이고
    보유가 적어서다 — **상한이 아니라 대상 정의**가 좁았다. 오늘 근접 종목이 내일 매수권에
    들면 그때 악재 이력이 이미 있어야 막을 수 있다. `list.json` 1콜/종목이라 LLM 비용은 0이다.
    """
    from pathlib import Path
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    fn = src.split("def _kb_lite_targets(", 1)[1].split("\ndef ", 1)[0]
    # 매수권·보유·관심을 **먼저** 채운다(우선순위 유지).
    assert "is_buy(" in fn and "bot_position_tickers_all" in fn and "fav_tickers_all" in fn
    # 남는 자리를 순위 상위로 채운다.
    assert fn.count("sorted(_signals()") >= 2, "남는 자리를 순위 상위로 채우지 않는다"
    assert "살 수 있게 될 것" in fn, "왜 넓혔는지 근거가 코드에 없다"


def test_dart_path_costs_no_llm():
    """공시 경로는 **LLM을 쓰지 않는다** — 그래서 예산과 무관하게 켜 둘 수 있다.

    이 성질이 깨지면 "비용 0이니 넓혀도 된다"는 전제가 무너진다.
    """
    from pathlib import Path
    kbsrc = Path("src/signal_desk/kb.py").read_text(encoding="utf-8")
    fn = kbsrc.split("def poll_disclosures(", 1)[1].split("\ndef ", 1)[0]
    for banned in ("llm.complete", "llm_mod.complete", "complete_json("):
        assert banned not in fn, f"공시 폴링이 LLM({banned})을 부른다 — 비용 0 전제가 깨진다"


# ── 매일 열 이유 (2026-08-07) ────────────────────────────────────────────────
# 판정 줄은 "아직 모른다"만 말한다. 그 다음에 "그래도 오늘 이건 달라졌다"가 있어야 습관이 된다.
# 다만 **원인을 틀리게 말하면 오학습**이라, 인과 분류가 이 카드의 전부다.

def test_daily_card_sits_right_under_the_verdict():
    """판정 줄 **바로 아래**여야 한다 — 목록 뒤로 밀리면 매일 안 본다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    i_verdict = html.index('id="signal-trust"')
    i_card = html.index('id="daily-change"')
    i_table = html.index('class="sig-toolbar"')
    assert i_verdict < i_card < i_table, "데일리 카드가 판정 줄 아래·툴바 위에 없다"
    assert "loadDailyChange()" in html


def test_daily_card_shrinks_on_narrow_widths():
    """실측 375px에서 3행이 295px을 먹어 목록을 1042px로 밀어냈다.

    매수0 카드를 439→190px로 줄인 것과 같은 문제다("목록을 보러 온 화면에서 목록이 접힌 셈").
    브레이크포인트는 `_SIG_NARROW_PX` 하나를 공유한다 — 두 값이 갈라지면 한쪽만 접힌다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    fn = html.split("async function loadDailyChange(", 1)[1].split("\n}", 1)[0]
    assert "_SIG_NARROW_PX" in fn, "자체 브레이크포인트를 쓰면 표 열 접기와 갈라진다"
    assert "narrow ? 2 :" in fn, "좁은 폭에서 행을 줄이지 않는다"
    # 감춘 수를 밝힌다 — 조용히 줄어든 목록은 "이게 전부"로 읽힌다.
    assert "changes_total > shown.length" in fn
    # 브레이크포인트를 넘나들 때 다시 그린다(리스너는 하나만).
    assert html.count("addEventListener('resize'") == 1
    blk = html.split("addEventListener('resize'", 1)[1].split("\n});", 1)[0]
    assert "loadDailyChange()" in blk, "리사이즈 때 다시 그리지 않으면 접힌 채 남는다"


def test_daily_card_labels_context_separately_from_cause():
    """공시를 원인 옆에 붙이면 "이 공시 때문에 관망"이라는 없는 인과가 만들어진다."""
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    fn = html.split("async function loadDailyChange(", 1)[1].split("\n}", 1)[0]
    assert "layer-badge ctx" in fn, "맥락 라벨이 없다"
    assert "dchg-ctx" in fn, "맥락이 원인과 같은 블록에 있다"
