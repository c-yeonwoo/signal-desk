"""리스트 API 슬림화 — 요약만 반환, 상세는 /detail, 차트는 상한·단일 패스."""

from types import SimpleNamespace

from signal_desk import api
from signal_desk.signals import engine


def _sig(**kw):
    base = dict(ticker="AAA", name="A", score=1.5, kind="BUY", confidence=0.7,
                technical_score=1.0, fundamental_score=0.0, has_fundamental=False,
                has_reversion=False, reversion_score=0.0, has_valuation=False,
                valuation_percentile=None, has_qualitative=False, qualitative_score=None,
                factor_scores={"technical": 0.5}, reasons=["[기술] x"], narrative="긴 해설",
                event_risk=False, earnings_soon=False, earnings_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_list_row_omits_heavy_fields():
    r = _sig()
    row = api._list_row_from_signal(
        r, name="에이", sector="기술", price=10.0, change_pct=1.0,
        mktcap=1e9, vol=100, vol_avg=90, per=12.0, pbr=1.2)
    assert row["ticker"] == "AAA" and row["score"] == 1.5 and row["opp_tags"] is not None
    for k in ("reasons", "narrative", "about", "moves", "target", "kb", "intro"):
        assert k not in row


def test_us_list_is_slim(monkeypatch):
    r = _sig(ticker="AAPL", name="Apple")
    monkeypatch.setattr(api, "_us_signals", lambda: {"AAPL": r})
    monkeypatch.setattr(api.store, "load_us_universe",
                        lambda: [{"ticker": "AAPL", "name": "Apple", "sector": "Tech"}])
    monkeypatch.setattr(api.store, "load_us_price_bundle",
                        lambda: ({"AAPL": [100.0, 101.0]}, {"AAPL": {"vol": 1, "vol_avg": 1}}))
    monkeypatch.setattr(api.store, "us_marketcaps", lambda hist: {"AAPL": {"mktcap": 1, "per": 20, "pbr": 5}})
    api._us_signal_items.cache_clear()
    items = api._us_signal_items()
    assert len(items) == 1
    assert "about" not in items[0] and "target" not in items[0] and "reasons" not in items[0]


def test_chart_scores_and_zones_single_pass():
    dates = [f"2026-01-{i:02d}" for i in range(1, 31)]
    closes = [100.0 + i * 0.3 for i in range(30)]
    scores, zones = engine.chart_scores_and_zones(dates, closes)
    assert len(scores) == 30
    assert engine.daily_signal_scores(dates, closes) == scores
    assert engine.signal_zones(dates, closes) == zones


def test_signal_chart_truncates(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import importlib
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)
    client.post("/api/auth/signup", json={"email": "slim@b.com", "pw": "abcdef"})
    import datetime as _dt
    start = _dt.date(2024, 1, 1)
    history = [{"date": (start + _dt.timedelta(days=i)).isoformat(), "close": 100.0 + i}
               for i in range(500)]
    monkeypatch.setattr(api_module.store, "load_price_history", lambda t: history)
    monkeypatch.setattr(api_module.store, "signal_history_for", lambda t: {})
    from signal_desk.ingest import naver
    monkeypatch.setattr(naver, "investor_flow_series", lambda *a, **k: None)
    d = client.get("/api/signals/005930/chart").json()
    assert d["ready"] and len(d["dates"]) == api_module._CHART_BARS
    assert len(d["scores"]) == api_module._CHART_BARS
