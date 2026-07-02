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
        {"ISU_SRT_CD": "111110", "ISU_ABBRV": "가나다", "MKTCAP": "100"},
        {"ISU_SRT_CD": "222220", "ISU_ABBRV": "라마바", "MKTCAP": "300"},
        {"ISU_SRT_CD": "333330", "ISU_ABBRV": "사아자", "MKTCAP": "200"},
    ]
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: fake_rows)
    out = kapi.universe_by_marketcap("20260630", limit=2)
    assert [o["ticker"] for o in out] == ["222220", "333330"]


def test_universe_by_marketcap_handles_alternate_field_name(monkeypatch):
    fake_rows = [{"ISU_CD": "111110", "ISU_NM": "가나다", "MKT_CAP": "100"}]
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: fake_rows)
    out = kapi.universe_by_marketcap("20260630")
    assert out == [{"ticker": "111110", "name": "가나다"}]


def test_universe_by_marketcap_excludes_preferred_shares(monkeypatch):
    fake_rows = [
        {"ISU_CD": "005930", "ISU_NM": "삼성전자", "MKTCAP": "100"},
        {"ISU_CD": "005935", "ISU_NM": "삼성전자우", "MKTCAP": "999999"},  # 우선주, 시총 더 커도 제외돼야 함
    ]
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: fake_rows)
    out = kapi.universe_by_marketcap("20260630")
    assert out == [{"ticker": "005930", "name": "삼성전자"}]


def test_is_common_share():
    assert kapi._is_common_share("005930") is True
    assert kapi._is_common_share("005935") is False
    assert kapi._is_common_share("00104K") is False


def test_universe_by_marketcap_no_recognizable_field_returns_empty(monkeypatch):
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: [{"UNKNOWN": "x"}])
    assert kapi.universe_by_marketcap("20260630") == []


def test_universe_by_marketcap_empty_trading_returns_empty(monkeypatch):
    monkeypatch.setattr(kapi, "daily_trading", lambda bas_dd: [])
    assert kapi.universe_by_marketcap("20260630") == []
