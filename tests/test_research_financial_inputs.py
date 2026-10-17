"""Core financial failure cases only: time leakage, identity and basis mixing."""

import copy

import pytest

from signal_desk.signals.research_financial_inputs import metrics_asof, normalize_annual


def _rows():
    return [{"stock_code": "005930", "corp_code": "00126380", "bsns_year": "2025",
             "reprt_code": "11011", "rcept_no": "20260310002820", "currency": "KRW",
             "fs_div": "CFS", "account_nm": name, "thstrm_dt": "2025.12.31 현재",
             "thstrm_amount": amount, "frmtrm_amount": "100"}
            for name, amount in (("자본총계", "1000"), ("부채총계", "200"),
                                 ("당기순이익", "100"), ("매출액", "120"))]


def test_receipt_is_dated_and_same_day_financials_are_not_assumed_known():
    record = normalize_annual(_rows(), ticker="005930", corp_code="00126380", year=2025)
    assert record["metrics"]["roe"] == 10
    assert record["usable_from_session_assumption"] == "2026-03-11"
    assert record["strict_pit_eligible"] is False and record["original_document_matched"] is False
    later = copy.deepcopy(record)
    later["usable_from_session_assumption"] = "2026-04-02"
    metrics, missing = metrics_asof([later], "2026-04-01", {"005930": 2000})
    assert not metrics and missing["005930"] == "returned_filing_not_available_at_decision"
    metrics, missing = metrics_asof([record], "2026-04-01", {"005930": 2000})
    assert metrics["005930"]["per"] == 20 and metrics["005930"]["pbr"] == 2


def test_missing_consolidated_account_is_not_filled_from_separate_statement():
    rows = [r for r in _rows() if r["account_nm"] != "당기순이익"]
    rows += [{**_rows()[2], "fs_div": "OFS", "thstrm_amount": "999"}]
    record = normalize_annual(rows, ticker="005930", corp_code="00126380", year=2025)
    assert "net_income" not in record["metrics"] and "roe" not in record["metrics"]
    assert record["fs_div"] == "CFS"


def test_identical_duplicate_income_is_not_a_conflict_but_differing_income_is():
    rows = _rows()
    rows.append({**rows[2], "ord": "99"})
    assert normalize_annual(rows, ticker="005930", corp_code="00126380", year=2025)["metrics"]["net_income"] == 100
    rows[-1]["thstrm_amount"] = "999"
    with pytest.raises(ValueError, match="ambiguous financial account"):
        normalize_annual(rows, ticker="005930", corp_code="00126380", year=2025)


@pytest.mark.parametrize("field,value", [("corp_code", "99999999"), ("currency", "USD"),
                                          ("rcept_no", "20260311002821")])
def test_company_currency_or_conflicting_receipt_is_rejected(field, value):
    rows = _rows()
    rows[0][field] = value
    with pytest.raises(ValueError):
        normalize_annual(rows, ticker="005930", corp_code="00126380", year=2025)


def test_protected_decisions_and_duplicate_years_are_rejected():
    record = normalize_annual(_rows(), ticker="005930", corp_code="00126380", year=2025)
    with pytest.raises(ValueError):
        metrics_asof([record], "2026-08-05", {"005930": 2000})
    with pytest.raises(ValueError):
        metrics_asof([record, record], "2026-04-01", {"005930": 2000})


def test_missing_selected_outcome_blocks_the_mean_instead_of_dropping_a_loser():
    from scripts.measure.kr_receipt_financial_lab import daily_outcome
    from types import SimpleNamespace
    signals = [SimpleNamespace(ticker=t, kind="BUY") for t in ("005930", "000660")]
    rows = [{"ticker": "005930", "horizons": {"5": {"state": "matured", "net_pct": 10}}},
            {"ticker": "000660", "horizons": {"5": {"state": "price_gap"}}}]
    result = daily_outcome(signals, rows)
    assert result["h5_mean_net_pct"] is None and result["selected_missing_outcomes"] == 1
    assert daily_outcome([], rows)["h5_mean_net_pct"] is None  # no BUY is not an observed 0% trade
