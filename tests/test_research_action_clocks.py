"""Revision, unit and rights boundaries. Every source here is synthetic."""

import copy
import datetime as dt
import hashlib
import json

import pytest

from signal_desk.signals import research_action_clocks as clocks
from signal_desk.signals import research_net_alpha_v2 as lab
from test_research_net_alpha_v2 import fact, reviewed, inputs, outcome, panel, spec

ISIN = "KR7005930003"


def assertion(aid, changes, *, published="2021-02-04", previous=(), component="schedule",
              kind="split", event="event", status="confirmed", observed="2026-10-18T00:00:00+09:00"):
    publication = published.replace("T", " ")[:19 if len(published) > 16 else 10]
    raw = json.dumps({"fixture": "SYNTHETIC TEST ONLY", "publication": publication, "changes": changes}).encode()
    sha = hashlib.sha256(raw).hexdigest()
    fields = {}
    for name, value in changes.items():
        precision = ("date" if name in clocks.DATE_ROLES else
            "session_phase" if name in clocks.BOUNDARIES and isinstance(value, dict) else
            "second" if name in clocks.BOUNDARIES else "value")
        fields[name] = {"value": value, "precision": precision,
                       "source_sha256": sha, "source_span": value if name in clocks.BOUNDARIES and isinstance(value, str) else "SYNTHETIC TEST ONLY"}
    a = {"assertion_id": aid, "event_id": event, "component_id": component,
        "security_id": ISIN, "share_class": "ordinary", "action_kind": kind,
        "component_kind": component, "changes": fields, "status": status,
        "supersedes_assertion_ids": list(previous), "published_on": published[:10],
        "publication_precision": "second" if len(published) > 10 else "date",
        "observed_at": observed, "availability_mode": "reconstructed_public",
        "publication_proof": {"source_sha256": sha, "source_span": publication}}
    if len(published) > 10:
        a["published_at"] = published
    return a, {sha: raw}


def book(*pairs):
    return clocks.ActionBook({k: v for _, docs in pairs for k, v in docs.items()}, [a for a, _ in pairs])


def test_partial_schedule_revision_preserves_ratio_source_and_historical_schedule():
    initial = assertion("a", {"legal_effective_on": "2021-04-09", "tradable_from_session": "2021-04-12", "share_ratio": 5})
    revised = assertion("b", {"legal_effective_on": "2021-04-12", "tradable_from_session": "2021-04-13"},
                        published="2021-02-23", previous=("a",))
    b = book(initial, revised)
    early = b.events(ISIN, clocks.session_time("2021-02-22"), "input")[0]
    later = b.events(ISIN, clocks.session_time("2021-02-24"), "input")[0]
    assert clocks.value(early, "legal_effective_on") == "2021-04-09"
    assert clocks.value(later, "legal_effective_on") == "2021-04-12"
    assert later["fields"]["share_ratio"]["assertion_ids"] == ["a"]
    assert later["fields"]["share_ratio"]["known_at"] == early["fields"]["share_ratio"]["known_at"]
    assert len(b.dependencies(ISIN, "2021-04-09", "2021-04-09", "input")) == 2


def test_independent_reaffirmation_preserves_first_availability_and_conflict_needs_edge():
    first = assertion("a", {"share_ratio": 5})
    same = assertion("b", {"share_ratio": 5}, published="2021-02-23")
    b = book(first, same)
    event = b.events(ISIN, clocks.session_time("2021-02-24"), "input")[0]
    assert event["fields"]["share_ratio"]["assertion_ids"] == ["a", "b"]
    assert event["fields"]["share_ratio"]["known_at"] == clocks.available_at(first[0]).isoformat()
    conflict = assertion("c", {"share_ratio": 6}, published="2021-02-23")
    assert book(first, conflict).events(ISIN, clocks.session_time("2021-02-24"), "input")[0]["conflicts"] == ["share_ratio"]
    correction = assertion("c", {"share_ratio": 6}, published="2021-02-23", previous=("a",))
    assert clocks.value(book(first, correction).events(ISIN, clocks.session_time("2021-02-24"), "input")[0], "share_ratio") == 6


def test_quote_rule_revision_does_not_create_economic_posting_or_erase_halt():
    halt = assertion("h", {"halt_start": "2022-10-17T09:00:00+09:00"},
                     published="2022-10-14", component="halt", kind="capital_reduction")
    old = assertion("q1", {"quote_rule_from_session": "2022-10-17", "maximum_quote": 13150},
                    published="2022-10-14", component="quote_rule", kind="capital_reduction")
    new = assertion("q2", {"quote_rule_from_session": "2023-01-26", "maximum_quote": 13140},
                    published="2023-01-25", previous=("q1",), component="quote_rule", kind="capital_reduction")
    b = book(halt, old, new)
    components = b.resolve_components(ISIN, clocks.session_time("2023-01-26"), "account")
    assert len(components) == 2
    assert next(c for c in components if c["component_kind"] == "quote_rule")["fields"]["maximum_quote"]["value"] == 13140
    assert len(b.events(ISIN, clocks.session_time("2023-01-26"), "outcome")) == 1
    assert all(e["fields"].keys() == {"halt_start"} for e in b.events(ISIN, clocks.session_time("2023-01-26"), "outcome"))


def test_kakao_legal_date_does_not_set_price_unit_boundary():
    pair = assertion("split", {"legal_effective_on": "2021-04-14", "price_basis_from_session": "2021-04-15", "share_ratio": 5},
                     published="2021-04-14")
    b = book(pair)
    events = b.events(ISIN, clocks.session_time("2021-04-15"), "input")
    c, v, units, reason = lab.normalized_history(["2021-04-13", "2021-04-14", "2021-04-15"],
                                               [558000, 558000, 120500], [0, 0, 100], events)
    assert reason is None and c == [111600, 111600, 120500] and v == [0, 0, 100]
    assert c[-1]/c[-2]-1 == pytest.approx(.07974910394)
    before = b.events(ISIN, clocks.session_time("2021-04-13"), "input")
    assert before == () and units[0]["price_basis_from_session"] == "2021-04-15"
    announced = assertion("a", {"price_basis_from_session": "2021-04-15", "share_ratio": 5}, status="announced")
    assert lab.normalized_history(["2021-04-15"], [120500], [100], book(announced).events(
        ISIN, clocks.session_time("2021-04-15"), "input"))[3] == "split_price_boundary_unverified"


def test_stock_dividend_is_not_a_split_adjustment():
    pair = assertion("a", {"price_basis_from_session": "2021-04-15", "share_ratio": 1.1}, kind="stock_dividend")
    assert lab.normalized_history(["2021-04-15"], [100], [100], book(pair).events(
        ISIN, clocks.session_time("2021-04-15"), "input"))[3] == "complex_history_units_unsupported"


def test_dividend_open_boundaries_and_unknown_record_only_right():
    _, sessions, _, _ = inputs()
    on_entry = fact("cash_dividend", f"{sessions[0]}T09:00:00+09:00", cash_per_share=3, amount_status="final")
    on_exit = fact("cash_dividend", f"{sessions[-1]}T09:00:00+09:00", cash_per_share=3, amount_status="final")
    assert outcome(reviewed([on_entry]))["cash_and_receivables"] == 0
    assert outcome(reviewed([on_exit]))["cash_and_receivables"] == 3
    pair = assertion("samsung", {"record_on": sessions[2], "cash_per_share": 361}, kind="cash_dividend")
    source = reviewed()
    # No invented ex-date; first exercise the event boundary itself.
    assert clocks.event_boundary(book(pair).events(ISIN, clocks.session_time("2021-12-30"), "outcome")[0]) is None


def test_rights_accrual_correction_and_delivery_are_idempotent_and_conserve_assets():
    ledger = clocks.RightsLedger("synthetic-portfolio")
    assert ledger.accrue("e", "r", "shares", 10) == 10
    assert ledger.accrue("e", "r", "shares", 10) == 0
    assert ledger.deliver("e", "r", "shares") == 10
    assert ledger.deliver("e", "r", "shares") == 0
    assert ledger.journal[-1]["wealth_delta"] == 0
    assert ledger.accrue("e", "cash", "cash_receivable", 300) == 300
    assert ledger.accrue("e", "cash", "cash_receivable", 320) == 20
    assert len(ledger.journal) == 4
    with pytest.raises(ValueError, match="without accrued"):
        ledger.deliver("other", "missing", "shares")


def test_observed_and_reconstructed_knowledge_and_tampered_source():
    pair = assertion("a", {"record_on": "2021-03-31"})
    b = book(pair)
    assert b.events(ISIN, clocks.session_time("2021-03-02"), "input")
    a = copy.deepcopy(pair[0]); a["availability_mode"] = "observed"
    assert not book((a, pair[1])).events(ISIN, clocks.session_time("2021-03-02"), "input")
    a = copy.deepcopy(pair[0]); a["changes"]["record_on"]["source_span"] = "ABSENT"
    with pytest.raises(ValueError, match="source/span"):
        book((a, pair[1]))
    a = copy.deepcopy(pair[0]); a["supersedes_assertion_ids"] = ["a"]
    with pytest.raises(ValueError, match="cyclic"):
        book((a, pair[1]))


def test_legacy_economic_effective_at_is_rejected():
    with pytest.raises(ValueError, match="halt only"):
        lab.Evidence(facts=[fact("cash_dividend", "2021-03-30T09:00:00+09:00")])


def test_split_normalization_requires_observed_counts_not_just_a_passed_schedule():
    days, prices, roster = panel()
    day = days[-1]
    event = fact("split", f"{day}T09:00:00+09:00", share_ratio=5,
        price_basis_from_session=day, listed_share_observed_on=day,
        listed_shares_before=1_000_000, listed_shares_after=5_000_000)
    source = reviewed([event])
    result = lab.universe(day, roster, days, prices, source, spec())
    assert result["state"] == "input_gap" and result["features"] is None
    prices[day]["A"]["LIST_SHRS"] = 5_000_000
    prices[day]["A"]["TDD_CLSPRC"] /= 5
    result = lab.universe(day, roster, days, prices, source, spec())
    assert result["state"] == "ready"
    assert result["price_unit_transitions"]["A"][0]["raw_from_unit_id"].endswith(":new")


def test_undelivered_stock_dividend_cannot_be_sold_as_new_shares():
    _, sessions, _, _ = inputs()
    event = fact("stock_dividend", f"{sessions[2]}T09:00:00+09:00",
        additional_share_ratio=.1, delivery_on=sessions[-1], tradable_from_session=sessions[-1])
    result = outcome(reviewed([event]))
    assert result["return"] is None and result["reason"] == "undelivered_stock_dividend_unvalued"
    delivered = {**event, "delivery_on": sessions[-2]}
    result = outcome(reviewed([delivered]))
    assert result["return"] == pytest.approx(.21)
    assert result["components"][0]["quantity"] == pytest.approx(1.1)


def test_conflicting_halt_never_becomes_tradable():
    first = assertion("a", {"halt_start": "2021-03-02T09:00:00+09:00"}, component="halt", kind="halt")
    conflict = assertion("b", {"halt_start": "2021-03-03T09:00:00+09:00"}, component="halt", kind="halt")
    docs = {**first[1], **conflict[1]}
    source = lab.Evidence(docs, assertions=[first[0], conflict[0]])
    assert source.halted(ISIN, clocks.session_time("2021-03-02"), clocks.session_time("2021-03-02")) is None


def test_explicit_reaffirmation_keeps_origin_but_reverting_changed_value_does_not():
    first = assertion("a", {"share_ratio": 5})
    same = assertion("b", {"share_ratio": 5}, published="2021-02-23", previous=("a",))
    resolved = book(first, same).events(ISIN, clocks.session_time("2021-03-02"), "input")[0]
    assert resolved["fields"]["share_ratio"]["known_at"] == clocks.available_at(first[0]).isoformat()
    changed = assertion("b", {"share_ratio": 6}, published="2021-02-23", previous=("a",))
    reverted = assertion("c", {"share_ratio": 5}, published="2021-02-24", previous=("b",))
    resolved = book(first, changed, reverted).events(ISIN, clocks.session_time("2021-03-02"), "input")[0]
    assert resolved["fields"]["share_ratio"]["known_at"] == clocks.available_at(reverted[0]).isoformat()


def test_revision_can_be_observed_before_its_original_without_inventing_old_fields():
    initial = assertion("a", {"share_ratio": 5, "legal_effective_on": "2021-04-09"}, observed="2026-10-18T09:00:00+09:00")
    correction = assertion("b", {"legal_effective_on": "2021-04-12"}, published="2021-02-23", previous=("a",),
                           observed="2026-10-17T09:00:00+09:00")
    for a, _ in (initial, correction):
        a["availability_mode"] = "observed"
    resolved = book(initial, correction).events(ISIN, clocks.timestamp("2026-10-17T10:00:00+09:00"), "input")[0]
    assert "share_ratio" not in resolved["fields"]
    assert clocks.value(resolved, "legal_effective_on") == "2021-04-12"


def test_date_only_source_cannot_prove_an_exact_midnight_boundary():
    pair = assertion("a", {"entitlement_boundary": "2021-03-30T00:00:00+09:00"}, kind="cash_dividend")
    pair[0]["changes"]["entitlement_boundary"]["source_span"] = "2021-03-30"
    with pytest.raises(ValueError, match="exact boundary clock"):
        book(pair)
