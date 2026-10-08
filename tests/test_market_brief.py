"""One-card market reading must be dated, read-only, and fail closed on stale bars."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from signal_desk import api, market_brief, market_brief_image


def _bars(last="2026-10-02", count=61):
    dates = [f"2026-07-{(i % 28) + 1:02d}" for i in range(count - 1)] + [last]
    prices = [100.0 + i for i in range(count)]
    return prices, dates


def test_fresh_market_card_uses_same_session_bars_and_dated_flow():
    prices, dates = _bars()
    out = market_brief.build(
        "kr", prices={"A": prices, "B": prices}, dates={"A": dates, "B": dates},
        tickers=["A", "B"], expected="2026-10-02", previous="2026-10-01",
        flow={"smart_net_20d": -2.3, "as_of": "2026-10-01"},
        selection={"buy_count": 1, "strong_buy_count": 0, "slots": 2,
                   "computed_at": "2026-10-02T07:00:00+00:00"},
        now=dt.datetime(2026, 10, 2, 9, tzinfo=dt.timezone.utc),
    )
    assert out["status"] == "ready"
    assert out["price_as_of"] == "2026-10-02"
    assert out["price_count"] == 2
    assert len(out["facts"]) == 3
    assert out["headline"] == "관찰 종목 다수의 흐름이 견조해요"
    assert out["facts"][0]["value"] == "2/2개"
    assert out["facts"][2]["as_of"] == "2026-10-01"
    assert out["selection"]["buy_count"] == 1
    assert out["scene"]["direction"] == "unknown"  # 직전 거래일 날짜가 이어지지 않음
    assert out["not_order_advice"] is True


def test_stale_market_card_withholds_claims_and_buy_count():
    prices, dates = _bars(last="2026-10-01")
    out = market_brief.build("kr", prices={"A": prices}, dates={"A": dates},
                             tickers=["A"], expected="2026-10-02",
                             selection={"buy_count": 3})
    assert out["status"] == "stale"
    assert out["scene"]["direction"] == "unknown"
    assert out["state"] is None and out["facts"] == [] and out["selection"] is None
    assert "2026-10-01" in out["unknown"][0]


def test_partial_market_card_excludes_old_symbol_and_reports_missing():
    prices, dates = _bars()
    old_prices, old_dates = _bars(last="2026-10-01")
    out = market_brief.build("us", prices={"A": prices, "B": old_prices},
                             dates={"A": dates, "B": old_dates}, tickers=["A", "B"],
                             expected="2026-10-02", previous="2026-10-01",
                             macro_indicators=[{"key": "NASDAQCOM", "change": 2.2,
                                                "asof": "2026-09-29"}])
    assert out["status"] == "partial"
    assert out["headline"] == "관찰 종목 다수의 흐름이 견조해요"
    assert out["price_count"] == 1
    assert out["selection"] is None
    assert len(out["facts"]) == 2  # old Nasdaq release is not today's reason
    assert any("1종목" in item for item in out["unknown"])
    assert any("나스닥" in item for item in out["unknown"])
    assert out["today_headline"] == "오늘 등락은 아직 확인하기 어려워요"


def test_partial_coverage_headline_names_sample_when_most_bars_are_available():
    prices, dates = _bars()
    old_prices, old_dates = _bars(last="2026-10-01")
    out = market_brief.build(
        "us", prices={**{str(i): prices for i in range(9)}, "old": old_prices},
        dates={**{str(i): dates for i in range(9)}, "old": old_dates},
        tickers=[*(str(i) for i in range(9)), "old"],
        expected="2026-10-02", previous=dates[-2],
    )
    assert out["status"] == "partial"
    assert out["daily_coverage"] == {"available": 9, "analyzed": 9}
    assert out["today_headline"] == "확인한 9개에서는 오른 종목이 더 많았어요"
    assert out["scene"]["direction"] == "up"
    assert out["selection"] is None


def test_low_daily_coverage_with_complete_latest_bars_still_withholds_direction():
    prices, dates = _bars()
    gapped = dates[:-2] + ["2026-09-29", dates[-1]]
    out = market_brief.build(
        "kr", prices={"A": prices, "B": prices},
        dates={"A": dates, "B": gapped}, tickers=["A", "B"],
        expected="2026-10-02", previous=dates[-2],
    )
    assert out["status"] == "ready"  # 최근 종가의 신선도와 당일 등락의 범위는 별개다.
    assert out["today_headline"] == "오늘 시장 방향은 자료가 부족해요"
    assert out["daily_coverage"] == {"available": 1, "analyzed": 2}
    assert out["scene"]["direction"] == "unknown"


def test_today_route_reads_market_card_without_eager_signal_list():
    html = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text(encoding="utf-8")
    route = html.split("function switchTab(t){", 1)[1].split("const _SEGS =", 1)[0]
    assert 'id="view-today"' in html and 'id="subnav-today"' in html
    assert "routeFromHash() || switchTab('today')" in html
    assert "if (t === 'today') loadRegime();" in route
    assert "if (t === 'signal') { loadSignals(); loadScorecard(); }" in route
    assert "if (t === 'today') { loadSignals()" not in route


def test_market_card_has_one_endpoint_and_defers_source_requests():
    html = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "'/api/market-brief?market='" in html
    assert 'id="market-brief-sources"' in html
    assert 'id="mb-image"' in html
    assert 'id="mb-image-fallback"' in html
    assert "prefers-reduced-motion:reduce" in html
    assert "img.onerror = () => { img.hidden = true; fallback.hidden = false; save.hidden = true;" in html
    assert "mb-figure-mobile-note" in html
    assert 'downloadMarketBriefPng()' in html
    start = html.split("async function startApp(){", 1)[1].split("// ===== 온보딩", 1)[0]
    assert "loadMacro();" not in start
    assert "loadIndustryPulse();" not in start


def test_market_brief_api_reuses_current_decisions_without_llm(monkeypatch):
    prices, dates = _bars()
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-02")
    monkeypatch.setattr(api.market_clock, "previous_session", lambda market, day: "2026-10-01")
    monkeypatch.setattr(api.store, "load_portfolio_close_bundle", lambda market: ({"A": prices}, {"A": dates}))
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(api, "_signals", lambda: [SimpleNamespace(kind="BUY", computed_at="2026-10-02T07:00:00+00:00")])
    monkeypatch.setattr(api, "_regime", lambda: {})
    monkeypatch.setattr(api, "_macro", lambda: {})
    monkeypatch.setattr(api.signalcfg, "effective_config", lambda *args, **kwargs: (object(), {}))
    monkeypatch.setattr(api, "selection_summary", lambda *args: {
        "mode": "rank", "rank_slots": 1, "cutoff_score": 1.4,
        "buy_threshold": 1.2, "rank_min_score": 0.0,
    })
    monkeypatch.setattr(api.llm, "complete", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM call")))
    out = api.market_brief_get("kr")
    assert out["status"] == "ready"
    assert out["selection"]["buy_count"] == 1
    assert out["selection_policy"]["cutoff_score"] == 1.4
    assert '<svg ' in out["image_svg"]
    assert 'data-scene="unknown"' in out["image_svg"]


def test_market_brief_api_does_not_compute_signals_when_prices_stale(monkeypatch):
    prices, dates = _bars(last="2026-10-01")
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-02")
    monkeypatch.setattr(api.market_clock, "previous_session", lambda market, day: "2026-10-01")
    monkeypatch.setattr(api.store, "load_portfolio_close_bundle", lambda market: ({"A": prices}, {"A": dates}))
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(api, "_signals", lambda: (_ for _ in ()).throw(AssertionError("stale signals")))
    out = api.market_brief_get("kr")
    assert out["status"] == "stale"
    assert out["selection"] is None
    assert "오래된 가격" in out["image_svg"]
    assert "매수 판정 3개" not in out["image_svg"]


def test_weak_breadth_is_explained_without_technical_headline():
    prices = {}
    dates = {}
    for n in range(112):
        prices[str(n)] = [100.0] * 60 + ([110.0] if n < 30 else [90.0])
        dates[str(n)] = ["2026-07-01"] * 60 + ["2026-10-02"]
    out = market_brief.build("us", prices=prices, dates=dates,
                             tickers=list(prices), expected="2026-10-02")
    assert out["facts"][0]["percent"] == 26.8
    assert out["facts"][0]["value"] == "30/112개"
    assert out["headline"] == "관찰 종목 다수의 흐름이 약해요"
    assert "26.8%" not in out["headline"]
    assert "최근 60개 종가" in out["facts"][0]["technical"]


def test_image_is_parseable_and_escapes_untrusted_text():
    out = market_brief_image.render({
        "market": "kr", "status": "ready", "price_as_of": "2026-10-02",
        "scene": {"direction": "up", "compared": 5, "universe": 5, "up": 3, "down": 2,
                  "flat": 0, "sectors": [{"sector": "A&B <업종>", "median_change_pct": 2.1}]},
        "headline": "관찰 종목의 흐름이 엇갈려요", "summary": "A&B <불확실>",
        "facts": [{"label": "최근 평균보다 높은 종목", "value": "2/5개", "percent": 40},
                  {"label": "20개 종가 전보다 오른 종목", "value": "3/5개", "percent": 60}],
        "selection": {"buy_count": 1, "strong_buy_count": 0,
                      "computed_at": "2026-10-02T07:00:00+00:00"}, "unknown": [],
    })
    ElementTree.fromstring(out)
    assert "A&amp;B &lt;불확실&gt;" in out
    assert "A&amp;B &lt;업종&gt;" in out
    assert 'data-scene="up"' in out
    assert "매수 판정 1개" in out


def test_illustration_follows_verified_direction_and_withholds_us_sector():
    prices = {}
    dates = {}
    for n in range(10):
        prices[str(n)] = [100.0] * 59 + [100.0, 105.0 if n < 8 else 95.0]
        dates[str(n)] = ["2026-07-01"] * 59 + ["2026-10-01", "2026-10-02"]
    kr = market_brief.build(
        "kr", prices=prices, dates=dates, tickers=list(prices), expected="2026-10-02",
        previous="2026-10-01", sector_by_ticker={str(n): "반도체" for n in range(10)},
    )
    assert kr["scene"]["direction"] == "up"
    assert kr["scene"]["up"] == 8 and kr["scene"]["down"] == 2
    svg = market_brief_image.render(kr)
    assert 'data-scene="up"' in svg
    assert "상승 8 · 하락 2 · 보합 0" in svg
    assert "반도체 +5.00%" in svg
    us = market_brief.build(
        "us", prices=prices, dates=dates, tickers=list(prices), expected="2026-10-02",
        previous="2026-10-01",
    )
    assert "반도체" not in market_brief_image.render(us)


def test_image_fogs_low_daily_coverage_even_when_latest_close_is_fresh():
    prices, dates = _bars()
    card = market_brief.build(
        "kr", prices={"A": prices, "B": prices},
        dates={"A": dates, "B": dates[:-2] + ["2026-09-29", dates[-1]]},
        tickers=["A", "B"], expected="2026-10-02", previous=dates[-2],
        selection={"buy_count": 2, "computed_at": "2026-10-02T07:00:00+00:00"},
    )
    svg = market_brief_image.render(card)
    assert 'data-scene="unknown"' in svg
    assert "매수 판정 2개" not in svg
    assert "오래된 가격이나 부족한 자료" in svg


def test_partial_scene_discloses_observed_sample_but_not_full_buy_count():
    prices = {}
    dates = {}
    for n in range(10):
        prices[str(n)] = [100.0] * 59 + [100.0, 104.0]
        dates[str(n)] = ["2026-07-01"] * 59 + ["2026-10-01", "2026-10-02"]
    dates["9"][-1] = "2026-10-01"
    card = market_brief.build(
        "kr", prices=prices, dates=dates, tickers=list(prices),
        expected="2026-10-02", previous="2026-10-01",
        selection={"buy_count": 4, "computed_at": "2026-10-02T07:00:00+00:00"},
    )
    svg = market_brief_image.render(card)
    assert card["status"] == "partial" and card["scene"]["direction"] == "up"
    assert "오늘 비교 9/10종목" in svg
    assert "매수 판정 4개" not in svg


def test_image_withholds_buy_count_without_verifiable_decision_time():
    out = market_brief_image.render({
        "market": "kr", "status": "ready", "headline": "관찰 종목의 흐름이 엇갈려요",
        "facts": [], "selection": {"buy_count": 4}, "unknown": [],
    })
    assert "매수 판정 4개" not in out
    assert "매수 판정 건수 보류" in out
