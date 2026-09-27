from concurrent.futures import ThreadPoolExecutor
import time

from signal_desk import config, db, notify


def test_outbox_is_deduplicated_and_retries_after_delivery_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert notify.enqueue("important", dedupe_key="trade:1", now=100) is True
    assert notify.enqueue("important", dedupe_key="trade:1", now=100) is False

    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111"])
    monkeypatch.setattr(notify, "_send_one", lambda *args: False)
    assert notify.drain(now=100)["failed"] == 1
    # first failure backs off 30 seconds; it cannot hammer Telegram on each 5-minute tick.
    assert notify.drain(now=129)["failed"] == 0

    monkeypatch.setattr(notify, "_send_one", lambda *args: True)
    assert notify.drain(now=130)["sent"] == 1
    assert db.notification_outbox_due(now=999) == []


def test_outbox_expires_before_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert notify.enqueue("stale", dedupe_key="signal:old", expires_at=99, now=98) is True
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111"])
    monkeypatch.setattr(notify, "_send_one", lambda *args: (_ for _ in ()).throw(AssertionError("must not send")))

    assert notify.drain(now=100)["expired"] == 1


def test_outbox_health_reports_pending_delivery_without_message_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notify.enqueue("do not expose", dedupe_key="health:pending", now=100)
    health = db.notification_outbox_health(now=100)

    assert health["pending"] == health["due"] == 1
    assert health["oldest_pending_ts"] == 100
    assert "do not expose" not in str(health)


def test_partial_delivery_retries_only_failed_recipient(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notify.enqueue("trade", dedupe_key="fill:one", now=100)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111", "222"])
    calls = []

    def send(token, chat, text):
        calls.append(chat)
        return chat == "111" or len(calls) > 2

    monkeypatch.setattr(notify, "_send_one", send)
    assert notify.drain(now=100)["failed"] == 1
    assert calls == ["111", "222"]
    assert notify.drain(now=130)["sent"] == 1
    assert calls == ["111", "222", "222"]
    assert db.notification_outbox_health(now=130)["pending"] == 0


def test_concurrent_drains_claim_only_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notify.enqueue("trade", dedupe_key="fill:concurrent", now=100)
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111"])
    calls = []

    def send(token, chat, text):
        calls.append(chat)
        time.sleep(0.03)
        return True

    monkeypatch.setattr(notify, "_send_one", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: notify.drain(now=100), range(2)))
    assert calls == ["111"]
    assert sum(o["sent"] for o in outcomes) == 1


def test_claim_reopens_after_worker_dies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notify.enqueue("trade", dedupe_key="fill:recover", now=100)
    first = db.notification_outbox_claim(now=100, lease_seconds=20)
    assert first and db.notification_outbox_claim(now=119) is None
    assert db.notification_outbox_claim(now=120)["id"] == first["id"]


def test_intraday_quote_and_execution_event_are_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.intraday_quotes_record("kr", {"005930": 70000}, ts=10) == 1
    assert db.intraday_quotes_record("kr", {"005930": 70000}, ts=10) == 0
    assert db.intraday_quotes_list("kr", "005930") == [{"ts": 10, "price": 70000.0}]

    assert db.execution_event_add("trade:kr:1", uid=1, market="kr", ticker="005930",
                                  event_type="filled_buy", price=70000, ts=10) is True
    assert db.execution_event_add("trade:kr:1", uid=1, market="kr", ticker="005930",
                                  event_type="filled_buy", price=70000, ts=10) is False
