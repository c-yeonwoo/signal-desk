import json

import pytest
from fastapi import HTTPException

from signal_desk.ingest import financial_evidence as intake
from signal_desk.signals import financial_change


def _report(year, *, revenue, profit, cash=None, inventory=None, currency="KRW", basis="CFS"):
    target = intake.Target("dart", "00126380", str(year), "11012", basis)
    items = []
    for statement, concept, amount, field in (
        ("IS", "ifrs-full_Revenue", revenue, "thstrm_amount"),
        ("IS", "dart_OperatingIncomeLoss", profit, "thstrm_amount"),
        ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", cash, "thstrm_amount"),
        ("BS", "ifrs-full_Inventories", inventory, "thstrm_amount"),
    ):
        if amount is None:
            continue
        items.append({"corp_code": target.issuer, "bsns_year": target.year,
                      "reprt_code": target.report, "fs_div": basis,
                      "rcept_no": f"{year}0814000001", "sj_div": statement,
                      "account_id": concept, "account_nm": concept,
                      "currency": currency, field: str(amount)})
    return target, {"status": "000", "list": items}


def _save(path, year, *, seen, **metrics):
    target, body = _report(year, **metrics)
    intake.archive(path, target, json.dumps(body).encode(), observed_at=seen)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(intake, "_now", lambda: "2026-10-03T00:00:00+00:00")


def test_same_report_comparison_and_source_links(tmp_path):
    path = tmp_path / "e.db"
    _save(path, 2025, seen="2026-10-03T01:00:00Z", revenue=100, profit=10, cash=20, inventory=30)
    _save(path, 2026, seen="2026-10-03T02:00:00Z", revenue=120, profit=18, cash=-2, inventory=50)
    result = financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                            as_of="2026-10-03T03:00:00Z")
    assert result["status"] == "comparison"
    assert result["metrics"]["revenue"]["change_pct"] == 20.0
    assert result["metrics"]["revenue"]["label"] == "매출액(3개월)"
    assert result["metrics"]["operating_cash_flow"]["label"] == "영업현금흐름(보고기간)"
    assert result["metrics"]["inventory"]["label"] == "재고자산(보고기말)"
    assert result["operating_margin_change_pp"] == 5.0
    assert result["metrics"]["operating_cash_flow"]["change_pct"] == -110.0
    assert result["metrics"]["revenue"]["current_source"].startswith("https://dart.fss.or.kr/")
    assert any("영업현금흐름이 음수" in message for message in result["cautions"])
    assert any("재고 증가율" in message for message in result["cautions"])
    assert result["source_available_at_verified"] is False and result["not_order_advice"] is True
    assert result["period_dates_verified"] is False
    assert "회계기간 시작·끝" in result["caveat"]


def test_asof_requires_both_reports_and_does_not_backfill(tmp_path):
    path = tmp_path / "e.db"
    _save(path, 2025, seen="2026-10-03T01:00:00Z", revenue=100, profit=10)
    _save(path, 2026, seen="2026-10-03T02:00:00Z", revenue=120, profit=12)
    assert financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                          as_of="2026-10-03T01:30:00Z")["status"] == "need_prior_year"
    assert financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                          as_of="2026-10-02T23:00:00Z")["status"] == "not_recorded"


def test_different_basis_and_currency_abstain(tmp_path):
    path = tmp_path / "e.db"
    target, body = _report(2025, revenue=100, profit=10, basis="OFS")
    intake.archive(path, target, json.dumps(body).encode(), observed_at="2026-10-03T01:00:00Z")
    _save(path, 2026, seen="2026-10-03T02:00:00Z", revenue=120, profit=12)
    report = financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                             as_of="2026-10-03T03:00:00Z")
    assert report["status"] == "need_prior_year"
    _save(path, 2025, seen="2026-10-03T01:30:00Z", revenue=100, profit=10, currency="USD")
    report = financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                             as_of="2026-10-03T03:00:00Z")
    assert report["status"] == "no_comparable_facts"


def test_duplicate_account_abstains_instead_of_picking_best(tmp_path):
    path = tmp_path / "e.db"
    _save(path, 2025, seen="2026-10-03T01:00:00Z", revenue=100, profit=10)
    target, body = _report(2026, revenue=120, profit=None)
    body["list"].append(dict(body["list"][0], thstrm_amount="999"))
    intake.archive(path, target, json.dumps(body).encode(), observed_at="2026-10-03T02:00:00Z")
    report = financial_change.describe_dart(path, ticker="005930", issuer="00126380",
                                             as_of="2026-10-03T03:00:00Z")
    assert report["status"] == "no_comparable_facts"


def test_watchlist_route_requires_own_favorite(monkeypatch, tmp_path):
    from signal_desk import api
    monkeypatch.setattr(api, "_uid", lambda _request: 7)
    monkeypatch.setattr(api.db, "fav_list", lambda _uid: [{"kind": "ticker", "key": "005930"}])
    monkeypatch.setattr(api, "_corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(financial_change, "DEFAULT_ARCHIVE", tmp_path / "e.db")
    assert api.watchlist_financial_change_get(object(), "kr", "005930")["status"] == "not_recorded"
    with pytest.raises(HTTPException) as exc:
        api.watchlist_financial_change_get(object(), "kr", "000660")
    assert exc.value.status_code == 403
