import copy
import base64
import hashlib

import pytest

from scripts.measure import intraday_confirmation as follow
from scripts.measure import intraday_orb_replay as orb
from scripts.measure import intraday_orb_sources as source


def setup_entry():
    opening, closing = orb.session_clocks("2025-01-02")
    candidate = {"symbol": "AAPL", "day": "2025-01-02", "open": 100., "close": 100.2,
                 "high": 100.5, "low": 99.5, "opening": opening, "closing": closing}
    bars = [{"t": opening + i * orb.MINUTE, "c": 100.2} for i in range(5)]
    bars += [{"t": opening + i * orb.MINUTE, "c": close}
             for i, close in enumerate([100.6, 100.65, 99., 101., 101.1], 5)]
    return candidate, bars


def test_confirmation_waits_until_next_complete_bar_without_changing_stop_or_limit():
    c, bars = setup_entry()
    original, confirmed = orb.entry_intent(c, bars), follow.confirmed_entry(c, bars)
    assert confirmed["ready"] == original["ready"] + orb.MINUTE
    assert confirmed["initial_stop"] == original["initial_stop"]
    assert confirmed["limit"] == original["limit"]
    assert follow.confirmed_entry(c, bars, 60)["ready"] == confirmed["ready"] + orb.MINUTE
    # Future collapse or rebound cannot alter an already observed confirmation.
    bars[-1]["c"] = 0
    assert follow.confirmed_entry(c, bars) == confirmed


@pytest.mark.parametrize("close", [100.5, 100.4])
def test_failed_first_confirmation_does_not_retry_later_rebound(close):
    c, bars = setup_entry()
    bars[6]["c"] = close
    assert follow.confirmed_entry(c, bars) is None


def test_missing_next_bar_is_not_a_zero_trade_or_lookahead_confirmation():
    c, bars = setup_entry()
    del bars[6]
    with pytest.raises(ValueError, match="confirmation minute absent"):
        follow.confirmed_entry(c, bars)


def test_expiration_applies_after_confirmation_and_after_extra_delay():
    c, bars = setup_entry()
    for b in bars[5:]:
        b["t"] += 53 * orb.MINUTE
    assert follow.confirmed_entry(c, bars) is None
    for b in bars[5:]:
        b["t"] -= orb.MINUTE
    assert follow.confirmed_entry(c, bars) is not None
    assert follow.confirmed_entry(c, bars, 60) is None


def test_fixed_followup_does_not_open_protected_dates_or_modify_previous_freeze():
    frozen = follow.plan()
    assert len(frozen["sessions"]) == 140
    assert list(map(len, frozen["splits"].values())) == [60, 60]
    assert max(frozen["splits"]["development"]) < min(frozen["splits"]["sealed_evaluation"])
    assert max(frozen["sessions"]) < "2025-07-01"
    assert not frozen["existing_operating_engine_comparable"]
    requests = source.requests_for_plan(frozen)
    assert len(requests) == 1120 and max(r["session"] for r in requests) < "2025-07-01"
    tampered = copy.deepcopy(frozen)
    tampered["sessions"].append("2026-10-26")
    with pytest.raises(ValueError, match="differs"):
        source.requests_for_plan(tampered)
    old = orb.plan()
    assert max(old["sessions"]) == "2024-12-12"
    assert len(source.requests_for_plan(old)) == 2080
    old["sessions"].append("2025-01-02")
    with pytest.raises(ValueError, match="research dates"):
        source.requests_for_plan(old)


def test_quote_plan_is_bound_to_frozen_policy_not_arbitrary_date_expansion():
    frozen = follow.plan()
    day = frozen["evaluation_sessions"][0]
    opening, _ = orb.session_clocks(day)
    quotes = {"schema": "intraday-confirmation-quote-plan-v1", "market": "us",
              "plan_sha256": hashlib.sha256(orb.json_bytes(frozen)).hexdigest(),
              "windows": [orb.request_window("AAPL", opening + 7 * orb.MINUTE)]}
    assert len(source.requests_for_plan(quotes, quote=True)) == 1
    quotes["plan_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="differs"):
        source.requests_for_plan(quotes, quote=True)


def test_source_missing_never_becomes_adoption():
    result = follow.compare({}, {}, follow.plan())
    assert result["verdict"] == "blocked_missing_execution_inputs"
    assert not result["live_order_eligible"]
    assert all(p["net_return_pct"] is None for p in result["paths"].values())


def test_shared_ledger_accepts_only_explicit_new_cost_model_and_preserves_old_default():
    assert follow.cost_model(12)["commission_bps"] == 12
    assert follow.cost_model(24)["commission_bps"] == 24
    with pytest.raises(ValueError):
        follow.cost_model(1)
    assert orb.costs(27)["commission_bps"] == 27
    with pytest.raises(ValueError):
        orb.costs(12)


def test_no_price_evidence_is_pinned_and_requires_complete_odd_lot_only_trades(tmp_path, monkeypatch):
    start = "2025-02-07T17:55:00Z"
    rows = [{"t": start, "z": "A", "c": [" ", "I"]} for _ in range(95)]
    def envelope():
        raw = orb.json_bytes({"trades": {"JPM": rows}, "next_page_token": None})
        return {"schema": "intraday-gap-trade-probe-v1", "http_status": 200,
                "request": {"symbols": "JPM", "start": start, "end": "2025-02-07T17:55:59.999999999Z",
                            "feed": "sip", "asof": "2025-02-07", "limit": "10000", "sort": "asc"},
                "has_next_page": False, "rows": 95, "raw_bytes": len(raw),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_b64": base64.b64encode(raw).decode()}
    path = tmp_path / "proof.json"
    encoded = orb.json_bytes(envelope())
    path.write_bytes(encoded)
    with pytest.raises(ValueError, match="unreviewed"):
        follow.verified_absence(path)
    monkeypatch.setattr(follow, "NO_PRICE_EVIDENCE_SHA256", hashlib.sha256(encoded).hexdigest())
    assert follow.verified_absence(path) == {("JPM", "2025-02-07", orb.clock(start))}
    assert follow.verified_absence(None) == set()
    # Even a reviewed fixture cannot claim a regular-price trade is absent OHLC.
    rows[0]["c"] = [" "]
    encoded = orb.json_bytes(envelope())
    path.write_bytes(encoded)
    monkeypatch.setattr(follow, "NO_PRICE_EVIDENCE_SHA256", hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match="invalid"):
        follow.verified_absence(path)
