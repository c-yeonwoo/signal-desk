import os

from signal_desk.ingest import dart


def test_derive_metrics_from_sample_items():
    items = [
        {"account_nm": "자산총계", "thstrm_amount": "1,000,000", "frmtrm_amount": "900,000"},
        {"account_nm": "부채총계", "thstrm_amount": "400,000", "frmtrm_amount": "380,000"},
        {"account_nm": "자본총계", "thstrm_amount": "600,000", "frmtrm_amount": "520,000"},
        {"account_nm": "매출액", "thstrm_amount": "300,000", "frmtrm_amount": "250,000"},
        {"account_nm": "당기순이익", "thstrm_amount": "60,000", "frmtrm_amount": "40,000"},
    ]
    metrics = dart._derive_metrics(items)
    assert metrics["roe"] == round(60_000 / 600_000 * 100, 2)
    assert metrics["debt_ratio"] == round(400_000 / 600_000 * 100, 2)
    assert metrics["revenue_growth"] == round((300_000 - 250_000) / 250_000 * 100, 2)
    assert metrics["net_income"] == 60_000
    assert metrics["equity"] == 600_000


def test_derive_metrics_prefers_consolidated_over_separate():
    # 삼성전자 실응답 패턴: 같은 계정명이 CFS(연결)·OFS(별도)로 값이 다르게 옴 -> CFS를 써야 함
    items = [
        {"fs_div": "OFS", "account_nm": "자본총계", "thstrm_amount": "254,330,083"},
        {"fs_div": "CFS", "account_nm": "자본총계", "thstrm_amount": "436,320,337"},
        {"fs_div": "OFS", "account_nm": "당기순이익(손실)", "thstrm_amount": "33,686,601"},
        {"fs_div": "CFS", "account_nm": "당기순이익(손실)", "thstrm_amount": "45,206,805"},
    ]
    metrics = dart._derive_metrics(items)
    assert metrics["equity"] == 436_320_337
    assert metrics["net_income"] == 45_206_805


def test_derive_metrics_missing_fields_are_skipped():
    metrics = dart._derive_metrics([{"account_nm": "자산총계", "thstrm_amount": "1,000"}])
    assert metrics == {}


def test_no_key_returns_none_and_empty(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    assert dart.corp_codes() == {}
    assert dart.fundamentals("005930", "00126380", "2025") is None
