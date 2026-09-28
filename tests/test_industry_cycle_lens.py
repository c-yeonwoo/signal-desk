"""반도체 업황은 세부 분야·공식 근거·독립성·시점이 충족될 때만 연구 판정한다."""

import datetime as dt

import pytest

from signal_desk import db
from signal_desk.signals import industry_cycle, lenses


NOW = dt.datetime(2026, 9, 29, 12, tzinfo=dt.timezone.utc)


def _candidate(dimension="demand", issuer="MU", **extra):
    return {"segment": "memory", "dimension": dimension, "direction": "improving",
            "issuer": issuer, "period": "2026-09-01",
            "source_url": f"https://www.sec.gov/Archives/edgar/data/{issuer}/{dimension}",
            "source_published_at": "2026-09-20T12:00:00+00:00",
            "evidence_quote": f"Official filing evidence for {issuer} {dimension} shows improvement.", **extra}


def _approved_rows():
    rows = []
    for n, (dimension, issuer) in enumerate((("demand", "MU"), ("inventory", "000660"),
                                              ("pricing", "MU")), 1):
        rows.append({**industry_cycle.candidate(_candidate(dimension, issuer), observed_at=NOW),
                     "id": n, "review_verdict": "approved"})
    return rows


def test_official_source_and_observation_window_required():
    with pytest.raises(ValueError, match="공식"):
        industry_cycle.candidate(_candidate(source_url="https://example.com/repost"), observed_at=NOW)
    with pytest.raises(ValueError, match="120일"):
        industry_cycle.candidate(_candidate(source_published_at="2026-01-01T00:00:00+00:00"), observed_at=NOW)
    with pytest.raises(ValueError, match="20~500자"):
        industry_cycle.candidate(_candidate(evidence_quote="good"), observed_at=NOW)


def test_evidence_is_deduplicated_and_review_is_append_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = industry_cycle.candidate(_candidate(), observed_at=NOW)
    evidence_id = db.industry_evidence_add(record)
    assert evidence_id and db.industry_evidence_add(record) is None
    db.industry_evidence_review(evidence_id, verdict="approved", note="공식 공시 원문 대조 완료",
                                reviewed_at=NOW.isoformat())
    assert db.industry_evidence_list()[0]["review_verdict"] == "approved"
    db.industry_evidence_review(evidence_id, verdict="rejected", note="원문 정정 확인으로 철회",
                                reviewed_at=NOW.isoformat())
    assert db.industry_evidence_list()[0]["review_verdict"] == "rejected"


def test_three_dimensions_two_issuers_and_no_deterioration_for_pass():
    rows = _approved_rows()
    assert industry_cycle.assess("memory", rows, as_of=NOW)["verdict"] == "pass"
    assert industry_cycle.assess("memory", rows[:2], as_of=NOW)["verdict"] == "unavailable"
    assert industry_cycle.assess("foundry", rows, as_of=NOW)["verdict"] == "unavailable"
    assert industry_cycle.assess("memory", [{**r, "issuer": "MU"} for r in rows], as_of=NOW)["verdict"] == "unavailable"
    assert industry_cycle.assess("memory", [{**rows[0], "direction": "deteriorating"}, *rows[1:]], as_of=NOW)["verdict"] == "hold"
    assert industry_cycle.assess("memory", rows, as_of=NOW + dt.timedelta(days=121))["verdict"] == "unavailable"


def test_ambiguous_company_is_not_mapped_or_promoted_to_orders():
    row = {"ticker": "005930", "name": "삼성전자", "kind": "BUY", "score": 2,
           "price": 100, "data_coverage": 0.9, "factor_scores": {"momentum": 1}}
    rows = [dict(row)]
    snapshot = lenses.build_snapshot(rows, market="kr", signal_policy_id="p",
                                     industry_evidence=_approved_rows(),
                                     observed_at=int(NOW.timestamp()))
    assert rows[0]["lens_results"]["industry_cycle"]["verdict"] == "unavailable"
    assert snapshot["order_eligible"] is False and rows[0]["kind"] == "BUY"
    mapped = [{**row, "ticker": "000660", "name": "SK하이닉스"}]
    lenses.build_snapshot(mapped, market="kr", signal_policy_id="p",
                          industry_evidence=_approved_rows(), observed_at=int(NOW.timestamp()))
    assert mapped[0]["lens_results"]["industry_cycle"]["verdict"] == "pass"
