"""네이버 일별 수급 시계열 파싱."""

from signal_desk.ingest import naver


def test_trend_date_formats():
    assert naver._trend_date({"bizdate": "20260714"}) == "2026-07-14"
    assert naver._trend_date({"localTradedAt": "2026-07-14T00:00:00"}) == "2026-07-14"
    assert naver._trend_date({}) is None


def test_investor_flow_series_parses_and_sorts(monkeypatch):
    naver._FLOW_CACHE.clear()
    rows = [
        {"bizdate": "20260714", "foreignerPureBuyQuant": "+100", "organPureBuyQuant": "-50",
         "accumulatedTradingVolume": "1,000"},
        {"bizdate": "20260710", "foreignerPureBuyQuant": "-20", "organPureBuyQuant": "+30",
         "accumulatedTradingVolume": "800"},
    ]
    calls = {"n": 0}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            import json
            return json.dumps(rows).encode()

    def open_once(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(naver.urllib.request, "urlopen", open_once)
    out = naver.investor_flow_series("005930", days=10)
    assert out and len(out) == 2
    assert out[0]["date"] == "2026-07-10"  # 오래된→최신
    assert out[0]["foreign_net"] == -20
    assert out[1]["inst_net"] == -50
    assert naver.investor_flow_series("005930", days=10) is out  # TTL 캐시 — HTTP 재호출 없음
    assert calls["n"] == 1


def test_investor_flow_aggregates_series(monkeypatch):
    monkeypatch.setattr(naver, "investor_flow_series", lambda code, days=20: [
        {"date": "2026-07-10", "foreign_net": 10, "inst_net": 5, "volume": 100},
        {"date": "2026-07-11", "foreign_net": -3, "inst_net": 2, "volume": 80},
    ])
    agg = naver.investor_flow("005930", days=20)
    assert agg == {"foreign_net": 7, "inst_net": 7, "total_buy": 180}


def test_missing_flow_field_is_not_silently_treated_as_zero(monkeypatch):
    naver._FLOW_CACHE.clear()
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            import json
            return json.dumps([
                {"bizdate": "20260923", "foreignerPureBuyQuant": "-",
                 "organPureBuyQuant": "+5", "accumulatedTradingVolume": "100"},
                {"bizdate": "20260922", "foreignerPureBuyQuant": "0",
                 "organPureBuyQuant": "+5", "accumulatedTradingVolume": "100"},
            ]).encode()
    monkeypatch.setattr(naver.urllib.request, "urlopen", lambda *args, **kwargs: _Resp())
    rows = naver.investor_flow_series("005930", days=10)
    assert rows == [{"date": "2026-09-22", "foreign_net": 0.0, "inst_net": 5.0, "volume": 100.0}]
