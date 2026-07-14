"""네이버 일별 수급 시계열 파싱."""

from signal_desk.ingest import naver


def test_trend_date_formats():
    assert naver._trend_date({"bizdate": "20260714"}) == "2026-07-14"
    assert naver._trend_date({"localTradedAt": "2026-07-14T00:00:00"}) == "2026-07-14"
    assert naver._trend_date({}) is None


def test_investor_flow_series_parses_and_sorts(monkeypatch):
    rows = [
        {"bizdate": "20260714", "foreignerPureBuyQuant": "+100", "organPureBuyQuant": "-50",
         "accumulatedTradingVolume": "1,000"},
        {"bizdate": "20260710", "foreignerPureBuyQuant": "-20", "organPureBuyQuant": "+30",
         "accumulatedTradingVolume": "800"},
    ]

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            import json
            return json.dumps(rows).encode()

    monkeypatch.setattr(naver.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = naver.investor_flow_series("005930", days=10)
    assert out and len(out) == 2
    assert out[0]["date"] == "2026-07-10"  # 오래된→최신
    assert out[0]["foreign_net"] == -20
    assert out[1]["inst_net"] == -50


def test_investor_flow_aggregates_series(monkeypatch):
    monkeypatch.setattr(naver, "investor_flow_series", lambda code, days=20: [
        {"date": "2026-07-10", "foreign_net": 10, "inst_net": 5, "volume": 100},
        {"date": "2026-07-11", "foreign_net": -3, "inst_net": 2, "volume": 80},
    ])
    agg = naver.investor_flow("005930", days=20)
    assert agg == {"foreign_net": 7, "inst_net": 7, "total_buy": 180}
