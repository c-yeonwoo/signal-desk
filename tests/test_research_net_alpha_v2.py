"""Source/time boundary tests; all certificates here are synthetic test fixtures."""

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha_v2 as lab
from signal_desk.signals.research_action_coverage import SourceWindow, CoverageCertificate

ISIN, OTHER = "KR7005930003", "KR0000000002"


def spec():
    return {**json.loads(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_text()),
        "candidate_id": "kr-separated-net-alpha-h5-v2", "universe_count": 2,
        "min_feature_eligible": 2, "min_complete_train_dates": 1, "min_complete_calibration_dates": 1}


def fact(kind, effective, published="2021-01-01", **terms):
    return {"fact_id": f"fixture-{kind}-{effective}", "event_id": f"fixture-{kind}-{effective}",
        "security_id": ISIN, "kind": kind, "effective_at": effective,
        "published_on": published, "source_span": "SYNTHETIC TEST ONLY", "currency": "KRW", **terms}


def reviewed(facts=(), securities=(ISIN, OTHER)):
    """Never used by a real-data command or promoted to empirical certification."""
    documents, bound = {}, []
    for row in facts:
        raw = json.dumps({**row, "fixture": "SYNTHETIC TEST ONLY"}).encode()
        sha = hashlib.sha256(raw).hexdigest()
        documents[sha] = raw
        bound.append({**row, "source_sha256": sha})
    certificates = []
    for isin in securities:
        windows = []
        for family in ("synthetic A", "synthetic B"):
            raw = f"SYNTHETIC TEST ONLY {family} {isin}".encode()
            sha = hashlib.sha256(raw).hexdigest(); documents[sha] = raw
            windows.append(SourceWindow("kr", isin, family, family, "2020-01-01", "2024-01-31",
                "effective", lab.LABEL_KINDS, "complete_for_source", (sha,),
                dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc), True))
        reconciliation = {"schema": "net-alpha-reconciliation-v1", "security_id": isin,
            "start": "2020-01-01", "end": "2024-01-31", "required_kinds": sorted(lab.LABEL_KINDS),
            "page_hashes": sorted(w.page_hashes[0] for w in windows), "unresolved_event_ids": [],
            "fact_ids": sorted(f["fact_id"] for f in bound if f["security_id"] == isin)}
        raw = json.dumps(reconciliation).encode()
        sha = hashlib.sha256(raw).hexdigest(); documents[sha] = raw
        certificates.append(CoverageCertificate("kr", isin, "2020-01-01", "2024-01-31",
            lab.LABEL_KINDS, tuple(windows), sha))
    return lab.Evidence(documents, certificates, bound)


def panel():
    days = [s.date().isoformat() for s in market_clock._calendar("kr").sessions_in_range(
        "2020-01-02", "2021-02-01")][:253]
    prices = {d: {t: {"TDD_OPNPRC": 100+i*.1+np.sin(i),
        "TDD_CLSPRC": 100+i*.1+np.sin(i), "MKTCAP": 1e10,
        "ACC_TRDVOL": 1000, "LIST_SHRS": 1_000_000}
        for t in ("A", "B")} for i, d in enumerate(days)}
    return days, prices, [{"ticker": "A", "isin": ISIN}, {"ticker": "B", "isin": OTHER}]


def test_no_trade_is_excluded_but_missing_source_blocks_day():
    days, prices, roster = panel()
    prices[days[-1]]["A"]["TDD_OPNPRC"] = 0
    row = lab.universe(days[-1], roster, days, prices, reviewed(), spec())
    assert row["rows"][0]["state"] == "excluded"
    assert row["rows"][0]["reasons"] == ["no_trade_observed_t"]
    assert row["state"] == "no_new_decision_insufficient_universe"
    del prices[days[-1]]["A"]["TDD_OPNPRC"]
    assert lab.universe(days[-1], roster, days, prices, reviewed(), spec())["state"] == "input_gap"


def test_future_fact_does_not_change_past_universe_and_known_halt_is_excluded():
    days, prices, roster = panel(); day = days[-1]
    future = fact("halt", f"{day}T15:29:00+09:00", published="2022-01-03")
    original = lab.universe(day, roster, days, prices, reviewed(), spec())
    assert lab.universe(day, roster, days, prices, reviewed([future]), spec()) == original
    known = {**future, "published_on": day, "published_at": f"{day}T15:29:00+09:00"}
    row = lab.universe(day, roster, days, prices, reviewed([known]), spec())
    assert row["rows"][0]["reasons"] == ["known_halt"]
    date_only = {k: v for k, v in known.items() if k != "published_at"}
    assert lab.universe(day, roster, days, prices, reviewed([date_only]), spec()) == original


def test_positive_prices_cannot_create_missing_action_coverage():
    days, prices, roster = panel()
    row = lab.universe(days[-1], roster, days, prices, lab.Evidence(), spec())
    assert row["state"] == "input_gap" and row["features"] is None
    assert row["provisional_tickers"] == ["A", "B"] and row["tickers"] == []


def test_untimed_release_and_minute_precision_use_conservative_availability():
    assert lab.available_at({"published_on": "2021-03-01"}) == lab.session_time("2021-03-02", "open")
    row = {"published_on": "2021-03-02", "published_at": "2021-03-02T15:30:00+09:00",
           "publication_precision": "minute"}
    assert lab.available_at(row) > lab.session_time("2021-03-02")


def inputs():
    day = "2021-03-02"; sessions = market_clock.next_sessions("kr", day, 6)
    prices = {d: {"A": {"TDD_OPNPRC": 100 if i == 0 else 110, "ACC_TRDVOL": 1000}}
              for i, d in enumerate(sessions)}
    identity = {d: {"A": ISIN} for d in sessions}
    return day, sessions, prices, identity


def outcome(source=None):
    day, sessions, prices, identity = inputs()
    return lab.label(day, "A", ISIN, sessions, prices, identity, source or reviewed(),
                     lab.session_time("2021-12-30"))


def test_label_uses_original_fixed_horizon_without_a_supplied_return():
    row = outcome()
    assert row["return"] == pytest.approx(.1)
    assert row["state"] == "verified_economic_total_return"
    day, sessions, prices, identity = inputs()
    with pytest.raises(ValueError, match="t\\+6"):
        lab.label(day, "A", ISIN, sessions[:-1], prices, identity, reviewed(),
                  lab.session_time("2021-12-30"))


@pytest.mark.parametrize("endpoint,expected", [(0, "not_enterable"), (-1, "censored_exit")])
def test_missing_open_is_never_a_zero_return(endpoint, expected):
    day, sessions, prices, identity = inputs()
    prices[sessions[endpoint]]["A"] = {"TDD_OPNPRC": 0, "ACC_TRDVOL": 0}
    row = lab.label(day, "A", ISIN, sessions, prices, identity, reviewed(),
                    lab.session_time("2021-12-30"))
    assert row["state"] == expected and row["return"] is None


def test_source_gap_is_not_proven_non_execution():
    day, sessions, prices, identity = inputs()
    del prices[sessions[0]]["A"]
    row = lab.label(day, "A", ISIN, sessions, prices, identity, reviewed(),
                    lab.session_time("2021-12-30"))
    assert row["state"] == "source_missing" and row["return"] is None


def test_dividend_is_marked_and_future_final_fact_does_not_enter_training():
    _, sessions, _, _ = inputs()
    row = fact("cash_dividend", f"{sessions[2]}T09:00:00+09:00", cash_per_share=3, amount_status="final")
    result = outcome(reviewed([row]))
    assert result["return"] == pytest.approx(.13) and result["cash_and_receivables"] == 3
    late = {**row, "published_on": "2022-01-04"}
    result = outcome(reviewed([late]))
    assert result["return"] is None and result["reason"] == "entitlement_facts_after_model_freeze"


def test_incomplete_date_does_not_delete_or_mutate_evaluation_snapshot():
    snapshot = {"date": "2021-03-02", "state": "ready", "tickers": ["A", "B"],
                "features": {"stock": [[0]*11]*2, "market": [0]*4}}
    saved = copy.deepcopy(snapshot)
    assert lab.sample(snapshot, {"A": outcome()}) is None
    assert snapshot == saved


def test_reconciled_source_cannot_hide_an_unknown_share_count_change():
    days, prices, roster = panel()
    prices[days[-1]]["A"]["LIST_SHRS"] = 2_000_000
    result = lab.universe(days[-1], roster, days, prices, reviewed(), spec())
    assert result["rows"][0]["reasons"] == ["listed_share_changes_not_reconciled"]
    assert result["features"] is None


def test_cash_exchange_has_a_value_without_a_fictitious_exit_stock_price():
    _, sessions, _, _ = inputs()
    event = fact("cash_exchange", f"{sessions[2]}T09:00:00+09:00",
                 cash_per_share=120, amount_status="final")
    result = outcome(reviewed([event]))
    assert result["return"] == pytest.approx(.2)
    assert result["cash_and_receivables"] == 120 and result["components"] == []


def test_verified_source_does_not_impute_an_unpriced_successor():
    _, sessions, _, _ = inputs()
    event = fact("merger_exchange", f"{sessions[2]}T09:00:00+09:00", terms_status="final",
                 components=[{"security_id": OTHER, "ratio": .5}], conversion_basis="all_assets_per_old_share")
    result = outcome(reviewed([event]))
    assert result["state"] == "unpriced_right" and result["return"] is None


def test_evidence_loader_rejects_path_escape_and_changed_review_scope(tmp_path):
    from scripts.measure.kr_net_alpha_v2 import evidence
    path = tmp_path/"packet.json"
    path.write_text(json.dumps({"schema": "net-alpha-reviewed-evidence-v1",
        "documents": [{"path": "../outside", "sha256": "0"*64}], "facts": [], "certificates": []}))
    with pytest.raises(ValueError, match="path"):
        evidence(path, base=tmp_path)
    store = reviewed(); cert = store.certificates[0]
    raw = json.loads(store.documents[cert.reconciliation_sha256])
    raw["start"] = "2021-01-01"
    encoded = json.dumps(raw).encode(); sha = hashlib.sha256(encoded).hexdigest()
    from dataclasses import replace
    changed = replace(cert, reconciliation_sha256=sha)
    with pytest.raises(ValueError, match="scope differs"):
        lab.Evidence({**store.documents, sha: encoded}, [changed])


def test_missing_proof_or_tampered_original_is_rejected():
    source = reviewed()
    documents = dict(source.documents)
    documents[source.certificates[0].reconciliation_sha256] = b"tampered"
    with pytest.raises(ValueError, match="hash"):
        lab.Evidence(documents, source.certificates)
    documents = dict(source.documents)
    documents.pop(source.certificates[0].reconciliation_sha256)
    with pytest.raises(ValueError, match="proof"):
        lab.Evidence(documents, source.certificates)


def test_readiness_is_counted_from_actual_samples_not_a_constant_zero():
    sample = {"real": "synthetic test sample"}
    rows = [{"date": "2021-03-02", "snapshot": {"state": "ready"}, "sample": sample, "labels": {}},
            {"date": "2022-03-02", "snapshot": {"state": "ready"}, "sample": sample, "labels": {}}]
    result = lab.readiness(rows, spec())
    assert result["state"] == "ready_for_fit" and result["phases"]["train"]["complete_label_dates"] == 1
    rows[0]["sample"] = None
    assert lab.readiness(rows, spec())["state"] == "blocked_data"


def test_large_audit_can_be_compressed_reproducibly_without_overwriting_evidence(tmp_path):
    import gzip
    from scripts.measure.kr_net_alpha_v2 import save
    first, second = tmp_path/"one.json.gz", tmp_path/"two.json.gz"
    value = {"state": "blocked_data", "sample": None}
    assert save(first, value) == save(second, value)
    assert json.loads(gzip.decompress(first.read_bytes())) == value
    with pytest.raises(ValueError, match="differs"):
        save(first, {"state": "ready_for_fit"})
