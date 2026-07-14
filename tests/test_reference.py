from signal_desk.reference import cycle, valuechain


def _ind(key, value, change):
    return {"key": key, "label": key, "value": value, "change": change, "dir": 0, "unit": "%"}


def test_cycle_has_four_ordered_phases():
    ph = cycle.phases()
    assert [p["order"] for p in ph] == [1, 2, 3, 4]
    assert {p["key"] for p in ph} == {"recovery", "expansion", "slowdown", "contraction"}


def test_cycle_position_not_ready_without_macro():
    assert cycle.position([])["ready"] is False


def test_cycle_position_hot_cpi_rising_rates_is_slowdown():
    ind = [_ind("CPIAUCSL", 4.3, 0.3), _ind("FEDFUNDS", 5.0, 0.25), _ind("DGS10", 4.6, 0.1)]
    out = cycle.position(ind)
    assert out["ready"] is True
    assert out["phase_key"] == "slowdown"
    assert out["x"] == 0.625


def test_cycle_position_falling_cpi_and_rates_with_nasdaq_up_is_recovery():
    ind = [_ind("CPIAUCSL", 2.4, -0.2), _ind("FEDFUNDS", 3.0, -0.25),
           {"key": "NASDAQCOM", "value": 15000, "change": 1.5, "dir": 1, "unit": ""}]
    out = cycle.position(ind)
    assert out["phase_key"] == "recovery"


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
