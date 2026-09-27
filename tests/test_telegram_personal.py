"""Personal Telegram boundary: no account-to-chat leakage, stale delivery, or group commands."""

import hashlib
import sqlite3
import time

from fastapi.testclient import TestClient

from signal_desk import api, config, db, notify, telegram_inbound, toss_manual_routes


def _claim(uid: int, chat: str, *, now: int = 100, code: str = "A1B2C3D4E5F6") -> None:
    db.telegram_link_code_issue(uid, hashlib.sha256(code.encode()).hexdigest(), now=now)
    assert db.telegram_link_code_claim(hashlib.sha256(code.encode()).hexdigest(), chat,
                                       now=now + 1, chat_label="owner")


def test_link_requires_app_confirmation_and_private_chat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    sent = []
    monkeypatch.setattr(notify, "_send_one", lambda *args: sent.append(args) or True)
    _claim(1, "111")
    assert db.telegram_link_get(1) is None
    assert telegram_inbound.handle_update({"message": {"chat": {"id": -9, "type": "group"},
        "from": {"id": 111}, "text": "/status"}}, now=101) is False
    assert not sent
    assert db.telegram_link_confirm(1, now=102)
    assert db.telegram_link_get(1)["chat_id"] == "111"
    assert not db.telegram_link_confirm(1, now=103)
    assert telegram_inbound.command_response("111", "/status", now=103).startswith("연결됨")
    assert "연결되지" in telegram_inbound.command_response("222", "/status", now=103)


def test_link_expiry_and_chat_cannot_be_reused_by_another_account(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _claim(1, "111")
    assert not db.telegram_link_confirm(1, now=701)
    _claim(1, "111", now=800, code="000000000001")
    assert db.telegram_link_confirm(1, now=802)
    _claim(2, "111", now=900, code="000000000002")
    assert not db.telegram_link_confirm(2, now=902)
    assert db.telegram_link_get(2) is None


def test_personal_delivery_only_to_bound_chat_and_unlink_cancels_pending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["GLOBAL"])
    sent = []
    monkeypatch.setattr(notify, "_send_one", lambda _token, chat, text: sent.append((chat, text)) or True)
    _claim(1, "111")
    assert db.telegram_link_confirm(1, now=102)
    assert notify.enqueue_user(1, "secret one", dedupe_key="watch:one", market="kr",
                               alert_type="watchlist", now=103, expires_at=150)
    assert not notify.enqueue_user(2, "secret two", dedupe_key="watch:two", market="kr",
                                   alert_type="watchlist", now=103)
    assert notify.drain(now=104)["sent"] == 1
    assert len(sent) == 1 and sent[0][0] == "111"
    assert sent[0][1].startswith("secret one\n전달 ")
    assert notify.enqueue_user(1, "late buy", dedupe_key="fill:late", market="kr",
                               alert_type="paper_fill", now=105, expires_at=110)
    assert notify.drain(now=111)["expired"] == 1
    assert notify.enqueue_user(1, "cancel me", dedupe_key="watch:cancel", market="kr",
                               alert_type="watchlist", now=112)
    db.telegram_link_unlink(1)
    assert notify.drain(now=113)["sent"] == 0
    assert len(sent) == 1


def test_relink_and_setting_change_expire_old_messages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: [])
    sent = []
    monkeypatch.setattr(notify, "_send_one", lambda _token, chat, text: sent.append(chat) or True)
    _claim(1, "111")
    assert db.telegram_link_confirm(1, now=102)
    assert notify.enqueue_user(1, "old", dedupe_key="one", market="kr", alert_type="watchlist", now=103)
    assert db.telegram_link_settings(1, style="aggressive", markets=["us"],
                                     alert_types=["paper_fill"], now=104)
    assert notify.drain(now=105)["sent"] == 0
    assert not notify.enqueue_user(1, "blocked", dedupe_key="two", market="kr",
                                   alert_type="watchlist", now=106)
    assert notify.enqueue_user(1, "new", dedupe_key="three", market="us",
                               alert_type="paper_fill", now=106)
    assert notify.drain(now=107)["sent"] == 1
    assert sent == ["111"]
    assert db.telegram_link_recipients("aggressive", "us", "paper_fill") == [(1, "111")]


def test_code_is_one_time_and_not_stored_in_plaintext(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    issued = telegram_inbound.issue_code(1, now=100)
    raw = issued["code"]
    assert len(raw) == 12 and raw not in str(db.telegram_link_code_status(1, now=100))
    first = telegram_inbound.command_response("111", "/link " + raw, now=101, chat_label="owner")
    assert "확인했습니다" in first
    assert "다른 채팅" in telegram_inbound.command_response("222", "/link " + raw, now=102)
    assert db.telegram_link_code_status(1, now=102)["chat_id"] == "111"


def test_daily_summary_requires_frozen_same_session_and_is_personal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "toss_account_owner", lambda: None)
    _claim(1, "111")
    _claim(2, "222", code="000000000002")
    assert db.telegram_link_confirm(1, now=102)
    assert db.telegram_link_confirm(2, now=102)
    db.portfolio_snapshot_add(1, "kr", as_of="2026-09-25", source="daily_close",
                              total_value=100, data_quality="complete",
                              payload={"guidance": [{"action": "비중 검토", "reason": "한도 초과"}]})
    assert telegram_inbound.enqueue_daily_summaries("2026-09-26", now=200) == 0
    assert telegram_inbound.enqueue_daily_summaries("2026-09-25", now=201) == 1
    assert telegram_inbound.enqueue_daily_summaries("2026-09-25", now=202) == 0
    due = db.notification_outbox_due(now=202)
    assert len(due) == 1 and due[0]["recipient_uid"] == 1
    assert due[0]["recipient_chat_id"] == "111"
    assert "계좌 전체" not in due[0]["text"] or "아님" in due[0]["text"]


def test_paper_fill_routes_by_selected_style_without_personal_global_leak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "balanced")
    monkeypatch.setattr(api.notify, "drain", lambda: None)
    _claim(1, "111")
    assert db.telegram_link_confirm(1, now=102)
    assert db.telegram_link_settings(1, style="aggressive", markets=["kr"],
                                     alert_types=["paper_fill"], now=103)
    result = {"ok": True, "buys": [{"ok": True, "ticker": "005930", "name": "삼성전자",
                                  "qty": 1, "fill_price": 70000, "order_no": "abc", "reason": "SIGNAL"}]}
    api._push_trades("kr", result, 900001)
    assert db.notification_outbox_due(now=10**10) == []
    api._push_trades("kr", result, 900003)
    due = db.notification_outbox_due(now=10**10)
    assert len(due) == 1 and due[0]["recipient_uid"] == 1
    assert due[0]["recipient_chat_id"] == "111"
    assert time.time() < due[0]["expires_at"] <= time.time() + 15 * 60 + 2


def test_authenticated_app_confirms_claim_and_checks_mutation_header(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(telegram_inbound, "bot_username", lambda: "SignalDeskBot")
    monkeypatch.setattr(api.notify, "drain", lambda: None)
    client = TestClient(api.app)
    assert client.post("/api/auth/signup", json={"email": "user@example.com", "pw": "abcdef"}).status_code == 200
    assert client.post("/api/telegram/link-code").status_code == 403
    headers = {"X-Signal-Desk-Telegram": "settings"}
    issued = client.post("/api/telegram/link-code", headers=headers)
    assert issued.status_code == 200
    code = issued.json()["code"]
    assert "확인했습니다" in telegram_inbound.command_response("111", "/link " + code,
                                                     chat_label="myname")
    settings = client.get("/api/telegram/settings").json()
    assert settings["pending_claim"] and settings["chat_label"] == "myname"
    assert client.post("/api/telegram/confirm").status_code == 403
    assert client.post("/api/telegram/confirm", headers=headers).status_code == 200
    assert client.put("/api/telegram/settings", headers=headers, json={"style": "aggressive",
                      "markets": ["us"], "alert_types": ["paper_fill"]}).status_code == 200
    assert client.get("/api/telegram/settings").json()["style"] == "aggressive"
    assert client.delete("/api/telegram/link", headers=headers).status_code == 200
    assert client.get("/api/telegram/settings").json()["linked"] is False


def test_legacy_personal_watchlist_queue_is_expired_on_migration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.DB.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(db.DB)
    raw.execute("CREATE TABLE notification_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "dedupe_key TEXT UNIQUE,text TEXT,priority TEXT,status TEXT,attempts INTEGER,"
                "next_attempt INTEGER,expires_at INTEGER,created INTEGER,sent_at INTEGER,last_error TEXT)")
    raw.execute("INSERT INTO notification_outbox(dedupe_key,text,priority,status,attempts,next_attempt,created) "
                "VALUES('signal:42:005930:BUY:SELL:2026-09-27','private','high','pending',0,1,1)")
    raw.commit(); raw.close()
    c = db.conn()
    row = c.execute("SELECT status,recipient_uid FROM notification_outbox WHERE text='private'").fetchone()
    c.close()
    assert row == ("expired", None)


def test_poll_offset_is_durable_and_duplicate_updates_are_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    calls = []
    update = {"update_id": 7, "message": {"chat": {"id": 111, "type": "private"},
              "from": {"id": 111}, "text": "/help"}}
    def fake_api(_method, payload):
        calls.append(payload["offset"])
        return {"ok": True, "result": [update]}
    monkeypatch.setattr(telegram_inbound, "_bot_api", fake_api)
    monkeypatch.setattr(telegram_inbound, "handle_update", lambda *_a, **_k: None)
    assert telegram_inbound.poll_once() == 1
    assert telegram_inbound.poll_once() == 0
    assert calls == [0, 8]
    assert db.kv_get("telegram_inbound_offset") == 8


def test_live_unknown_alert_is_personal_and_does_not_change_order_response(monkeypatch):
    row = {"uid": 7, "symbol": "005930", "side": "BUY", "quantity": "2", "updated": 100}
    monkeypatch.setattr(toss_manual_routes.intent_ledger, "get", lambda _id: row)
    calls = []
    monkeypatch.setattr(toss_manual_routes.notify, "enqueue_user",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    toss_manual_routes._notify_order_state(7, "intent-1", "UNKNOWN")
    assert len(calls) == 1 and calls[0][0][0] == 7
    assert "재제출하지 마세요" in calls[0][0][1]
    assert calls[0][1]["alert_type"] == "live_order"
    assert calls[0][1]["dedupe_key"] == "live-order:intent-1:UNKNOWN"
    toss_manual_routes._notify_order_state(8, "intent-1", "FILLED")
    assert len(calls) == 1
    monkeypatch.setattr(toss_manual_routes.notify, "enqueue_user", lambda *_a, **_k: 1 / 0)
    toss_manual_routes._notify_order_state(7, "intent-1", "UNKNOWN")  # optional channel cannot break orders
