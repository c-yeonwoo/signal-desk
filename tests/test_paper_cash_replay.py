"""Same-capital cash study must retain gaps and settlement boundaries."""

import datetime as dt
from zoneinfo import ZoneInfo

from signal_desk import market_clock
from signal_desk.signals.paper_cash_replay import replay
from signal_desk.signals.research_action_coverage import CoverageCertificate, SourceWindow
from signal_desk.signals.research_daily_simulator import SETTLEMENT_REQUIRED_KINDS
from signal_desk.signals.research_event_evidence import DailyEventEvidence, SettlementEvent

ZONE = ZoneInfo("Asia/Seoul")
OBSERVED = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
HASH = "c" * 64
SECURITY = "KR7005930003"


def _trade(id, side, qty, price, day="2026-07-14"):
    ts = int(dt.datetime.fromisoformat(day + "T10:00:00").replace(tzinfo=ZONE).timestamp())
    return {"id": id, "ticker": "ABC", "side": side, "qty": qty,
            "price": price, "ts": ts}


def _panel(start="2026-07-14"):
    days = [start, *market_clock.next_sessions("kr", start, 20)]
    prices = [100.0] * len(days)
    prices[5] = 120.0
    prices[20] = 130.0
    return {"ABC": (days, prices)}


def _now():
    return dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)


def test_same_seed_replays_actual_no_add_fixed_exits_and_cash():
    panel = _panel("2026-07-01")
    days = panel["ABC"][0]
    trades = [_trade(1, "buy", 10, 100, days[0]), _trade(2, "buy", 10, 80, days[1]),
              _trade(3, "sell", 20, 90, days[2])]
    out = replay(trades, panel, market="kr", seed_cash=10000,
                 as_of=days[20], now=_now())
    assert out["state"] == "complete"
    assert out["seed_cash"] == 10000
    assert out["arms"]["cash"]["nav"] == 10000
    assert out["arms"]["actual_schedule"]["fills"] == 3
    assert out["arms"]["no_add"]["fills"] == 2
    assert out["arms"]["no_add"]["cash"] < out["arms"]["actual_schedule"]["cash"]
    assert out["arms"]["hold5"]["return_pct"] > out["arms"]["actual_schedule"]["return_pct"]
    assert out["arms"]["hold20"]["return_pct"] > out["arms"]["hold5"]["return_pct"]
    assert all(point["cash"] == 10000 for point in out["path"])
    assert out["verified_total_return"] is None
    assert out["action_certified"] is False
    assert "unverified_price_basis" in out["certificate_gaps_sample"]
    assert out["live_eligible"] is False


def test_missing_held_close_blocks_whole_portfolio_instead_of_dropping_loser():
    panel = _panel()
    days, prices = panel["ABC"]
    panel["ABC"] = (days, [prices[0], None, *prices[2:]])
    out = replay([_trade(1, "buy", 1, 100)], panel, market="kr",
                 seed_cash=10000, as_of=days[2], now=_now())
    assert out["state"] == "blocked"
    assert "missing held close" in out["reason"]
    assert out["arms"] == {}


def test_verified_split_changes_shares_only_with_complete_scope_and_raw_basis():
    first = "2026-07-14"
    second = market_clock.next_sessions("kr", first, 1)[0]
    windows = tuple(SourceWindow("kr", SECURITY, family, family, first, second,
                                 "effective", SETTLEMENT_REQUIRED_KINDS,
                                 "complete_for_source", (letter * 64,), OBSERVED, True)
                    for family, letter in (("A", "a"), ("B", "b")))
    certificate = CoverageCertificate("kr", SECURITY, first, second,
                                      SETTLEMENT_REQUIRED_KINDS, windows, HASH)
    evidence = {(day, "ABC"): DailyEventEvidence(
        "kr", "ABC", day, "verified_complete",
        (SettlementEvent("split", split_numerator=2, split_denominator=1),)
        if day == second else (), f"source:{day}", OBSERVED, OBSERVED,
        scope_reconciliation_sha256=HASH)
        for day in (first, second)}
    trades = [_trade(1, "buy", 1, 100, first), _trade(2, "sell", 2, 50, second)]
    panel = {"ABC": ([first, second], [100, 50])}
    out = replay(trades, panel, market="kr", seed_cash=1000, as_of=second,
                 now=_now(), price_basis="unadjusted_verified",
                 certificates={"ABC": certificate}, event_evidence=evidence,
                 security_ids={"ABC": SECURITY})
    assert out["state"] == "complete"
    assert out["action_certified"] is True
    assert out["action_certified_model_return_pct"] is not None
    assert out["verified_total_return"] is None  # Model costs/fills are still hypothetical.
    assert out["arms"]["actual_schedule"]["holdings"] == {}
    assert out["arms"]["hold5"]["holdings"] == {"ABC": 2}
    assert out["arms"]["hold5"]["pending_scheduled_exits"] == 1
    wrong_id = replay(trades, panel, market="kr", seed_cash=1000, as_of=second,
                      now=_now(), price_basis="unadjusted_verified",
                      certificates={"ABC": certificate}, event_evidence=evidence,
                      security_ids={"ABC": "KR7000000000"})
    assert wrong_id["state"] == "blocked"
    assert wrong_id["verified_total_return"] is None
    future_source = replay(trades, panel, market="kr", seed_cash=1000, as_of=second,
                           now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
                           price_basis="unadjusted_verified",
                           certificates={"ABC": certificate}, event_evidence=evidence,
                           security_ids={"ABC": SECURITY})
    assert future_source["verified_total_return"] is None
    no_event = {(day, "ABC"): DailyEventEvidence(
        "kr", "ABC", day, "verified_complete", (), f"source:{day}",
        OBSERVED, OBSERVED, scope_reconciliation_sha256=HASH)
        for day in (first, second)}
    late_proof = replay([_trade(1, "buy", 1, 100, first),
                         _trade(2, "sell", 1, 100, second)],
                        {"ABC": ([first, second], [100, 100])}, market="kr",
                        seed_cash=1000, as_of=second,
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
                        price_basis="unadjusted_verified",
                        certificates={"ABC": certificate}, event_evidence=no_event,
                        security_ids={"ABC": SECURITY})
    assert late_proof["state"] == "complete"
    assert late_proof["action_certified"] is False


def test_confirmed_dividend_is_receivable_then_cash_once():
    days = ["2026-07-14", *market_clock.next_sessions("kr", "2026-07-14", 2)]
    windows = tuple(SourceWindow("kr", SECURITY, family, family, days[0], days[-1],
                                 "effective", SETTLEMENT_REQUIRED_KINDS,
                                 "complete_for_source", (letter * 64,), OBSERVED, True)
                    for family, letter in (("A", "a"), ("B", "b")))
    certificate = CoverageCertificate("kr", SECURITY, days[0], days[-1],
                                      SETTLEMENT_REQUIRED_KINDS, windows, HASH)
    dividend = SettlementEvent("cash_dividend", cash_per_share=5,
                               payment_date=days[2], event_id="dividend-1",
                               cash_amount_status="final", payment_status="confirmed",
                               payment_source_id="payment", payment_source_sha256=HASH,
                               payment_observed_at=OBSERVED)
    evidence = {(day, "ABC"): DailyEventEvidence(
        "kr", "ABC", day, "verified_complete", (dividend,) if day == days[1] else (),
        f"source:{day}", OBSERVED, OBSERVED, scope_reconciliation_sha256=HASH)
        for day in days}
    out = replay([_trade(1, "buy", 1, 100)],
                 {"ABC": (days, [100, 95, 95])}, market="kr", seed_cash=1000,
                 as_of=days[2], now=_now(), price_basis="unadjusted_verified",
                 certificates={"ABC": certificate}, event_evidence=evidence,
                 security_ids={"ABC": SECURITY})
    assert out["state"] == "complete"
    assert out["action_certified"] is True
    assert out["path"][1]["actual_schedule"] == out["path"][2]["actual_schedule"]
    assert out["arms"]["actual_schedule"]["receivable"] == 0
    assert out["arms"]["actual_schedule"]["cash"] > 900
    duplicate = {(day, "ABC"): DailyEventEvidence(
        "kr", "ABC", day, "verified_complete", (dividend,) if day in days[1:] else (),
        f"source:{day}", OBSERVED, OBSERVED, scope_reconciliation_sha256=HASH)
        for day in days}
    blocked = replay([_trade(1, "buy", 1, 100)],
                     {"ABC": (days, [100, 95, 95])}, market="kr", seed_cash=1000,
                     as_of=days[2], now=_now(), price_basis="unadjusted_verified",
                     certificates={"ABC": certificate}, event_evidence=duplicate,
                     security_ids={"ABC": SECURITY})
    assert blocked["reason"] == "duplicate_cash_entitlement"


def test_unmatched_actual_sell_and_future_asof_fail_closed():
    panel = _panel()
    days = panel["ABC"][0]
    bad = replay([_trade(1, "buy", 1, 100), _trade(2, "sell", 2, 90, days[1])],
                 panel, market="kr", seed_cash=1000, as_of=days[2], now=_now())
    assert bad["state"] == "blocked"
    assert bad["verified_total_return"] is None
    pending = replay([_trade(1, "buy", 1, 100)], panel, market="kr", seed_cash=1000,
                     as_of=days[20], now=dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc))
    assert pending["reason"] == "as_of_not_completed"


def test_protected_registered_rows_cannot_enter_pure_development_replay():
    panel = _panel()
    future = market_clock.next_sessions("kr", "2026-08-04", 1)[0]
    assert replay([_trade(1, "buy", 1, 100)], panel, market="kr",
                  seed_cash=1000, as_of=future, now=_now())["reason"] == "protected_research_period"
    assert replay([_trade(1, "buy", 1, 100), _trade(2, "sell", 1, 90, future)],
                  panel, market="kr", seed_cash=1000,
                  as_of="2026-08-04", now=_now())["reason"] == "protected_research_period"


def test_us_local_session_and_us_costs_are_separate_from_kr():
    first = "2026-07-14"
    second = market_clock.next_sessions("us", first, 1)[0]
    eastern = ZoneInfo("America/New_York")

    def us_trade(id, side, day, price):
        timestamp = int(dt.datetime.fromisoformat(day + "T10:00:00").replace(
            tzinfo=eastern).timestamp())
        return {"id": id, "ticker": "ABC", "side": side, "qty": 2,
                "price": price, "ts": timestamp}
    out = replay([us_trade(1, "buy", first, 100),
                  us_trade(2, "sell", second, 105)],
                 {"ABC": ([first, second], [100, 105])}, market="us",
                 seed_cash=10000, as_of=second, now=_now())
    assert out["state"] == "complete"
    assert out["market"] == "us"
    assert out["arms"]["actual_schedule"]["nav"] > 10000
    assert out["arms"]["hold20"]["pending_scheduled_exits"] == 1
    assert out["cost_model"]["observed_fills"]["sell_tax_bps"] == 0


def test_admin_replay_is_read_only_and_does_not_claim_certified_return(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, auth, bot, db, store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "cash-replay-admin@example.com")
    uid = next(u for u, style in bot.REFERENCE_BOTS.items() if style == "balanced")
    db.user_bot_get(uid)
    day = "2026-07-14"
    after = market_clock.next_sessions("kr", day, 1)[0]
    c = db.conn()
    try:
        c.executemany(
            "INSERT INTO bot_trades(uid,market,ticker,side,qty,price,reason,ts) "
            "VALUES(?,'kr','ABC',?,?,?,?,?)",
            [(uid, "buy", 1, 100, "SIGNAL", _trade(1, "buy", 1, 100, day)["ts"]),
             (uid, "sell", 1, 90, "SIGNAL", _trade(2, "sell", 1, 90, after)["ts"])],
        )
        c.commit()
    finally:
        c.close()
    db.kv_set(f"paper_account:{uid}", '{"cash": 9999990, "positions": {}}')
    monkeypatch.setattr(store, "load_market_dated_closes", lambda market: _panel())
    client = TestClient(api.app)
    assert client.get("/api/admin/research/paper-cash-replay").status_code == 401
    token, error = auth.signup("cash-replay-admin@example.com", "abcdef12")
    assert error is None
    client.cookies.set(auth.COOKIE, token)
    result = client.get("/api/admin/research/paper-cash-replay")
    assert result.status_code == 200
    body = result.json()
    assert body["state"] == "complete"
    assert body["arms"]["cash"]["nav"] == body["seed_cash"]
    assert body["verified_total_return"] is None
    assert body["action_certified"] is False
    assert body["study_window_end"] == "2026-08-04"
    assert body["excluded_protected_trade_rows"] == 0
    assert client.get("/api/admin/research/paper-cash-replay?market=bad").status_code == 422
    db.kv_set(f"paper_account:{uid}", '{"cash": 10000000, "positions": {}}')
    blocked = client.get("/api/admin/research/paper-cash-replay").json()
    assert blocked["reason"] == "paper_account_not_reconciled"
    assert blocked["arms"] == {}


def test_admin_policy_studies_exclude_registered_evaluation_rows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, auth, bot, db, store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "cash-replay-admin@example.com")
    uid = next(u for u, style in bot.REFERENCE_BOTS.items() if style == "balanced")
    db.user_bot_get(uid)
    rows = [_trade(1, "buy", 1, 100, "2026-07-14"),
            _trade(2, "sell", 1, 90, "2026-07-15"),
            _trade(3, "buy", 1, 100, "2026-08-06"),
            _trade(4, "sell", 1, 100, "2026-08-07")]
    c = db.conn()
    try:
        c.executemany(
            "INSERT INTO bot_trades(uid,market,ticker,side,qty,price,reason,ts) "
            "VALUES(?,'kr','ABC',?,1,?,'SIGNAL',?)",
            [(uid, row["side"], row["price"], row["ts"]) for row in rows],
        )
        c.commit()
    finally:
        c.close()
    db.kv_set(f"paper_account:{uid}", '{"cash": 9999990, "positions": {}}')
    monkeypatch.setattr(store, "load_market_dated_closes", lambda market: _panel())
    token, error = auth.signup("cash-replay-admin@example.com", "abcdef12")
    assert error is None
    client = TestClient(api.app)
    client.cookies.set(auth.COOKIE, token)
    for endpoint in ("paper-policy-sensitivity", "paper-cash-replay"):
        body = client.get(f"/api/admin/research/{endpoint}").json()
        assert body["excluded_protected_trade_rows"] == 2
        assert body["study_window_end"] == "2026-08-04"
        if endpoint == "paper-cash-replay":
            assert body["state"] == "complete"
            assert body["journal_rows_used"] == 2
        else:
            assert body["journal_rows"] == 2
