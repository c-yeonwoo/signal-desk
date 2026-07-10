"""종목별 과거 이벤트 타임라인 — 공시 분류 + 엔드포인트(공시 필터·배당)."""


def test_disc_kind_classifies():
    from signal_desk import api
    assert api._disc_kind("주요사항보고서(자기주식취득결정)") == "good"
    assert api._disc_kind("주요사항보고서(감자결정)") == "caution"      # critical
    assert api._disc_kind("유상증자 결정") == "caution"                # serious
    assert api._disc_kind("기타 경영사항 안내") == "note"


def test_events_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import api, store
    store._write_json(store.FUNDAMENTALS_FILE, {"005930": {"dps": 1500}})
    api._corp_codes.cache_clear()
    api._disclosures_cached.cache_clear()
    monkeypatch.setattr(api, "_corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(api, "_disclosures_cached", lambda c, b, e: (
        ("자기주식취득 결과보고서", "20260506", "X1"),
        ("주요사항보고서(감자결정)", "20260522", "X2"),
        ("분기보고서 (2026.03)", "20260401", "X3"),   # routine → 제외돼야
    ))
    monkeypatch.setattr(api, "_quotes", lambda: {"005930": {"price": 75000}})

    out = api.signal_events_get("005930", market="kospi")
    assert out["ready"] and out["has_corp"]
    names = [d["name"] for d in out["disclosures"]]
    assert any("자기주식" in n for n in names)
    assert not any("분기보고서" in n for n in names)       # routine 공시는 필터
    kinds = {d["name"]: d["kind"] for d in out["disclosures"]}
    assert kinds["자기주식취득 결과보고서"] == "good"
    assert out["disclosures"][0]["date"] == "2026-05-06"   # YYYY-MM-DD 포맷
    assert out["dividend"]["dps"] == 1500
    assert out["dividend"]["div_yield"] == round(1500 / 75000 * 100, 2)
    assert out["upcoming"] == []   # KR은 미래 일정 소스 없음


def test_events_us_earnings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import api, store
    # 미래·과거 예정일 각각 — 과거는 제외돼야
    future = "2099-01-15"
    store._write_json(store.US_EARNINGS_FILE, {"AAPL": future, "OLD": "2000-01-01", "_fetched": "2099-01-01"})
    out = api.signal_events_get("AAPL", market="us")
    assert out["ready"] and out["market"] == "us"
    assert out["upcoming"] == [{"date": future, "label": "실적발표(예정)", "kind": "earnings"}]
    # 과거 예정일은 미노출
    assert api.signal_events_get("OLD", market="us")["upcoming"] == []
    # 캘린더에 없는 종목 → 빈 upcoming
    assert api.signal_events_get("ZZZZ", market="us")["upcoming"] == []


def test_earnings_calendar_csv_parse(monkeypatch):
    from signal_desk import config
    from signal_desk.ingest import alphavantage
    monkeypatch.setattr(config, "alphavantage_key", lambda: "k")
    csv = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
           "AAPL,Apple,2099-02-01,2098-12-31,2.1,USD\n"
           "AAPL,Apple,2099-05-01,2099-03-31,1.9,USD\n"   # 같은 종목 → 이른 날짜 채택
           "MSFT,Microsoft,2099-03-10,2099-02-28,3.0,USD\n")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return csv.encode()
    monkeypatch.setattr(alphavantage.urllib.request, "urlopen", lambda *a, **k: _Resp())
    cal = alphavantage.earnings_calendar("3month")
    assert cal == {"AAPL": "2099-02-01", "MSFT": "2099-03-10"}   # AAPL은 더 이른 날짜

    # 스로틀 응답(JSON) → {}
    class _R2(_Resp):
        def read(self): return b'{"Information":"rate limit"}'
    monkeypatch.setattr(alphavantage.urllib.request, "urlopen", lambda *a, **k: _R2())
    assert alphavantage.earnings_calendar() == {}
