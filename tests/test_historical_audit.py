"""Historical signal audits must preserve timing, missingness, and all denominators."""

import pandas as pd
import pytest
import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

from signal_desk import market_clock
from signal_desk.signals.historical_audit import (
    audit_snapshots, inventory_recorded_inputs, select_recorded_inputs,
)


def _bars(ticker: str, dates: list[str], *, first_open: float = 100.0,
          close: float = 100.0) -> list[dict]:
    return [{"date": day, "ticker": ticker, "open": first_open, "close": close}
            for day in dates]


def test_audits_all_rows_and_marks_transitions_without_future_prices():
    signal_day = "2026-07-20"
    days = market_clock.next_sessions("kr", signal_day, 5)
    signals = pd.DataFrame([
        {"date": signal_day, "ticker": "AAA", "score": 1.0, "kind": "HOLD", "momentum": 0.2},
        {"date": signal_day, "ticker": "BBB", "score": 1.8, "kind": "BUY"},
        {"date": days[0], "ticker": "AAA", "score": 1.7, "kind": "BUY", "momentum": 0.4},
    ])
    price_days = [signal_day, *days, *market_clock.next_sessions("kr", days[-1], 1)]
    bars = _bars("AAA", price_days) + _bars("BBB", price_days)
    # The signal-day close is irrelevant: entry is the *next* scheduled open.
    bars[0]["close"] = 999.0
    for bar in bars:
        if bar["ticker"] == "AAA" and bar["date"] == days[0]:
            bar["open"] = 120.0
            bar["close"] = 90.0
    result = audit_snapshots(signals, pd.DataFrame(bars), market="kr", major_tickers=("AAA",))
    assert result["signal_rows"] == 3
    rows = {(row["date"], row["ticker"]): row for row in result["rows"]}
    first = rows[(signal_day, "AAA")]
    assert first["outcomes"]["1"]["entry_open"] == 120.0
    assert first["outcomes"]["1"]["net_pct"] == -25.25
    assert first["outcomes"]["5"]["state"] == "matured"
    assert first["outcomes"]["20"]["state"] == "not_matured"
    change = rows[(days[0], "AAA")]
    assert change["kind_change"] and change["score_change"]
    assert change["factor_changes"]["momentum"] == 0.2
    assert result["summary"]["kind_changed"]["5"]["observations"] == 1
    assert result["summary"]["all"]["5"]["observations"] == 3


def test_missing_middle_session_is_not_interpolated():
    day = "2026-07-20"
    future = market_clock.next_sessions("kr", day, 5)
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 2.0, "kind": "BUY"}])
    prices = pd.DataFrame(_bars("AAA", [future[0], future[1], future[3], future[4]]))
    out = audit_snapshots(signals, prices, market="kr")["rows"][0]["outcomes"]["5"]
    assert out == {"state": "price_gap", "entry_date": future[0],
                   "exit_date": future[4], "first_missing_date": future[2]}


def test_duplicate_signal_or_bar_is_rejected_instead_of_picking_a_version():
    day = "2026-07-20"
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 1.0, "kind": "HOLD"}])
    bars = pd.DataFrame(_bars("AAA", market_clock.next_sessions("kr", day, 1)))
    with pytest.raises(ValueError, match="duplicate signal"):
        audit_snapshots(pd.concat([signals, signals], ignore_index=True), bars, market="kr")
    with pytest.raises(ValueError, match="duplicate price"):
        audit_snapshots(signals, pd.concat([bars, bars], ignore_index=True), market="kr")


def test_gap_between_recorded_signals_is_visible():
    day = "2026-07-20"
    future = market_clock.next_sessions("kr", day, 4)
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 1.0, "kind": "HOLD"},
        {"date": future[2], "ticker": "AAA", "score": 1.9, "kind": "BUY"},
    ])
    bars = pd.DataFrame(_bars("AAA", future))
    result = audit_snapshots(signals, bars, market="kr")
    assert result["snapshot_gap_rows"] == 1
    assert result["rows"][1]["kind_change"]
    assert result["rows"][1]["snapshot_gap"]


def test_same_score_kind_flip_keeps_saved_selection_evidence_and_unknowns():
    day = "2026-07-20"
    next_day = market_clock.next_sessions("kr", day, 1)[0]
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 2.15, "kind": "STRONG_BUY",
         "rank": 2, "rank_eligible": 1, "gate_blocked": 0,
         "reasons_json": '["[선정] 시장 200종목 중 2위"]'},
        {"date": next_day, "ticker": "AAA", "score": 2.15, "kind": "HOLD",
         "rank": 8, "rank_eligible": 0, "gate_blocked": 0,
         "reasons_json": '["[선정] 시장 200종목 중 8위 — 매수권 밖"]'},
    ])
    prices = pd.DataFrame(_bars("AAA", [day, next_day]))
    selected, _ = select_recorded_inputs(signals, prices, market="kr", sessions=2)
    assert {"rank_eligible", "gate_blocked", "reasons_json"} <= set(selected.columns)
    row = audit_snapshots(selected, prices, market="kr")["rows"][1]
    assert row["kind_change"] and row["score_delta"] == 0
    assert row["selection_changes"] == ["rank", "rank_eligible"]
    assert row["previous_selection"]["rank"] == 2
    assert row["selection"]["rank"] == 8
    assert row["selection"]["selection_reason"].endswith("매수권 밖")
    assert row["selection"]["event_risk"] is None


def test_gate_release_reentry_is_flagged_without_claiming_return_cause():
    day = "2026-07-20"
    next_day = market_clock.next_sessions("kr", day, 1)[0]
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 2.15, "kind": "HOLD",
         "rank": 2, "rank_eligible": 0, "gate_blocked": 1,
         "reasons_json": '["[선반영] 호재 전 사전상승 11.2% — 신규 매수 보류"]'},
        {"date": next_day, "ticker": "AAA", "score": 2.15, "kind": "STRONG_BUY",
         "rank": 2, "rank_eligible": 1, "gate_blocked": 0,
         "reasons_json": '["[선정] 시장 200종목 중 2위"]'},
    ])
    prices = pd.DataFrame(_bars("AAA", [day, next_day]))
    row = audit_snapshots(signals, prices, market="kr")["rows"][1]
    assert row["gate_release_reentry_without_score_gain"]
    assert "11.2%" in row["previous_selection"]["gate_reason"]
    assert row["selection"]["gate_reason"] is None


def test_gate_release_across_missing_sessions_is_not_called_immediate_reentry():
    day = "2026-07-20"
    later = market_clock.next_sessions("kr", day, 3)[-1]
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 2.15, "kind": "HOLD", "gate_blocked": 1},
        {"date": later, "ticker": "AAA", "score": 2.15, "kind": "STRONG_BUY", "gate_blocked": 0},
    ])
    prices = pd.DataFrame(_bars("AAA", [day, later]))
    row = audit_snapshots(signals, prices, market="kr")["rows"][1]
    assert row["snapshot_gap"] and "gate_blocked" in row["selection_changes"]
    assert row["gate_release_reentry_without_score_gain"] is False


def test_buy_loss_path_distinguishes_sell_before_from_no_sell_and_signal_gap():
    day = "2026-07-20"
    sessions = market_clock.next_sessions("kr", day, 5)
    signals = pd.DataFrame([
        {"date": day, "ticker": ticker, "score": 2.0, "kind": "BUY"}
        for ticker in ("EARLY", "NONE", "GAP", "SAME")
    ] + [
        {"date": session, "ticker": ticker, "score": 0.0,
         "kind": ("SELL" if (ticker == "EARLY" and session == sessions[1]) or
                  (ticker == "SAME" and session == sessions[3]) else "HOLD")}
        for ticker in ("EARLY", "NONE", "GAP", "SAME") for session in sessions[:4]
        if not (ticker == "GAP" and session == sessions[1])
    ])
    bars = pd.DataFrame([
        {"date": session, "ticker": ticker, "open": 100.0,
         "close": 89.0 if session == sessions[3] else 100.0}
        for ticker in ("EARLY", "NONE", "GAP", "SAME") for session in sessions
    ])
    rows = audit_snapshots(signals, bars, market="kr")["rows"]
    by_ticker = {row["ticker"]: row["loss_warning_path"] for row in rows if row["date"] == day}
    assert all(path["first_loss_date"] == sessions[3] for path in by_ticker.values())
    assert by_ticker["EARLY"]["prior_sell_evidence"] == "recorded_before_loss"
    assert by_ticker["EARLY"]["first_sell_before_loss"] == sessions[1]
    assert by_ticker["NONE"]["prior_sell_evidence"] == "none_recorded"
    assert by_ticker["NONE"]["new_entry_block_evidence"] == "unknown_metadata"
    assert by_ticker["GAP"]["prior_sell_evidence"] == "unknown_signal_gap"
    assert by_ticker["GAP"]["first_missing_signal_date"] == sessions[1]
    assert by_ticker["SAME"]["prior_sell_evidence"] == "none_recorded"
    assert by_ticker["SAME"]["first_sell_on_or_after_loss"] == sessions[3]


def test_buy_loss_path_stops_at_missing_price_and_does_not_assume_loss_from_later_bar():
    day = "2026-07-20"
    sessions = market_clock.next_sessions("kr", day, 5)
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 2.0, "kind": "BUY"}])
    bars = pd.DataFrame(_bars("AAA", [sessions[0], sessions[2], sessions[3]], close=80.0))
    bars.loc[bars["date"] == sessions[0], "close"] = 100.0
    path = audit_snapshots(signals, bars, market="kr")["rows"][0]["loss_warning_path"]
    assert path["state"] == "price_gap" and path["first_missing_date"] == sessions[1]


def test_entry_block_before_loss_is_not_misreported_as_sell_or_holder_exit():
    day = "2026-07-20"
    sessions = market_clock.next_sessions("kr", day, 4)
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 2.0, "kind": "BUY"},
        {"date": sessions[0], "ticker": "AAA", "score": 1.7, "kind": "HOLD",
         "gate_blocked": 1, "event_risk": 0},
        {"date": sessions[1], "ticker": "AAA", "score": 1.6, "kind": "HOLD",
         "gate_blocked": 0, "event_risk": 0},
    ])
    bars = pd.DataFrame([
        {"date": session, "ticker": "AAA", "open": 100.0,
         "close": 89.0 if session == sessions[2] else 100.0}
        for session in sessions
    ])
    path = audit_snapshots(signals, bars, market="kr")["rows"][0]["loss_warning_path"]
    assert path["first_loss_date"] == sessions[2]
    assert path["first_new_entry_block_before_loss"] == sessions[0]
    assert path["new_entry_block_fields"] == ["gate_blocked"]
    assert path["new_entry_block_evidence"] == "recorded_before_loss"
    assert path["first_sell_before_loss"] is None
    assert path["prior_sell_evidence"] == "none_recorded"
    assert "not_holder_exit" in path["warning_scope"]


def test_buy_loss_path_keeps_unmatured_and_nonbuy_cases_distinct():
    day = "2026-07-20"
    sessions = market_clock.next_sessions("kr", day, 20)
    signals = pd.DataFrame([
        {"date": day, "ticker": "BUY", "score": 2.0, "kind": "STRONG_BUY"},
        {"date": day, "ticker": "HOLD", "score": 1.0, "kind": "HOLD"},
    ])
    bars = pd.DataFrame(_bars("BUY", sessions[:2]) + _bars("HOLD", sessions[:2]))
    rows = {row["ticker"]: row for row in audit_snapshots(signals, bars, market="kr")["rows"]}
    assert rows["BUY"]["loss_warning_path"]["state"] == "not_matured"
    assert rows["HOLD"]["loss_warning_path"]["state"] == "not_buy_signal"


@pytest.mark.parametrize("reason", [
    "[추세] 하락추세 확인 — 반등 전 매수 차단(관망)",
    "[실적] 2일 뒤 실적발표 예정 — 발표 전 신규 매수 보류(관망)",
    "[급락] 1일 -8.0% — 단기 급락으로 신규 매수 보류(관망)",
    "[악재] 주요 공시 — 신규 매수 보류(관망)",
    "[선반영] 호재 전 사전상승 11.2% — 신규 매수 보류",
    "[추격] 진입 늦음 — 신규 매수 보류",
])
def test_every_blocking_gate_reason_survives_case_audit(reason):
    day = "2026-07-20"
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 2.0,
                             "kind": "HOLD", "gate_blocked": 1,
                             "reasons_json": json.dumps([reason, "[추세] 하락추세지만 게이트 완화 조건 충족"])}])
    prices = pd.DataFrame(_bars("AAA", [day]))
    row = audit_snapshots(signals, prices, market="kr")["rows"][0]
    assert row["selection"]["gate_reason"] == reason


def test_holiday_signal_is_recorded_but_never_given_a_forward_return():
    signals = pd.DataFrame([{"date": "2026-09-24", "ticker": "267250",
                             "score": 2.0, "kind": "STRONG_BUY"}])
    bars = pd.DataFrame(_bars("267250", ["2026-09-28", "2026-09-29"]))
    row = audit_snapshots(signals, bars, market="kr")["rows"][0]
    assert all(row["outcomes"][str(h)]["state"] == "invalid_signal_session"
               for h in (1, 5, 20))


def test_export_selects_last_recorded_sessions_and_matching_prices_only():
    signals = pd.DataFrame([
        {"date": "2026-07-10", "ticker": "AAA", "score": 1.0, "kind": "HOLD", "private": "drop"},
        {"date": "2026-07-13", "ticker": "AAA", "score": 1.1, "kind": "HOLD", "private": "drop"},
        {"date": "2026-07-14", "ticker": "BBB", "score": 2.0, "kind": "BUY", "private": "drop"},
    ])
    prices = pd.DataFrame(_bars("AAA", ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"])
                          + _bars("BBB", ["2026-07-13", "2026-07-14", "2026-07-15"]))
    selected, bars = select_recorded_inputs(signals, prices, market="kr", sessions=2)
    assert selected["date"].tolist() == ["2026-07-13", "2026-07-14"]
    assert "private" not in selected
    assert len(bars) == 6
    assert not (bars["date"] == "2026-07-10").any()
    with pytest.raises(ValueError, match="invalid market"):
        select_recorded_inputs(signals, prices, market="kr", sessions=62)


def test_inventory_keeps_protected_period_as_metadata_without_outcomes():
    signals = pd.DataFrame([
        {"date": "2026-07-20", "ticker": "AAA", "score": 1.0, "kind": "BUY",
         "observed_at": "2026-07-20T07:00:00Z"},
        {"date": "2026-09-24", "ticker": "AAA", "score": 9.0, "kind": "STRONG_BUY",
         "observed_at": None},
    ])
    prices = pd.DataFrame([
        {"date": "2026-07-21", "ticker": "AAA", "open": 100.0, "close": 80.0},
        {"date": "2026-09-28", "ticker": "AAA", "open": None, "close": 200.0},
    ])
    original = signals.copy(deep=True)
    result = inventory_recorded_inputs(signals, prices, market="kr",
                                       protected_start="2026-08-05")
    assert result["development_rows"] == result["protected_rows"] == 1
    assert result["invalid_signal_session_rows"] == 1
    assert result["invalid_signal_sessions"] == ["2026-09-24"]
    assert result["price_missing_open_rows"] == 1
    assert result["saved_signal_output_fields"]["observed_at"]["non_null_rows"] == 1
    assert result["saved_signal_output_fields"]["observed_at"]["source_time_verified"] is False
    assert result["observed_membership_by_date"] == [
        {"date": "2026-07-20", "tickers": ["AAA"]},
        {"date": "2026-09-24", "tickers": ["AAA"]}]
    assert result["signal_fields_by_date"][0]["non_null"]["observed_at"] == 1
    assert result["signal_fields_by_date"][1]["non_null"]["observed_at"] == 0
    assert result["price_fields_by_date"][1]["open"] == 0
    assert result["strict_pit_eligible"] is False
    assert "summary" not in result and "outcomes" not in result
    pd.testing.assert_frame_equal(signals, original)


def test_inventory_accepts_market_scoped_us_export_without_market_column():
    signals = pd.DataFrame([{"date": "2026-09-28", "ticker": "AAPL",
                             "score": 1.0, "kind": "BUY"}])
    prices = pd.DataFrame([{"date": "2026-09-29", "ticker": "AAPL",
                            "open": 100.0, "close": 101.0}])
    result = inventory_recorded_inputs(signals, prices, market="us",
                                       protected_start="2026-08-05")
    assert result["signal_rows"] == 1
    assert result["signal_tickers"] == 1
    assert result["protected_rows"] == 1


def test_case_only_audit_never_builds_protected_aggregates(monkeypatch):
    from signal_desk.signals import historical_audit

    signals = pd.DataFrame([{"date": "2026-09-28", "ticker": "AAA",
                             "score": 1.0, "kind": "BUY"}])
    prices = pd.DataFrame(_bars("AAA", ["2026-09-29", "2026-09-30", "2026-10-01",
                                         "2026-10-02", "2026-10-06"]))
    monkeypatch.setattr(historical_audit, "_score_results", lambda *_:
                        pytest.fail("protected outcomes must not be aggregated"))
    monkeypatch.setattr(historical_audit, "mean", lambda *_:
                        pytest.fail("protected cohort mean must not be computed"))
    result = audit_snapshots(signals, prices, market="kr", include_aggregates=False)
    assert "summary" not in result
    assert "cohort_comparison" not in result
    assert result["rows"][0]["outcomes"]["5"]["excess_pct"] is None


def test_inventory_cli_never_runs_forward_audit(tmp_path, monkeypatch, capsys):
    from scripts.measure import historical_signal_audit as cli

    signals_file = tmp_path / "signals.parquet"
    prices_file = tmp_path / "prices.parquet"
    pd.DataFrame([{"date": "2026-09-24", "ticker": "AAA", "score": 9.0,
                   "kind": "STRONG_BUY"}]).to_parquet(signals_file)
    pd.DataFrame(_bars("AAA", ["2026-09-28"])).to_parquet(prices_file)
    monkeypatch.setattr(cli, "_registered_start", lambda: "2026-08-05")
    monkeypatch.setattr(cli, "audit_snapshots", lambda *args, **kwargs:
                        pytest.fail("inventory must not compute forward outcomes"))
    monkeypatch.setattr("sys.argv", ["historical_signal_audit.py", "--market", "kr",
                                    "--signals", str(signals_file), "--prices", str(prices_file),
                                    "--inventory-only"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["protected_rows"] == 1
    assert result["strict_pit_eligible"] is False
    assert "summary" not in result


def test_inventory_cli_keeps_market_scoped_us_bundle(tmp_path, monkeypatch, capsys):
    from scripts.measure import historical_signal_audit as cli

    signals = pd.DataFrame([{"date": "2026-09-28", "ticker": "AAPL",
                             "score": 1.0, "kind": "BUY"}])
    prices = pd.DataFrame(_bars("AAPL", ["2026-09-29"]))
    signal_buf, price_buf = BytesIO(), BytesIO()
    signals.to_parquet(signal_buf, index=False)
    prices.to_parquet(price_buf, index=False)
    signal_bytes, price_bytes = signal_buf.getvalue(), price_buf.getvalue()
    bundle_path = tmp_path / "us.zip"
    with ZipFile(bundle_path, "w") as bundle:
        bundle.writestr("signals.parquet", signal_bytes)
        bundle.writestr("prices.parquet", price_bytes)
        bundle.writestr("manifest.json", json.dumps({
            "schema": "historical-inputs-v1", "market": "us",
            "signals_sha256": hashlib.sha256(signal_bytes).hexdigest(),
            "prices_sha256": hashlib.sha256(price_bytes).hexdigest()}))
    monkeypatch.setattr(cli, "_registered_start", lambda: "2026-08-05")
    monkeypatch.setattr(cli, "audit_snapshots", lambda *args, **kwargs:
                        pytest.fail("inventory must not read forward outcomes"))
    monkeypatch.setattr("sys.argv", ["historical_signal_audit.py", "--market", "us",
                                    "--bundle", str(bundle_path), "--inventory-only"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["signal_rows"] == result["protected_rows"] == 1
    assert result["signal_dates"] == ["2026-09-28", "2026-09-28"]


def test_admin_export_is_protected_and_has_checkable_inputs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "historical-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})  # 이 테스트의 가입 제한 횟수를 다른 API 테스트와 격리
    signal_file, price_file = tmp_path / "signals.parquet", tmp_path / "prices.parquet"
    pd.DataFrame([{"date": "2026-07-10", "ticker": "267250", "score": 1.0, "kind": "HOLD"},
                  {"date": "2026-07-13", "ticker": "267250", "score": 1.7, "kind": "BUY"}]).to_parquet(signal_file)
    pd.DataFrame(_bars("267250", ["2026-07-13", "2026-07-14"])).to_parquet(price_file)
    monkeypatch.setattr(store, "SIGNAL_HISTORY_FILE", signal_file)
    monkeypatch.setattr(store, "PRICES_FILE", price_file)
    url = "/api/admin/research/historical-inputs?market=kr"
    guest = TestClient(api.app)
    assert guest.get(url).status_code == 401
    guest.post("/api/auth/signup", json={"email": "historical-reader@example.com", "pw": "abcdef12"})
    assert guest.get(url).status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "historical-admin@example.com", "pw": "abcdef12"})
    response = admin.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    with ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        signal_bytes = archive.read("signals.parquet")
        price_bytes = archive.read("prices.parquet")
    assert manifest["signal_rows"] == manifest["price_rows"] == 2
    assert hashlib.sha256(signal_bytes).hexdigest() == manifest["signals_sha256"]
    assert hashlib.sha256(price_bytes).hexdigest() == manifest["prices_sha256"]
    case_url = "/api/admin/research/historical-cases?market=kr"
    assert guest.get(case_url).status_code == 403
    case_response = admin.get(case_url + "&sessions=1")
    assert case_response.status_code == 200
    assert "summary" not in case_response.json()  # 등록 기간의 조기 성적 합산을 반환하지 않는다
    assert case_response.json()["case_rows"][0]["ticker"] == "267250"
    assert case_response.json()["case_rows"][0]["kind_change"] is True
    assert case_response.json()["recorded_signal_rows"] == 1
    assert case_response.json()["case_rows"][0]["outcomes"]["5"]["state"] == "not_matured"


def test_historical_paper_trade_context_is_admin_only_and_not_real_execution(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "paper-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    url = "/api/admin/research/historical-paper-trades?market=kr&ticker=267250&signal_date=2026-09-14"
    guest = TestClient(api.app)
    assert guest.get(url).status_code == 401
    guest.post("/api/auth/signup", json={"email": "paper-guest@example.com", "pw": "abcdef12"})
    assert guest.get(url).status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "paper-admin@example.com", "pw": "abcdef12"})
    db.bot_trade_log(900002, "267250", "HD현대", "buy", 2, 239500.0,
                     "SIGNAL", "PAPER-CASE", market="kr")
    c = db.conn()
    try:
        c.execute("UPDATE bot_trades SET ts=? WHERE order_no=?",
                  (int(pd.Timestamp("2026-09-15T09:01:00", tz="Asia/Seoul").timestamp()),
                   "PAPER-CASE"))
        c.commit()
    finally:
        c.close()
    result = admin.get(url)
    assert result.status_code == 200
    body = result.json()
    assert body["source"] == "reference_paper_bot_journal_only"
    assert set(body["styles"]) == {"conservative", "balanced", "aggressive"}
    assert len(body["styles"]["balanced"]["trades"]) == 1
    assert body["styles"]["balanced"]["trades"][0]["price"] == 239500.0
    assert body["styles"]["balanced"]["trades"][0]["execution_event_state"] == "not_recorded"
    assert body["styles"]["balanced"]["trades"][0]["notification"] is None
    assert not body["styles"]["conservative"]["trades"]
    assert "실계좌" in body["limitations"][0]
    assert result.headers["cache-control"] == "private, no-store"
    assert admin.get(url.replace("2026-09-14", "2026-09-25")).status_code == 400


def test_historical_dart_observations_preserve_first_fetch_and_uncertain_same_day(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "source-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    url = ("/api/admin/research/historical-disclosure-observations"
           "?market=kr&ticker=267250&signal_date=2026-09-14")
    guest = TestClient(api.app)
    assert guest.get(url).status_code == 401
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "source-admin@example.com", "pw": "abcdef12"})
    entries = [
        {"title": "타법인주식및출자증권취득결정", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=1",
         "source": "dart", "published": "2026-09-10"},
        {"title": "당일 공시", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2",
         "source": "dart", "published": "2026-09-14"},
        {"title": "다른 종목 공시", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=3",
         "source": "dart", "published": "2026-09-10"},
    ]
    assert db.kb_entry_add_many("267250", entries[:2]) == 2
    assert db.kb_entry_add_many("005380", entries[2:]) == 1
    db.kb_event_upsert({"event_key": "dart:1", "ticker": "267250",
                        "event_type": "disclosure_review"},
                       {"url": entries[0]["url"], "source_key": "dart"})
    c = db.conn()
    try:
        before = int(pd.Timestamp("2026-09-10T17:00:00", tz="Asia/Seoul").timestamp())
        same = int(pd.Timestamp("2026-09-14T10:00:00", tz="Asia/Seoul").timestamp())
        c.execute("UPDATE kb_entries SET fetched=? WHERE url=?", (before, entries[0]["url"]))
        c.execute("UPDATE kb_entries SET fetched=? WHERE url=?", (same, entries[1]["url"]))
        c.execute("UPDATE kb_events SET created=? WHERE event_key='dart:1'", (before,))
        c.commit()
    finally:
        c.close()
    response = admin.get(url)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "retained_dart_kb_rows_only"
    assert [row["timing"] for row in body["documents"]] == [
        "before_signal_day", "same_day_order_unknown"]
    assert body["events"][0]["timing"] == "before_signal_day"
    assert body["events"][0]["url"] == entries[0]["url"]
    assert "원천 공개시각" in body["limitations"][1]
    assert admin.get(url.replace("267250", "005380")).json()["documents"] == []
    assert admin.get(url.replace("market=kr", "market=us")).status_code == 400
    assert admin.get(url.replace("2026-09-14", "2026-09-25")).status_code == 400
