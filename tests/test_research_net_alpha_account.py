"""Only synthetic source/certificate fixtures; no empirical profit claims."""

import copy
from dataclasses import replace
import hashlib
import json

import pytest

from signal_desk.signals import research_action_clocks as clocks
from signal_desk.signals.research_net_alpha_account import AccountLedger
from signal_desk.signals.research_net_alpha_execution import ExecutionBook
from test_research_net_alpha_v2 import reviewed, ISIN, spec
from test_research_action_clocks import assertion


def setup(source, *, held=100):
    cfg = spec(); cfg["initial_cash_krw"] = 100000
    book = ExecutionBook(cfg, "75bp_full")
    book.holdings = {"A": held} if held else {}
    book.settled_cash = 100000-held*100
    identities = {d: {"A": ISIN} for d in ("2022-07-01", "2022-07-04", "2022-07-05", "2022-07-06", "2022-07-07", "2022-07-08", "2022-07-11", "2022-07-12")}
    return book, AccountLedger(book, source, identities, "synthetic-only")


def source(*pairs):
    base = reviewed()
    documents = {**base.documents, **{k:v for a,docs in pairs for k,v in docs.items()}}
    assertions = [a for a,docs in pairs]
    actions = clocks.ActionBook(documents, assertions)
    certificates = []
    for c in base.certificates:
        r = json.loads(documents[c.reconciliation_sha256])
        r["assertion_ids"] = sorted(a["assertion_id"] for a in actions.dependencies(c.security_id, c.start, c.end, "account"))
        raw = json.dumps(r).encode(); sha = hashlib.sha256(raw).hexdigest(); documents[sha] = raw
        certificates.append(replace(c, reconciliation_sha256=sha))
    return type(base)(documents, certificates, (), assertions)


def cash(changes=None, **kwargs):
    return assertion("cash", {"entitlement_boundary": {"on": "2022-07-04", "phase": "before_open"},
        "cash_per_share": 3, "amount_status": "final", "currency": "KRW", **(changes or {})},
        kind="cash_dividend", event="dividend", **kwargs)


def test_ex_open_seller_keeps_right_and_new_buyer_does_not_receive_it():
    for held in (0, 100):
        b, ledger = setup(source(cash()), held=held)
        b.submit("2022-07-01", {} if held else {"A": .1}, {"A": 100}, {"A": 100000},
            100000, priority={"A": 1}, input_id="test", budget=1)
        ledger.advance("2022-07-04", "before_open")
        opens = {"A": {"price": 100, "open_tradable": True, "source_id": "synthetic"}}
        b.open_session("2022-07-04", ledger.gate_opens("2022-07-04", opens))
        ledger.record("2022-07-04", clocks.session_time("2022-07-04", "open"))
        ledger.advance("2022-07-04", "close")
        nav = ledger.valuation("2022-07-04", {"A": 100}, clocks.session_time("2022-07-04"))["value"]
        assert nav == pytest.approx(100000-b.fees+(300 if held else 0))
        assert b.buying_power <= 100000-b.fees  # receivable is never buying power


def test_late_amount_revalues_old_frame_without_rewriting_old_orders_or_cash():
    schedule = assertion("schedule", {"entitlement_boundary": {"on": "2022-07-04", "phase": "before_open"}},
        kind="cash_dividend", event="dividend", component="entitlement")
    amount = assertion("amount", {"cash_per_share": 3, "amount_status": "final", "currency": "KRW"},
        published="2022-07-05", kind="cash_dividend", event="dividend", component="payment")
    b, ledger = setup(source(schedule, amount))
    ledger.advance("2022-07-04", "before_open")
    frame = ledger.frame("2022-07-04", {"A": 100}, clocks.session_time("2022-07-04"))
    saved = copy.deepcopy(b.state())
    assert ledger.mark(frame)["value"] is None
    assert ledger.mark(frame, final_at=clocks.session_time("2022-07-07"))["value"] == 100300
    assert b.state() == saved
    ledger.advance("2022-07-06", "before_open")
    assert b.buying_power == 90000
    assert ledger.valuation("2022-07-06", {"A": 100}, clocks.session_time("2022-07-06"))["value"] == 100300


def test_scheduled_payment_is_not_cash_confirmed_transfer_conserves_wealth_and_is_idempotent():
    schedule = cash({"payment_scheduled_on": "2022-07-05"})
    payment = assertion("paid", {"payment_confirmed_on": "2022-07-08"}, component="payment",
        published="2022-07-07", kind="cash_dividend", event="dividend")
    b, ledger = setup(source(schedule, payment))
    ledger.advance("2022-07-04", "before_open")
    ledger.advance("2022-07-05", "after_close")
    assert b.buying_power == 90000
    ledger.advance("2022-07-08", "close")
    before = ledger.valuation("2022-07-08", {"A": 100}, clocks.session_time("2022-07-08"))["value"]
    assert b.buying_power == 90000
    ledger.advance("2022-07-08", "after_close")
    ledger.advance("2022-07-08", "after_close")
    assert b.buying_power == 90300
    assert ledger.valuation("2022-07-08", {"A": 100}, clocks.session_time("2022-07-08"))["value"] == before
    assert len([j for j in ledger.journal if j["role"] == "cash_transfer"]) == 1


def test_split_cancels_old_unit_order_and_old_reference_close_cannot_inflate_nav():
    split = assertion("split", {"conversion_boundary": {"on": "2022-07-01", "phase": "after_close"},
        "share_ratio": 5, "price_basis_from_session": "2022-07-04", "tradable_from_session": "2022-07-04"})
    b, ledger = setup(source(split))
    b.submit("2022-07-01", {}, {"A": 100}, {"A": 100000}, 100000, priority={}, input_id="old-unit", budget=1)
    ledger.advance("2022-07-01", "before_open")
    ledger.advance("2022-07-01", "after_close")
    assert b.holdings == {"A": 500} and b.pending == []
    assert ledger.valuation("2022-07-01", {"A": 100}, clocks.session_time("2022-07-01"))["value"] is None
    ledger.advance("2022-07-04", "before_open")
    assert ledger.valuation("2022-07-04", {"A": 20}, clocks.session_time("2022-07-04"))["value"] == 100000


def test_stock_dividend_is_receivable_until_delivery_even_after_original_shares_sold():
    stock = assertion("stock", {"entitlement_boundary": {"on": "2022-07-04", "phase": "before_open"},
        "additional_share_ratio": .1, "delivery_boundary": {"on": "2022-07-06", "phase": "before_open"},
        "tradable_from_session": "2022-07-06", "receivable_value_per_right": 100, "currency": "KRW"},
        kind="stock_dividend")
    b, ledger = setup(source(stock))
    ledger.advance("2022-07-04", "before_open")
    assert b.holdings == {"A": 100}
    assert ledger.valuation("2022-07-04", {"A": 100}, clocks.session_time("2022-07-04"))["value"] == 101000
    b.holdings = {}; b.settled_cash += 10000  # fixture-only original holding sale
    ledger.record("2022-07-04", clocks.session_time("2022-07-04", "open"))
    ledger.advance("2022-07-06", "before_open")
    ledger.advance("2022-07-06", "before_open")
    assert b.holdings == {"A": 10}
    assert ledger.valuation("2022-07-06", {"A": 100}, clocks.session_time("2022-07-06"))["value"] == 101000


def test_fractional_or_unsupported_conversion_never_drops_assets_or_invents_return():
    split = assertion("split", {"conversion_boundary": {"on": "2022-07-04", "phase": "before_open"},
        "share_ratio": .15, "price_basis_from_session": "2022-07-04", "tradable_from_session": "2022-07-04"})
    b, ledger = setup(source(split), held=3)
    ledger.advance("2022-07-04", "before_open")
    assert b.holdings == {"A": 3}
    assert ledger.valuation("2022-07-04", {"A": 100}, clocks.session_time("2022-07-04"))["value"] is None
    assert ledger.gaps[0]["reason"] == "split_terms_or_fraction_unverified"


def test_late_boundary_disclosure_does_not_turn_price_only_nav_into_final_nav():
    late = cash(published="2022-07-05")
    b, ledger = setup(source(late))
    ledger.advance("2022-07-04", "before_open")
    frame = ledger.frame("2022-07-04", {"A":100}, clocks.session_time("2022-07-04"))
    before = copy.deepcopy(b.state())
    assert ledger.mark(frame)["value"] == 100000  # event not yet publicly known
    final = ledger.mark(frame, final_at=clocks.session_time("2022-07-07"))
    assert final["value"] is None
    assert "unrecorded_final_entitlement_requires_reconciliation" in final["reasons"]
    assert b.state() == before


def test_confirmed_payment_correction_posts_delta_only_once():
    original = cash({"payment_confirmed_on":"2022-07-04"})
    corrected = assertion("corrected", {"cash_per_share":2}, published="2022-07-05",
        kind="cash_dividend", event="dividend", previous=("cash",))
    b, ledger = setup(source(original, corrected))
    ledger.advance("2022-07-04", "before_open")
    ledger.advance("2022-07-04", "after_close")
    assert b.buying_power == 90300
    ledger.advance("2022-07-06", "before_open")
    ledger.advance("2022-07-06", "before_open")
    assert b.buying_power == 90200
    assert ledger.valuation("2022-07-06", {"A":100}, clocks.session_time("2022-07-06"))["value"] == 100200


def test_reused_ticker_cannot_reassign_an_existing_holding_to_a_new_security():
    b, ledger = setup(source())
    ledger.advance("2022-07-04", "before_open")
    ledger.identities["2022-07-05"]["A"] = "KR0000000002"
    ledger.advance("2022-07-05", "before_open")
    assert b.holdings == {"A":100}
    assert ledger.valuation("2022-07-05", {"A":100}, clocks.session_time("2022-07-05"))["value"] is None
    assert ledger.gate_opens("2022-07-05", {"A":{"price":100, "open_tradable":True}})["A"]["open_tradable"] is False
    assert ledger.history[-1]["identities"]["A"] == ISIN


def test_unresolved_conversion_cancels_old_unit_sells_as_well_as_buys():
    conversion = assertion("merger", {"conversion_boundary":{"on":"2022-07-04", "phase":"before_open"}},
        kind="merger_exchange")
    b, ledger = setup(source(conversion))
    b.submit_exits("2022-07-01", ["A"], {"A":100}, {"A":100000}, input_id="old-unit")
    ledger.advance("2022-07-04", "before_open")
    assert b.pending == [] and b.holdings == {"A":100}
    opens = ledger.gate_opens("2022-07-04", {"A":{"price":100, "open_tradable":True, "source_id":"synthetic"}})
    assert opens["A"]["open_tradable"] is False
    assert b.open_session("2022-07-04", opens) == []
    assert ledger.gaps[0]["reason"] == "unsupported_account_entitlement"


def test_final_stock_ratio_revalues_only_the_recorded_receivable_not_old_trades():
    original = assertion("stock", {"entitlement_boundary":{"on":"2022-07-04", "phase":"before_open"},
        "additional_share_ratio":.1, "receivable_value_per_right":100, "currency":"KRW"}, kind="stock_dividend")
    revised = assertion("stock-correction", {"additional_share_ratio":.2}, published="2022-07-05",
        kind="stock_dividend", previous=("stock",))
    b, ledger = setup(source(original, revised))
    ledger.advance("2022-07-04", "before_open")
    frame = ledger.frame("2022-07-04", {"A":100}, clocks.session_time("2022-07-04"))
    before = copy.deepcopy(b.state())
    assert ledger.mark(frame)["value"] == 101000
    assert ledger.mark(frame, final_at=clocks.session_time("2022-07-07"))["value"] == 102000
    assert b.state() == before and b.holdings == {"A":100}


def test_final_split_revision_after_trading_tail_cannot_certify_old_quantity():
    original = assertion("split", {"conversion_boundary":{"on":"2022-07-04", "phase":"before_open"},
        "share_ratio":5, "price_basis_from_session":"2022-07-04", "tradable_from_session":"2022-07-04"})
    revised = assertion("late-revision", {"share_ratio":4}, published="2025-01-02", previous=("split",))
    b, ledger = setup(source(original, revised))
    ledger.advance("2022-07-04", "before_open")
    frame = ledger.frame("2022-07-04", {"A":20}, clocks.session_time("2022-07-04"))
    assert ledger.mark(frame)["value"] == 100000
    result = ledger.mark(frame, final_at=clocks.timestamp("2026-10-18T00:00:00+09:00"))
    assert result["value"] is None and b.holdings == {"A":500}
    assert "final_applied_conversion_revision_unreconciled" in result["reasons"]
