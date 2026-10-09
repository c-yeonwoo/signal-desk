from signal_desk import db
import datetime
from zoneinfo import ZoneInfo

UID = 5


def test_user_bot_defaults_and_toggle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = db.user_bot_get(UID)
    assert cfg["enabled"] is False and cfg["trading_style"] == "balanced" and cfg["seed_cash"] == 10_000_000

    db.user_bot_set_enabled(UID, True)
    assert db.user_bot_get(UID)["enabled"] is True
    assert db.user_bots_enabled() == [UID]
    db.user_bot_set_enabled(UID, False)
    assert db.user_bots_enabled() == []

    db.user_bot_set_style(UID, "aggressive")
    db.user_bot_set_seed(UID, 5_000_000)
    c = db.user_bot_get(UID)
    assert c["trading_style"] == "aggressive" and c["seed_cash"] == 5_000_000


def test_bot_run_provenance_keeps_capture_result_and_uid_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_run_provenance_add(
        "run-a", uid=UID, market="kr", session="2026-10-09", decision_at=123,
        mode="regular", signal_policy_id="signal-v1", execution_policy_id="execution-v1",
        capture={"status": "saved", "replay_match": True, "signal_output_id": "artifact-a"})
    c = db.conn()
    try:
        row = c.execute("SELECT capture_status,signal_output_id,decision_at FROM bot_run_provenance "
                        "WHERE run_id=? AND uid=?", ("run-a", UID)).fetchone()
    finally:
        c.close()
    assert row == ("saved", "artifact-a", 123)


def test_bot_position_upsert_and_delete_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.bot_positions_all(UID) == []

    db.bot_position_upsert(UID, "005930", "삼성전자", 10, 70000.0, 72000.0, "2026-07-01")
    pos = db.bot_position_get(UID, "005930")
    assert pos == {"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 70000.0,
                   "peak_price": 72000.0, "entry_date": "2026-07-01",
                   "last_price": None, "last_pnl_pct": None,
                   # 분할 회차·마지막 매수일. 안 넘기면 1회차로 시작한다(0이면 상한이 즉시 풀린다).
                   "tranches_done": 1, "last_buy_date": None}
    # 다른 유저 격리
    assert db.bot_positions_all(99) == []
    db.bot_position_upsert(UID, "005930", "삼성전자", 15, 71000.0, 73000.0, "2026-07-01")
    assert db.bot_position_get(UID, "005930")["qty"] == 15

    # **`INSERT OR REPLACE` 라서 안 넘긴 값은 사라진다.** 회차를 넣고 나서 스냅샷만 갱신해도
    # 보존돼야 한다 — 안 그러면 시세 갱신이 회차를 매번 1로 되돌려 상한이 아무 것도 안 막는다.
    db.bot_position_upsert(UID, "005930", "삼성전자", 15, 71000.0, 73000.0, "2026-07-01",
                           tranches_done=3, last_buy_date="2026-07-05")
    db.bot_position_upsert(UID, "005930", "삼성전자", 15, 71000.0, 74000.0, "2026-07-01",
                           last_price=71500.0)
    kept = db.bot_position_get(UID, "005930")
    assert kept["tranches_done"] == 3 and kept["last_buy_date"] == "2026-07-05"
    assert kept["peak_price"] == 74000.0, "스냅샷 갱신 자체는 반영돼야 한다"

    db.bot_position_delete(UID, "005930")
    assert db.bot_position_get(UID, "005930") is None


def test_bot_trade_log_and_recent_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_trade_log(UID, "005930", "삼성전자", "buy", 10, 70000.0, "SIGNAL", "ORD1")
    db.bot_trade_log(UID, "000660", "SK하이닉스", "sell", 5, 200000.0, "STOP_LOSS", "ORD2")
    db.bot_trade_log(99, "035720", "카카오", "buy", 1, 50000.0, "SIGNAL", "ORD3")  # 다른 유저

    recent = db.bot_trades_recent(UID, limit=10)
    assert [r["ticker"] for r in recent] == ["000660", "005930"]  # 최신순, UID 것만
    assert recent[0]["reason"] == "STOP_LOSS"


def test_historical_case_paper_trades_are_bounded_and_account_scoped(tmp_path, monkeypatch):
    from signal_desk import bot_alerts

    monkeypatch.chdir(tmp_path)
    zone = ZoneInfo("Asia/Seoul")
    stamp = lambda day: int(datetime.datetime.fromisoformat(day).replace(tzinfo=zone).timestamp())
    db.bot_trade_log(900002, "267250", "HD현대", "buy", 3, 250000, "SIGNAL", "PRE")
    db.bot_trade_log(900002, "267250", "HD현대", "buy", 2, 239500, "ADD", "IN")
    db.bot_trade_log(900002, "267250", "HD현대", "sell", 4, 210000, "STOP_LOSS", "OUT")
    db.bot_trade_log(900001, "267250", "HD현대", "buy", 9, 239500, "SIGNAL", "OTHER")
    db.bot_trade_log(900002, "267250", "HD현대", "buy", 8, 239500, "SIGNAL", "US", market="us")
    c = db.conn()
    try:
        for order_no, day in (("PRE", "2026-09-13T12:00:00"),
                              ("IN", "2026-09-15T09:00:00"),
                              ("OUT", "2026-09-18T09:00:00")):
            c.execute("UPDATE bot_trades SET ts=? WHERE order_no=?", (stamp(day), order_no))
        c.commit()
    finally:
        c.close()
    db.execution_event_add("trade:kr:900002:IN", uid=900002, market="kr", ticker="267250",
                           event_type="filled_buy", price=239500, payload={"qty": 2},
                           ts=stamp("2026-09-15T09:00:00"))
    db.execution_event_add("trade:kr:900002:OUT", uid=900002, market="kr", ticker="267250",
                           event_type="filled_sell", price=210000, payload={"qty": 99},
                           ts=stamp("2026-09-18T09:00:00"))
    alert_key = bot_alerts.dedupe_key(900002, "kr", [{"side": "BUY", "order_no": "IN"}])
    assert db.notification_enqueue(alert_key, "paper fill", now=stamp("2026-09-15T09:00:00"))
    c = db.conn()
    item_id = c.execute("SELECT id FROM notification_outbox WHERE dedupe_key=?", (alert_key,)).fetchone()[0]
    c.close()
    assert db.notification_delivery_pending(item_id, ["chat-A"]) == ["chat-A"]
    db.notification_delivery_mark(item_id, "chat-A", sent=True,
                                  now=stamp("2026-09-15T09:01:00"))
    db.notification_outbox_sent(item_id, now=stamp("2026-09-15T09:01:00"))
    result = db.bot_trades_for_case(900002, "kr", "267250",
                                    stamp("2026-09-14T00:00:00"),
                                    stamp("2026-09-19T00:00:00"), limit=1)
    assert result["prior_trade_rows"] == 1
    assert result["prior_journal_net_qty"] == 3
    assert result["truncated"] is True
    assert [(row["side"], row["qty"]) for row in result["trades"]] == [("buy", 2)]
    assert result["trades"][0]["execution_event_state"] == "matched"
    assert result["trades"][0]["decision_link_state"] == "legacy_run_unlinked"
    assert result["trades"][0]["notification"] == {
        "outbox_status": "sent", "outbox_sent_at": stamp("2026-09-15T09:01:00"),
        "recipient_count": 1, "recipient_sent_count": 1,
    }
    all_rows = db.bot_trades_for_case(900002, "kr", "267250",
                                      stamp("2026-09-14T00:00:00"),
                                      stamp("2026-09-19T00:00:00"))
    assert all_rows["trades"][1]["execution_event_state"] == "mismatch"
    assert all_rows["trades"][1]["decision_link_state"] == "execution_event_unverified"
    assert all_rows["trades"][1]["notification"] is None


def test_historical_case_verifies_run_fill_links_without_inventing_capture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_trade_log(UID, "AAA", "가", "buy", 1, 100, "SIGNAL", "ORDER")
    c = db.conn()
    try:
        trade_ts = c.execute("SELECT ts FROM bot_trades WHERE order_no='ORDER'").fetchone()[0]
    finally:
        c.close()
    assert db.execution_event_add(
        f"trade:kr:{UID}:ORDER", uid=UID, market="kr", ticker="AAA",
        event_type="filled_buy", price=100,
        payload={"qty": 1, "run_id": "run-a", "signal_policy_id": "signal-v1",
                 "execution_policy_id": "execution-v1"}, ts=trade_ts)
    session = datetime.datetime.fromtimestamp(trade_ts, ZoneInfo("Asia/Seoul")).date().isoformat()
    db.bot_run_provenance_add(
        "run-a", uid=UID, market="kr", session=session, decision_at=trade_ts,
        mode="regular", signal_policy_id="signal-v1", execution_policy_id="execution-v1",
        capture={"status": "not_requested", "reason": "outside_production"})

    def state():
        return db.bot_trades_for_case(UID, "kr", "AAA", trade_ts - 1,
                                      trade_ts + 1)["trades"][0]["decision_link_state"]

    def change(sql, params):
        c = db.conn()
        try:
            c.execute(sql, params)
            c.commit()
        finally:
            c.close()

    assert state() == "run_linked_capture_unavailable"
    change("UPDATE bot_run_provenance SET decision_at=? WHERE run_id='run-a'", (trade_ts + 1,))
    assert state() == "run_fill_order_inconsistent"
    change("UPDATE bot_run_provenance SET decision_at=?,completed_at=? WHERE run_id='run-a'",
           (trade_ts, trade_ts - 1))
    assert state() == "run_fill_order_inconsistent"
    change("UPDATE bot_run_provenance SET decision_at=?,session=? WHERE run_id='run-a'",
           (trade_ts, "2000-01-01"))
    change("UPDATE bot_run_provenance SET completed_at=? WHERE run_id='run-a'", (trade_ts,))
    assert state() == "run_session_mismatch"
    change("UPDATE bot_run_provenance SET session=?,signal_policy_id=? WHERE run_id='run-a'",
           (session, "other-policy"))
    assert state() == "run_policy_mismatch"
    change("UPDATE bot_run_provenance SET signal_policy_id=NULL WHERE run_id='run-a'", ())
    assert state() == "run_policy_unverified"
    change("UPDATE bot_run_provenance SET signal_policy_id=?,capture_status='saved',"
           "signal_output_id=? WHERE run_id='run-a'", ("signal-v1", "missing-artifact"))
    assert state() == "capture_artifact_missing"
    artifact_id = db.decision_artifact_put("kr", "signal_output", {"rows": []},
                                           observed_at=trade_ts)
    change("UPDATE bot_run_provenance SET signal_output_id=? WHERE run_id='run-a'",
           (artifact_id,))
    assert state() == "same_run_capture_recorded"
    change("DELETE FROM bot_run_provenance WHERE run_id='run-a'", ())
    assert state() == "run_record_missing"


def test_bot_reset_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_position_upsert(UID, "005930", "삼성", 1, 100.0, 100.0, "2026-07-01")
    db.bot_trade_log(UID, "005930", "삼성", "buy", 1, 100.0, "SIGNAL", "O")
    db.kv_set(f"paper_account:{UID}", '{"cash": 5, "positions": {}}')
    db.bot_reset(UID)
    assert db.bot_positions_all(UID) == [] and db.bot_trades_recent(UID) == []
    assert db.kv_get(f"paper_account:{UID}") is None  # 시드로 리셋(kv 삭제)


def test_fav_tickers_all_distinct_cross_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.fav_tickers_all() == set()
    db.fav_add(UID, "ticker", "005930", "삼성전자")
    db.fav_add(99, "ticker", "005930", "삼성전자")   # 다른 유저 중복 → 1건으로
    db.fav_add(99, "ticker", "000660", "SK하이닉스")
    db.fav_add(UID, "index", "KS200", "코스피200")   # kind!='ticker' → 제외
    assert db.fav_tickers_all() == {"005930", "000660"}
