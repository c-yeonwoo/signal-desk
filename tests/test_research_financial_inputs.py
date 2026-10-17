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


def _original_zip(*, corp="00126380", currency="KRW", extra_dimension=False, missing_revenue=False):
    import io
    from zipfile import ZipFile
    dimensions = ('<d:explicitMember dimension="ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis">'
                  'ifrs-full:ConsolidatedMember</d:explicitMember>')
    if extra_dimension:
        dimensions += '<d:explicitMember dimension="custom:SegmentAxis">custom:SegmentMember</d:explicitMember>'
    contexts = []
    for cid, period in (("instant", "<instant>2025-12-31</instant>"),
                        ("current", "<startDate>2025-01-01</startDate><endDate>2025-12-31</endDate>"),
                        ("prior", "<startDate>2024-01-01</startDate><endDate>2024-12-31</endDate>")):
        contexts.append(f'<context id="{cid}"><entity><identifier scheme="DART">{corp}</identifier>'
                        f'<segment>{dimensions}</segment></entity><period>{period}</period></context>')
    facts = "".join(f'<ifrs-full:{name} contextRef="{cid}" unitRef="won">{value}</ifrs-full:{name}>'
                    for name, cid, value in (("Equity", "instant", 1000), ("Liabilities", "instant", 200),
                                              ("ProfitLoss", "current", 100), ("Revenue", "current", 120),
                                              ("Revenue", "prior", 100)) if not (missing_revenue and name == "Revenue"))
    xml = ('<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:ifrs-full="http://example/ifrs-full" '
           'xmlns:d="http://xbrl.org/2006/xbrldi">' + "".join(contexts)
           + f'<unit id="won"><measure>iso4217:{currency}</measure></unit>' + facts + '</xbrl>')
    out = io.BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("annual.xbrl", xml)
    return out.getvalue()


def test_original_totals_match_and_wrong_entity_currency_or_segment_cannot_substitute():
    from signal_desk.signals.research_financial_inputs import original_annual
    kwargs = dict(ticker="005930", corp_code="00126380", year=2025, receipt="20260310002820")
    result = original_annual(_original_zip(), **kwargs)
    assert result["metrics"] == normalize_annual(_rows(), **{k: kwargs[k] for k in ("ticker", "corp_code", "year")})["metrics"]
    assert result["original_document_matched"] is True and result["strict_pit_eligible"] is False
    for bad in (dict(corp="99999999"), dict(currency="USD"), dict(extra_dimension=True)):
        with pytest.raises(ValueError, match="totals unavailable"):
            original_annual(_original_zip(**bad), **kwargs)
    partial = original_annual(_original_zip(missing_revenue=True), **kwargs)
    assert partial["metrics"]["net_income"] == 100 and "revenue_growth" not in partial["metrics"]


def test_versions_change_only_after_the_amendment_and_failed_amendments_block_fallback():
    record = normalize_annual(_rows(), ticker="005930", corp_code="00126380", year=2025)
    amendment = {**copy.deepcopy(record), "filing_date": "2026-04-01",
                 "usable_from_session_assumption": "2026-04-02", "receipt": "20260401000001"}
    amendment["metrics"]["net_income"] = 200
    versions = {"start": "2026-03-01", "end": "2026-07-31", "tickers": ["005930"],
                "records": [record, amendment]}
    old, _ = metrics_asof([amendment], "2026-04-01", {"005930": 2000}, versions=versions)
    new, _ = metrics_asof([amendment], "2026-04-02", {"005930": 2000}, versions=versions)
    assert old["005930"]["per"] == 20 and new["005930"]["per"] == 10
    amendment["metrics"]["net_income"] = 99999
    assert metrics_asof([amendment], "2026-04-01", {"005930": 2000}, versions=versions)[0] == old
    amendment["status"] = "original_unavailable_or_ambiguous"
    missing, reason = metrics_asof([amendment], "2026-04-02", {"005930": 2000}, versions=versions)
    assert not missing and reason["005930"] == "original_unavailable_or_ambiguous"


def test_same_day_conflicting_versions_have_no_assumed_intraday_order():
    record = normalize_annual(_rows(), ticker="005930", corp_code="00126380", year=2025)
    other = copy.deepcopy(record)
    other["metrics"]["net_income"] = 999
    versions = {"start": "2026-03-01", "end": "2026-07-31", "tickers": ["005930"],
                "records": [record, other]}
    result, missing = metrics_asof([record], "2026-04-01", {"005930": 2000}, versions=versions)
    assert not result and missing["005930"] == "same_day_version_order_unverified"
