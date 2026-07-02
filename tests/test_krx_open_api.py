from signal_desk.ingest import krx_open_api as kapi


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    assert kapi.stock_base_info("20260630") == []
    assert kapi.daily_trading("20260630") == []


def test_pick_first_matching_candidate():
    row = {"ISU_NM": "삼성전자", "OTHER": "x"}
    assert kapi._pick(row, kapi._NAME_FIELDS) == "삼성전자"
    assert kapi._pick(row, kapi._CODE_FIELDS) is None


def test_universe_by_marketcap_ranks_and_limits(monkeypatch):
    fake_rows = [
        {"ISU_SRT_CD": "AAA", "ISU_ABBRV": "가나다", "MKTCAP": "100"},
        {"ISU_SRT_CD": "BBB", "ISU_ABBRV": "라마바", "MKTCAP": "300"},
        {"ISU_SRT_CD": "CCC", "ISU_ABBRV": "사아자", "MKTCAP": "200"},
    ]
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: fake_rows)
    out = kapi.universe_by_marketcap("20260630", limit=2)
    assert [o["ticker"] for o in out] == ["BBB", "CCC"]


def test_universe_by_marketcap_handles_alternate_field_name(monkeypatch):
    fake_rows = [{"ISU_CD": "AAA", "ISU_NM": "가나다", "MKT_CAP": "100"}]
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: fake_rows)
    out = kapi.universe_by_marketcap("20260630")
    assert out == [{"ticker": "AAA", "name": "가나다"}]


def test_universe_by_marketcap_no_recognizable_field_returns_empty(monkeypatch):
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: [{"UNKNOWN": "x"}])
    assert kapi.universe_by_marketcap("20260630") == []


def test_universe_by_marketcap_empty_trading_returns_empty(monkeypatch):
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: [])
    assert kapi.universe_by_marketcap("20260630") == []
