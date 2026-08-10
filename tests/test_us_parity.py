"""미국이 국내와 **같은 기준**으로 계산되는가 — 문턱은 같았는데 입력이 달랐다.

실측(2026-08-08 프로덕션):

    US   자료부족 503/503 (전부) · 매수권 0 · 커버리지 중위 0.36 (최대 0.60)

    발동         US        KR
    technical   503/503   200/200
    fundamental 499/503   191/200
    valuation   443/503   169/200
    reversion    18/503     6/200   조건부 — 정상
    flow          0/503   158/200   원리적으로 없음(네이버 = 국내 전용)
    short         0/503   152/200   원리적으로 없음(KRX = 국내 전용)
    quality       0/503   191/200   **배선이 없었을 뿐**
    momentum      4/503   197/200   **봉이 216개인데 252 필요**

원인 셋이 성질이 다 다르다: ① 원리적으로 없음 ② 배선 누락 ③ 데이터 깊이 부족.
"""

from __future__ import annotations

from signal_desk import api, store
from signal_desk.signals import engine, quality


# ─────────────── ① 원리적으로 없는 팩터는 분모에서 뺀다 ───────────────

def test_us_unavailable_factors_are_excluded_from_the_denominator():
    """수급·공매도는 미국에 **애초에 없는 데이터**다 — 결측으로 세면 전 종목이 문턱에 걸린다.

    실측: 커버리지 0.72 < 문턱 0.80 → US 503종목 전부 `low_coverage`, 매수권 0건.
    """
    cfg = engine.SignalConfig()
    has = {"technical": 1, "fundamental": 1, "valuation": 1, "reversion": 0,
           "flow": 0, "quality": 1, "momentum": 1, "short": 0}
    before = engine.data_coverage(has, cfg)["ratio"]
    after = engine.data_coverage(has, cfg, unavailable=api.US_UNAVAILABLE_FACTORS)["ratio"]
    assert before < cfg.min_data_coverage, "전제 확인: 예전엔 문턱 미달이었다"
    assert after >= cfg.min_data_coverage, f"분모에서 빼도 여전히 미달({after})"
    assert api.US_UNAVAILABLE_FACTORS == ("flow", "short")


def test_both_markets_declare_unavailable_so_the_rule_is_visible():
    """한쪽만 선언하면 두 시장이 커버리지를 **다른 규약으로** 세고 그 차이가 안 드러난다."""
    import inspect
    assert "unavailable" in store.kr_engine_inputs(), "국내가 규약을 선언하지 않는다"
    assert store.kr_engine_inputs()["unavailable"] == (), "국내는 8팩터를 모두 볼 수 있다"
    us = inspect.getsource(api._us_signals)
    assert "unavailable=US_UNAVAILABLE_FACTORS" in us, "미국이 규약을 넘기지 않는다"


def test_unavailable_is_not_a_way_to_hide_real_gaps():
    """**진짜 결측을 여기 넣으면 안 된다.** 미국에도 있는 데이터는 빼면 안 된다 —
    `quality` 를 넣었으면 배선 누락이 영원히 숨는다(실측 0/503이 바로 그 상태였다)."""
    for banned in ("quality", "fundamental", "valuation", "momentum", "technical"):
        assert banned not in api.US_UNAVAILABLE_FACTORS, \
            f"{banned} 는 미국에도 있는 데이터다 — 빼면 결함이 숨는다"


# ─────────────── ② 퀄리티: 판정 불가를 실패로 세지 않는다 ───────────────

def test_quality_max_is_what_could_be_evaluated_not_a_fixed_five():
    """**고정 5는 판정할 수 없었던 항목을 실패로 센다.**

    미국 재무(EDGAR)는 순이익·자기자본만 있어 평가 가능 항목이 2개다. 고정 5면
    재무가 완벽한 기업도 `(2/5)*2-1 = -0.20` 으로 **음수**를 받는다 — 시장 전체에 걸린
    조용한 감점이다.
    """
    us = quality.evaluate({"net_income": 1000, "equity": 5000, "roe": 20.0}, {})
    assert us["has"] and us["max"] == 2 and us["points"] == 2
    norm, w, _, pts, has = quality.component({"quality": us}, 0.15)
    assert norm == 1.0, f"건강한 미국 기업이 {norm} 을 받는다"
    assert (pts / 5) * 2 - 1 < 0, "전제 확인: 고정 5였다면 음수였다"


def test_domestic_quality_scores_do_not_change():
    """국내는 DART가 5개 항목을 모두 주므로 `max=5` 그대로 — **점수가 바뀌지 않는다.**

    판정 보류 중에 국내 점수를 바꾸면 그건 전략 변경이다. 이 수정은 판정 불가를 실패로 세던
    버그만 고친다.
    """
    kr = quality.evaluate(
        {"net_income": 1000, "equity": 5000, "roe": 20.0,
         "debt_ratio": 27.5, "revenue_growth": 4.1},
        {"roe": 15.0, "debt_ratio": 30.0})
    assert kr["max"] == 5 and kr["points"] == 5


def test_quality_needs_two_evaluable_checks_not_just_two_fields():
    """필드가 2개 있어도 **판정 가능한 항목이 2개 미만**이면 분모가 무의미하다."""
    only_one = quality.evaluate({"net_income": 1000}, {})
    assert only_one["evaluable"] == 1 and only_one["has"] is False


# ─────────────── ③ 모멘텀: 봉 깊이가 모자랐다 ───────────────

def test_toss_cap_escalates_to_kis_when_depth_is_needed():
    """**토스는 200봉 상한**이라 더 요청해도 200만 온다 — 모멘텀은 252거래일이 필요하다.

    토스를 아예 건너뛰지는 않는다(기본 경로이고 일상 갱신은 200봉으로 충분) — **모자랄 때만**
    KIS로 올라가고 더 긴 쪽을 쓴다.
    """
    import inspect
    src = inspect.getsource(store.fetch_us_prices)
    assert "days > _TOSS_MAX_BARS and len(bars) <= _TOSS_MAX_BARS" in src, \
        "토스가 깊이를 못 채웠을 때 KIS로 올라가지 않는다"
    assert "len(deep_bars) > len(bars)" in src, "더 짧은 쪽으로 덮어쓸 수 있다"
    assert store.US_MIN_BARS_FOR_MOMENTUM >= engine.SignalConfig().momentum_lookback, \
        "요건이 엔진의 모멘텀 창보다 작으면 발동을 보장하지 못한다"
    assert store.US_DEEP_TARGET_BARS > store.US_MIN_BARS_FOR_MOMENTUM, "여유가 없다"


def test_shallow_is_a_separate_defect_from_stale(tmp_path, monkeypatch):
    """**얕음과 뒤처짐은 다른 병이다.** 마지막 봉이 오늘이어도 깊이가 모자라면 모멘텀이 안 돈다."""
    import pandas as pd
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "data" / "cache"
    p.mkdir(parents=True)
    monkeypatch.setattr(store, "US_PRICES_FILE", p / "us_prices.parquet")
    rows = ([{"ticker": "SHORT", "date": f"2026-01-{i%28+1:02d}", "close": 1.0} for i in range(10)]
            + [{"ticker": "DEEP", "date": f"d{i}", "close": 1.0} for i in range(300)])
    pd.DataFrame(rows).to_parquet(store.US_PRICES_FILE)
    shallow = store.us_prices_shallow_tickers(["SHORT", "DEEP"])
    assert shallow == ["SHORT"], f"얕은 종목을 못 골랐다: {shallow}"


def test_deep_backfill_is_wired_into_the_loop():
    """코드만 있고 아무도 안 부르면 봉은 영원히 안 깊어진다 — 이 레포가 다섯 번 겪은 병이다."""
    import inspect
    src = inspect.getsource(api._backfill_us_prices_batch)
    assert "us_prices_shallow_tickers" in src, "얕은 종목 백필이 루프에 없다"
    assert "US_DEEP_TARGET_BARS" in src, "깊이 목표를 넘기지 않는다"
