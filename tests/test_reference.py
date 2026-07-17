from datetime import date, timedelta

from signal_desk.reference import cycle, valuechain


def _ind(key, value, change):
    return {"key": key, "label": key, "value": value, "change": change, "dir": 0, "unit": "%"}


def test_cycle_has_four_ordered_phases():
    ph = cycle.phases()
    assert [p["order"] for p in ph] == [1, 2, 3, 4]
    assert {p["key"] for p in ph} == {"recovery", "expansion", "slowdown", "contraction"}


def test_cycle_raw_not_ready_without_macro():
    assert cycle.raw_position([])["ready"] is False


def test_cycle_raw_hot_cpi_rising_rates_is_slowdown():
    ind = [_ind("CPIAUCSL", 4.3, 0.3), _ind("FEDFUNDS", 5.0, 0.25), _ind("DGS10", 4.6, 0.1)]
    out = cycle.raw_position(ind)
    assert out["ready"] is True
    assert out["phase_key"] == "slowdown"
    assert out["x"] == 0.625


def test_cycle_raw_falling_cpi_and_rates_with_nasdaq_up_is_recovery():
    ind = [_ind("CPIAUCSL", 2.4, -0.2), _ind("FEDFUNDS", 3.0, -0.25),
           {"key": "NASDAQCOM", "value": 15000, "change": 1.5, "dir": 1, "unit": ""}]
    out = cycle.raw_position(ind)
    assert out["phase_key"] == "recovery"


def test_stabilize_holds_until_confirm_days(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    slow = cycle.raw_position([_ind("CPIAUCSL", 4.3, 0.3), _ind("FEDFUNDS", 5.0, 0.25)])
    rec = cycle.raw_position([_ind("CPIAUCSL", 2.4, -0.2), _ind("FEDFUNDS", 3.0, -0.25),
                              {"key": "NASDAQCOM", "value": 15000, "change": 1.5}])
    t0 = date(2026, 7, 1)
    out0, st0 = cycle.stabilize(slow, None, today=t0, confirm_days=7)
    assert out0["phase_key"] == "slowdown" and st0["phase_key"] == "slowdown"

    # 다음날 원시가 회복으로 바뀌어도 확정은 둔화 유지
    out1, st1 = cycle.stabilize(rec, st0, today=t0 + timedelta(days=1), confirm_days=7)
    assert out1["phase_key"] == "slowdown"
    assert out1["raw_phase_key"] == "recovery"
    assert out1["stable"] is False
    assert out1["pending_days"] == 0  # pending 시작일 = today → 0일째

    # 6일째까지는 아직 전환 안 됨
    out6, st6 = cycle.stabilize(rec, st1, today=t0 + timedelta(days=1 + 6), confirm_days=7)
    assert out6["phase_key"] == "slowdown"
    assert out6["pending_days"] == 6

    # 7일 경과 + 동일 raw → 전환
    out7, st7 = cycle.stabilize(rec, st6, today=t0 + timedelta(days=1 + 7), confirm_days=7)
    assert out7["phase_key"] == "recovery"
    assert out7["stable"] is True
    assert st7["phase_key"] == "recovery"


def test_position_persists_stable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ind = [_ind("CPIAUCSL", 4.3, 0.3), _ind("FEDFUNDS", 5.0, 0.25)]
    a = cycle.position(ind, today=date(2026, 7, 1))
    assert a["phase_key"] == "slowdown"
    # 다른 raw여도 확정 유지
    ind2 = [_ind("CPIAUCSL", 2.4, -0.2), _ind("FEDFUNDS", 3.0, -0.25),
            {"key": "NASDAQCOM", "value": 15000, "change": 1.5}]
    b = cycle.position(ind2, today=date(2026, 7, 2))
    assert b["phase_key"] == "slowdown"
    assert b["raw_phase_key"] == "recovery"


def test_risk_sentiment_vix():
    fear = cycle.risk_sentiment([{"key": "VIXCLS", "value": 30, "change": 1}])
    assert fear["label"] == "fear" and fear["kb_hint_phase_key"] == "contraction"
    calm = cycle.risk_sentiment([{"key": "VIXCLS", "value": 12, "change": -1}])
    assert calm["label"] == "calm" and calm["kb_hint_phase_key"] == "expansion"


def test_valuechain_sectors_have_three_stages_with_companies():
    for s in valuechain.sectors():
        assert len(s["stages"]) == 3
        for st in s["stages"]:
            assert st["domestic"] and st["overseas"]


def test_valuechain_tag_maps_to_sector_key():
    assert valuechain.key_for_tag("반도체") == "semiconductor"
    assert valuechain.key_for_tag("방산") == "defense"
    assert valuechain.key_for_tag("은행·금융") == "finance"
    assert valuechain.key_for_tag("은행/금융") == "finance"  # 표기 변형
    assert valuechain.key_for_tag("존재하지않는섹터") is None


def test_valuechain_sector_lookup():
    assert valuechain.sector("battery")["name"] == "2차전지"
    assert valuechain.sector("nope") is None


def test_tickers_for_lead_tags():
    rows = valuechain.tickers_for_lead_tags(["반도체", "방산"], limit=6)
    assert rows and all("ticker" in r for r in rows)
    assert any(r["ticker"] == "005930" for r in rows)  # 삼성전자 in 반도체 VC
