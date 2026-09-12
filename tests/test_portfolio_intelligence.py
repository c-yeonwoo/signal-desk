from signal_desk.signals import portfolio_intelligence as pi


PROFILE = {
    "cash": 0, "monthly_contribution": 0, "horizon_months": 36,
    "max_drawdown_pct": 20, "max_single_position_pct": 40,
    "max_sector_pct": 55, "max_cluster_pct": 60, "min_cash_pct": 5,
    "configured": True,
}


def test_reports_concentration_using_total_assets_including_cash():
    out = pi.analyze(
        rows=[
            {"ticker": "A", "name": "A", "value": 600, "sector": "tech", "history_ready": True},
            {"ticker": "B", "name": "B", "value": 200, "sector": "health", "history_ready": True},
        ],
        cash=200, profile=PROFILE,
        risk={"clusters": [{"tickers": ["A", "B"], "weight_pct": 100.0}], "pair_coverage": {"pct": 100}},
        market="us", currency="USD", as_of="2026-09-12",
    )
    assert out["summary"] == {"total_value": 1000.0, "invested_value": 800.0, "cash": 200.0,
                              "cash_pct": 20.0, "positions": 2}
    # A는 총자산의 60%라 단일종목 한도 위반, 고상관 묶음은 현금 포함 80%로 계산한다.
    assert any(g["kind"] == "concentration" and g["ticker"] == "A" for g in out["guidance"])
    cluster = next(g for g in out["guidance"] if g["kind"] == "correlation")
    assert cluster["current_pct"] == 80.0
    assert out["data_quality"]["status"] == "complete"


def test_missing_price_is_a_blocker_not_a_fake_valuation():
    out = pi.analyze(
        rows=[{"ticker": "UNKNOWN", "name": "?", "value": None, "sector": None, "history_ready": False}],
        cash=0, profile=PROFILE, risk={"clusters": []}, market="kr", currency="KRW", as_of="2026-09-12",
    )
    assert out["summary"]["total_value"] == 0.0
    assert out["data_quality"]["status"] == "insufficient"
    assert out["guidance"][0]["action"] == "시세 데이터 보완"


def test_profile_and_snapshot_are_market_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import importlib
    from signal_desk import db
    importlib.reload(db)
    assert db.portfolio_profile_get(7, "kr")["configured"] is False
    saved = db.portfolio_profile_set(7, "kr", {"cash": 1_000_000, "max_sector_pct": 30})
    assert saved["configured"] is True and saved["cash"] == 1_000_000
    assert db.portfolio_profile_get(7, "us")["cash"] == 0.0
    sid = db.portfolio_snapshot_add(7, "kr", as_of="2026-09-12", source="test", total_value=1_000_000,
                                    data_quality="complete", payload={"safe": True})
    assert sid > 0
