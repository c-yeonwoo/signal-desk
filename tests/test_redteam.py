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

    def _capture(panel, cfg=None, regimes=None, scores=None, score_source="price"):
        seen["cfg"] = cfg
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
